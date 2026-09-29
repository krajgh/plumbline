#!/usr/bin/env python3
"""PreToolUse hook for plumbline.

Reads the hook's JSON on stdin and, to deny a tool call, prints
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
"permissionDecisionReason": ...}} and exits 0. It acts only inside a repository
that has adopted plumbline (a plumbline.toml at its top level), except for the
override rule:

  Bash (and PowerShell), for everyone
    - `plumbline.py override` is denied to the model, whoever runs it: the builder
      types /plumbline:override, which runs the command itself.
    - `git push` and `gh pr create` are denied unless HEAD has a pass record or
      an override record (.plumbline/pass/<HEAD>.json or .override.json) that
      validates and names HEAD; a pass record is not taken at its word (its run is
      evaluated again). `--dry-run` and `--help` are not pushes.
    - `git commit` is denied when what it would commit adds a symlink, an
      absolute home path, or a key-shaped secret (sk-ant- followed by 20 or more
      characters).
    - a redirection, `tee` or another writer aimed at .plumbline/pass/ or at a
      run's ledger.jsonl is denied: only plumbline.py commands write them.
  Bash, for plumbline agents
    - every simple command must match the command classes of the agent's role
      (the [roles.*] tables of the pipeline); a denial names what is allowed.
  Edit, Write and NotebookEdit
    - .plumbline/pass/ and every ledger.jsonl are denied to everyone.
    - a plumbline agent writes only what its role's write targets cover: its own
      record, the tests type, or everything else except .plumbline/,
      plumbline.toml and the pipeline file.
  Agent
    - launching a plumbline agent with `isolation: "worktree"` is denied: a run's records live in the
      main checkout, never in an agent's worktree.
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

# The plumbline agents, and the review units' among them (which write under a review stage's round directories).
ROLES = ("planner", "test-writer", "builder", "verifier", "prosecutor", "defender", "detective")
REVIEW_ROLES = ("prosecutor", "defender", "detective")
# A command of the main session concerns plumbline besides git and gh only if it mentions one of these.
CARES = re.compile(r"plumbline|override|ledger")

# The command classes of a role policy (see [roles.*] in the pipeline), besides the repo's [commands].
CONFIG_COMMANDS = ("test", "lint", "typecheck", "build")
GIT_READ = ("diff", "show", "log", "status", "rev-parse", "merge-base", "ls-files", "grep", "blame")
GIT_QUIET_FLAGS = {"--no-pager", "-P", "--no-optional-locks", "--literal-pathspecs", "--no-replace-objects"}
GIT_WRITES_OR_RUNS = ("--output", "--open-files-in-pager", "-O")  # a file is written, or a program is run
SEARCH_TOOLS = ("grep", "egrep", "fgrep", "rg", "cat", "head", "tail", "wc", "ls", "sed", "find")
FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
RG_RUNS = ("--pre", "--hostname-bin")  # ripgrep options that run a program
SED_PRINT = re.compile(r"\s*(?:\d+|\$|/[^/]*/)?(?:,(?:\d+|\$|/[^/]*/))?\s*p\s*")  # `sed -n` prints lines: nothing else
NO_EFFECT = {"cd", "pushd", "popd", "pwd", "true", "false", ":", "echo", "printf"}  # allowed wherever any command is
WRITERS_ALL = {"tee", "rm", "unlink", "shred", "truncate", "touch", "mv"}  # every operand is written or removed
WRITERS_LAST = {"cp", "install", "ln", "rsync"}  # the destination is written
PASS_DIR = ".plumbline/pass"
RUNS_DIR = ".plumbline/runs"


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
    return [words for words, _redirects in split_commands_ex(text, depth) if words]


def split_commands_ex(text: str, depth: int = 0) -> list[tuple[list[str], list[tuple[str, str]]]]:
    """Like split_commands, but each command comes with its redirections as (operator, target) pairs,
    in the order written, so that a caller can see what a command line writes to. A redirection with
    no command (`> file`) is a command with no words."""
    commands: list[tuple[list[str], list[tuple[str, str]]]] = []
    if depth > 4:
        return commands
    words: list[str] = []
    redirects: list[tuple[str, str]] = []
    word: list[str] | None = None
    pending: str | None = None  # the operator whose target is the next word
    heredocs: list[tuple[str, bool]] = []
    n, i = len(text), 0

    def end_word():
        nonlocal word, pending
        if word is not None:
            if pending is not None:
                redirects.append((pending, "".join(word)))
                pending = None
            else:
                words.append("".join(word))
            word = None

    def end_command():
        nonlocal words, redirects, pending
        end_word()
        if words or redirects:
            commands.append((words, redirects))
        words, redirects, pending = [], [], None

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
                    commands.extend(split_commands_ex(text[i + 2 : j], depth + 1))
                    add("$(...)")
                    i = j + 1
                elif d == "`":
                    j = text.find("`", i + 1)
                    j = n if j < 0 else j
                    commands.extend(split_commands_ex(text[i + 1 : j], depth + 1))
                    add("`...`")
                    i = j + 1
                else:
                    add(d)
                    i += 1
            i += 1
        elif c == "$" and text[i + 1 : i + 2] == "(":
            j = _matching_paren(text, i + 1)
            commands.extend(split_commands_ex(text[i + 2 : j], depth + 1))
            add("$(...)")
            i = j + 1
        elif c == "`":
            j = text.find("`", i + 1)
            j = n if j < 0 else j
            commands.extend(split_commands_ex(text[i + 1 : j], depth + 1))
            add("`...`")
            i = j + 1
        elif c in "<>":
            if word is not None and "".join(word).isdigit():
                word = None  # a file descriptor number, as in 2>&1
            end_word()
            if text[i : i + 3] == "<<<":
                pending = "<<<"
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
                start = i
                while i < n and (text[i] in "<>&" or (text[i] == "|" and text[i - 1] == ">")):  # `>|` overrides noclobber
                    i += 1
                pending = text[start:i]
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
    """The top level of the repository `directory` is in, when it has adopted plumbline. A directory that does not
    exist (a `cd` into it fails, and the command runs where the shell was) is looked up from the nearest one that does."""
    while not directory.is_dir() and directory != directory.parent:
        directory = directory.parent
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


def commit_problems(pl, root: Path, scope: str, base: str | None = None) -> list[str]:
    """What the coming commit adds that must not be committed. With a `base` commit (scope "all"), what
    the whole change adds: everything from that commit to the working tree, .plumbline/ left out."""
    problems: list[str] = []
    tracked = scope in ("tracked", "all")
    heads = _git(pl, root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}").returncode == 0
    variants = [["diff", "HEAD"]] if tracked and heads else ([["diff", "--cached"], ["diff"]] if tracked else [["diff", "--cached"]])
    hide = []
    if base is not None:
        variants, hide = [["diff", base]], ["--", ".", ":(exclude).plumbline"]
    for variant in variants:
        raw = _git(pl, root, *variant, "--raw", "-z", "--no-renames", "--no-ext-diff", *hide).stdout.decode("utf-8", "replace").split("\0")
        for index, token in enumerate(raw):
            if token.startswith(":") and index + 1 < len(raw):
                fields = token[1:].split(" ")
                if len(fields) >= 5 and fields[1] == "120000" and not fields[4].startswith("D"):
                    problems.append(f"adds a symlink: {raw[index + 1]}")
        patch = _git(pl, root, *variant, "-U0", "--no-color", "--no-ext-diff", "--no-renames", *hide).stdout[:MAX_SCANNED_BYTES].decode("utf-8", "replace")
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
    how, detail = pl.coverage(root, head, pl.load_project(root))
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


def gate_reason(pl, command: str, cwd: Path) -> str | None:
    """The push gate and the commit checks."""
    if "git" not in command and "gh" not in command:
        return None
    added = head_moves = False
    for action in analyze(command, cwd):
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


def bash_reason(data: dict) -> str | None:
    command = (data.get("tool_input") or {}).get("command")
    cwd = data.get("cwd")
    if not isinstance(command, str) or not isinstance(cwd, str):
        return None
    role = agent_role(data)
    if role is None and not CARES.search(command) and ".plumbline" not in cwd and "git" not in command and "gh" not in command:
        return None  # nothing here concerns plumbline
    import plumbline as pl

    steps = list(walk(command, Path(cwd)))
    reason = override_reason(pl, steps)
    if reason is None and role is not None:
        reason = role_command_reason(pl, role, steps, Path(cwd))
    if reason is None:
        reason = protected_command_reason(pl, steps)
    return reason or gate_reason(pl, command, Path(cwd))


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


# ---------------------------------------------- what an agent may do


@dataclass
class Step:
    """One simple command of a command line, with where it runs and what it redirects to."""

    raw: list[str]  # the words as written
    argv: list[str]  # the command itself: no VAR=value words, wrappers or shell keywords
    redirects: list[tuple[str, str]]  # (operator, target)
    cwd: Path


def walk(text: str, cwd: Path, depth: int = 0):
    """Every simple command of a command line, those inside `$( )`, backticks, `bash -c` and `eval`
    included, each with the directory it runs in (`cd` is followed)."""
    for words, redirects in split_commands_ex(text):
        argv = strip_wrappers(words)
        yield Step(words, argv, redirects, cwd)
        name = os.path.basename(argv[0]) if argv else ""
        if name in ("cd", "pushd"):
            target = next((w for w in argv[1:] if not w.startswith("-")), None)
            if target and not any(ch in target for ch in "$`") and target != "-":
                cwd = _join(cwd, target)
        elif name in SHELLS and depth < 3:
            for i, word in enumerate(argv[1:], 1):
                if word.startswith("-") and not word.startswith("--") and "c" in word[1:] and i + 1 < len(argv):
                    yield from walk(argv[i + 1], cwd, depth + 1)
                    break
        elif name == "eval" and depth < 3:
            yield from walk(" ".join(argv[1:]), cwd, depth + 1)


def agent_role(data: dict) -> str | None:
    """The role of the plumbline agent making this tool call, or None (the main session, or another agent)."""
    agent_type = data.get("agent_type")
    if isinstance(agent_type, str) and agent_type.startswith("plumbline:"):
        role = agent_type[len("plumbline:") :]
        return role if role in ROLES else None
    return None


def plumbline_cli(pl, argv: list[str]) -> str | None:
    """The subcommand, when `argv` runs this plugin's own plumbline.py (`python3 <path>/plumbline.py <subcommand> ...`)."""
    if not argv or not re.fullmatch(r"python(\d+(\.\d+)*)?", os.path.basename(argv[0])):
        return None
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] in ("-c", "-m"):
            return None
        i += 1
    if i + 1 >= len(argv):
        return None
    if os.path.realpath(argv[i]) != os.path.realpath(pl.PLUGIN_ROOT / "scripts" / "plumbline.py"):
        return None  # another script that happens to be called plumbline.py is not ours
    return argv[i + 1]


def runs_override(step: Step) -> bool:
    """Does this command run `plumbline.py override`? By its script name, by the pair of words `override` and
    `--reason` (which catches a script path held in a variable), or by inline code that names both."""
    words = step.raw
    for i, word in enumerate(words):
        if os.path.basename(word) == "plumbline.py" and words[i + 1 : i + 2] == ["override"]:
            return True
    if "override" in words and any(w == "--reason" or w.startswith("--reason=") for w in words):
        return True
    interpreter = os.path.basename(step.argv[0]) if step.argv else ""
    return bool(re.fullmatch(r"(python[\d.]*|node|ruby|perl|php)", interpreter)) and any("plumbline" in w and "override" in w for w in step.argv[1:])


def override_reason(pl, steps: list[Step]) -> str | None:
    """`plumbline.py override` is denied to every agent and to the main session, wherever the command would act:
    in the directory it runs in, or in the one `--project` names, when that repository has adopted plumbline
    (elsewhere the command refuses by itself, and this hook stays silent)."""
    for step in steps:
        if not runs_override(step):
            continue
        places = [step.cwd] + [_join(step.cwd, step.raw[i + 1]) for i, w in enumerate(step.raw[:-1]) if w == "--project"]
        places += [_join(step.cwd, w.split("=", 1)[1]) for w in step.raw if w.startswith("--project=")]
        if any(adopted_root(pl, place) is not None for place in places):
            return (
                "plumbline: `plumbline.py override` is the builder's command, and the builder types it: /plumbline:override followed by the reason. "
                "An agent, or the main session, does not run it. To skip the pipeline for this commit, ask the builder to type /plumbline:override."
            )
    return None


def written_operands(argv: list[str]) -> list[str]:
    """The operands a command writes or removes, for the writers plumbline knows: `tee`, `rm`, `cp`, `mv`, `dd of=`, `sed -i` and the like."""
    if not argv:
        return []
    name, args = os.path.basename(argv[0]), argv[1:]
    operands = [a for a in args if not a.startswith("-") or a == "-"]
    if name in WRITERS_ALL:
        return operands
    if name in WRITERS_LAST:
        into = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-t", "--target-directory")]
        return into + operands[-1:]
    if name == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    if name in ("sed", "perl") and any(re.fullmatch(r"-[A-Za-z]*i.*", a) or a.startswith("--in-place") for a in args):
        return operands
    return []


def written_targets(step: Step) -> list[str]:
    """Where a command writes to: its output redirections (not fd duplications like `2>&1`) and its writer operands."""
    targets = [
        target
        for op, target in step.redirects
        if ">" in op and not (op.endswith("&") and re.fullmatch(r"\d+|-", target))
    ]
    return targets + written_operands(step.argv)


def protected_rel(pl, roots: dict, path: Path) -> str | None:
    """The repository-relative path, when `path` (or where it really leads) is under .plumbline/pass/ or is a
    run's ledger.jsonl in a repository that has adopted plumbline: those are written only by plumbline.py."""
    for candidate in dict.fromkeys((path, Path(os.path.realpath(path)))):
        if ".plumbline" not in str(candidate) and candidate.name != "ledger.jsonl":
            continue  # cannot be one of them: no need to look for the repository
        directory = candidate.parent
        while not directory.is_dir() and directory != directory.parent:
            directory = directory.parent
        if directory not in roots:
            roots[directory] = adopted_root(pl, directory)
        root = roots[directory]
        rel = _relative(root, candidate) if root is not None else None
        if rel is not None and (
            rel == PASS_DIR or rel.startswith(PASS_DIR + "/") or re.fullmatch(re.escape(RUNS_DIR) + r"/[^/]+/ledger\.jsonl", rel)
        ):
            return rel
    return None


def protected_message(rel: str) -> str:
    return (
        f"plumbline: {rel} is written only by plumbline.py commands (`pass` writes .plumbline/pass/; the gates and the hooks write a run's ledger.jsonl), "
        "so nothing edits it, redirects into it or pipes into it. Run the pipeline's commands instead."
    )


def protected_command_reason(pl, steps: list[Step]) -> str | None:
    roots: dict = {}
    for step in steps:
        for target in written_targets(step):
            if target:
                rel = protected_rel(pl, roots, _join(step.cwd, target))
                if rel:
                    return protected_message(rel)
    return None


def _prefix_matches(argv: list[str], prefixes: list[str]) -> bool:
    for prefix in prefixes:
        parsed = split_commands(prefix)
        want = strip_wrappers(parsed[0]) if len(parsed) == 1 else []
        if not want:
            continue  # not a simple command: nothing can match it
        head = [argv[0] if "/" in want[0] else os.path.basename(argv[0])] + argv[1:]  # a bare name matches wherever the program lives
        if head[: len(want)] == want:
            return True
    return False


def _git_read_only(argv: list[str]) -> bool:
    i = 1
    while i < len(argv):
        word = argv[i]
        if word == "-C" and i + 1 < len(argv):
            i += 2
        elif word in GIT_QUIET_FLAGS:
            i += 1
        elif word.startswith("-"):
            return False  # -c, --git-dir, --exec-path and the like change what git runs
        else:
            break
    if i >= len(argv) or argv[i] not in GIT_READ:
        return False
    rest = argv[i + 1 :]
    return not any(a.startswith(GIT_WRITES_OR_RUNS) for a in (rest[: rest.index("--")] if "--" in rest else rest))


def _sed_read_only(args: list[str]) -> bool:
    quiet, scripts, positional, i = False, [], [], 0
    while i < len(args):
        word = args[i]
        if word in ("-n", "--quiet", "--silent"):
            quiet = True
        elif word in ("-E", "-r", "--regexp-extended"):
            pass
        elif word in ("-e", "--expression"):
            if i + 1 >= len(args):
                return False
            scripts.append(args[i + 1])
            i += 1
        elif word.startswith("--expression="):
            scripts.append(word.split("=", 1)[1])
        elif word.startswith("-") and len(word) > 1:
            return False  # -i, -f, -s, -z ...
        else:
            positional.append(word)
        i += 1
    if not scripts and positional:
        scripts = [positional[0]]
    return quiet and bool(scripts) and all(SED_PRINT.fullmatch(script) for script in scripts)


def _search_only(argv: list[str]) -> bool:
    name, args = os.path.basename(argv[0]), argv[1:]
    if name not in SEARCH_TOOLS:
        return False
    if name == "find":
        return not any(a in FIND_ACTIONS for a in args)
    if name == "sed":
        return _sed_read_only(args)
    if name == "rg":
        return not any(a.startswith(RG_RUNS) for a in args)
    return True


def _allowed_by_class(pl, project, classes: list[str], argv: list[str]) -> bool:
    name = os.path.basename(argv[0])
    for kind in classes:
        if kind in CONFIG_COMMANDS and _prefix_matches(argv, project.commands.get(kind, [])):
            return True
        if kind == "git-read" and name == "git" and _git_read_only(argv):
            return True
        if kind == "search" and _search_only(argv):
            return True
        if kind == "plumbline-check" and plumbline_cli(pl, argv) == "check-diff":
            return True
        if kind == "graft" and project.graft_enabled and name == "graft":
            return True
    return False


def _allowed_summary(project, role: str, classes: list[str]) -> str:
    parts = []
    configured = [kind for kind in classes if kind in CONFIG_COMMANDS]
    for kind in configured:
        if project.commands.get(kind):
            parts.append(f"the repository's {kind} command ({', '.join(project.commands[kind])})")
    unset = [kind for kind in configured if not project.commands.get(kind)]
    if unset:
        parts.append(f"the repository's {', '.join(unset)} command{'s' if len(unset) > 1 else ''} (not set: [commands] in plumbline.toml sets them)")
    for kind in classes:
        if kind in CONFIG_COMMANDS:
            continue
        if kind == "git-read":
            parts.append(f"read-only git ({', '.join(GIT_READ)})")
        elif kind == "search":
            parts.append("read-only search tools (grep, rg, cat, head, tail, wc, sed -n, ls, find without -exec or -delete)")
        elif kind == "plumbline-check":
            parts.append("`plumbline.py check-diff`")
        elif kind == "graft":
            parts.append("the graft wrapper" if project.graft_enabled else "the graft wrapper (graft is off in this repository)")
    parts.append("`plumbline.py check-record TYPE FILE` to check your record")
    return "; ".join(parts)


def role_command_reason(pl, role: str, steps: list[Step], cwd: Path) -> str | None:
    """Every simple command of an agent's Bash line must be of a class its role allows, and nothing may be
    redirected into a file (the write targets cover Edit, Write and NotebookEdit; Bash is for running things)."""
    root = adopted_root(pl, cwd)
    if root is None:
        return None
    project = pl.load_project(root)
    policy = project.roles.get(role)
    if not isinstance(policy, dict):
        return None
    classes = [c for c in policy.get("commands", []) if isinstance(c, str)]
    for step in steps:
        if not step.raw and not step.redirects:
            continue
        shown = " ".join(step.raw)[:100] or "a redirection"
        if not classes:
            return f"plumbline: the {role} has no Bash. Work with Read, Grep, Glob, Edit and Write, and end with your record. (`{shown}` was refused.)"
        for op, target in step.redirects:
            if ">" in op and target != "/dev/null" and not (op.endswith("&") and re.fullmatch(r"\d+|-", target)):
                return (
                    f"plumbline: the {role}'s Bash does not write files (`{shown}` redirects into {target}). "
                    "Write your record with the Write tool; Bash may redirect only to /dev/null."
                )
        if not step.argv and all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w) or w in KEYWORDS for w in step.raw):
            continue  # nothing but variable assignments and shell keywords: no command runs
        if step.argv and os.path.basename(step.argv[0]) in NO_EFFECT:
            continue
        if step.raw and os.path.basename(step.raw[0]) in ("sudo", "doas"):
            return f"plumbline: the {role} does not run commands as another user (`{shown}`)."
        if step.argv and (_allowed_by_class(pl, project, classes, step.argv) or plumbline_cli(pl, step.argv) == "check-record"):
            continue
        return f"plumbline: the {role}'s Bash may run only {_allowed_summary(project, role, classes)}. `{shown}` is none of these."
    return None


def own_record_paths(pipeline: dict, role: str) -> list[str]:
    """Where an agent of `role` writes its record, as patterns for a message."""
    if role in REVIEW_ROLES:
        stages = [s["id"] for s in pipeline.get("stage", []) if s.get("kind") == "review"]
        return [f"{RUNS_DIR}/<run-id>/{stages[0] if len(stages) == 1 else '{' + ','.join(stages) + '}'}/round-<n>/<name>.json"] if stages else []
    return [f"{RUNS_DIR}/<run-id>/{s['id']}.json" for s in pipeline.get("stage", []) if s.get("kind", "agent") == "agent" and s.get("role") == role]


def is_own_record(pipeline: dict, role: str, rel: str) -> bool:
    match = re.fullmatch(re.escape(RUNS_DIR) + r"/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/(.+)", rel)
    if not match:
        return False
    rest = match.group(1)
    stages = pipeline.get("stage", [])
    if any(s.get("kind", "agent") == "agent" and s.get("role") == role and rest == f"{s['id']}.json" for s in stages):
        return True
    return role in REVIEW_ROLES and any(
        s.get("kind") == "review" and re.fullmatch(re.escape(s["id"]) + r"/round-[1-9][0-9]*/[A-Za-z0-9][A-Za-z0-9._-]*\.json", rest) for s in stages
    )


def pipeline_paths(pl, project, root: Path) -> set[str]:
    """The pipeline file the repository names, as a path inside the repository (none, when it lies elsewhere)."""
    value = (project.config or {}).get("pipeline", pl.DEFAULT_PIPELINE)
    try:
        rel = _relative(root, pl.resolve_pipeline_path(value, root)) if isinstance(value, str) else None
    except pl.PlumblineError:
        rel = None
    return {rel} if rel else set()


def partition_reason(pl, root: Path, role: str, target: Path, real: Path) -> str | None:
    """Is a write by an agent of `role` to `target` inside the role's write targets? Where the path leads, through
    symbolic links, must be inside them too."""
    project = pl.load_project(root)
    policy = project.roles.get(role)
    if not isinstance(policy, dict):
        return None
    writes = [w for w in policy.get("writes", []) if isinstance(w, str)]
    pipeline = project.pipeline or pl.load_toml(pl.PIPELINE_DIR / f"{pl.DEFAULT_PIPELINE}.toml")
    patterns = next((t["paths"] for t in pipeline.get("type", []) if t.get("id") == "tests"), [])
    fixed = pipeline_paths(pl, project, root) | {pl.CONFIG_FILE}
    own = own_record_paths(pipeline, role)
    for candidate in dict.fromkeys((target, real)):
        rel = _relative(root, candidate)
        if rel is None:
            return f"plumbline: the {role} writes inside the repository, and {candidate} is outside it."
        if "record" in writes and is_own_record(pipeline, role, rel):
            continue
        if rel == ".plumbline" or rel.startswith(".plumbline/"):
            where = ", ".join(own) or "nowhere"
            return f"plumbline: the {role} writes only its own record ({where}); {rel} is another of plumbline's files."
        if rel in fixed:
            return f"plumbline: {rel} defines how the pipeline runs, and the {role} leaves it alone."
        if any(pl.glob_match(pattern, rel) for pattern in patterns):
            if "tests" not in writes:
                hint = " The test-writer writes them; build from the spec." if role == "builder" else ""
                return f"plumbline: {rel} is a test path, and the {role} does not write tests.{hint}"
        elif "code" not in writes:
            can = ["its own record (" + (own[0] if own else "under .plumbline/runs/") + ")"] + (["test paths"] if "tests" in writes else [])
            hint = " Put stubs under a test path; the source is the builder's." if role == "test-writer" else ""
            return f"plumbline: the {role} writes {' and '.join(can)}; {rel} is neither.{hint}"
    return None


def agent_launch_reason(data: dict) -> str | None:
    """A run's records live in the main checkout, so a plumbline agent is not launched into a worktree of its own."""
    tool_input, cwd = data.get("tool_input"), data.get("cwd")
    if not isinstance(tool_input, dict) or tool_input.get("isolation") != "worktree":
        return None
    subagent = tool_input.get("subagent_type")
    if not isinstance(subagent, str) or not subagent.startswith("plumbline:") or not isinstance(cwd, str) or not cwd:
        return None
    import plumbline as pl

    if adopted_root(pl, Path(cwd)) is None:
        return None
    return f"plumbline: a run's records live in the main checkout, so {subagent} runs there. Launch it without `isolation`."


def write_reason(data: dict) -> str | None:
    """Edit, Write and NotebookEdit: nobody writes the protected files, and an agent writes inside its role's targets."""
    tool_input, cwd = data.get("tool_input"), data.get("cwd")
    if not isinstance(tool_input, dict) or not isinstance(cwd, str) or not cwd:
        return None
    named = next((tool_input[k] for k in ("file_path", "notebook_path") if isinstance(tool_input.get(k), str) and tool_input[k]), None)
    if named is None:
        return None
    role = agent_role(data)
    target = _join(Path(cwd), named)
    real = Path(os.path.realpath(target))
    if role is None and ".plumbline" not in f"{target}{real}" and target.name != "ledger.jsonl" and real.name != "ledger.jsonl":
        return None  # nothing here concerns plumbline: not even the repository is looked up
    import plumbline as pl

    rel = protected_rel(pl, {}, target)
    if rel:
        return protected_message(rel)
    if role is None:
        return None
    root = adopted_root(pl, Path(cwd))
    return partition_reason(pl, root, role, target, real) if root is not None else None


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
    if tool in ("Edit", "Write", "NotebookEdit"):
        return write_reason(data)
    if tool in ("Agent", "Task"):  # the tool has been called both
        return agent_launch_reason(data)
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
