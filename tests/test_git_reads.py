"""Git reads and search tools that execute or write: PG-GITEXEC-1 (git takes any unambiguous prefix of a long option, so
`--open-files-in-p=...` is `--open-files-in-pager`) and PG-GITEXEC-2 (`GIT_EXTERNAL_DIFF=... git diff` turns a read into a run).

The agents that get read-only git and search tools (planner, verifier, prosecutor, defender, detective) may run neither; the test
command and the main session are not touched by these rules."""
import subprocess

import pytest

import pre_tool_use as pre
from helpers import commit_all, write
from hookdata import bash_payload
from rundata import adopt

READERS = ("planner", "verifier", "prosecutor", "defender", "detective")


@pytest.fixture(autouse=True)
def hook_errors_surface(monkeypatch):
    monkeypatch.setenv("PLUMBLINE_HOOK_DEBUG", "1")


@pytest.fixture
def adopted(repo):
    adopt(repo)
    return repo


def run(repo, role, command):
    return pre.decide(bash_payload(repo, command, agent_type=f"plumbline:{role}" if role else None))


# ------------------------------------------------------------ PG-GITEXEC-1: options git takes in a shortened form

DANGEROUS = [
    # -O / --open-files-in-pager runs the pager program it is given
    "git grep --open-files-in-pager='touch pwn1;true' return",
    "git grep --open-files-in-p='touch pwn3;true' return",
    "git grep --open-files='touch pwn4;true' return",
    "git grep --open='touch pwn5;true' return",
    "git grep --ope='touch pwn5b;true' return",
    "git grep --op='touch x' return",
    "git grep -O'touch pwn2;true' return",
    "git grep -O less foo",
    "git grep -nO less foo",
    "git grep -inO less foo",
    "git grep -n -O less foo",
    # --output writes a file
    "git diff --output=out1.txt",
    "git diff --outpu=out2.txt",
    "git diff --outp=out4.txt",
    "git diff --out=out5.txt",
    "git diff --ou=o",
    "git diff --o=o",
    "git log --output=out2.txt",
    "git log --outpu=out6.txt",
    "git show --output=out3.txt HEAD",
    "git show --outpu=x HEAD",
    "git diff HEAD~1 --output out.patch",
    # --ext-diff and --textconv run the programs the git config names
    "git diff --ext-diff",
    "git diff --ext",
    "git diff --ex",
    "git log -p --ext-diff",
    "git log -p --ext-di",
    "git show --ext-diff HEAD",
    "git diff --textconv",
    "git diff --textc",
    "git blame --textconv README.md",
    "git show --textc HEAD",
    "git log -p --te",
    # --exec, --upload-pack and --receive-pack run the command they are given
    "git diff --exec=x",
    "git diff --exe=x",
    "git log --e=x",
    "git diff --upload-pack=x",
    "git diff --upload=x",
    "git diff --up=x",
    "git show --receive-pack=x HEAD",
    "git show --receive=x HEAD",
    "git log --rec=x",
]


@pytest.mark.parametrize("command", DANGEROUS)
def test_a_git_read_that_runs_a_program_or_writes_a_file_is_denied_in_any_abbreviation_git_accepts(adopted, command):
    for role in READERS:
        reason = run(adopted, role, command)
        assert reason and reason.startswith(f"plumbline: the {role}'s Bash may run only") and "is none of these" in reason, (role, command)


SAFE = [
    "git diff --text",  # --text (treat files as text) is an option of its own; only the prefixes that could be --textconv are refused
    "git grep --text needle",
    "git log -p --text",
    "git diff --stat",
    "git diff --name-only",
    "git diff --name-status HEAD~1",
    "git diff --exit-code",
    "git diff --no-ext-diff",
    "git diff --no-textconv",
    "git log --no-ext-diff -p",
    "git log --no-textconv -p",
    "git diff --output-indicator-new=+ --output-indicator-old=-",
    "git diff --exclude-standard",
    "git ls-files --exclude-standard -o",
    "git ls-files -x '*.pyc'",
    "git log --oneline -5",
    "git log --grep=Output",
    "git log --grep=Order --oneline",
    "git log -S'Order' --oneline",
    "git log -SOrder",
    "git log -G'Object' --oneline",
    "git log -n5",
    "git log -n 5",
    "git log --format=%h",
    "git log --since=yesterday",
    "git log --author=Ola",
    "git blame -L 1,5 README.md",
    "git blame -e README.md",
    "git diff -U3",
    "git diff -w -b",
    "git diff --color-words",
    "git diff --submodule=log",
    "git show HEAD:README.md",
    "git status -uno",
    "git status --porcelain",
    "git status -sb",
    "git grep -n -e pattern",
    "git grep -e Output -- src",
    "git grep -h needle",
    "git grep --no-index needle",
    "git diff --no-index a b",
    "git rev-parse --short HEAD",
    "git merge-base main HEAD",
    "git diff -- --output=x",  # after `--` a word is a path
    "git grep needle -- -O",
    "git log -- --ext-diff",
    "git diff HEAD~1 -- 'src/O.py'",
]


@pytest.mark.parametrize("command", SAFE)
def test_the_options_that_only_look_like_them_are_still_read_only(adopted, command):
    for role in ("prosecutor", "verifier"):
        assert run(adopted, role, command) is None, (role, command)


def test_the_reviewers_abbreviations_really_do_run_a_program(adopted):
    # the premise of PG-GITEXEC-1: each of these ran the pager it was given, in a real repository, with the hook of 0.3.0 allowing it
    ran = []
    for n, flag in enumerate(("--open-files-in-pager", "--open-files-in-p", "--open-files", "--open", "--ope")):
        marker = adopted / f"pwn{n}"
        command = f"git grep {flag}='touch {marker.name};true' return"
        subprocess.run(["sh", "-c", command], cwd=adopted, capture_output=True)
        ran.append(marker.exists())
    if not all(ran):
        pytest.skip(f"this git does not take those abbreviations of --open-files-in-pager ({ran})")
    assert run(adopted, "prosecutor", "git grep --ope='touch x;true' return")


# ------------------------------------------------------ PG-GITEXEC-2: an assignment before git or a search tool

ASSIGNED = [
    "GIT_EXTERNAL_DIFF='touch pwn7;true' git diff",
    "env GIT_EXTERNAL_DIFF='touch pwn8;true' git diff",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=diff.external GIT_CONFIG_VALUE_0='touch pwn9;true' git diff",
    "GIT_PAGER='touch pwn10' git log",
    "GIT_DIR=/tmp/x git log",
    "GIT_ASKPASS=x git log",
    "GIT_CONFIG_PARAMETERS=\"'core.fsmonitor=x'\" git status",
    "FOO=bar git status",
    "A=1 B=2 git log",
    "PATH=/tmp/x:$PATH git diff",
    "timeout 5 env FOO=1 git diff",
    "env -i FOO=1 git status",
    "env FOO=1 git diff",
    "RIPGREP_CONFIG_PATH=x rg foo",
    "GREP_OPTIONS=x grep foo README.md",
    "LESS=x cat README.md",
    "FOO=1 ls",
    "FOO=1 find . -name x",
    "FOO=bar sed -n '1p' README.md",
    "env FOO=1 grep x README.md",
    "LC_ALL=C head README.md",
    "X=1 git diff | head",
    "git status; X=1 git diff",
    "echo done && FOO=1 git log",
]


@pytest.mark.parametrize("command", ASSIGNED)
def test_an_assignment_before_git_or_a_search_tool_is_refused(adopted, command):
    for role in READERS:
        reason = run(adopted, role, command)
        assert reason and "no VAR=value before them" in reason and "Run it without the assignment" in reason, (role, command)


def test_the_reviewers_env_reproductions_really_execute(adopted):
    write(adopted / "src" / "app.py", "def main():\n    return 2\n")  # something for git diff to show
    subprocess.run(["sh", "-c", "GIT_EXTERNAL_DIFF='touch pwn7;true' git diff"], cwd=adopted, capture_output=True)
    subprocess.run(
        ["sh", "-c", "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=diff.external GIT_CONFIG_VALUE_0='touch pwn9;true' git diff"], cwd=adopted, capture_output=True
    )
    assert (adopted / "pwn7").exists() and (adopted / "pwn9").exists()


def test_without_the_assignment_the_same_commands_are_allowed(adopted):
    for command in ("git diff", "git log", "env git diff", "rg foo", "grep foo README.md", "cat README.md", "head README.md", "ls", "find . -name x", "sed -n '1p' README.md"):
        for role in ("prosecutor", "planner"):
            assert run(adopted, role, command) is None, (role, command)


def test_a_bare_assignment_runs_no_command_and_is_still_allowed(adopted):
    for command in ("FOO=bar", "FOO=bar; git diff", "A=1 B=2"):
        assert run(adopted, "defender", command) is None, command


def test_an_assignment_before_a_declared_command_is_not_refused(adopted):
    with open(adopted / "plumbline.toml", "a", encoding="utf-8") as handle:
        handle.write('\n[commands]\ntest = "PYTHONPATH=src python3 -m pytest"\nlint = "ruff check"\n')
    commit_all(adopted, "commands")
    for command in ("PYTHONPATH=src python3 -m pytest -q", "env CI=1 python3 -m pytest", "python3 -m pytest", "CI=1 ruff check .", "timeout 60 env X=1 ruff check"):
        assert run(adopted, "verifier", command) is None, command


def test_the_main_session_and_the_builders_own_commands_are_not_held_by_these_rules(adopted):
    for command in ("GIT_PAGER=cat git log", "env FOO=1 git diff", "git diff --output=x.patch"):
        assert run(adopted, None, command) is None, command


def test_the_denial_of_an_assignment_names_the_command(adopted):
    reason = run(adopted, "prosecutor", "GIT_EXTERNAL_DIFF=x git diff")
    assert reason == (
        "plumbline: the prosecutor's read-only git and search tools run as they are, with no VAR=value before them "
        "(an environment variable can make git or grep run a program), so `GIT_EXTERNAL_DIFF=x git diff` is refused. Run it without the assignment."
    )


# ---------------------------------------------------------------------------- the option check, on its own


@pytest.mark.parametrize(
    "word,dangerous",
    [
        ("--output", True), ("--output=x", True), ("--outpu=x", True), ("--o", True), ("--open-files-in-pager=less", True), ("--ope", True), ("--ext-diff", True),
        ("--ex", True), ("--textconv", True), ("--t", True), ("--te", True), ("--textc", True), ("--text", False), ("--exec=x", True), ("--upload-pack=x", True), ("--receive-pack=x", True), ("-O", True), ("-Oless", True),
        ("-nO", True), ("-inOless", True), ("-nOless", True),
        ("--oneline", False), ("--output-indicator-new=+", False), ("--no-ext-diff", False), ("--no-textconv", False), ("--exit-code", False), ("--exclude=x", False),
        ("--stat", False), ("--name-only", False), ("--", False), ("-", False), ("-n", False), ("-SOrder", False), ("-GObject", False), ("-e", False), ("-5", False),
        ("--grep=Output", False), ("--format=%h", False), ("path/O.py", False),
    ],
)
def test_dangerous_git_option(word, dangerous):
    assert pre._dangerous_git_option(word) is dangerous, word


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["git", "diff"], True),
        (["git", "-C", ".", "diff"], True),
        (["git", "--no-pager", "log"], True),
        (["git", "diff", "--output=x"], False),
        (["git", "diff", "--ou=x"], False),
        (["git", "diff", "--", "--output=x"], True),
        (["git", "-c", "core.pager=x", "diff"], False),
        (["git", "--git-dir=x", "diff"], False),
        (["git", "push"], False),
        (["git"], False),
    ],
)
def test_git_read_only(argv, expected):
    assert pre._git_read_only(argv) is expected, argv
