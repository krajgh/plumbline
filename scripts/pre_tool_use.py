#!/usr/bin/env python3
"""PreToolUse hook for plumbline.

Reads the hook's JSON on stdin and, to deny a tool call, prints
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
"permissionDecisionReason": ...}} and exits 0. It acts only inside a repository
that has adopted plumbline (a plumbline.toml at its top level):

  Bash (and PowerShell)
    - `git push` and `gh pr create` are denied unless HEAD has a pass record or
      an override record (.plumbline/pass/<HEAD>.json or .override.json) that
      validates and names HEAD. `--dry-run` and `--help` are not pushes.
    - `git commit` is denied when what it would commit adds a symlink, an
      absolute home path, or a key-shaped secret (sk-ant- followed by 20 or more
      characters).
  Read, Grep and Glob, for the agent plumbline:builder only
    - a path that matches the tests type of the pipeline, or is listed in the
      newest run's tests record (that record included), is denied; so is a Grep
      or Glob without an explicit path, and one whose path leads to a directory
      holding tests.

Any error, and any repository without a plumbline.toml, allows the call:
silently, with exit status 0. The command is parsed, not searched, so
`echo git push` is not a push.
"""
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

GIT_TIMEOUT = 20
MAX_SCANNED_BYTES = 8_000_000
MAX_LISTED = 8

# Written so that this file does not itself contain an absolute home path.
HOME_PATH = re.compile(r"(?<![\w./~-])/(?:home|Users)/[A-Za-z0-9_][A-Za-z0-9._-]*/")
SECRET = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")

SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}"}  # words that may come before a command
# wrapper -> options that take a value
WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S"},
    "command": set(),
    "exec": {"-a"},
    "nohup": set(),
    "time": {"-f", "-o"},
    "nice": {"-n"},
    "setsid": set(),
    "timeout": {"-k", "-s"},
}
GIT_VALUE_OPTIONS = {"-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
COMMIT_LONG_VALUE = {
    "--message", "--file", "--reuse-message", "--reedit-message", "--template", "--author",
    "--date", "--cleanup", "--trailer", "--fixup", "--squash", "--pathspec-from-file",
}
COMMIT_SHORT_VALUE = "mFCct"
HEAD_MOVERS = {"merge", "rebase", "cherry-pick", "revert", "pull", "am", "reset", "checkout", "switch"}


# ------------------------------------------------------- parsing a command


def _matching_paren(text: str, start: int) -> int:
    """The index of the `)` that closes the `(` at `start` (len(text) when none does)."""
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if (c == '"' and text[j] == "\\") else 1
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def split_commands(text: str, depth: int = 0) -> list[list[str]]:
    """The simple commands of a shell command line, each as a list of words:
    split at ; & | ( ) and newlines, quotes removed, redirections and here-document
    bodies dropped. The commands inside $( ) and backticks come out too."""
    commands: list[list[str]] = []
    if depth > 4:
        return commands
    words: list[str] = []
    word: list[str] | None = None
    skip_next = False  # the word after a redirection operator is a target, not an argument
    heredocs: list[tuple[str, bool]] = []
    n, i = len(text), 0

    def end_word():
        nonlocal word, skip_next
        if word is not None:
            if skip_next:
                skip_next = False
            else:
                words.append("".join(word))
            word = None

    def end_command():
        nonlocal words
        end_word()
        if words:
            commands.append(words)
        words = []

    def add(chunk: str):
        nonlocal word
        if word is None:
            word = []
        word.append(chunk)

    while i < n:
        c = text[i]
        if c in " \t\r":
            end_word()
            i += 1
        elif c == "\n":
            end_command()
            i += 1
            while heredocs:
                delimiter, strip_tabs = heredocs.pop(0)
                while i < n:
                    j = text.find("\n", i)
                    j = n if j < 0 else j
                    line = text[i:j]
                    i = j + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                        break
        elif c == "#" and word is None:
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in ";&|()":
            end_command()
            i += 1
        elif c == "\\":
            if i + 1 < n and text[i + 1] != "\n":
                add(text[i + 1])
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            add(text[i + 1 : j])
            i = j + 1
        elif c == '"':
            i += 1
            add("")
            while i < n and text[i] != '"':
                d = text[i]
                if d == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    add(nxt if nxt in '"\\$`' else "\\" + nxt)
                    i += 2
                elif d == "$" and text[i + 1 : i + 2] == "(":
                    j = _matching_paren(text, i + 1)
                    commands.extend(split_commands(text[i + 2 : j], depth + 1))
                    add("$(...)")
                    i = j + 1
                elif d == "`":
                    j = text.find("`", i + 1)
                    j = n if j < 0 else j
                    commands.extend(split_commands(text[i + 1 : j], depth + 1))
                    add("`...`")
                    i = j + 1
                else:
                    add(d)
                    i += 1
            i += 1
        elif c == "$" and text[i + 1 : i + 2] == "(":
            j = _matching_paren(text, i + 1)
            commands.extend(split_commands(text[i + 2 : j], depth + 1))
            add("$(...)")
            i = j + 1
        elif c == "`":
            j = text.find("`", i + 1)
            j = n if j < 0 else j
            commands.extend(split_commands(text[i + 1 : j], depth + 1))
            add("`...`")
            i = j + 1
        elif c in "<>":
            if word is not None and "".join(word).isdigit():
                word = None  # a file descriptor number, as in 2>&1
            end_word()
            if text[i : i + 3] == "<<<":
                skip_next = True
                i += 3
            elif text[i : i + 2] == "<<":
                i += 2
                strip_tabs = text[i : i + 1] == "-"
                i += 1 if strip_tabs else 0
                while i < n and text[i] in " \t":
                    i += 1
                delimiter: list[str] = []
                while i < n and text[i] not in " \t\r\n;&|()<>":
                    if text[i] in "'\"":
                        i += 1
                        continue
                    delimiter.append(text[i])
                    i += 1
                heredocs.append(("".join(delimiter), strip_tabs))
            else:
                while i < n and text[i] in "<>&":
                    i += 1
                skip_next = True
        else:
            add(c)
            i += 1
    end_command()
    return commands


def strip_wrappers(argv: list[str]) -> list[str]:
    """The command itself, without leading shell keywords (then, do, ...), VAR=value
    words and wrappers such as sudo or env."""
    argv = list(argv)
    while argv:
        first = argv[0]
        if first in KEYWORDS or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", first):
            argv = argv[1:]
            continue
        name = os.path.basename(first)
        if name not in WRAPPERS:
            break
        with_value = WRAPPERS[name]
        i = 1
        while i < len(argv):
            word = argv[i]
            if word in with_value and i + 1 < len(argv):
                i += 2
            elif word.startswith("-") or (name == "env" and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word)):
                i += 1
            else:
                break
        if name == "timeout" and i < len(argv):
            i += 1  # the duration
        argv = argv[i:]
    return argv


@dataclass
class Action:
    kind: str  # push, pr-create, commit, add, head-moves
    cwd: Path
    args: list[str]


def _join(cwd: Path, target: str) -> Path:
    return Path(os.path.normpath(os.path.join(cwd, os.path.expanduser(target))))


def _is_dry_run(args: list[str]) -> bool:
    for word in args:
        if word in ("--dry-run", "--help", "-h"):
            return True
        if word.startswith("-") and not word.startswith("--") and "n" in word[1:]:
            return True
    return False


def git_action(argv: list[str], cwd: Path) -> Action | None:
    directory, i = cwd, 1
    while i < len(argv):
        word = argv[i]
        if word == "-C" and i + 1 < len(argv):
            directory = _join(directory, argv[i + 1])
            i += 2
        elif word in GIT_VALUE_OPTIONS and i + 1 < len(argv):
            i += 2
        elif word.startswith("-"):
            i += 1
        else:
            break
    if i >= len(argv):
        return None
    sub, args = argv[i], argv[i + 1 :]
    if sub == "push":
        return None if _is_dry_run(args) else Action("push", directory, args)
    if sub == "commit":
        return Action("commit", directory, args)
    if sub in ("add", "stage"):
        return Action("add", directory, args)
    if sub in HEAD_MOVERS:
        return Action("head-moves", directory, args)
    return None


def gh_action(argv: list[str], cwd: Path) -> Action | None:
    positionals, i = [], 1
    while i < len(argv) and len(positionals) < 2:
        word = argv[i]
        if word in ("-R", "--repo") and i + 1 < len(argv):
            i += 2
        elif word.startswith("-"):
            i += 1
        else:
            positionals.append(word)
            i += 1
    if positionals == ["pr", "create"] and not (_is_dry_run(argv[i:]) or "--dry-run" in argv):
        return Action("pr-create", cwd, [])
    return None


def analyze(text: str, cwd: Path, depth: int = 0) -> list[Action]:
    """The actions plumbline cares about in a command line, in order, each with the
    directory it runs in (`cd` and `git -C` are followed)."""
    actions: list[Action] = []
    for argv in split_commands(text):
        argv = strip_wrappers(argv)
        if not argv:
            continue
        name = os.path.basename(argv[0])
        if name in ("cd", "pushd"):
            target = next((w for w in argv[1:] if not w.startswith("-")), None)
            if target and not any(ch in target for ch in "$`") and target != "-":
                cwd = _join(cwd, target)
        elif name == "git":
            action = git_action(argv, cwd)
            if action:
                actions.append(action)
        elif name == "gh":
            action = gh_action(argv, cwd)
            if action:
                actions.append(action)
        elif name in SHELLS and depth < 3:
            for i, word in enumerate(argv[1:], 1):
                if word.startswith("-") and not word.startswith("--") and "c" in word[1:] and i + 1 < len(argv):
                    actions += analyze(argv[i + 1], cwd, depth + 1)
                    break
        elif name == "eval" and depth < 3:
            actions += analyze(" ".join(argv[1:]), cwd, depth + 1)
    return actions


def commit_scope(args: list[str], added: bool) -> str:
    """What `git commit` will commit, as far as the command line shows: only the
    index ("index"), also the tracked files' working-tree changes ("tracked": -a, a
    pathspec, --only, --include), or also whatever an earlier `git add` in the same
    command line stages ("all")."""
    tracked, i = False, 0
    while i < len(args):
        word = args[i]
        if word == "--":
            tracked = tracked or i + 1 < len(args)
            break
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            if name in ("--all", "--include", "--only"):
                tracked = True
            elif name in COMMIT_LONG_VALUE and "=" not in word:
                i += 1
        elif word.startswith("-") and len(word) > 1:
            for position, letter in enumerate(word[1:], 1):
                if letter in "aio":
                    tracked = True
                if letter in COMMIT_SHORT_VALUE:
                    if position == len(word) - 1:
                        i += 1  # the value is the next word
                    break
        else:
            tracked = True  # a pathspec
        i += 1
    return "all" if added else ("tracked" if tracked else "index")


# ---------------------------------------------------------- what to allow


def adopted_root(pl, directory: Path) -> Path | None:
    """The top level of the repository `directory` is in, when it has adopted plumbline."""
    root = pl.git_toplevel(directory, timeout=5) if directory.is_dir() else None
    return root if root is not None and (root / pl.CONFIG_FILE).is_file() else None


def _git(pl, root: Path, *args: str):
    return pl._git(root, *args, timeout=GIT_TIMEOUT)


def added_lines(patch: str):
    """(path, line number, text) of each added line of a unified diff with -U0."""
    path, line = "?", 0
    for raw in patch.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].split("\t")[0]
            path = path[2:] if path.startswith(("b/", "a/")) else path
            path = path.strip('"')
        elif raw.startswith("@@"):
            match = re.match(r"@@ -\S+ \+(\d+)", raw)
            line = int(match.group(1)) if match else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            yield path, line, raw[1:]
            line += 1


def commit_problems(pl, root: Path, scope: str) -> list[str]:
    """What the coming commit adds that must not be committed."""
    problems: list[str] = []
    tracked = scope in ("tracked", "all")
    heads = _git(pl, root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").returncode == 0
    variants = [["diff", "HEAD"]] if tracked and heads else ([["diff", "--cached"], ["diff"]] if tracked else [["diff", "--cached"]])
    for variant in variants:
        raw = _git(pl, root, *variant, "--raw", "-z", "--no-renames", "--no-ext-diff").stdout.decode("utf-8", "replace").split("\0")
        for index, token in enumerate(raw):
            if token.startswith(":") and index + 1 < len(raw):
                fields = token[1:].split(" ")
                if len(fields) >= 5 and fields[1] == "120000" and not fields[4].startswith("D"):
                    problems.append(f"adds a symlink: {raw[index + 1]}")
        patch = _git(pl, root, *variant, "-U0", "--no-color", "--no-ext-diff", "--no-renames").stdout[:MAX_SCANNED_BYTES].decode("utf-8", "replace")
        for path, line, text in added_lines(patch):
            if HOME_PATH.search(text):
                problems.append(f"adds an absolute home path: {path}:{line}")
            if SECRET.search(text):
                problems.append(f"adds a key-shaped secret: {path}:{line} (the value is not shown)")
    if scope == "all":
        listing = _git(pl, root, "ls-files", "--others", "--exclude-standard", "-z").stdout.decode("utf-8", "replace")
        for name in filter(None, listing.split("\0")):
            if name.startswith(".plumbline/"):
                continue  # plumbline's own working files
            path = root / name
            if path.is_symlink():
                problems.append(f"adds a symlink: {name}")
            elif path.is_file() and path.stat().st_size <= MAX_SCANNED_BYTES:
                data = path.read_bytes()
                if b"\0" in data[:8192]:
                    continue
                for number, text in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                    if HOME_PATH.search(text):
                        problems.append(f"adds an absolute home path: {name}:{number}")
                    if SECRET.search(text):
                        problems.append(f"adds a key-shaped secret: {name}:{number} (the value is not shown)")
    return list(dict.fromkeys(problems))


def push_reason(pl, root: Path, action: Action, head_moves: bool) -> str | None:
    what = "open a pull request" if action.kind == "pr-create" else "push"
    if head_moves:
        return (
            f"plumbline: this command changes HEAD and then tries to {what}, so the commit it would publish cannot have a pass record yet. "
            "Run the pipeline and commit first, record the pass with `plumbline.py pass <run>`, then push in a separate command."
        )
    result = _git(pl, root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    head = result.stdout.decode().strip()
    if result.returncode != 0 or not head:
        return None  # nothing to gate: git itself has nothing to push
    how, detail = pl.coverage(root, head)
    if how:
        return None
    return (
        f"plumbline: HEAD {head[:7]} has no pass or override record, so it is not {'opened for review' if action.kind == 'pr-create' else 'pushed'} ({detail}). "
        f"Run the pipeline (/plumbline:run), commit, and record the pass; or, if the builder decides to skip the pipeline, /plumbline:override. "
        f"Then {what} again."
    )


def commit_reason(pl, root: Path, action: Action, added: bool) -> str | None:
    problems = commit_problems(pl, root, commit_scope(action.args, added))
    if not problems:
        return None
    listed = "; ".join(problems[:MAX_LISTED]) + (f"; and {len(problems) - MAX_LISTED} more" if len(problems) > MAX_LISTED else "")
    return f"plumbline: this commit is held back. It {listed}. Remove them, stage the change again, and commit again."


def bash_reason(data: dict) -> str | None:
    command = (data.get("tool_input") or {}).get("command")
    cwd = data.get("cwd")
    if not isinstance(command, str) or not isinstance(cwd, str) or ("git" not in command and "gh" not in command):
        return None
    import plumbline as pl

    added = head_moves = False
    for action in analyze(command, Path(cwd)):
        root = adopted_root(pl, action.cwd)
        if root is None:
            continue
        if action.kind == "add":
            added = True
        elif action.kind == "head-moves":
            head_moves = True
        elif action.kind == "commit":
            reason = commit_reason(pl, root, action, added)
            if reason:
                return reason
            head_moves = True
        else:
            reason = push_reason(pl, root, action, head_moves)
            if reason:
                return reason
    return None


# -------------------------------------------------- the builder's blindness


def tests_of(pl, root: Path) -> tuple[list[str], set[str]]:
    """The tests type's path patterns, and the files the newest run's tests record
    lists, that record included: it names the tests and what each one checks."""
    project = pl.load_project(root)
    pipeline = project.pipeline or pl.load_toml(pl.PIPELINE_DIR / f"{pl.DEFAULT_PIPELINE}.toml")
    patterns = next((t["paths"] for t in pipeline.get("type", []) if t.get("id") == "tests"), [])
    listed: set[str] = set()
    run_id = pl.latest_run_id(root)
    if run_id is not None:
        stage_ids = [s["id"] for s in pipeline.get("stage", []) if s.get("record") == "tests_record"] or ["tests"]
        for stage_id in stage_ids:
            record_file = pl.run_dir(root, run_id) / f"{stage_id}.json"
            listed.add(pl.rel_path(root, record_file))
            data, _problem = pl.load_json_file(record_file)
            if isinstance(data, dict):
                names = [t.get("file") for t in data.get("tests", []) if isinstance(t, dict)] + list(data.get("files_written", []))
                for name in names:
                    if isinstance(name, str) and name:
                        path = Path(name)
                        listed.add(pl.rel_path(root, path) if path.is_absolute() else Path(os.path.normpath(name)).as_posix())
    return list(patterns), listed


def _test_file(pl, rel: str, patterns: list[str], listed: set[str]) -> bool:
    return rel in listed or any(pl.glob_match(pattern, rel) for pattern in patterns)


def _relative(root: Path, path: Path) -> str | None:
    """`path` relative to the repository root, or None when it lies outside."""
    for base, candidate in ((root, path), (root.resolve(), path.resolve())):
        try:
            return candidate.relative_to(base).as_posix()
        except ValueError:
            continue
    return None


def exposed_test(pl, root: Path, target: Path, patterns: list[str], listed: set[str]) -> str | None:
    """The repository-relative path of a test that reading or searching `target`
    would show (the target itself, or a file below it), or None."""
    names = {target, Path(os.path.realpath(target))}
    for candidate in names:
        rel = _relative(root, candidate)
        if rel is None:
            continue
        if candidate.is_dir():
            result = _git(pl, root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", rel or ".")
            if result.returncode != 0:
                return rel or "."  # cannot list what is there: assume the worst
            for name in filter(None, result.stdout.decode("utf-8", "replace").split("\0")):
                if _test_file(pl, name, patterns, listed):
                    return name
            for name in sorted(listed):  # listed files that git does not show, such as those under an ignored .plumbline/
                if (rel == "" or name.startswith(rel + "/")) and (root / name).exists():
                    return name
        elif _test_file(pl, rel, patterns, listed) or (not candidate.exists() and _test_file(pl, rel + "/x", patterns, listed)):
            return rel
    return None


def _static_prefix(pattern: str) -> str:
    """The leading path segments of a glob pattern that hold no wildcard."""
    prefix = []
    for segment in pattern.replace("\\", "/").split("/"):
        if any(ch in segment for ch in "*?[{"):
            break
        prefix.append(segment)
    else:
        prefix = prefix[:-1]  # no wildcard at all: the last segment is a file name
    return "/".join(prefix) if prefix != [""] else "/"


def builder_reason(data: dict) -> str | None:
    if data.get("agent_type") != "plumbline:builder":
        return None
    tool, tool_input, cwd = data.get("tool_name"), data.get("tool_input") or {}, data.get("cwd")
    if not isinstance(tool_input, dict) or not isinstance(cwd, str) or not cwd:
        return None
    import plumbline as pl

    root = adopted_root(pl, Path(cwd))
    if root is None:
        return None
    patterns, listed = tests_of(pl, root)
    if tool == "Read":
        named = tool_input.get("file_path")
        if not isinstance(named, str) or not named:
            return None
        hit = exposed_test(pl, root, _join(Path(cwd), named), patterns, listed)
        if hit:
            return f"plumbline: the builder works blind to the tests, and {hit} is one. Build from the spec and the source."
        return None
    if tool in ("Grep", "Glob"):
        named = tool_input.get("path")
        if not isinstance(named, str) or not named.strip():
            return (
                f"plumbline: the builder works blind to the tests, so {tool} needs an explicit path that is not a test path (for example src/). "
                "A search of the whole repository would show the tests."
            )
        target = _join(Path(cwd), named)
        if tool == "Glob" and isinstance(tool_input.get("pattern"), str):
            target = _join(target, _static_prefix(tool_input["pattern"]))
        hit = exposed_test(pl, root, target, patterns, listed)
        if hit:
            return f"plumbline: the builder works blind to the tests, and a {tool} of {named} would show them (for example {hit}). Search a directory without tests."
    return None


# ------------------------------------------------------------- the hook


def decide(data) -> str | None:
    """The reason to deny this tool call, or None to allow it."""
    if not isinstance(data, dict):
        return None
    tool = data.get("tool_name")
    if tool in ("Bash", "PowerShell"):
        return bash_reason(data)
    if tool in ("Read", "Grep", "Glob"):
        return builder_reason(data)
    return None


def main() -> int:
    try:
        reason = decide(json.load(sys.stdin))
    except BaseException:  # a hook must never crash, and must not hold anything up
        return 0
    if reason:
        sys.stdout.write(
            json.dumps(
                {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}
            )
            + "\n"
        )
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        code = 0
    sys.exit(code)
