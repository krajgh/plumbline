#!/usr/bin/env python3
"""PreToolUse hook for plumbline.

Reads the hook's JSON on stdin and, to deny a tool call, prints
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
"permissionDecisionReason": ...}} and exits 0. It acts only inside a repository
that has adopted plumbline (a plumbline.toml at its top level), except for the
override rule. A repository is found from the directory a call runs in and from
the paths the call names, so an agent whose working directory drifts is held all the same.

  Bash, PowerShell and Monitor, for everyone
    - `plumbline.py override` is denied to the model, whoever runs it: the builder
      types /plumbline:override, which runs the command itself. Abbreviated options
      (`--rea`, `--proj`) count.
    - `git push` and `gh pr create` are denied unless the commit a push publishes has
      a pass record or an override record (.plumbline/pass/<sha>.json or .override.json)
      that validates and names it; a pass record is not taken at its word (its run is
      evaluated again). Every directory the command line may run in is checked (the
      working directory and each cd, pushd or env -C target), a `.git` working
      directory included; every ref a push names is checked, not only HEAD; `--all`,
      `--mirror` and `--tags` are denied; deletions are not pushes; aliases (`-c
      alias.p=push`, or from git config) are expanded. `--dry-run` and `--help` are not pushes.
    - `git commit` is denied when what it would commit adds a symlink, an
      absolute home path, or a key-shaped secret (sk-ant- followed by 20 or more
      characters).
    - a redirection, `tee` or another writer aimed at .plumbline/pass/, at a
      run's ledger.jsonl or at .plumbline/runs/ACTIVE is denied: only plumbline.py
      commands write them.
    - the commit checks read the change on a copy of the index, so that a check leaves
      the repository's own index as it was (`git diff` rewrites the index it reads).
  Bash, for plumbline agents
    - every simple command must match the command classes of the agent's role
      (the [roles.*] tables of the pipeline); a denial names what is allowed. The
      read-only git and search classes take no leading VAR=value, and no git option
      that runs a program or writes a file, in any abbreviation git accepts.
    - the orchestrator's classes are plumbline-run (plumbline.py's check-diff, gate, merge-review,
      status, tokens, check-record and open, and `plan --run RUN --json`: no `plan --intent`, `pass`,
      `override` or `init`) and git-meta (git status, rev-parse, log, branch --show-current, and diff
      with --stat, --numstat or --name-only: nothing that prints a patch). It has no search class,
      so `cat` and `grep` of source are refused, and no VAR=value goes before either class.
  Edit, Write and NotebookEdit
    - .plumbline/pass/, every ledger.jsonl and .plumbline/runs/ACTIVE are denied to everyone, and the
      orchestrator (whose policy writes nothing) writes no file at all.
    - a plumbline agent writes only what its role's write targets cover: its own
      record (in the active run, and for the review roles in the current round of the
      stage, in the file named for the role), the tests type, or everything else except
      .plumbline/, plumbline.toml and the pipeline file; no agent writes .git/ (a linked
      worktree's git directory included), .claude/, .github/, .husky/, .mcp.json,
      CLAUDE.md, AGENTS.md, .gitattributes, .worktreeinclude or .pre-commit-config.yaml,
      and an agent that writes code but not tests also leaves conftest.py, pytest.ini, tox.ini,
      setup.cfg, noxfile.py and the files the repository's [commands] name to the main session.
  Agent
    - the orchestrator launches the stage agents (plumbline:planner, test-writer, builder, verifier,
      prosecutor, defender, detective, canary) and no other agent: not itself, not a general agent
      and not another plugin's. The rules below hold for its launches as for the main session's.
    - a plumbline agent is launched in the main checkout (no `isolation`) and with the model
      its definition pins; another agent is not briefed on .plumbline/ paths; in a calibration
      run a defender's brief does not name the canary (its record is listed among the findings
      records under a prosecutor's kind of name, so that nothing names it).
  Read, Grep, Glob and Bash, for the agent plumbline:defender, where a calibration run has a canary key
    - canary-key.json, in a round directory, the ledger.jsonl of its run (it names the agent that wrote
      each record, the canary's among them), and a directory that holds either are not read or searched:
      Read, Grep and Glob are denied them, and so is a Bash command that names one or runs a search
      tool over it (a glob counts as what it matches).
  Read, Grep and Glob, for the agent plumbline:orchestrator
    - only inside .plumbline/runs/<the active run>/ (symbolic links followed): its records, ledger and
      round directories. A Grep or Glob needs an explicit path there, and a Glob pattern that leaves it
      by `..` or an absolute path is refused. Source, tests and diffs stay with the stage agents.
  Read, Grep and Glob, for the agent plumbline:builder only
    - a path that matches the tests type of the pipeline, or is listed in the tests record
      of any run, is denied; under .plumbline/ only the active run's intake, plan and the
      builder's own record are read; test-run leftovers (.pytest_cache, junit*.xml, .coverage*,
      htmlcov, .tox) and the agent transcripts under ~/.claude are denied; so is a Grep or Glob
      without an explicit path, and one whose path leads to a directory holding any of these.

Any error, and any repository without a plumbline.toml, allows the call:
silently, with exit status 0; each rule runs on its own, so one that meets input it cannot handle
(PLUMBLINE_HOOK_DEBUG=1 lets the error through, for the tests) does not silence the others. The command
is parsed, not searched, so `echo git push` is not a push and `g""it push` is one (once Python runs: the sh
filter does not start it for that spelling). Without python3 the sh wrapper (pre_tool_use.sh) answers in its place.
"""
import sys

if sys.version_info < (3, 11):  # plumbline.py reads TOML with tomllib: answer "python3 not found" (the sh wrapper turns that into a deny where plumbline is adopted)
    sys.stderr.write("plumbline needs Python 3.11 or newer\n")
    sys.exit(127)

import base64
import contextlib
import glob
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

GIT_TIMEOUT = 20
MAX_SCANNED_BYTES = 8_000_000
MAX_LISTED = 8
MAX_DIRS = 64  # the directories one command line may run in: more are not followed
MAX_ANCHORS = 12  # the places an agent's call is looked up from, to find its repository
PASS_READ_SECONDS = 10  # how long the pass records of a repository may take to answer (a hook has 30 s)
NO_RECORD = "no pass or override record"  # the reason plumbline.coverage gives for a commit that has neither record

# Written so that this file does not itself contain an absolute home path.
HOME_PATH = re.compile(r"(?<![\w./~-])/(?:home|Users)/[A-Za-z0-9_][A-Za-z0-9._-]*/")
SECRET = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)

SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash", "mksh", "pdksh", "rbash", "fish", "csh", "tcsh"}
KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}"}  # words that may come before a command
# wrapper -> options that take a value
WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "--user", "--group", "--host", "--prompt", "--chdir", "--close-from", "--role", "--type", "--other-user"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string", "--argv0"},
    "command": set(),
    "exec": {"-a"},
    "nohup": set(),
    "time": {"-f", "-o", "--format", "--output"},
    "nice": {"-n", "--adjustment"},
    "setsid": set(),
    "timeout": {"-k", "-s", "--kill-after", "--signal"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid", "--pgid", "--uid"},
    "ssh-agent": {"-t", "-a", "-E", "-P"},
    "taskset": set(),
    "flock": {"-w", "-E", "--timeout", "--conflict-exit-code"},
    "chrt": set(),
    "unbuffer": set(),
    "chronic": set(),
    "busybox": set(),
    "coproc": set(),
}
WRAPPER_POSITIONALS = {"timeout": 1, "taskset": 1, "flock": 1, "chrt": 1}  # words a wrapper takes before the command it runs: the duration, the mask, the lock file, the priority
GIT_VALUE_OPTIONS = {"-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env", "--attr-source"}
COMMIT_LONG_VALUE = {
    "--message", "--file", "--reuse-message", "--reedit-message", "--template", "--author",
    "--date", "--cleanup", "--trailer", "--fixup", "--squash", "--pathspec-from-file",
}
COMMIT_SHORT_VALUE = "mFCct"
HEAD_MOVERS = {"merge", "rebase", "cherry-pick", "revert", "pull", "am", "reset"}  # what a commit of the current branch, or a move of its tip, does
REF_CHANGERS = ("checkout", "switch", "branch", "tag", "update-ref", "symbolic-ref", "worktree", "fetch")  # they make or point refs: what a later push names may not be what it names now
MAX_SOURCES = 100  # the refs one push may name: each is looked up
# Git's own commands: an alias never replaces one of these, so only another word can be an alias.
GIT_BUILTINS = frozenset(
    """add am annotate apply archive bisect blame branch bugreport bundle cat-file check-attr check-ignore check-mailmap
    check-ref-format checkout checkout-index cherry cherry-pick citool clean clone column commit commit-graph commit-tree config
    count-objects credential describe diagnose diff diff-files diff-index diff-tree difftool fast-export fast-import fetch
    fetch-pack filter-branch fmt-merge-msg for-each-ref for-each-repo format-patch fsck gc get-tar-commit-id grep gui hash-object
    help hook index-pack init interpret-trailers log ls-files ls-remote ls-tree mailinfo mailsplit maintenance merge merge-base
    merge-file merge-index merge-tree mergetool mktag mktree multi-pack-index mv name-rev notes pack-objects pack-redundant
    pack-refs patch-id prune prune-packed pull push range-diff read-tree rebase receive-pack reflog refs remote repack replace
    request-pull rerere reset restore rev-list rev-parse revert rm scalar send-email send-pack shortlog show show-branch
    show-index show-ref sparse-checkout stage stash status stripspace submodule switch symbolic-ref tag unpack-file
    unpack-objects update-index update-ref update-server-info upload-archive upload-pack var verify-commit verify-pack
    verify-tag version whatchanged worktree write-tree gitk""".split()
)

# The plumbline agents, and the review units' among them (which write under a review stage's round directories). The stage agents are the ones a
# pipeline's stages run; the orchestrator runs them for the main session, and is held to its own lane (what it reads, runs and launches).
STAGE_ROLES = ("planner", "test-writer", "builder", "verifier", "prosecutor", "defender", "detective", "canary")
ORCHESTRATOR = "orchestrator"
ROLES = (*STAGE_ROLES, ORCHESTRATOR)
REVIEW_ROLES = ("prosecutor", "defender", "detective", "canary")
# The files a review agent writes in a round directory, by role: the defender's are the panel's (defender-<n>.json) and a screening defender's (screen-<k>.json),
# the canary's are its record, named as a second prosecutor's of a lens (prosecutor-<lens>-b.json), and its key, and a name of that kind is the canary's alone.
REVIEW_FILES = {
    "prosecutor": r"prosecutor-(?![A-Za-z0-9._-]*-b\.json)[A-Za-z0-9._-]+\.json",
    "defender": r"(?:defender|screen)-[A-Za-z0-9._-]+\.json",
    "detective": r"detective\.json",
    "canary": r"(?:prosecutor-[A-Za-z]+-b|canary-key)\.json",
}
REVIEW_FILE_NAMES = {
    "prosecutor": ("prosecutor-<lens>.json",), "defender": ("defender-<n>.json", "screen-<k>.json"), "detective": ("detective.json",),
    "canary": ("prosecutor-<lens>-b.json", "canary-key.json"),
}
CANARY_KEY_FILE = "canary-key.json"  # in the round directory of a calibration run: what tells the canary's finding from the others, which the defenders do not read
LEDGER_NAME = "ledger.jsonl"  # a run's ledger names the agent that wrote each record, so it tells the canary's record too: the defenders do not read it either
# A command of the main session concerns plumbline besides git and gh only if it mentions one of these.
CARES = re.compile(r"plumbline|override|ledger")

# The command classes of a role policy (see [roles.*] in the pipeline), besides the repo's [commands].
CONFIG_COMMANDS = ("test", "lint", "typecheck", "build")
GIT_READ = ("diff", "show", "log", "status", "rev-parse", "merge-base", "ls-files", "grep", "blame")
GIT_QUIET_FLAGS = {"--no-pager", "-P", "--no-optional-locks", "--literal-pathspecs", "--no-replace-objects"}
# Long options that run a program or write a file. Git accepts any unambiguous prefix of a long option, so a prefix of one of these is one.
GIT_DANGEROUS_LONG = ("open-files-in-pager", "output", "ext-diff", "textconv", "exec", "upload-pack", "receive-pack")
GIT_VALUE_SHORTS = "SGLefxX"  # short options that take a string: the rest of a bundle (or the next word) is their value, not more options
SEARCH_TOOLS = ("grep", "egrep", "fgrep", "rg", "cat", "head", "tail", "wc", "ls", "sed", "find")
FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
RG_RUNS = ("--pre", "--hostname-bin")  # ripgrep options that run a program
SED_PRINT = re.compile(r"\s*(?:\d+|\$|/[^/]*/)?(?:,(?:\d+|\$|/[^/]*/))?\s*p\s*")  # `sed -n` prints lines: nothing else
NO_EFFECT = {"cd", "pushd", "popd", "pwd", "true", "false", ":", "echo", "printf"}  # allowed wherever any command is
WRITERS_ALL = {"tee", "rm", "unlink", "shred", "truncate", "touch", "mv"}  # every operand is written or removed
WRITERS_LAST = {"cp", "install", "ln", "rsync"}  # the destination is written
PASS_DIR = ".plumbline/pass"
RUNS_DIR = ".plumbline/runs"
STUBS_DIR = "stubs"  # .plumbline/runs/<run-id>/stubs/: where a role with the `stubs` write target puts the stubs of brand-new modules
ACTIVE_FILE = "ACTIVE"  # .plumbline/runs/ACTIVE holds the id of the run the agents work in
ACTIVE_REL = f"{RUNS_DIR}/{ACTIVE_FILE}".lower()

# Where no agent writes, whatever its role: git's own files, agent settings and instructions, repository automation.
LANE_DIRS = {".git": "git", ".claude": "settings", ".github": "automation", ".husky": "automation"}
LANE_FILES = {
    ".mcp.json": "settings", "claude.md": "settings", "claude.local.md": "settings", "agents.md": "settings",
    ".gitattributes": "automation", ".worktreeinclude": "automation", ".pre-commit-config.yaml": "automation",
}
TEST_CONFIG_FILES = ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "noxfile.py")  # how the tests run: not the builder's to change
# What a test run leaves behind, and shows: the builder does not read it.
TEST_OUTPUT = (
    "**/.pytest_cache/**", "**/junit*.xml", "**/.coverage*", "**/htmlcov/**", "**/.tox/**", "**/coverage.xml", "**/lcov.info", "**/.nyc_output/**", "**/.hypothesis/**",
    "**/test-results/**", "**/test-reports/**", "**/pytest-report*.xml", "**/pytest-report*.html",
)

TRANSCRIPTS = re.compile(r"(^|/)\.claude/projects(/|$)")  # where Claude Code keeps its transcripts, wherever the home directory is

# The per-call caches (see decide): None while nothing is being decided, so that helpers used on their own see the disk as it is.
_CACHE: dict | None = None


def _scrub(value):
    """The input without NUL characters: a shell drops them, and an operating system call refuses a path that holds one."""
    if isinstance(value, str):
        return value.replace("\0", "")
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _guarded(rule, *args):
    """Run one rule of the hook so that input it cannot handle (an odd path, a failing system call) does not take the other rules down
    with it: the rule then says nothing. PLUMBLINE_HOOK_DEBUG=1 lets the error through, for the tests."""
    try:
        return rule(*args)
    except Exception:
        if os.environ.get("PLUMBLINE_HOOK_DEBUG"):
            raise
        return None


def _isdir(path: Path) -> bool:
    try:
        return path.is_dir()
    except (OSError, ValueError):  # a name too long for the file system, say
        return False


def _isfile(path: Path) -> bool:
    try:
        return path.is_file()
    except (OSError, ValueError):
        return False


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except (OSError, ValueError):
        return False


def _cached(key, compute):
    if _CACHE is None:
        return compute()
    if key not in _CACHE:
        _CACHE[key] = compute()
    return _CACHE[key]


# ------------------------------------------------------- parsing a command


ANSI_ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _matching_paren(text: str, start: int) -> int:
    """The index of the `)` that closes the `(` at `start` (len(text) when none does). It knows quotes but not here-documents:
    it serves only to skip past what is nested too deeply to follow."""
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


def _ansi_c(text: str, i: int) -> tuple[str, int]:
    """The body of a $'...' string that starts at `i` (after the opening quote), with its escapes decoded, and the index after the
    closing quote. A backslash escapes a quote here, unlike in a plain single-quoted string."""
    out: list[str] = []
    n = len(text)
    while i < n and text[i] != "'":
        c = text[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        d = text[i + 1]
        if d in ANSI_ESCAPES:
            out.append(ANSI_ESCAPES[d])
            i += 2
            continue
        if d == "x":
            digits, skip = re.match(r"[0-9A-Fa-f]{0,2}", text[i + 2 : i + 4]).group(), 2
        elif d in "uU":
            limit = 4 if d == "u" else 8
            digits, skip = re.match(r"[0-9A-Fa-f]*", text[i + 2 : i + 2 + limit]).group(), 2
        elif d in "01234567":
            digits, skip = re.match(r"[0-7]{1,3}", text[i + 1 : i + 4]).group(), 1
        elif d == "c" and i + 2 < n:
            out.append(chr(ord(text[i + 2]) & 0x1F))
            i += 3
            continue
        else:
            out.append("\\" + d)
            i += 2
            continue
        if not digits:
            out.append("\\" + d)
            i += 2
            continue
        try:
            char = chr(int(digits, 8 if d in "01234567" else 16))
        except (ValueError, OverflowError):
            char = "?"
        out.append(char if char != "\0" else "")
        i += skip + len(digits)
    return "".join(out), i + 1


def _backtick(text: str, i: int) -> tuple[str, int]:
    """The command inside the backticks that open at `i` (its escaped backticks, backslashes and dollars unescaped), and the index
    after the closing backtick."""
    out: list[str] = []
    j, n = i + 1, len(text)
    while j < n and text[j] != "`":
        if text[j] == "\\" and j + 1 < n and text[j + 1] in "`\\$":
            out.append(text[j + 1])
            j += 2
        else:
            out.append(text[j])
            j += 1
    return "".join(out), j + 1


def _heredoc_word(text: str, i: int) -> tuple[str, bool, int]:
    """The delimiter word of a here-document, read at `i`, with its quoting removed; whether any of it was quoted (then the body is
    not expanded); and the index after it. `EOF`, `'EOF'`, `"EOF"`, `\\EOF` and `E"O"F` all name the delimiter EOF."""
    parts: list[str] = []
    quoted, n = False, len(text)
    while i < n and text[i] not in " \t\r\n;&|()<>":
        c = text[i]
        if c == "\\":
            quoted = True
            if i + 1 < n and text[i + 1] != "\n":
                parts.append(text[i + 1])
            i += 2
        elif c == "'":
            quoted = True
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            parts.append(text[i + 1 : j])
            i = j + 1
        elif c == '"':
            quoted = True
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n and text[j + 1] in '"\\$`':
                    j += 1
                parts.append(text[j])
                j += 1
            i = j + 1
        elif c == "$" and text[i + 1 : i + 2] == "'":
            quoted = True
            body, i = _ansi_c(text, i + 2)
            parts.append(body)
        else:
            parts.append(c)
            i += 1
    return "".join(parts), quoted, i


def _substitutions(body: str, depth: int) -> list[tuple[list[str], list[tuple[str, str]]]]:
    """The commands inside the $( ) and backticks of a here-document body that the shell expands (an unquoted delimiter)."""
    commands: list[tuple[list[str], list[tuple[str, str]]]] = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            i += 2
        elif c == "$" and body[i + 1 : i + 2] == "(":
            found, i = _parse_commands(body, i + 2, depth, True)
            commands.extend(found)
        elif c == "`":
            content, i = _backtick(body, i)
            commands.extend(split_commands_ex(content, depth))
        else:
            i += 1
    return commands


def split_commands(text: str, depth: int = 0) -> list[list[str]]:
    """The simple commands of a shell command line, each as a list of words:
    split at ; & | ( ) and newlines, quotes removed, redirections and here-document
    bodies dropped. The commands inside $( ) and backticks come out too."""
    return [words for words, _redirects in split_commands_ex(text, depth) if words]


def split_commands_ex(text: str, depth: int = 0) -> list[tuple[list[str], list[tuple[str, str]]]]:
    """Like split_commands, but each command comes with its redirections as (operator, target) pairs,
    in the order written, so that a caller can see what a command line writes to. A redirection with
    no command (`> file`) is a command with no words."""
    return _parse_commands(text, 0, depth, False)[0]


def _parse_commands(text: str, start: int, depth: int, substitution: bool):
    """Parse `text` from `start` into its simple commands. With `substitution` it stops after the `)` that closes a `$(`, which is
    found by parsing (a quote or a parenthesis in a here-document body inside it is not one), and returns the index after it."""
    commands: list[tuple[list[str], list[tuple[str, str]]]] = []
    n = len(text)
    if depth > 4:
        return commands, ((_matching_paren(text, start - 1) + 1) if substitution else n)
    words: list[str] = []
    redirects: list[tuple[str, str]] = []
    word: list[str] | None = None
    pending: str | None = None  # the operator whose target is the next word
    heredocs: list[tuple[str, bool, bool]] = []  # (delimiter, tabs stripped, expanded)
    parens = 0  # subshell parentheses open inside a substitution
    i = start

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

    def substitute(j: int) -> int:  # `$(` at j
        found, k = _parse_commands(text, j + 2, depth + 1, True)
        commands.extend(found)
        return k

    def backticks(j: int) -> int:  # a backtick at j
        content, k = _backtick(text, j)
        commands.extend(split_commands_ex(content, depth + 1))
        return k

    while i < n:
        c = text[i]
        if c in " \t\r":
            end_word()
            i += 1
        elif c == "\n":
            end_command()
            i += 1
            while heredocs:
                delimiter, strip_tabs, expands = heredocs.pop(0)
                body_start, body_end = i, n
                while i < n:
                    j = text.find("\n", i)
                    j = n if j < 0 else j
                    line = text[i:j]
                    if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                        body_end, i = i, j + 1
                        break
                    i = j + 1
                if expands:  # the shell expands an unquoted here-document: a $( ) or backticks in it run
                    commands.extend(_substitutions(text[body_start:body_end], depth + 1))
        elif c == "#" and word is None:
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in ";&|":
            end_command()
            i += 1
        elif c == "(":
            end_command()
            parens += 1 if substitution else 0
            i += 1
        elif c == ")":
            end_command()
            i += 1
            if substitution:
                if parens == 0:
                    return commands, i
                parens -= 1
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
                    i = substitute(i)
                    add("$(...)")
                elif d == "`":
                    i = backticks(i)
                    add("`...`")
                else:
                    add(d)
                    i += 1
            i += 1
        elif c == "$" and text[i + 1 : i + 2] == "(":
            i = substitute(i)
            add("$(...)")
        elif c == "$" and text[i + 1 : i + 2] == "'":
            body, i = _ansi_c(text, i + 2)
            add(body)
        elif c == "$" and text[i + 1 : i + 2] == '"':
            i += 1  # $"..." is a double-quoted string
        elif c == "`":
            i = backticks(i)
            add("`...`")
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
                delimiter, quoted, i = _heredoc_word(text, i)
                heredocs.append((delimiter, strip_tabs, not quoted))
            else:
                begin = i
                while i < n and (text[i] in "<>&" or (text[i] == "|" and text[i - 1] == ">")):  # `>|` overrides noclobber
                    i += 1
                pending = text[begin:i]
        else:
            add(c)
            i += 1
    end_command()
    return commands, min(i, n)


def _unwrap(argv: list[str]):
    """Take a command apart: (the command itself, the VAR=value words before it, the directories `env -C` moves to, and a command line
    that `env -S` holds). The command comes without leading shell keywords (then, do, ...), assignments and wrappers such as sudo or env."""
    argv = list(argv)
    assigns: list[str] = []
    chdirs: list[str] = []
    while argv:
        first = argv[0]
        if first == "function" and len(argv) > 1:
            argv = argv[2:]  # `function name {`
            continue
        if first in KEYWORDS:
            argv = argv[1:]
            continue
        if ASSIGNMENT.fullmatch(first):
            assigns.append(first)
            argv = argv[1:]
            continue
        name = os.path.basename(first)
        if name not in WRAPPERS:
            break
        if name == "coproc" and len(argv) >= 3 and argv[2] == "{":
            argv = argv[2:]  # `coproc NAME { command; }`: NAME is not the command
            continue
        with_value = WRAPPERS[name]
        i = 1
        while i < len(argv):
            word = argv[i]
            if name == "env" and (word in ("-S", "--split-string") or word.startswith("--split-string=") or (word.startswith("-S") and len(word) > 2)):
                # env -S 'command line' more words: the string is a command line, and the rest are its last words
                if word in ("-S", "--split-string"):
                    line, rest = (argv[i + 1] if i + 1 < len(argv) else ""), argv[i + 2 :]
                elif word.startswith("--split-string="):
                    line, rest = word.split("=", 1)[1], argv[i + 1 :]
                else:
                    line, rest = word[2:], argv[i + 1 :]
                return [], assigns, chdirs, " ".join([line, *(shlex.quote(w) for w in rest)])
            if name == "env" and word in ("-C", "--chdir") and i + 1 < len(argv):
                chdirs.append(argv[i + 1])
                i += 2
            elif name == "env" and word.startswith("--chdir="):
                chdirs.append(word.split("=", 1)[1])
                i += 1
            elif word in with_value and i + 1 < len(argv):
                i += 2
            elif word.startswith("-") or (name == "env" and ASSIGNMENT.fullmatch(word)):
                if name == "env" and ASSIGNMENT.fullmatch(word):
                    assigns.append(word)
                i += 1
            else:
                break
        i = min(len(argv), i + WRAPPER_POSITIONALS.get(name, 0))
        if name == "flock" and argv[i : i + 1] in (["-c"], ["--command"]) and i + 1 < len(argv):
            return [], assigns, chdirs, argv[i + 1]  # `flock FILE -c 'command line'`
        argv = argv[i:]
    return argv, assigns, chdirs, None


def strip_wrappers(argv: list[str]) -> list[str]:
    """The command itself, without leading shell keywords (then, do, ...), VAR=value words and wrappers such as sudo or env."""
    return _unwrap(argv)[0]


@dataclass
class Action:
    kind: str  # push, pr-create, commit, add, head-moves, or the name of a command that changes refs (checkout, switch, branch, tag, update-ref, symbolic-ref)
    cwd: Path  # where it runs when every earlier `cd` took effect
    args: list[str]
    dirs: tuple[Path, ...] = ()  # every directory it may run in (see walk): empty means `cwd` alone
    config: dict = field(default_factory=dict)  # the -c name=value pairs of the git command (a push reads push.default from them)
    assigns: list = field(default_factory=list)  # the VAR=value words in front of it (GIT_CONFIG_GLOBAL and the like)
    trees: list = field(default_factory=list)  # its --git-dir, --work-tree, GIT_DIR and GIT_WORK_TREE


def _program(argv: list[str]) -> str:
    """The name of the program a command runs: its file name, without a Windows `.exe`."""
    if not argv:
        return ""
    name = os.path.basename(argv[0])
    return name[:-4].lower() if name.lower().endswith(".exe") else name  # Windows does not tell GIT.EXE from git.exe


def _join(cwd: Path, target: str) -> Path:
    return Path(os.path.normpath(os.path.join(cwd, os.path.expanduser(target))))


@dataclass
class GitCall:
    """What a `git ...` command line says before its subcommand's own arguments."""

    sub: str | None
    args: list[str]
    index: int  # where the subcommand sits in the argv
    chdirs: list[str] = field(default_factory=list)  # the -C values, in order
    trees: list[str] = field(default_factory=list)  # --git-dir, --work-tree, GIT_DIR and GIT_WORK_TREE: other places the command may act
    config: dict = field(default_factory=dict)  # -c name=value and GIT_CONFIG_* pairs, names lower-cased
    config_env: list[str] = field(default_factory=list)  # names --config-env sets from an environment variable whose value is not known here


def _env_config(assigns: list[str]) -> dict:
    """The config pairs that GIT_CONFIG_COUNT, GIT_CONFIG_KEY_<n> and GIT_CONFIG_VALUE_<n> assignments set."""
    values = dict(a.split("=", 1) for a in assigns if "=" in a)
    found = {}
    try:
        count = int(values.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    for n in range(max(0, min(count, 50))):
        key = values.get(f"GIT_CONFIG_KEY_{n}")
        if key:
            found[key.lower()] = values.get(f"GIT_CONFIG_VALUE_{n}", "")
    try:  # GIT_CONFIG_PARAMETERS is what `git -c` itself passes on: 'name=value' 'name=value'
        for pair in shlex.split(values.get("GIT_CONFIG_PARAMETERS", "")):
            name, _, value = pair.partition("=")
            found[name.lower()] = value
    except ValueError:
        pass
    return found


def parse_git(argv: list[str], assigns: list[str] | tuple = ()) -> GitCall:
    call = GitCall(None, [], len(argv))
    call.config.update(_env_config(list(assigns)))
    for assignment in assigns:
        name, _, value = assignment.partition("=")
        if name in ("GIT_DIR", "GIT_WORK_TREE") and value:
            call.trees.append(value)
    i = 1
    while i < len(argv):
        word = argv[i]
        if word == "-C" and i + 1 < len(argv):
            call.chdirs.append(argv[i + 1])
            i += 2
        elif word == "-c" and i + 1 < len(argv):
            name, _, value = argv[i + 1].partition("=")
            call.config[name.lower()] = value
            i += 2
        elif word in ("--git-dir", "--work-tree") and i + 1 < len(argv):
            call.trees.append(argv[i + 1])
            i += 2
        elif word.startswith(("--git-dir=", "--work-tree=")):
            call.trees.append(word.split("=", 1)[1])
            i += 1
        elif word == "--config-env" and i + 1 < len(argv):
            call.config_env.append(argv[i + 1].partition("=")[0].lower())
            i += 2
        elif word.startswith("--config-env="):
            call.config_env.append(word[len("--config-env=") :].partition("=")[0].lower())
            i += 1
        elif word in GIT_VALUE_OPTIONS and i + 1 < len(argv):
            i += 2
        elif word.startswith("-"):
            i += 1
        else:
            break
    if i < len(argv):
        call.sub, call.args, call.index = argv[i], argv[i + 1 :], i
    return call


# `git push` takes its options like any git command: a long option may be shortened to any unambiguous prefix.
PUSH_OPTIONS = {
    "verbose": False, "quiet": False, "repo": True, "all": False, "branches": False, "mirror": False, "tags": False, "follow-tags": False,
    "atomic": False, "dry-run": False, "porcelain": False, "delete": False, "prune": False, "thin": False, "force": False,
    "force-with-lease": False, "force-if-includes": False, "receive-pack": True, "exec": True, "set-upstream": False, "push-option": True,
    "signed": False, "verify": False, "recurse-submodules": True, "progress": False, "ipv4": False, "ipv6": False, "help": False,
}
PUSH_OPTIONS.update({f"no-{name}": False for name in [n for n, takes in PUSH_OPTIONS.items() if not takes]})


@dataclass
class PushSpec:
    dry_run: bool = False
    help: bool = False
    delete: bool = False
    many: str | None = None  # the option (--all, --mirror, --tags, --branches) that publishes more than one ref
    repo_option: str | None = None  # --repo=<repository>: the repository when no word names it
    repository: str | None = None
    refspecs: list[str] = field(default_factory=list)


def _push_option(name: str) -> str | None:
    """The `git push` long option a (possibly shortened) name stands for, or None when it is unknown or ambiguous."""
    if name in PUSH_OPTIONS:
        return name
    found = [option for option in PUSH_OPTIONS if option.startswith(name)] if name else []
    return found[0] if len(found) == 1 else None


def parse_push(args: list[str]) -> PushSpec:
    """The arguments of `git push`: its flags, the repository, and the refspecs, with each option's value kept out of them."""
    spec = PushSpec()
    positional: list[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        i += 1
        if word == "--":
            positional.extend(args[i:])
            break
        if word.startswith("--"):
            name, has_value = word[2:].split("=", 1)[0], "=" in word
            option = _push_option(name)
            if option is None:
                continue
            if option == "repo":
                spec.repo_option = word.split("=", 1)[1] if has_value else (args[i] if i < len(args) else None)
            if PUSH_OPTIONS[option] and not has_value:
                i += 1  # its value is the next word
            off = option.startswith("no-")  # `--no-dry-run` takes back an earlier `--dry-run`
            base = option[3:] if off else option
            if base == "dry-run":
                spec.dry_run = not off
            elif base == "help":
                spec.help = not off
            elif base == "delete":
                spec.delete = not off
            elif base in ("all", "branches", "mirror", "tags"):
                if not off:
                    spec.many = f"--{base}"
                elif spec.many == f"--{base}":
                    spec.many = None
        elif word.startswith("-") and len(word) > 1:
            for position, letter in enumerate(word[1:], 1):
                if letter == "n":
                    spec.dry_run = True
                elif letter == "h":
                    spec.help = True
                elif letter == "d":
                    spec.delete = True
                elif letter == "o":
                    if position == len(word) - 1:
                        i += 1  # the push option is the next word
                    break
        else:
            positional.append(word)
    if positional:
        spec.repository, spec.refspecs = positional[0], positional[1:]
    return spec


def _reset_moves(args: list[str]) -> bool:
    """Does `git reset ...` move the branch HEAD is on? Not when it names paths (`git reset HEAD file`, `git reset -- file`), which only unstages them."""
    flags = [a for a in args if a.startswith("-") and a != "--"]
    words = [a for a in args if not a.startswith("-")]
    if any(f in ("--soft", "--hard", "--mixed", "--merge", "--keep") for f in flags):
        return True
    if "-p" in flags or "--patch" in flags:
        return False
    if "--" in args:
        return not args[args.index("--") + 1 :]
    return len(words) < 2


def git_action(argv: list[str], cwd: Path, call: GitCall | None = None) -> Action | None:
    call = call or parse_git(argv)
    directory = cwd
    for target in call.chdirs:
        directory = _join(directory, target)
    if call.sub is None:
        return None
    sub, args = call.sub, call.args
    if sub in ("push", "send-pack") or (sub == "subtree" and args[:1] == ["push"]):  # each of them publishes commits
        pushed = args[1:] if sub == "subtree" else args
        spec = parse_push(pushed)
        return None if spec.dry_run or spec.help else Action("push", directory, pushed, config=call.config)
    if sub == "commit":
        return Action("commit", directory, args)
    if sub in ("add", "stage"):
        return Action("add", directory, args)
    if sub == "reset" and not _reset_moves(args):
        return None
    if sub in HEAD_MOVERS or (sub == "stash" and (not args or args[0] in ("push", "save", "create", "store"))):
        return Action("head-moves", directory, args)
    if sub in REF_CHANGERS:
        return Action(sub, directory, args)
    if sub == "config":
        words = _config_words(args)
        if len(words) >= 2 and re.match(r"(push|remote|branch)\.", words[0], re.I):  # a setting a push reads
            return Action("config-push", directory, args)
    if sub == "remote" and args[:1] in (["add"], ["set-url"]) and any(a.startswith("--mirror") for a in args):  # a mirror remote pushes every ref
        return Action("config-push", directory, args)
    return None


# gh options whose value is the next word (in `gh pr create`): a value such as `--dry-run` is text, not the flag.
GH_VALUE_OPTIONS = {
    "-a", "--assignee", "-B", "--base", "-b", "--body", "-F", "--body-file", "-H", "--head", "-l", "--label", "-m", "--milestone",
    "-p", "--project", "-r", "--reviewer", "-T", "--template", "-t", "--title", "-R", "--repo", "--recover",
}


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
    if positionals not in (["pr", "create"], ["pr", "new"]):  # `new` is gh's other name for `create`
        return None
    rest = argv[i:]
    j = 0
    while j < len(rest):
        word = rest[j]
        if word in ("--dry-run", "--help", "-h"):
            return None
        j += 2 if word in GH_VALUE_OPTIONS else 1
    return Action("pr-create", cwd, [])


def _closure(seed: Path, targets: list[str]) -> tuple[Path, ...]:
    """The directories a command line may run in: where it starts, and where each cd, pushd or `env -C` target leads from any of them.
    Whether a cd takes effect (an earlier `&&` or `||` may stop it, a subshell or a popd may undo it) is not followed: every place counts."""
    seen = [seed]
    for target in targets:
        for base in list(seen):
            new = _join(base, target)
            if new not in seen and len(seen) < MAX_DIRS:
                seen.append(new)
    return tuple(seen)


def _git_config(directory: Path, key: str, assigns=(), config: dict | None = None, git_dir: Path | None = None, all_values: bool = False):
    """`git config --get key` (or --get-all), run in `directory` with what the command itself gives git: its -c pairs, its --git-dir, and the
    GIT_CONFIG_GLOBAL and GIT_CONFIG_SYSTEM it sets. A single value (or None); with `all_values`, a list."""
    env = dict(os.environ)
    for assignment in assigns:
        name, _, value = assignment.partition("=")
        if name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"):
            env[name] = value
    command = ["git", "--no-optional-locks"]
    for name, value in (config or {}).items():
        command += ["-c", f"{name}={value}"]
    if git_dir is not None:
        command += ["--git-dir", str(git_dir)]
    command += ["-C", str(directory), "config", "--get-all" if all_values else "--get", key]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return [] if all_values else None
    if all_values:
        return result.stdout.splitlines() if result.returncode == 0 else []
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


def _places_of(directory: Path, trees: list[str]) -> list[tuple[Path, Path | None]]:
    """(directory, git directory) pairs a git command's settings are read from: where it runs, and each --git-dir or --work-tree it names."""
    places: list[tuple[Path, Path | None]] = [(directory, None)]
    for tree in trees:
        found = _join(directory, tree)
        places.append((directory, found) if _isfile(found / "config") or found.name == ".git" else (found if _isdir(found) else directory, None))
    return places


def _alias_line(step: "Step", cwd: Path, defined: dict, trees: list[str] = ()) -> tuple[str | None, bool]:
    """When a `git ...` step names an alias (from -c, GIT_CONFIG_*, --config-env, an earlier `git config alias.x ...` of the same line, or the
    repository's and the user's git config), the command line it stands for, and whether that is a shell alias (`!...`)."""
    call = parse_git(step.argv, step.assigns)
    if call.sub is None or call.sub in GIT_BUILTINS:
        return None, False  # an alias never hides one of git's own commands
    key = f"alias.{call.sub}".lower()
    value = call.config.get(key, defined.get(key))
    if value is None and key in call.config_env:
        value = "!git push"  # the value comes from an environment variable that cannot be read here: take the worst
    if value is None:
        directory = cwd
        for target in call.chdirs:
            directory = _join(directory, target)
        for where, git_dir in _places_of(directory, [*call.trees, *trees]):
            value = _git_config(where, key, step.assigns, call.config, git_dir)
            if value is not None:
                break
    if value is None:
        return None, False
    if value.startswith("!"):
        quoted = [shlex.quote(word) for word in call.args]
        text = re.sub(r'"\$[@*]"|\$[@*]', lambda _m: " ".join(quoted), value[1:])  # git hands the arguments on as "$@": the command inside sees them
        text = re.sub(r'"?\$([1-9])"?', lambda m: quoted[int(m.group(1)) - 1] if int(m.group(1)) <= len(quoted) else "", text)
        return text + "".join(" " + word for word in quoted), True
    words = (split_commands(value) or [[]])[0]
    return " ".join(shlex.quote(word) for word in [*step.argv[: call.index], *words, *call.args]), False


def _config_words(args: list[str]) -> list[str]:
    """The key and value words of `git config ...`, without the options and their values (`-f FILE`, `--file FILE`)."""
    return _split_args(args, value_options=("--file", "--blob", "--default", "--type", "--comment"), short_values="f")[1]


def _defined_alias(call: GitCall) -> tuple[str, str] | None:
    """The (key, value) of `git config [--global] [--add] alias.x value`, which defines an alias that a later command of the line may use."""
    if call.sub != "config":
        return None
    words = _config_words(call.args)
    if len(words) >= 2 and words[0].lower().startswith("alias."):
        return words[0].lower(), words[1]
    return None


def analyze(text: str, cwd: Path, depth: int = 0) -> list[Action]:
    """The actions plumbline cares about in a command line, in order, each with the directory it runs in (`cd` and `git -C` are
    followed) and every directory it may run in."""
    return actions_of(walk(text, cwd))


def actions_of(steps: list["Step"]) -> list[Action]:
    actions: list[Action] = []
    for step in steps:
        name = _program(step.argv)
        argv = step.argv
        if name.startswith("git-") and name[4:] in GIT_BUILTINS:  # the dashed form: `git-push` is `git push`
            name, argv = "git", ["git", name[4:], *step.argv[1:]]
        if name == "git":
            call = parse_git(argv, step.assigns)
            action = git_action(argv, step.cwd, call)
            if action:
                action.assigns, action.trees = list(step.assigns), list(call.trees)
                places = []
                for base in step.dirs or (step.cwd,):
                    directory = base
                    for target in call.chdirs:
                        directory = _join(directory, target)
                    places.append(directory)
                    places.extend(_join(directory, tree) for tree in call.trees)
                action.dirs = tuple(dict.fromkeys(places))
                actions.append(action)
        elif name == "gh":
            action = gh_action(argv, step.cwd)
            if action:
                action.dirs = step.dirs or (step.cwd,)
                actions.append(action)
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


def _existing(directory: Path) -> Path | None:
    """`directory`, or its nearest ancestor that exists (a `cd` into a missing directory fails, and the command runs where the shell was)."""
    while not _isdir(directory) and directory != directory.parent:
        directory = directory.parent
    return directory if _isdir(directory) else None


def _adopted_above(directory: Path) -> Path | None:
    """The nearest directory above `directory` (itself included) that holds a plumbline.toml next to a .git: how a repository is found
    when git will not say (a working directory inside .git, or a repository owned by someone else)."""
    real = Path(os.path.realpath(directory))
    for candidate in (real, *real.parents):
        if _isfile(candidate / "plumbline.toml") and _exists(candidate / ".git"):
            return candidate
    return None


def adopted_root(pl, directory: Path) -> Path | None:
    """The top level of the repository `directory` is in, when it has adopted plumbline. A directory that does not
    exist (a `cd` into it fails, and the command runs where the shell was) is looked up from the nearest one that does. Where git
    cannot say (a working directory inside .git, or a repository it does not trust), a plumbline.toml next to a .git above it counts."""

    def compute():
        start = _existing(directory)
        if start is None:
            return None
        root = pl.git_toplevel(start, timeout=5)
        if root is None:
            root = _adopted_above(start)
        return root if root is not None and _isfile(root / pl.CONFIG_FILE) else None

    return _cached(("root", str(directory)), compute)


def anchors_of(cwd: Path, *paths: Path) -> list[Path]:
    """The places a call is looked up from: the working directory, the paths the call names, and the session's project directory."""
    found = [cwd, *paths]
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if project:
        found.append(Path(project))
    return list(dict.fromkeys(found))[:MAX_ANCHORS]


def adopted_roots(pl, places: list[Path]) -> list[Path]:
    """The distinct repositories, of those the places lie in, that have adopted plumbline."""
    roots: list[Path] = []
    for place in places:
        root = _guarded(adopted_root, pl, place)  # a place git or the file system will not describe says nothing about the others
        if root is not None and root not in roots:
            roots.append(root)
    return roots


def _git(pl, root: Path, *args: str):
    return pl._git(root, "--no-optional-locks", *args, timeout=GIT_TIMEOUT)  # a look at the repository must not refresh its index


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
    the whole change adds: everything from that commit to the working tree, .plumbline/ left out. `pl` needs to offer
    only `_git`: `check-diff` and the hook's check of a `git commit` pass plumbline.OnIndexCopy, whose `_git` works on a
    copy of the index, because `git diff` rewrites the index it reads."""
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
            try:
                if path.is_symlink():
                    problems.append(f"adds a symlink: {name}")
                elif _isfile(path) and path.stat().st_size <= MAX_SCANNED_BYTES:
                    data = path.read_bytes()
                    if b"\0" in data[:8192]:
                        continue
                    for number, text in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                        if HOME_PATH.search(text):
                            problems.append(f"adds an absolute home path: {name}:{number}")
                        if SECRET.search(text):
                            problems.append(f"adds a key-shaped secret: {name}:{number} (the value is not shown)")
            except OSError:
                continue  # a file that cannot be read is not one `git add` can take either
    return list(dict.fromkeys(problems))


def _uncovered_message(what: str, label: str, sha: str, detail: str, action: Action) -> str:
    opened = "opened for review" if action.kind == "pr-create" else "pushed"
    why = "" if detail == NO_RECORD else f" ({detail})"  # the message opens with those words: a detail that only repeats them is left out
    if label == "HEAD":
        return (
            f"plumbline: HEAD {sha[:7]} has {NO_RECORD}, so it is not {opened}{why}. "
            f"Run the pipeline (/plumbline:run), commit, and record the pass; or, if the builder decides to skip the pipeline, /plumbline:override. "
            f"Then {what} again."
        )
    return (
        f"plumbline: {label} ({sha[:7]}) has {NO_RECORD}, so it is not {opened}{why}. "
        f"Check {label} out and run the pipeline there (/plumbline:run), commit, and record the pass; or, if the builder decides to skip the pipeline, /plumbline:override. "
        f"Then {what} again."
    )


def _many_message(why: str) -> str:
    return (
        f"plumbline: {why} publishes several refs at once, and each commit needs its own pass or override record; "
        "push one reviewed branch at a time (git push <remote> <branch>)."
    )


def _changes_message(what: str) -> str:
    return (
        f"plumbline: this command changes HEAD or a ref and then tries to {what}, so the commit it would publish cannot have a pass record yet. "
        "Run the pipeline and commit first, record the pass with `plumbline.py pass <run>`, then push in a separate command."
    )


def _commit_of(pl, root: Path, name: str) -> str | None:
    """The commit a push source names (a branch, a tag, HEAD, a sha, an expression such as main~2), or None when it names none."""
    if not name or name.startswith("-"):
        return None
    result = _git(pl, root, "rev-parse", "--verify", "--quiet", name if name.startswith(":/") else f"{name}^{{commit}}")  # `:/text` takes all it is given as the text
    sha = result.stdout.decode().strip()
    return sha if result.returncode == 0 and sha else None


def _refspec_sources(refspecs: list[str]) -> tuple[list[str], bool]:
    """(sources, many): the sources of the refs a push updates, as the words written, and whether a refspec names many refs at once (a wildcard,
    or a bare `:`, which means the branches that exist on both sides). A deletion (`:dst`) has no source."""
    sources: list[str] = []
    i = 0
    while i < len(refspecs):
        refspec = refspecs[i]
        i += 1
        if refspec == "tag" and i < len(refspecs):
            sources.append(f"refs/tags/{refspecs[i]}")  # `tag <name>` is refs/tags/<name>
            i += 1
            continue
        refspec = refspec.lstrip("+")
        if refspec.startswith("^"):
            continue  # a negative refspec excludes refs
        if refspec == ":":
            return sources, True
        source = refspec.rpartition(":")[0] if ":" in refspec else refspec  # git parts source and destination at the last colon: `:/text` is a source
        if not source:
            continue  # `:dst` deletes dst
        if any(ch in source for ch in "*?["):
            return sources, True
        sources.append(source)
    return sources, False


@dataclass
class RefState:
    """What the earlier commands of a command line did to the refs a later push may name: a commit moved the tips (`tips_moved`), HEAD points
    somewhere else (`head`: an expression that names its commit, None when that cannot be told), a branch or tag was made (`created`, name to
    the expression that names the commit it points to), or git's push settings were changed (`config_changed`)."""

    tips_moved: bool = False
    head: str | None = "HEAD"
    created: dict = field(default_factory=dict)
    config_changed: bool = False
    new_dirs: list = field(default_factory=list)  # working trees the line makes (git worktree add): they do not exist yet, and are the repository's
    new_root: Path | None = None


def _split_args(args: list[str], value_options=(), short_values: str = "") -> tuple[list[str], list[str]]:
    """The options and the other words of a command's arguments; an option in `value_options`, or a short bundle ending in a letter of
    `short_values`, takes the next word as its value. Everything after `--` is a word."""
    flags: list[str] = []
    words: list[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        i += 1
        if word == "--":
            flags.append("--")
            words.extend(args[i:])
            break
        if word.startswith("-") and len(word) > 1:
            flags.append(word)
            if word in value_options or (not word.startswith("--") and word[-1] in short_values and "=" not in word):
                i += 1
        else:
            words.append(word)
    return flags, words


def _create(state: RefState, name: str, expr: str | None, kind: str) -> None:
    keys = {name}
    if name.startswith("refs/heads/"):
        keys.add(name[len("refs/heads/") :])
    elif name.startswith("refs/tags/"):
        keys.add(name[len("refs/tags/") :])
    elif kind == "heads":
        keys.add(f"refs/heads/{name}")
    elif kind == "tags":
        keys.add(f"refs/tags/{name}")
    for key in keys:
        state.created[key] = expr


def _expr(state: RefState, word: str | None) -> str | None:
    """The expression that names the commit `word` names after the earlier commands of the line: a ref they made, or the word itself."""
    if word is None or word in ("HEAD", "@"):
        return state.head
    stripped = word[len("refs/") :] if word.startswith("refs/") else word
    spellings = [word, stripped, f"refs/heads/{word}", f"refs/tags/{word}"]
    spellings += [stripped[len(prefix) :] for prefix in ("heads/", "tags/") if stripped.startswith(prefix)]  # heads/x, tags/x
    for spelling in spellings:
        if spelling in state.created:
            return state.created[spelling]
    return word


PLAIN_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]*")  # a ref written as its plain name: no ^, ~, @{ }, : or spaces
SWITCH_FLAGS = {
    "-f", "--force", "-q", "--quiet", "--detach", "-d", "-m", "--merge", "--no-guess", "--guess", "--no-track", "-t", "--track", "--progress", "--no-progress",
    "--ignore-other-worktrees", "--recurse-submodules", "--no-recurse-submodules", "--overwrite-ignore", "--no-overwrite-ignore",
}
BRANCH_READS = {
    "-l", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--show-current", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at",
    "--sort", "--format", "--column", "--no-column", "-i", "--ignore-case", "-d", "-D", "--delete", "-u", "--set-upstream-to", "--unset-upstream",
}
TAG_READS = {"-l", "--list", "-d", "--delete", "-v", "--verify", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at"}


def _ref_effects(pl, root: Path, action: Action, state: RefState) -> None:
    """What a command that makes or points refs does to what a later push of the same line names, as far as its arguments say. What it cannot
    tell, it marks as not known (`head` None, or a created ref with no expression): a push that depends on that is held back."""
    kind, args = action.kind, action.args
    if kind in ("checkout", "switch"):
        create_flags = ("-b", "-B") if kind == "checkout" else ("-c", "-C", "--create", "--force-create")
        create, words, unknown, pathspec, i = None, [], False, False, 0
        while i < len(args):
            word = args[i]
            i += 1
            if word == "--":
                pathspec = kind == "checkout" and i < len(args)  # `git checkout other --` switches; with paths after it restores them. For `switch`, `--` only ends the options
                if kind == "switch":
                    continue
                break
            if word in create_flags:
                create = args[i] if i < len(args) else None
                i += 1
            elif kind == "checkout" and word[:2] in ("-b", "-B") and len(word) > 2:
                create = word[2:]
            elif word in SWITCH_FLAGS:
                continue
            elif word.startswith("-"):
                unknown = True  # `-` (the previous branch), --orphan, --patch ...
            else:
                words.append(word)
        if unknown:
            state.head = None
        elif create:
            start = words[0] if words else None
            expr = _expr(state, start)
            _create(state, create, expr, "heads")
            if start:
                state.head = expr
        elif not pathspec and len(words) == 1:
            expr = _expr(state, words[0])
            if expr is not None and _commit_of(pl, root, expr) is not None:
                state.head = expr
            elif not _exists(action.cwd / words[0]):
                state.head = None  # not a file either: a branch git makes from a remote one, or something it will refuse
    elif kind == "branch":
        flags, words = _split_args(args, value_options=("-u", "--set-upstream-to", "--sort", "--format", "--contains", "--merged", "--no-merged", "--points-at"), short_values="u")
        if any(f in BRANCH_READS for f in flags):
            return
        if any(f in ("-m", "-M", "--move", "-c", "-C", "--copy") for f in flags):
            old, new = (words[0], words[1]) if len(words) >= 2 else (None, words[0] if words else None)
            if new:
                _create(state, new, _expr(state, old), "heads")
        elif words:
            _create(state, words[0], _expr(state, words[1] if len(words) > 1 else None), "heads")
    elif kind == "tag":
        flags, words = _split_args(
            args, value_options=("--message", "--file", "--local-user", "--cleanup", "--sort", "--format", "--column"), short_values="mFu"
        )
        if any(f in TAG_READS for f in flags):
            return
        if words:
            _create(state, words[0], _expr(state, words[1] if len(words) > 1 else None), "tags")
    elif kind == "update-ref":
        flags, words = _split_args(args, value_options=(), short_values="m")
        if "--stdin" in flags or (words and words[0] in ("HEAD", "@")):  # `update-ref HEAD x` moves the branch HEAD is on
            state.tips_moved = True
        elif "-d" not in flags and len(words) >= 2:
            _create(state, words[0], _expr(state, words[1]), "auto")
    elif kind == "worktree":
        if args[:1] == ["add"]:
            state.tips_moved = True  # a new working tree is a place where HEAD is somewhere else, and the branch it makes may be anywhere
            _flags, words = _split_args(args[1:], value_options=("-b", "-B", "--reason"), short_values="bB")
            if words:
                state.new_dirs.append(_join(action.cwd, words[0]))
                state.new_root = root
    elif kind == "fetch":
        _flags, words = _split_args(args, value_options=("--depth", "--upload-pack", "--negotiation-tip", "--refmap", "-j", "--jobs", "--deepen", "--shallow-since", "--shallow-exclude"))
        for refspec in words[1:]:  # the first word is the repository; a refspec with a local destination makes or moves a ref there
            destination = refspec.rpartition(":")[2].lstrip("+") if ":" in refspec else ""
            if destination and not destination.startswith("refs/remotes/"):
                _create(state, destination, None, "heads")
    elif kind == "symbolic-ref":
        flags, words = _split_args(args, value_options=(), short_values="m")
        if not any(f in ("-d", "--delete", "--short", "-q", "--quiet") for f in flags) and len(words) >= 2 and words[0] == "HEAD":
            state.head = _expr(state, words[1])


class TookTooLong(BaseException):
    """Not an Exception, and no OSError: the code that reads a record must not catch the alarm for a failed read."""


def _within(seconds: int, run):
    """Run `run()`, and raise TookTooLong when it takes longer (where SIGALRM exists): a pass record that is a named pipe blocks a read for good."""
    import signal

    if not hasattr(signal, "SIGALRM"):
        return run()

    def on_alarm(signum, frame):
        raise TookTooLong

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return run()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _configured_push(pl, root: Path, spec: PushSpec, action: Action) -> tuple[str | None, list[str]]:
    """What git's own settings make a push with no refspec publish: (a reason it publishes many refs, or None; the refspecs the remote is
    configured to push). The settings are read as the command itself would see them: its -c pairs, its --git-dir, the config files
    its environment names."""
    git_dir = next((found for _where, found in _places_of(root, action.trees) if found is not None), None)

    def value(key: str) -> str | None:
        return _git_config(root, key, action.assigns, action.config, git_dir)

    if (value("push.default") or "").lower() == "matching":
        return "git's push.default=matching", []
    remote = spec.repository or spec.repo_option
    if remote is None:
        branch = _git(pl, root, "symbolic-ref", "--short", "-q", "HEAD").stdout.decode("utf-8", "replace").strip()
        remote = (
            (branch and value(f"branch.{branch}.pushRemote")) or value("remote.pushDefault") or (branch and value(f"branch.{branch}.remote")) or "origin"
        )
    if "/" in remote or ":" in remote:
        return None, []  # a URL or a path: no remote of that name holds settings
    if (value(f"remote.{remote}.mirror") or "").lower() in ("true", "yes", "on", "1"):
        return f"the mirror remote {remote}", []
    configured = _git_config(root, f"remote.{remote}.push", action.assigns, action.config, git_dir, all_values=True)
    return None, [line for line in configured if line.strip()]


def push_reason(pl, root: Path, action: Action, state: RefState) -> str | None:
    what = "open a pull request" if action.kind == "pr-create" else "push"
    sources = ["HEAD"]
    if action.kind == "push":
        spec = parse_push(action.args)
        if spec.many:
            return _many_message(f"`git push {spec.many}`")
        if spec.delete and spec.refspecs:
            return None  # every named ref is deleted: no commit is published
        refspecs = list(spec.refspecs)
        if not refspecs:  # what a push with no refspec publishes is what the git settings say: the current branch, unless they say more
            why, refspecs = _configured_push(pl, root, spec, action)
            if why:
                return _many_message(why)
        found, many = _refspec_sources(refspecs)
        if many:
            return _many_message(
                "a refspec with a wildcard, or a bare `:`," if spec.refspecs else "the push refspec configured for this remote (a wildcard, or a bare `:`)"
            )
        if refspecs and not found:
            return None  # only deletions
        sources = found or ["HEAD"]
    if state.config_changed:
        return (
            f"plumbline: this command changes git's push settings (an alias, push.default, a remote's push) and then tries to {what}, so what the push "
            "publishes cannot be read from them yet. Change the settings in one command and push in another."
        )
    if state.tips_moved:
        return _changes_message(what)
    result = _git(pl, root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    head = result.stdout.decode().strip()
    if result.returncode > 1:  # git will not work in this repository from here: ownership, safe.directory. The push must not pass unseen.
        why = (result.stderr.decode("utf-8", "replace").strip().splitlines() or ["git failed"])[0][:160]
        return (
            f"plumbline: git cannot read {root} from here ({why}), so HEAD's pass record cannot be checked. "
            f"Let git use the repository (the ownership, or safe.directory in the git config), then {what} again."
        )
    if result.returncode != 0 or not head:
        return None  # nothing to gate: git itself has nothing to push
    project = _cached(("project", str(root)), lambda: pl.load_project(root))
    if len(sources) > MAX_SOURCES:
        return _many_message(f"a push that names {len(sources)} refs")
    moved = bool(state.created) or state.head != "HEAD"
    checked: list[tuple[str, str]] = []
    for source in sources:
        if moved and source not in ("HEAD", "@") and not PLAIN_REF.fullmatch(source):
            return _changes_message(what)  # an expression such as pub^{} or HEAD~1 cannot be worked out from what the line has already changed
        expr = _expr(state, source)
        if expr is None:
            return _changes_message(what)  # HEAD was pointed somewhere that cannot be told from here
        sha = head if expr in ("HEAD", "@") else _commit_of(pl, root, expr)
        label = source
        if sha is None and not expr.startswith("-") and _git(pl, root, "rev-parse", "--verify", "--quiet", expr).returncode == 0:
            return (
                f"plumbline: {source} names something that is not a commit (a tree, a blob), and only reviewed commits are pushed; "
                "push one reviewed branch at a time (git push <remote> <branch>)."
            )
        if sha is None:  # it names no commit that can be seen here (a variable, or a ref that is not there): HEAD stands in
            if state.head is None:
                return _changes_message(what)
            sha = head if state.head in ("HEAD", "@") else _commit_of(pl, root, state.head)
            label = "HEAD"
        if sha is not None and sha not in [found for _, found in checked]:
            checked.append((label, sha))
    for label, sha in checked:
        try:
            how, detail = _within(PASS_READ_SECONDS, lambda: pl.coverage(root, sha, project))
        except TookTooLong:
            return f"plumbline: the pass records of {root} did not answer in time (is one of .plumbline/pass/ or the run's files a named pipe?), so the {what} is held back."
        if not how:
            return _uncovered_message(what, "HEAD" if sha == head else label, sha, detail, action)
    return None


def commit_reason(pl, root: Path, action: Action, added: bool) -> str | None:
    scope = commit_scope(action.args, added)
    with contextlib.ExitStack() as stack:
        try:
            # `git diff` refreshes and rewrites the index it reads, whatever --no-optional-locks says: on a copy, the real one stays as it was
            index = stack.enter_context(pl.index_copy(root))
        except (OSError, pl.PlumblineError):
            checked = pl  # no copy could be made: the check still runs, on the index itself, as it always did
        else:
            checked = pl.OnIndexCopy(index)
        problems = commit_problems(checked, root, scope)
    if not problems:
        return None
    listed = "; ".join(problems[:MAX_LISTED]) + (f"; and {len(problems) - MAX_LISTED} more" if len(problems) > MAX_LISTED else "")
    return f"plumbline: this commit is held back. It {listed}. Remove them, stage the change again, and commit again."


def _push_check(pl, root: Path, action: Action, state: RefState) -> str | None:
    """The push gate is the one rule that must not fail open: what it cannot check, it holds back."""
    try:
        return push_reason(pl, root, action, state)
    except Exception as error:
        if os.environ.get("PLUMBLINE_HOOK_DEBUG"):
            raise
        return (
            f"plumbline: the pass records of {root} could not be checked ({type(error).__name__}), so the push is held back. "
            "Fix what the error names, or /plumbline:override if the builder decides to skip the pipeline, then push again."
        )


def gate_reason(pl, command: str, cwd: Path, steps: list["Step"] | None = None) -> str | None:
    """The push gate and the commit checks. Each action is checked in every directory it may run in that has adopted plumbline."""
    added = False
    state = RefState()
    for action in actions_of(steps if steps is not None else walk(command, cwd)):
        places = list(action.dirs or (action.cwd,))
        roots = adopted_roots(pl, places)
        if not roots and state.new_root is not None and any(_inside(place, new) for place in places for new in state.new_dirs):
            roots = [state.new_root]  # it does not exist yet: it is a working tree of the repository that makes it
        for root in roots:
            if action.kind == "add":
                added = True
            elif action.kind == "head-moves":
                state.tips_moved = True
            elif action.kind == "config-push":
                state.config_changed = True
            elif action.kind == "commit":
                reason = _guarded(commit_reason, pl, root, action, added)
                if reason:
                    return reason
                state.tips_moved = True
            elif action.kind in REF_CHANGERS:
                try:
                    _ref_effects(pl, root, action, state)
                except Exception:
                    if os.environ.get("PLUMBLINE_HOOK_DEBUG"):
                        raise
                    state.tips_moved = True  # what it did cannot be told: a push after it is held back
            else:
                reason = _push_check(pl, root, action, state)
                if reason:
                    return reason
    return None


def concerns_plumbline(command: str, cwd: str, steps: list["Step"]) -> bool:
    """Could a command line of the main session concern plumbline: does it run git or gh, or mention plumbline, an override or a ledger?"""
    if ".plumbline" in cwd or CARES.search(command):
        return True
    words = " ".join(word for step in steps for word in step.raw)
    return bool(CARES.search(words)) or any(_program(step.argv) in ("git", "gh") or _program(step.argv).startswith("git-") for step in steps)


def bash_reason(data: dict) -> str | None:
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    cwd = data.get("cwd")
    if not isinstance(command, str) or not isinstance(cwd, str):
        return None
    role = agent_role(data)
    steps = walk(command, Path(cwd))  # by what the shell will run, not by the letters typed: g""it is git
    if role is None and not concerns_plumbline(command, cwd, steps):
        return None  # nothing here concerns plumbline
    import plumbline as pl

    rules = [(override_reason, pl, steps)]
    if role is not None:
        rules += [(role_command_reason, pl, role, steps, Path(cwd)), (canary_command_reason, pl, role, steps, Path(cwd))]
    rules += [(protected_command_reason, pl, steps), (gate_reason, pl, command, Path(cwd), steps)]
    for rule in rules:
        reason = _guarded(*rule)
        if reason:
            return reason
    return None


# ----------------------------------------------------------- the active run


def active_run(pl, root: Path) -> str | None:
    """The run the agents work in: the one `.plumbline/runs/ACTIVE` names, when that run exists; otherwise (the file is missing, or names
    a run that is gone) the newest run. None when there is no run at all."""

    def compute():
        try:
            text = (root / RUNS_DIR / ACTIVE_FILE).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        name = text.strip()
        if name and pl.RUN_ID_PATTERN.fullmatch(name) and _isdir(root / RUNS_DIR / name):
            return name
        return pl.latest_run_id(root)

    return _cached(("active", str(root)), compute)


def current_round(pl, root: Path, run_id: str, stage_id: str) -> int:
    """The round a review stage is in: the highest round-<n> directory it has, or 1 while it has none."""
    rounds = [1]
    try:
        for entry in (root / RUNS_DIR / run_id / stage_id).iterdir():
            match = re.fullmatch(r"round-([1-9][0-9]*)", entry.name)
            if match and _isdir(entry):
                rounds.append(int(match.group(1)))
    except OSError:
        pass
    return max(rounds)


# -------------------------------------------------- the builder's blindness


def _pipeline_of(pl, root: Path) -> dict:
    project = _cached(("project", str(root)), lambda: pl.load_project(root))
    return project.pipeline or pl.load_toml(pl.PIPELINE_DIR / f"{pl.DEFAULT_PIPELINE}.toml")


def tests_of(pl, root: Path) -> tuple[list[str], set[str]]:
    """The tests type's path patterns, and the files the tests records of every run list, those records included: they name
    the tests and what each one checks."""
    pipeline = _pipeline_of(pl, root)
    patterns = next((t["paths"] for t in pipeline.get("type", []) if t.get("id") == "tests"), [])
    listed: set[str] = set()
    stage_ids = [s["id"] for s in pipeline.get("stage", []) if s.get("record") == "tests_record"] or ["tests"]
    try:
        run_dirs = sorted(d for d in (root / pl.RUNS_DIR).iterdir() if _isdir(d) and pl.RUN_ID_PATTERN.fullmatch(d.name))
    except OSError:
        run_dirs = []
    for run_dir in run_dirs:
        for stage_id in stage_ids:
            record_file = run_dir / f"{stage_id}.json"
            listed.add(pl.rel_path(root, record_file))
            data, _problem = pl.load_json_file(record_file)
            if isinstance(data, dict):
                names = [t.get("file") for t in data.get("tests", []) if isinstance(t, dict)] + list(data.get("files_written", []))
                for name in names:
                    if isinstance(name, str) and name:
                        path = Path(name)
                        listed.add(pl.rel_path(root, path) if path.is_absolute() else Path(os.path.normpath(name)).as_posix())
    return list(patterns), listed


@dataclass
class View:
    """What the builder does not see: the tests, the records of a run but three, the leftovers of test runs, the agent transcripts."""

    root: Path
    patterns: list[str]
    listed: set[str]
    allowed: set[str]  # the .plumbline files the builder reads: the active run's intake and plan, and its own record
    config_dirs: list[Path]  # where Claude Code keeps its transcripts


def _claude_dirs(data: dict) -> list[Path]:
    """The directories that hold the agent transcripts: ~/.claude, $CLAUDE_CONFIG_DIR, and what the event's transcript_path leads to."""
    found = [Path(os.path.realpath(os.path.expanduser("~/.claude")))]
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        found.append(Path(os.path.realpath(os.path.expanduser(configured))))
    transcript = data.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        parts = Path(os.path.realpath(transcript)).parts
        if "projects" in parts:
            index = len(parts) - 1 - parts[::-1].index("projects")
            if index > 1:
                found.append(Path(*parts[:index]))
    return list(dict.fromkeys(found))


def builder_view(pl, root: Path, data: dict) -> View:
    pipeline = _pipeline_of(pl, root)
    patterns, listed = tests_of(pl, root)
    allowed: set[str] = set()
    run = active_run(pl, root)
    if run is not None:
        stages = pipeline.get("stage", [])
        names = [s["id"] for s in stages if s.get("record") in ("change_class", "spec")]
        names += [s["id"] for s in stages if s.get("kind", "agent") == "agent" and s.get("role") == "builder"]
        allowed = {f"{RUNS_DIR}/{run}/{name}.json" for name in names}
    return View(root, patterns, listed, allowed, _claude_dirs(data))


def _relative(root: Path, path: Path) -> str | None:
    """`path` relative to the repository root, or None when it lies outside."""
    for base, candidate in ((root, path), (root.resolve(), path.resolve())):
        try:
            return candidate.relative_to(base).as_posix()
        except ValueError:
            continue
    return None


def hidden_kind(pl, view: View, rel: str) -> str | None:
    """Why a file (repository-relative) is hidden from the builder: "test", "record" (one of plumbline's files it does not read),
    "output" (what a test run leaves), or None."""
    if rel in view.listed or any(pl.glob_match(pattern, rel) for pattern in view.patterns) or pl.glob_match("**/conftest.py", rel):
        return "test"  # a conftest.py is test code wherever it lies
    low = rel.lower()
    if low == ".plumbline" or low.startswith(".plumbline/"):
        return None if rel in view.allowed else "record"
    if any(pl.glob_match(pattern, rel) for pattern in TEST_OUTPUT):
        return "output"
    return None


def _inside(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def exposed(pl, view: View, target: Path) -> tuple[str, str] | None:
    """(what, kind) for something hidden from the builder that reading or searching `target` would show (the target itself, or a file
    below it), or None. `what` is a repository-relative path or a directory; `kind` is as in hidden_kind, or "transcript" or "repository"."""
    root = view.root
    schemas = Path(os.path.realpath(pl.PLUGIN_ROOT / "schemas"))  # the record schemas the agents are told to read: the plugin may be installed under ~/.claude
    for candidate in dict.fromkeys((target, Path(os.path.realpath(target)))):
        if not _inside(candidate, schemas):
            if TRANSCRIPTS.search(candidate.as_posix()):
                return str(candidate), "transcript"
            for config in view.config_dirs:
                if _inside(candidate, config) or (_isdir(candidate) and _inside(config, candidate)):
                    return str(config), "transcript"
        rel = _relative(root, candidate)
        if rel is None:
            if _isdir(candidate) and _inside(Path(os.path.realpath(root)), Path(os.path.realpath(candidate))):
                return str(candidate), "repository"  # a search from above the repository covers it
            continue
        if not _isdir(candidate):
            kind = hidden_kind(pl, view, rel)
            if kind is None and not _exists(candidate):  # a path that is not there yet may become a directory of tests or test output
                below = hidden_kind(pl, view, rel + "/x")
                kind = below if below in ("test", "output") else None
            if kind:
                return rel, kind
            continue
        kind = hidden_kind(pl, view, rel + "/x") if rel else None
        if kind in ("test", "output"):
            return rel, kind  # everything below it is one
        scope = rel or "."
        listing = _git(pl, root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", scope)
        if listing.returncode != 0:
            return scope, "test"  # cannot list what is there: assume the worst
        for name in filter(None, listing.stdout.decode("utf-8", "replace").split("\0")):
            kind = hidden_kind(pl, view, name)
            if kind:
                return name, kind
        ignored = _git(pl, root, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z", "--", scope)
        for name in filter(None, ignored.stdout.decode("utf-8", "replace").split("\0")):
            kind = hidden_kind(pl, view, name + "x" if name.endswith("/") else name)
            if kind and not name.lower().startswith(".plumbline"):
                return name.rstrip("/"), kind
        found = _walk_plumbline(pl, view, candidate, rel)
        if found:
            return found
        for name in sorted(view.listed):  # listed files that git does not show
            if (rel == "" or name.startswith(rel + "/")) and _exists(root / name):
                return name, "test"
    return None


def _walk_plumbline(pl, view: View, directory: Path, rel: str) -> tuple[str, str] | None:
    """The first file of .plumbline/ below `directory` (which is `rel` in the repository) that the builder does not read: git does
    not list ignored files, and .plumbline/ is ignored."""
    low = rel.lower()
    if rel == "":
        start = view.root / ".plumbline"
    elif low == ".plumbline" or low.startswith(".plumbline/"):
        start = directory
    else:
        return None
    for folder, _dirs, files in os.walk(start):
        for name in files:
            path = Path(folder) / name
            file_rel = _relative(view.root, path)
            if file_rel is not None and hidden_kind(pl, view, file_rel):
                return file_rel, hidden_kind(pl, view, file_rel)
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


def _blind_message(view: View, what: str, kind: str, tool: str | None = None, named: str | None = None) -> str:
    shown = ", ".join(sorted(Path(p).name for p in view.allowed)) or "nothing"
    if kind == "test":
        if tool:
            return f"plumbline: the builder works blind to the tests, and a {tool} of {named} would show them (for example {what}). Search a directory without tests."
        return f"plumbline: the builder works blind to the tests, and {what} is one. Build from the spec and the source."
    if kind == "record":
        return (
            f"plumbline: under .plumbline/ the builder reads the active run's {shown} and nothing else; {what} is another of plumbline's files. "
            "Build from the spec (the plan record) and the source."
        )
    if kind == "output":
        return f"plumbline: {what} is what a test run leaves behind, and the builder works blind to the tests. Build from the spec and the source."
    if kind == "transcript":
        return (
            f"plumbline: {what} holds the agent transcripts, which show the tests, and the builder works blind to them. "
            "Build from the spec and the source."
        )
    return (
        f"plumbline: a {tool or 'search'} of {named or what} would cover the whole repository, tests and plumbline's records included, and the builder works blind to them. "
        "Search a directory of source, for example src/."
    )


def builder_reason(data: dict) -> str | None:
    if data.get("agent_type") != "plumbline:builder":
        return None
    tool, tool_input, cwd = data.get("tool_name"), data.get("tool_input") or {}, data.get("cwd")
    if not isinstance(tool_input, dict) or not isinstance(cwd, str) or not cwd:
        return None
    import plumbline as pl

    here = Path(cwd)
    if tool == "Read":
        named = tool_input.get("file_path")
        if not isinstance(named, str) or not named:
            return None
        targets = [(_join(here, named), named)]
    elif tool in ("Grep", "Glob"):
        named = tool_input.get("path")
        if not isinstance(named, str) or not named.strip():
            if adopted_root(pl, here) is None:
                return None
            return (
                f"plumbline: the builder works blind to the tests, so {tool} needs an explicit path that is not a test path (for example src/). "
                "A search of the whole repository would show the tests."
            )
        target = _join(here, named)
        if tool == "Glob" and isinstance(tool_input.get("pattern"), str):
            target = _join(target, _static_prefix(tool_input["pattern"]))
        targets = [(target, named)]
    else:
        return None
    for root in adopted_roots(pl, anchors_of(here, *[t for t, _ in targets])):
        view = _guarded(builder_view, pl, root, data)
        if view is None:
            continue
        for target, named in targets:
            hit = _guarded(exposed, pl, view, target)
            if hit:
                return _blind_message(view, hit[0], hit[1], tool if tool != "Read" else None, named)
    return None


# ------------------------------- the defenders' blindness to the canary's key
#
# A calibration run plants a false finding, the canary's, in a record that a prosecutor's cannot be told from, and the key to it (canary-key.json) lies in the
# round directory: nothing else says which finding is the canary's, except the run's ledger, which names the agent that wrote each record. A defender that read
# either would be answering the key and not the code, so a defender's Read, Grep, Glob and Bash do not reach them. The rules look only where a key exists: no key,
# no calibration run, and nothing here changes what a defender does.


def canary_files(root: Path) -> list[Path]:
    """What a defender does not read where a calibration run has a canary: the key of each round that holds one (only a calibration run's round directories
    do), and the ledger of that round's run."""
    try:
        keys = sorted((root / RUNS_DIR).glob(f"*/*/round-*/{CANARY_KEY_FILE}"))
    except OSError:
        return []
    return [*keys, *dict.fromkeys(key.parents[2] / LEDGER_NAME for key in keys)]  # round-<n>, the stage, the run


def shows_canary_file(hidden: list[Path], target: Path) -> Path | None:
    """The first of the hidden files that reading or searching `target` would show: the file itself, or a directory it lies in, at any depth."""
    for candidate in dict.fromkeys((target, Path(os.path.realpath(target)))):
        for key in hidden:
            for real in dict.fromkeys((key, Path(os.path.realpath(key)))):
                if candidate == real or candidate in real.parents:
                    return key
    return None


def names_canary_file(text: str) -> bool:
    """Does this word, or this glob pattern, name the key or the ledger?"""
    return any(name in text.lower() for name in (CANARY_KEY_FILE, LEDGER_NAME))


def _key_message(what: str) -> str:
    return (
        f"plumbline: {what} would show a file that a defender answers without: the run keeps its own key to the round there, and its own ledger. "
        "Read the findings records of the round and the code, and search a directory of source, for example src/."
    )


def defender_reason(data: dict) -> str | None:
    """A defender's Read, Grep or Glob of a canary key or a ledger, or of a directory that holds one, is denied; so is a Glob whose pattern names one."""
    if data.get("agent_type") != "plumbline:defender":
        return None
    tool, tool_input, cwd = data.get("tool_name"), data.get("tool_input") or {}, data.get("cwd")
    if not isinstance(tool_input, dict) or not isinstance(cwd, str) or not cwd:
        return None
    import plumbline as pl

    here = Path(cwd)
    if tool == "Read":
        named = tool_input.get("file_path")
        if not isinstance(named, str) or not named:
            return None
        target, shown = _join(here, named), f"a Read of {named}"
    elif tool in ("Grep", "Glob"):
        named = tool_input.get("path")
        base = _join(here, named) if isinstance(named, str) and named.strip() else here  # a search without a path starts where the agent is
        pattern = tool_input.get("pattern") if tool == "Glob" and isinstance(tool_input.get("pattern"), str) else None
        target, shown = (_join(base, _static_prefix(pattern)) if pattern else base), f"a {tool} of {named if isinstance(named, str) and named.strip() else 'the working directory'}"
        if pattern and names_canary_file(pattern) and any(canary_files(root) for root in adopted_roots(pl, anchors_of(here, base))):
            return _key_message(f"a Glob for {pattern}")
    else:
        return None
    for root in adopted_roots(pl, anchors_of(here, target)):
        hidden = canary_files(root)
        if hidden and shows_canary_file(hidden, target):
            return _key_message(shown)
    return None


RECURSIVE_GREP = re.compile(r"-[A-Za-z]*[rR][A-Za-z]*|--recursive|--dereference-recursive")
RG_UNFILTERED = {"-u", "-uu", "-uuu", "--no-ignore", "--no-ignore-vcs", "--no-ignore-dot", "--no-ignore-parent", "--hidden", "-."}  # ripgrep skips what git ignores, and dot directories, without these


def canary_command_reason(pl, role: str, steps: list["Step"], cwd: Path) -> str | None:
    """A defender's Bash does not reach a canary key or the ledger beside it: a word that names one, or a search tool whose operands (a glob counts as what it
    matches, and a recursive search with no operand as the directory it runs in) take in the file or a directory that holds it."""
    if role != "defender":
        return None
    for root in adopted_roots(pl, anchors_of(cwd, *_anchor_paths(steps))):
        hidden = canary_files(root)
        if not hidden:
            continue
        for step in steps:
            if any(names_canary_file(word) for word in step.raw):
                return _key_message(f"`{' '.join(step.raw)[:100]}`")
            name = os.path.basename(step.argv[0]) if step.argv else ""
            if name not in SEARCH_TOOLS:
                continue
            args = step.argv[1:]
            operands = [word.split("=", 1)[1] if word.startswith("--") and "=" in word else word for word in args if not word.startswith("-") or word == "-"]
            paths: list[Path] = []
            for word in operands:
                joined = _join(step.cwd, word)
                paths += [Path(match) for match in glob.glob(str(joined))] if re.search(r"[*?\[]", word) else ([joined] if _exists(joined) else [])
            implicit = (
                (name in ("grep", "egrep", "fgrep") and any(RECURSIVE_GREP.fullmatch(arg) for arg in args))
                or name == "find"
                or (name == "rg" and any(arg in RG_UNFILTERED for arg in args))
                or (name == "ls" and any(arg.startswith("-") and not arg.startswith("--") and "R" in arg for arg in args))
            )
            for target in paths or ([step.cwd] if implicit else []):
                if shows_canary_file(hidden, target):
                    return _key_message(f"`{' '.join(step.raw)[:100]}`")
    return None


# ------------------------------------------------- the orchestrator's lane: the run's directory
#
# The orchestrator launches the stage agents and runs plumbline's commands, so what it needs to read is the run's own files: records, the ledger, the round
# directories. Source, tests and the content of diffs stay with the stage agents, which keeps the orchestrator's context small and leaves it nothing to fix
# code with. An allowlist, like the builder's blindness turned inside out: what is not the active run's directory is not read.


def orchestrator_reason(data: dict) -> str | None:
    """The orchestrator's Read, Grep and Glob reach the active run's directory (.plumbline/runs/<run>/) and nothing outside it. A Grep or Glob names its
    path, and a Glob pattern that leaves that path by `..` or an absolute pattern is refused."""
    if data.get("agent_type") != "plumbline:orchestrator":
        return None
    tool, tool_input, cwd = data.get("tool_name"), data.get("tool_input") or {}, data.get("cwd")
    if not isinstance(tool_input, dict) or not isinstance(cwd, str) or not cwd:
        return None
    import plumbline as pl

    here = Path(cwd)
    if tool == "Read":
        named = tool_input.get("file_path")
        if not isinstance(named, str) or not named:
            return None
        target, shown = _join(here, named), f"a Read of {named}"
    elif tool in ("Grep", "Glob"):
        named = tool_input.get("path")
        if not isinstance(named, str) or not named.strip():
            if adopted_root(pl, here) is None:
                return None
            return f"plumbline: the orchestrator's {tool} needs an explicit path inside the active run's directory (.plumbline/runs/<run id>/): a search from where it stands would cover the source."
        target, shown = _join(here, named), f"a {tool} of {named}"
        pattern = tool_input.get("pattern") if tool == "Glob" and isinstance(tool_input.get("pattern"), str) else None
        if pattern:
            if ".." in pattern.replace("\\", "/").split("/") or pattern.startswith(("/", "~")) or re.match(r"[A-Za-z]:", pattern):
                target, shown = Path("/"), f"a Glob for {pattern}"  # it leaves the path it names: nowhere inside the run's directory
            else:
                target = _join(target, _static_prefix(pattern))
    else:
        return None
    for root in adopted_roots(pl, anchors_of(here, target)):
        run = active_run(pl, root)
        directory = root / RUNS_DIR / run if run else None
        inside = directory is not None and _inside(Path(os.path.realpath(target)), Path(os.path.realpath(directory)))  # where a link leads is what counts
        if not inside:
            where = f"the active run's directory, {RUNS_DIR}/{run}/" if run else "the active run's directory (no run is in progress, so there is none)"
            return f"plumbline: the orchestrator reads {where} and nothing else: its records, the ledger and the round directories. {shown} reaches outside it, and source and tests stay with the stage agents."
    return None


def calibrating(pl, root: Path) -> bool:
    """Is the run in progress a calibration run? Its intake record says so."""
    run = active_run(pl, root)
    stage = next((s for s in _pipeline_of(pl, root).get("stage", []) if s.get("record") == "change_class"), None)
    if run is None or stage is None:
        return False
    data, _problem = pl.load_json_file(root / RUNS_DIR / run / f"{stage['id']}.json")
    return isinstance(data, dict) and data.get("calibrate") is True


# ---------------------------------------------- what an agent may do


@dataclass
class Step:
    """One simple command of a command line, with where it runs and what it redirects to."""

    raw: list[str]  # the words as written
    argv: list[str]  # the command itself: no VAR=value words, wrappers or shell keywords
    redirects: list[tuple[str, str]]  # (operator, target)
    cwd: Path  # where it runs when every earlier cd took effect
    assigns: list[str] = field(default_factory=list)  # the VAR=value words that came before the command
    dirs: tuple[Path, ...] = ()  # every directory the command line may run in, this command included


def _shell_command(argv: list[str]) -> str | None:
    """The command string of `sh -c ...`: the first word after the options that follow `-c` (`bash -c -e 'cmd'`, `sh -c -- 'cmd'`, `bash -c -O extglob 'cmd'`)."""
    for i, word in enumerate(argv[1:], 1):
        if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
            j = i + 1
            while j < len(argv):
                option = argv[j]
                if option == "--":
                    j += 1
                    break
                if option in ("-o", "+o", "-O", "+O"):
                    j += 2  # these take a value
                elif option[:1] in "-+" and len(option) > 1:
                    j += 1
                else:
                    break
            return argv[j] if j < len(argv) else None
    return None


def _nested_line(name: str, argv: list[str]) -> str | None:
    """The command line a program takes as a string to run (`trap 'cmd' EXIT`, `cmd /c cmd`, `iex 'cmd'`, `pwsh -Command 'cmd'`), or None."""
    lower = name.lower()
    rest = argv[1:]
    if lower == "trap":
        words = [w for w in rest if w not in ("-p", "-l", "--")]
        return words[0] if len(words) >= 2 and words[0] != "-" else None
    if lower in ("iex", "invoke-expression"):
        return " ".join(rest) or None
    if lower == "cmd":
        for i, word in enumerate(rest):
            if word.lower() in ("/c", "/k", "/r"):
                return " ".join(rest[i + 1 :]) or None
    if lower in ("powershell", "pwsh"):
        for i, word in enumerate(rest):
            low = word.lower()
            if len(word) >= 2 and low.startswith("-c") and "-command".startswith(low):
                return " ".join(rest[i + 1 :]) or None
            if len(word) >= 2 and low.startswith("-e") and "-encodedcommand".startswith(low) and i + 1 < len(rest):
                try:
                    return base64.b64decode(rest[i + 1]).decode("utf-16-le") or None
                except ValueError:
                    return None
    if lower == "watch":  # it joins its words into one command line
        i = 0
        while i < len(rest):
            if rest[i] in ("-n", "--interval"):
                i += 2
            elif rest[i].startswith("-") and len(rest[i]) > 1:
                i += 1
            else:
                return " ".join(rest[i:]) or None
    return None


def walk(text: str, cwd: Path, depth: int = 0, _line: dict | None = None) -> list[Step]:
    """Every simple command of a command line, those inside `$( )`, backticks, `bash -c`, `eval`, `env -S` and git aliases
    included, each with the directory it runs in (`cd` is followed) and with every directory the line may run in."""
    top = _line is None
    line = {"targets": [], "aliases": {}, "trees": []} if top else _line
    targets: list[str] = line["targets"]
    steps: list[Step] = []
    seed = cwd
    for words, redirects in split_commands_ex(text):
        argv, assigns, chdirs, nested = _unwrap(words)
        steps.append(Step(words, argv, redirects, cwd, assigns))
        targets.extend(chdirs)
        name = _program(argv)
        exported = argv[1:] if name in ("export", "declare", "typeset") else []
        for word in (*assigns, *exported):  # GIT_DIR=... set on one command, or exported for the ones after it, points a later `git push` at that repository
            variable, _, value = word.partition("=")
            if variable in ("GIT_DIR", "GIT_WORK_TREE") and value:
                targets.append(value)
                line["trees"].append(value)
        if nested is None and depth < 3:
            nested = _nested_line(name, argv)
        if nested is not None:
            if depth < 3:
                steps.extend(walk(nested, cwd, depth + 1, line))
        elif name in ("cd", "pushd"):
            target = next((w for w in argv[1:] if not w.startswith("-")), None)
            if target is None and name == "cd":
                target = "~"
            if target and not any(ch in target for ch in "$`") and target != "-":
                targets.append(target)
                cwd = _join(cwd, target)
        elif name in SHELLS and depth < 3:
            command = _shell_command(argv)
            if command is not None:
                steps.extend(walk(command, cwd, depth + 1, line))
        elif name == "eval" and depth < 3:
            words = argv[1:]
            steps.extend(walk(" ".join(words[1:] if words[:1] == ["--"] else words), cwd, depth + 1, line))
        elif name == "git":
            defined = _defined_alias(parse_git(argv, assigns))
            if defined:
                line["aliases"][defined[0]] = defined[1]
            if depth < 3:
                expansion, shell = _alias_line(steps[-1], cwd, line["aliases"], line["trees"])
                if expansion is not None:
                    inner = walk(expansion, cwd, depth + 1, line)
                    steps.extend(inner)
                    if shell and not any(a.kind == "push" for a in actions_of(inner)) and re.search(r"\bpush\b", expansion):
                        steps.append(Step(["git", "push"], ["git", "push"], [], cwd))  # a shell alias that mentions push is one
    if top:
        dirs = _closure(seed, targets)
        for step in steps:
            step.dirs = dirs
    return steps


def agent_role(data: dict) -> str | None:
    """The role of the plumbline agent making this tool call, or None (the main session, or another agent)."""
    agent_type = data.get("agent_type")
    if isinstance(agent_type, str) and agent_type.startswith("plumbline:"):
        role = agent_type[len("plumbline:") :]
        return role if role in ROLES else None
    return None


def plumbline_args(pl, argv: list[str]) -> tuple[str, list[str]] | None:
    """The subcommand and its arguments, when `argv` runs this plugin's own plumbline.py (`python3 <path>/plumbline.py <subcommand> ...`)."""
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
    return argv[i + 1], argv[i + 2 :]


def plumbline_cli(pl, argv: list[str]) -> str | None:
    """The subcommand, when `argv` runs this plugin's own plumbline.py (`python3 <path>/plumbline.py <subcommand> ...`)."""
    found = plumbline_args(pl, argv)
    return found[0] if found else None


# The options of `plumbline.py override`: argparse accepts any unambiguous prefix of a long option, so `--rea` is `--reason`.
OVERRIDE_OPTIONS = ("reason", "run", "project", "help")


def override_option(word: str) -> str | None:
    """The `plumbline.py override` option a word stands for (`--reason`, `--rea=x`, `--proj`), or None."""
    if not word.startswith("--") or len(word) < 3:
        return None
    name = word[2:].split("=", 1)[0]
    if name in OVERRIDE_OPTIONS:
        return name
    found = [option for option in OVERRIDE_OPTIONS if option.startswith(name)] if name else []
    return found[0] if len(found) == 1 else None


def runs_override(step: Step) -> bool:
    """Does this command run `plumbline.py override`? By its script name, by the pair of words `override` and
    `--reason` (which catches a script path held in a variable; any abbreviation of the option counts), or by inline code that names both."""
    words = step.raw
    for i, word in enumerate(words):
        if os.path.basename(word) == "plumbline.py" and words[i + 1 : i + 2] == ["override"]:
            return True
    if "override" in words and any(override_option(w) == "reason" for w in words):
        return True
    interpreter = os.path.basename(step.argv[0]) if step.argv else ""
    return bool(re.fullmatch(r"(python[\d.]*|node|ruby|perl|php)", interpreter)) and any("plumbline" in w and "override" in w for w in step.argv[1:])


def override_reason(pl, steps: list[Step]) -> str | None:
    """`plumbline.py override` is denied to every agent and to the main session, wherever the command would act:
    in a directory the line may run in, or in the one `--project` names, when that repository has adopted plumbline
    (elsewhere the command refuses by itself, and this hook stays silent)."""
    for step in steps:
        if not runs_override(step):
            continue
        bases = list(dict.fromkeys((step.cwd, *step.dirs)))
        places = list(bases)
        for i, w in enumerate(step.raw):
            if override_option(w) != "project":
                continue
            value = w.split("=", 1)[1] if "=" in w else (step.raw[i + 1] if i + 1 < len(step.raw) else None)
            if value:
                places += [_join(base, value) for base in bases]
        if adopted_roots(pl, places):
            return (
                "plumbline: `plumbline.py override` is the builder's command, and the builder types it: /plumbline:override followed by the reason. "
                "An agent, or the main session, does not run it. To skip the pipeline for this commit, ask the builder to type /plumbline:override."
            )
    return None


def _target_directory(args: list[str]) -> list[str]:
    """The directory `-t DIR`, `-tDIR`, `--target-directory DIR` or `--target-directory=DIR` names."""
    found = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-t", "--target-directory")]
    found += [a.split("=", 1)[1] for a in args if a.startswith("--target-directory=")]
    found += [a[2:] for a in args if a.startswith("-t") and not a.startswith("--") and len(a) > 2 and "=" not in a]
    return found


def written_operands(argv: list[str]) -> list[str]:
    """The operands a command writes or removes, for the writers plumbline knows: `tee`, `rm`, `cp`, `mv`, `dd of=`, `sed -i` and the like."""
    if not argv:
        return []
    name, args = os.path.basename(argv[0]), argv[1:]
    operands = [a for a in args if not a.startswith("-") or a == "-"]
    if name in WRITERS_ALL:
        return operands
    if name in WRITERS_LAST:
        into = _target_directory(args)
        return into + operands[-1:]
    if name == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    if name in ("sed", "perl") and any(re.fullmatch(r"-[A-Za-z]*i.*", a) or a.startswith("--in-place") for a in args):
        return operands
    return []


def _copy_plan(step: Step) -> tuple[str, list[str], list[str]] | None:
    """(destination, sources, flags) of `cp`, `mv`, `install`, `ln` and `rsync` when the destination is a directory, else None."""
    name = os.path.basename(step.argv[0]) if step.argv else ""
    if name not in WRITERS_LAST and name != "mv":
        return None
    args = step.argv[1:]
    operands = [a for a in args if not a.startswith("-") or a == "-"]
    into = _target_directory(args)
    if into:
        destination, sources = into[0], operands
    elif len(operands) >= 2:
        destination, sources = operands[-1], operands[:-1]
    else:
        return None
    if not (destination.endswith("/") or _isdir(_join(step.cwd, destination))):
        return None
    return destination, sources, [a for a in args if a.startswith("-")]


def landing_places(step: Step) -> list[str]:
    """Where `cp`, `mv`, `install`, `ln` and `rsync` put each source when the destination is a directory: `cp -r /tmp/pass .plumbline/` writes
    `.plumbline/pass`, which no operand names."""
    plan = _copy_plan(step)
    if plan is None:
        return []
    destination, sources, _flags = plan
    return [destination.rstrip("/") + "/" + os.path.basename(source.rstrip("/")) for source in sources if os.path.basename(source.rstrip("/"))]


UNKNOWN_SOURCE = re.compile(r"[*?\[]|/\.$|/$")  # a glob, or the contents of a directory (`dir/.`, `dir/`): the names that land are not written down


def _option_values(args: list[str], short: str, long: str) -> list[str]:
    """The values of an option given as `-C DIR`, `-CDIR`, `--directory DIR` or `--directory=DIR`."""
    found = [args[i + 1] for i, a in enumerate(args[:-1]) if a in (f"-{short}", f"--{long}")]
    found += [a.split("=", 1)[1] for a in args if a.startswith(f"--{long}=")]
    found += [a[2:] for a in args if a.startswith(f"-{short}") and not a.startswith("--") and len(a) > 2 and "=" not in a]
    return found


def unknown_landings(step: Step) -> tuple[list[str], list[str]]:
    """(directories that get files whose names the command line does not show, files a download is written to): a copy of a glob or of a
    directory's contents, an unpacked archive, a patch, a download."""
    name = os.path.basename(step.argv[0]) if step.argv else ""
    args = step.argv[1:]
    plan = _copy_plan(step)
    if plan is not None:
        destination, sources, flags = plan
        if any(UNKNOWN_SOURCE.search(source) for source in sources) or any(f in ("-T", "--no-target-directory") or (f.startswith("-") and not f.startswith("--") and "T" in f[1:]) for f in flags):
            return [destination], []
        return [], []
    if name == "tar":
        first = args[0] if args else ""
        extracting = any(a in ("--extract", "--get") or (a.startswith("-") and not a.startswith("--") and "x" in a[1:]) for a in args) or bool(re.fullmatch(r"[a-zA-Z]*x[a-zA-Z]*", first))
        return (_option_values(args, "C", "directory") or ["."] if extracting else []), []
    if name == "unzip":
        return _option_values(args, "d", "d") or ["."], []
    if name == "patch":
        return _option_values(args, "d", "directory") or ["."], []
    if name == "curl":
        directories = [a.split("=", 1)[1] for a in args if a.startswith("--output-dir=")] + [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--output-dir"]
        return directories, _option_values(args, "o", "output")
    if name == "wget":
        return _option_values(args, "P", "directory-prefix"), _option_values(args, "O", "output-document")
    return [], []


def written_targets(step: Step) -> list[str]:
    """Where a command writes to: its output redirections (not fd duplications like `2>&1`), its writer operands, where a copy or a move into
    a directory lands, and, for what lands under names the line does not show, the places the protected files would be."""
    targets = [
        target
        for op, target in step.redirects
        if ">" in op and not (op.endswith("&") and re.fullmatch(r"\d+|-", target))
    ]
    directories, files = unknown_landings(step)
    for directory in directories:
        base = directory.rstrip("/") or "/"
        targets += [f"{base}/pass/x", f"{base}/x/ledger.jsonl", f"{base}/ledger.jsonl"]
    return targets + files + written_operands(step.argv) + landing_places(step)


def protected_rel(pl, roots: dict, path: Path) -> str | None:
    """The repository-relative path, when `path` (or where it really leads) is under .plumbline/pass/, is a run's ledger.jsonl, or is
    .plumbline/runs/ACTIVE (the file that names the run the agents work in) in a repository that has adopted plumbline: those are
    written only by plumbline.py."""
    for candidate in dict.fromkeys((path, Path(os.path.realpath(path)))):
        if ".plumbline" not in str(candidate).lower() and candidate.name != "ledger.jsonl":
            continue  # cannot be one of them: no need to look for the repository
        directory = candidate.parent
        while not _isdir(directory) and directory != directory.parent:
            directory = directory.parent
        if directory not in roots:
            roots[directory] = adopted_root(pl, directory)
        root = roots[directory]
        rel = _relative(root, candidate) if root is not None else None
        low = rel.lower() if rel is not None else None
        if low is not None and (
            low == PASS_DIR or low.startswith(PASS_DIR + "/") or low == ACTIVE_REL or re.fullmatch(re.escape(RUNS_DIR) + r"/[^/]+/ledger\.jsonl", low)
        ):
            return rel
    return None


def protected_message(rel: str) -> str:
    return (
        f"plumbline: {rel} is written only by plumbline.py commands (`pass` writes .plumbline/pass/, `plan --intent` names the run in progress in "
        ".plumbline/runs/ACTIVE, and the gates and the hooks write a run's ledger.jsonl), "
        "so nothing edits it, redirects into it or pipes into it. Run the pipeline's commands instead."
    )


def protected_command_reason(pl, steps: list[Step]) -> str | None:
    roots: dict = {}
    for step in steps:
        for target in written_targets(step):
            if target:
                for base in dict.fromkeys((step.cwd, *step.dirs)):
                    rel = protected_rel(pl, roots, _join(base, target))
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


def _dangerous_git_option(word: str) -> bool:
    """Is this argument of a git-read command an option that runs a program or writes a file (--output, -O, --ext-diff, --textconv, ...)?
    Git takes any unambiguous prefix of a long option, so a prefix of one of them is that option; a short option can hide in a bundle."""
    if word.startswith("--"):
        name = word[2:].split("=", 1)[0]
        if name == "text":
            return False  # `--text` (treat files as text) is an option in its own right; `--textconv` is the one that runs a program
        return bool(name) and any(option.startswith(name) for option in GIT_DANGEROUS_LONG)
    if word.startswith("-") and len(word) > 1:
        for letter in word[1:]:
            if letter == "O":
                return True
            if letter in GIT_VALUE_SHORTS:
                break  # the rest of the bundle is that option's value
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
    options = rest[: rest.index("--")] if "--" in rest else rest
    return not any(_dangerous_git_option(a) for a in options)


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


PLUMBLINE_RUN = ("check-diff", "gate", "merge-review", "status", "tokens", "check-record", "open")  # the commands of the plumbline-run class, besides `plan --run`
GIT_CONTENT_LONG = (  # long options of git diff and git log (and status) that print what the files say: a patch, a word diff, a verbose status
    "patch", "unified", "word-diff", "word-diff-regex", "color-words", "patch-with-stat", "patch-with-raw", "combined", "cc", "binary", "diff-merges", "verbose",
)
GIT_CONTENT_SHORT = "puUcvL"  # -p, -u, -U<n>, -c, -v and -L<range>: the short ones. A bundle counts up to the first letter that takes a value (see GIT_VALUE_SHORTS)
GIT_SUMMARIES = ("--numstat", "--name-only")  # what `git diff` shows with no content (--stat, which takes `=width` too, is the third)


def _plan_run_only(args: list[str]) -> bool:
    """`plan --run RUN [--json] [--project PATH]`, the plan of a run that has begun: nothing else the command takes (`--intent` and the rest start a
    run). The command line takes no abbreviation of an option, so the exact spellings are all there is."""
    seen_run = False
    i = 0
    while i < len(args):
        name, equals, _value = args[i].partition("=")
        if name == "--json" and not equals:
            i += 1
        elif name in ("--run", "--project") and (equals or i + 1 < len(args)):
            seen_run = seen_run or name == "--run"
            i += 1 if equals else 2
        else:
            return False
    return seen_run


def _plumbline_run(pl, argv: list[str]) -> bool:
    """Is this one of the plumbline-run commands: `plumbline.py` check-diff, gate, merge-review, status, tokens, check-record or open, or `plan --run`?"""
    found = plumbline_args(pl, argv)
    if found is None:
        return False
    sub, args = found
    return sub in PLUMBLINE_RUN or (sub == "plan" and _plan_run_only(args))


def _shows_content(word: str) -> bool:
    """Is this option of git diff, log or status one that prints what the files say (a patch, a word diff, a verbose status)? Git takes any
    unambiguous prefix of a long option, so a prefix of one of them is one; `--color` is its own option and not a prefix of `--color-words`."""
    if word.startswith("--"):
        name = word[2:].split("=", 1)[0]
        return bool(name) and name != "color" and any(option.startswith(name) for option in GIT_CONTENT_LONG)
    if word.startswith("-") and len(word) > 1:
        for letter in word[1:]:
            if letter in GIT_CONTENT_SHORT:
                return True
            if letter in GIT_VALUE_SHORTS:
                break  # the rest of the bundle is that option's value
    return False


def _git_meta(argv: list[str]) -> bool:
    """git's summary views: status and rev-parse, log, `branch --show-current`, and diff only with --stat, --numstat or --name-only. Nothing
    prints a patch, and no option runs a program or writes a file (the git-read rules, which this class keeps)."""
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
    if i >= len(argv):
        return False
    sub, rest = argv[i], argv[i + 1 :]
    if sub == "branch":
        return rest == ["--show-current"]
    if sub not in ("status", "rev-parse", "log", "diff"):
        return False
    options = rest[: rest.index("--")] if "--" in rest else rest
    if any(_dangerous_git_option(a) for a in options):
        return False
    if sub == "rev-parse":
        return True
    if any(_shows_content(a) for a in options):
        return False
    if sub == "diff":
        return any(a in GIT_SUMMARIES or a == "--stat" or a.startswith("--stat=") for a in options)
    return True


def _class_matches(pl, project, classes: list[str], argv: list[str]) -> list[str]:
    """The command classes of a role's policy that this command belongs to."""
    name = os.path.basename(argv[0])
    found = []
    for kind in classes:
        if kind in CONFIG_COMMANDS and _prefix_matches(argv, project.commands.get(kind, [])):
            found.append(kind)
        elif kind == "git-read" and name == "git" and _git_read_only(argv):
            found.append(kind)
        elif kind == "search" and _search_only(argv):
            found.append(kind)
        elif kind == "plumbline-check" and plumbline_cli(pl, argv) == "check-diff":
            found.append(kind)
        elif kind == "graft" and project.graft_enabled and name == "graft":
            found.append(kind)
        elif kind == "plumbline-run" and _plumbline_run(pl, argv):
            found.append(kind)
        elif kind == "git-meta" and name == "git" and _git_meta(argv):
            found.append(kind)
    return found


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
        elif kind == "plumbline-run":
            parts.append("`plumbline.py` " + ", ".join(PLUMBLINE_RUN[:-1]) + f" and {PLUMBLINE_RUN[-1]}, and `plan --run RUN --json`")
        elif kind == "git-meta":
            parts.append("git's summary views (status, rev-parse, log with no patch, branch --show-current, and diff with --stat, --numstat or --name-only)")
    parts.append("`plumbline.py check-record TYPE FILE` to check your record")
    return "; ".join(parts)


def _anchor_paths(steps: list[Step]) -> list[Path]:
    """Places an agent's command line names: the directories it runs in, and every absolute path among its words (`git -C /repo`, `--git-dir=/x`)."""
    found: list[Path] = []
    for step in steps:
        found.append(step.cwd)
        for word in step.raw:
            value = word.split("=", 1)[1] if word.startswith("-") and "=" in word else word
            if value.startswith(("/", "~/")):
                found.append(Path(os.path.expanduser(value)))
    return list(dict.fromkeys(found))


def role_command_reason(pl, role: str, steps: list[Step], cwd: Path) -> str | None:
    """Every simple command of an agent's Bash line must be of a class its role allows, and nothing may be
    redirected into a file (the write targets cover Edit, Write and NotebookEdit; Bash is for running things)."""
    for root in adopted_roots(pl, anchors_of(cwd, *_anchor_paths(steps))):
        reason = _role_command_reason(pl, role, steps, root)
        if reason:
            return reason
    return None


def _role_command_reason(pl, role: str, steps: list[Step], root: Path) -> str | None:
    project = _cached(("project", str(root)), lambda: pl.load_project(root))
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
        if step.raw and (step.raw[0] in ("for", "select", "case") or (len(step.raw) == 1 and step.raw[0] in ("fi", "done", "esac"))):
            continue  # the head or the end of a compound command: nothing runs here, and what it wraps is checked as commands of its own
        if not step.argv and all(ASSIGNMENT.fullmatch(w) or w in KEYWORDS for w in step.raw):
            continue  # nothing but variable assignments and shell keywords: no command runs
        if step.argv and os.path.basename(step.argv[0]) in NO_EFFECT:
            continue
        if step.raw and os.path.basename(step.raw[0]) in ("sudo", "doas"):
            return f"plumbline: the {role} does not run commands as another user (`{shown}`)."
        if step.argv:
            kinds = _class_matches(pl, project, classes, step.argv)
            if kinds and step.assigns and all(kind in ("git-read", "search") for kind in kinds):
                return (
                    f"plumbline: the {role}'s read-only git and search tools run as they are, with no VAR=value before them "
                    f"(an environment variable can make git or grep run a program), so `{shown}` is refused. Run it without the assignment."
                )
            if kinds and step.assigns and all(kind in ("plumbline-run", "git-meta") for kind in kinds):
                return (
                    f"plumbline: the {role}'s plumbline and git commands run as they are, with no VAR=value before them "
                    f"(an environment variable can make python or git run a program), so `{shown}` is refused. Run it without the assignment."
                )
            if kinds or plumbline_cli(pl, step.argv) == "check-record":
                continue
        return f"plumbline: the {role}'s Bash may run only {_allowed_summary(project, role, classes)}. `{shown}` is none of these."
    return None


def own_record_paths(pipeline: dict, role: str) -> list[str]:
    """Where an agent of `role` writes its record, as patterns for a message."""
    if role in REVIEW_ROLES:
        stages = [s["id"] for s in pipeline.get("stage", []) if s.get("kind") == "review"]
        name = " or ".join(REVIEW_FILE_NAMES[role])
        return [f"{RUNS_DIR}/<run-id>/{stages[0] if len(stages) == 1 else '{' + ','.join(stages) + '}'}/round-<n>/{name}"] if stages else []
    return [f"{RUNS_DIR}/<run-id>/{s['id']}.json" for s in pipeline.get("stage", []) if s.get("kind", "agent") == "agent" and s.get("role") == role]


def own_record(pl, root: Path, pipeline: dict, role: str, rel: str) -> tuple[bool, str | None]:
    """Is `rel` a record this agent writes? (True, None) when it is, in the active run and, for the review roles, in the current round of
    its stage; (False, why) when it is shaped like one of the agent's records but lies in another run or round; (False, None) when it is not shaped like one."""
    match = re.fullmatch(re.escape(RUNS_DIR) + r"/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/(.+)", rel)
    if not match:
        return False, None
    run, rest = match.groups()
    stages = pipeline.get("stage", [])
    rounds: tuple[str, int] | None = None
    if not any(s.get("kind", "agent") == "agent" and s.get("role") == role and rest == f"{s['id']}.json" for s in stages):
        if role not in REVIEW_ROLES:
            return False, None
        for s in stages:
            found = s.get("kind") == "review" and re.fullmatch(re.escape(s["id"]) + r"/round-([1-9][0-9]*)/" + REVIEW_FILES[role], rest)
            if found:
                rounds = (s["id"], int(found.group(1)))
                break
        if rounds is None:
            return False, None
    active = active_run(pl, root)
    if active is None:
        return False, (
            f"plumbline: no run is in progress, and the {role} writes its record in one. The main session starts a run with "
            "`plumbline.py plan --intent <intent>` (/plumbline:run) before it launches the stage's agent."
        )
    if run != active:
        return False, f"plumbline: the {role} writes its record in the active run ({active}); {rel} belongs to run {run}."
    if rounds is not None:
        current = current_round(pl, root, run, rounds[0])
        if rounds[1] != current:
            return False, (
                f"plumbline: the {role} writes in the current round of {rounds[0]} (round-{current}); {rel} is in round-{rounds[1]}. "
                "`plumbline.py gate` opens the next round's directory when a review fails with rounds left; write in the round your brief names."
            )
    return True, None


def own_stubs(pl, root: Path, role: str, rel: str) -> tuple[bool, str | None]:
    """Is `rel` a file under the stubs directory of the active run? (True, None) when it is; (False, why) when it lies under another run's
    stubs directory, or no run is in progress; (False, None) when it is not shaped like a file of any run's stubs directory."""
    match = re.fullmatch(re.escape(RUNS_DIR) + r"/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/" + re.escape(STUBS_DIR) + r"/.+", rel)
    if not match:
        return False, None
    active = active_run(pl, root)
    if active is None:
        return False, (
            f"plumbline: no run is in progress, and the {role} puts stubs in the run's stubs directory. The main session starts a run with "
            "`plumbline.py plan --intent <intent>` (/plumbline:run) before it launches the stage's agent."
        )
    if match.group(1) != active:
        return False, f"plumbline: the {role} writes stubs in the active run's stubs directory ({RUNS_DIR}/{active}/{STUBS_DIR}/); {rel} belongs to run {match.group(1)}."
    return True, None


def _listed(items: list[str]) -> str:
    """`a`, `a and b`, `a, b and c`."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def pipeline_paths(pl, project, root: Path) -> set[str]:
    """The pipeline file the repository names, as a path inside the repository (none, when it lies elsewhere)."""
    value = (project.config or {}).get("pipeline", pl.DEFAULT_PIPELINE)
    try:
        rel = _relative(root, pl.resolve_pipeline_path(value, root)) if isinstance(value, str) else None
    except pl.PlumblineError:
        rel = None
    return {rel} if rel else set()


def git_dirs(root: Path) -> set[Path]:
    """Where a repository's git data lives: its .git, and for a linked worktree also the directory that .git file points to and the main
    repository's git directory (the common one)."""
    dot = root / ".git"
    found = {Path(os.path.realpath(dot))}
    try:
        if _isfile(dot):
            match = re.match(r"gitdir:\s*(.+)", dot.read_text(encoding="utf-8", errors="replace"))
            if match:
                gitdir = Path(os.path.realpath(_join(root, match.group(1).strip())))
                found.add(gitdir)
                try:
                    found.add(Path(os.path.realpath(_join(gitdir, (gitdir / "commondir").read_text(encoding="utf-8").strip()))))
                except OSError:
                    pass
    except OSError:
        pass
    return found


def git_lane_message(role: str, shown: str) -> str:
    return f"plumbline: {shown} is git's own configuration and hooks. Git commands run by the main session change it, and the {role} leaves it alone."


def lane_reason(role: str, rel: str) -> str | None:
    """A denial when `rel` is one of the files no agent writes: git's own, agent settings and instructions, repository automation.
    They match at any depth (a nested repository has its own .git; CLAUDE.md files are read from subdirectories)."""
    parts = [p.lower() for p in rel.split("/")]
    kind = next((LANE_DIRS[p] for p in parts if p in LANE_DIRS), None) or LANE_FILES.get(parts[-1])
    if kind is None:
        return None
    if kind == "git":
        return git_lane_message(role, rel)
    what = "holds agent settings and instructions" if kind == "settings" else "is repository automation (CI, hooks, attributes)"
    return f"plumbline: {rel} {what}. CI and agent settings change through the main session, and the {role} leaves it alone."


def command_files(project, root: Path) -> dict[Path, str]:
    """The existing files of the repository that the [commands] strings name (`sh run_tests.sh`, `pytest --config=pytest.ini`), each with
    the command that names it: what runs the tests is not the builder's to change."""
    found: dict[Path, str] = {}
    for prefixes in project.commands.values():
        for prefix in prefixes:
            for words in split_commands(prefix):
                for word in words:
                    for value in {word, word.split("=", 1)[1] if "=" in word else ""}:
                        if value and not value.startswith("-"):
                            path = _join(root, value)
                            if _isfile(path):
                                found[Path(os.path.realpath(path))] = prefix
    return found


def partition_reason(pl, root: Path, role: str, target: Path, real: Path) -> str | None:
    """Is a write by an agent of `role` to `target` inside the role's write targets? Where the path leads, through
    symbolic links, must be inside them too."""
    project = _cached(("project", str(root)), lambda: pl.load_project(root))
    policy = project.roles.get(role)
    if not isinstance(policy, dict):
        return None
    writes = [w for w in policy.get("writes", []) if isinstance(w, str)]
    if not writes:  # the orchestrator: it launches the stage agents and runs plumbline's commands, and the files are theirs
        return f"plumbline: the {role} writes no file: the stage agents write the records, and the main session commits. Launch the agent whose work this is."
    pipeline = project.pipeline or pl.load_toml(pl.PIPELINE_DIR / f"{pl.DEFAULT_PIPELINE}.toml")
    patterns = next((t["paths"] for t in pipeline.get("type", []) if t.get("id") == "tests"), [])
    fixed = {name.lower() for name in pipeline_paths(pl, project, root) | {pl.CONFIG_FILE}}  # compared without case: a filesystem may not tell them apart
    own = own_record_paths(pipeline, role)
    gitdirs = git_dirs(root)
    keeps_tests_out = "code" in writes and "tests" not in writes  # writes code, not tests: the test harness is not its either
    named = command_files(project, root) if keeps_tests_out else {}
    for candidate in dict.fromkeys((target, real)):
        rel = _relative(root, candidate)
        if any(_inside(candidate, gitdir) for gitdir in gitdirs):  # git's data, wherever it lies: a linked worktree's is outside the repository
            return git_lane_message(role, rel if rel is not None else str(candidate))
        if rel is None:
            return f"plumbline: the {role} writes inside the repository, and {candidate} is outside it."
        lane = lane_reason(role, rel)
        if lane:
            return lane
        if "record" in writes:
            ok, why = own_record(pl, root, pipeline, role, rel)
            if ok:
                continue
            if why:
                return why
        if "stubs" in writes:
            ok, why = own_stubs(pl, root, role, rel)
            if ok:
                continue
            if why:
                return why
        if rel == ".plumbline" or rel.lower().startswith(".plumbline/"):
            where = ", ".join(own) or "nowhere"
            stubs = f" and stubs ({RUNS_DIR}/<run-id>/{STUBS_DIR}/)" if "stubs" in writes else ""
            return f"plumbline: the {role} writes only its own record ({where}){stubs}; {rel} is another of plumbline's files."
        if rel.lower() in fixed:
            return f"plumbline: {rel} defines how the pipeline runs; it changes through the main session, and the {role} leaves it alone."
        if any(pl.glob_match(pattern, rel) for pattern in patterns):
            if "tests" not in writes:
                hint = " The test-writer writes them; build from the spec." if role == "builder" else ""
                return f"plumbline: {rel} is a test path, and the {role} does not write tests.{hint}"
        elif keeps_tests_out and (os.path.basename(rel).lower() in TEST_CONFIG_FILES or real in named):
            command = named.get(real)
            if command:
                return (
                    f"plumbline: {rel} is run by the repository's test tooling ([commands]: {command}); "
                    f"it changes through the main session, and the {role} leaves it alone."
                )
            return f"plumbline: {rel} configures how the tests run; it changes through the main session (the test-writer writes test files), and the {role} leaves it alone."
        elif "code" not in writes:
            can = ["its own record (" + (own[0] if own else "under .plumbline/runs/") + ")"]
            if "tests" in writes:
                can.append("test paths")
            if "stubs" in writes:
                can.append(f"stubs ({RUNS_DIR}/<run-id>/{STUBS_DIR}/)")
            hint = " Put the stubs of a brand-new module in the run's stubs directory; the source is the builder's." if "stubs" in writes else ""
            return f"plumbline: the {role} writes {_listed(can)}; {rel} is neither.{hint}"
    return None


def pinned_model(pl, role: str) -> str | None:
    """The model an agent's definition pins (`model:` in the front matter of agents/<role>.md), or None."""
    try:
        text = (pl.PLUGIN_ROOT / "agents" / f"{role}.md").read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    for line in text.split("\n")[1:]:
        if line.strip() == "---":
            break
        match = re.match(r"model:\s*['\"]?([^'\"\s]+)", line)
        if match:
            return match.group(1)
    return None


def agent_launch_reason(data: dict) -> str | None:
    """A plumbline agent is launched in the main checkout (records live there), with the model its definition pins; an agent that is
    not one of plumbline's is not briefed on the run's files, which the stage agents write and read. The orchestrator launches the stage
    agents and no other: its launches meet these rules too, as the main session's do."""
    tool_input, cwd = data.get("tool_input"), data.get("cwd")
    if not isinstance(tool_input, dict) or not isinstance(cwd, str) or not cwd:
        return None
    subagent = tool_input.get("subagent_type")
    plumbline_agent = isinstance(subagent, str) and subagent.startswith("plumbline:")
    text = f"{tool_input.get('prompt', '')}\n{tool_input.get('description', '')}"
    names_canary = subagent == "plumbline:defender" and bool(re.search(r"canary", text, re.I))
    from_orchestrator = data.get("agent_type") == f"plumbline:{ORCHESTRATOR}"
    if plumbline_agent:
        if not from_orchestrator and tool_input.get("isolation") is None and tool_input.get("model") is None and not names_canary:
            return None
    elif not from_orchestrator and not re.search(r"\.plumbline[/\\]", text, re.I):
        return None
    import plumbline as pl

    roots = adopted_roots(pl, anchors_of(Path(cwd)))
    if not roots:
        return None
    if from_orchestrator and not (plumbline_agent and subagent[len("plumbline:") :] in STAGE_ROLES):
        who = subagent if isinstance(subagent, str) and subagent else "a general agent"
        return (
            f"plumbline: the orchestrator launches the stage agents (plumbline:{', '.join(STAGE_ROLES[:-1])} or {STAGE_ROLES[-1]}) and no other agent, "
            f"itself included: {who} is not one of them. The stage agents do the work, and the main session does the rest."
        )
    if names_canary and any(calibrating(pl, root) for root in roots):
        return (
            "plumbline: a defender's brief lists the findings records of the round alike, the canary's among them, and names neither the canary nor its key. "
            "Leave the word out of the brief."
        )
    if not plumbline_agent:
        who = subagent if isinstance(subagent, str) and subagent else "a general agent"
        return (
            f"plumbline: plumbline stages run through the plumbline:* agents, so a brief about .plumbline/ goes to the stage's agent "
            f"(plumbline:planner, test-writer, builder, verifier, prosecutor, defender, detective or canary), not to {who}."
        )
    if tool_input.get("isolation") is not None:
        return f"plumbline: a run's records live in the main checkout, so {subagent} runs there. Launch it without `isolation`."
    role = subagent[len("plumbline:") :]
    pinned = pinned_model(pl, role) if role in ROLES else None
    model = tool_input.get("model")
    if pinned and model is not None and (not isinstance(model, str) or model.strip().lower() != pinned.lower()):
        return f"plumbline: {subagent} is pinned to the {pinned} model. Launch it without `model`, or with `model: {pinned}`."
    return None


def write_reason(data: dict) -> str | None:
    """Edit, Write and NotebookEdit: nobody writes the protected files, and an agent writes inside its role's targets."""
    tool_input, cwd = data.get("tool_input"), data.get("cwd")
    if not isinstance(tool_input, dict) or not isinstance(cwd, str) or not cwd:
        return None
    named = [tool_input[k] for k in ("file_path", "notebook_path") if isinstance(tool_input.get(k), str) and tool_input[k]]
    role = agent_role(data)
    for one in named:  # whichever of the two keys the tool takes, both are looked at
        target = _join(Path(cwd), one)
        real = Path(os.path.realpath(target))
        if role is None and ".plumbline" not in f"{target}{real}".lower() and target.name != "ledger.jsonl" and real.name != "ledger.jsonl":
            continue  # nothing here concerns plumbline: not even the repository is looked up
        import plumbline as pl

        rel = _guarded(protected_rel, pl, {}, target)
        if rel:
            return protected_message(rel)
        if role is None:
            continue
        for root in adopted_roots(pl, anchors_of(Path(cwd), target, real)):
            reason = _guarded(partition_reason, pl, root, role, target, real)
            if reason:
                return reason
    return None


# ------------------------------------------------------------- the hook


def decide(data) -> str | None:
    """The reason to deny this tool call, or None to allow it."""
    global _CACHE
    if not isinstance(data, dict):
        return None
    data = _scrub(data)
    tool = data.get("tool_name")
    _CACHE = {}
    try:
        if tool in ("Bash", "PowerShell", "Monitor"):
            return bash_reason(data)
        if tool in ("Read", "Grep", "Glob"):
            return builder_reason(data) or defender_reason(data) or orchestrator_reason(data)
        if tool in ("Edit", "Write", "NotebookEdit"):
            return write_reason(data)
        if tool in ("Agent", "Task"):  # the tool has been called both
            return agent_launch_reason(data)
        return None
    finally:
        _CACHE = None


def main() -> int:
    os.environ["GIT_OPTIONAL_LOCKS"] = "0"  # whatever git the hook starts, directly or through plumbline.py, leaves the index alone
    try:
        reason = decide(json.loads(sys.stdin.buffer.read().decode("utf-8-sig", "replace")))  # the event is UTF-8 (a byte order mark too) whatever the locale says
    except BaseException:  # a hook must never crash, and must not hold anything up
        if os.environ.get("PLUMBLINE_HOOK_DEBUG"):
            raise
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
        if os.environ.get("PLUMBLINE_HOOK_DEBUG"):
            raise
        code = 0
    sys.exit(code)
