"""Plain helpers shared by the tests (fixtures live in conftest.py)."""
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
CLI = SCRIPTS / "plumbline.py"
SESSION_START = SCRIPTS / "session_start.py"
SUBAGENT_STOP = SCRIPTS / "subagent_stop.py"
PRE_TOOL_USE = SCRIPTS / "pre_tool_use.py"
DEFAULT_TOML = REPO / "pipeline" / "default.toml"

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test User",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test User",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def clean_env(home: Path, **extra) -> dict:
    """A minimal environment for a subprocess: a throw-away HOME, no git config
    of the developer's, and none of the variables Claude Code sets."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        **GIT_IDENTITY,
    }
    env.update(extra)
    return env


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def numbered(count: int, prefix: str = "line") -> str:
    """`count` lines of text."""
    return "".join(f"{prefix} {i}\n" for i in range(count))


def git(root: Path, *args: str) -> str:
    """Run git in `root` (in the ambient environment, which the autouse
    fixture has isolated) and return its stdout."""
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


def commit_all(root: Path, message: str = "change") -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD").strip()


def default_pipeline() -> dict:
    """A fresh copy of the shipped default pipeline, ready to be mutated."""
    return tomllib.loads(DEFAULT_TOML.read_text(encoding="utf-8"))


def run_script(script: Path, args, cwd: Path, home: Path, stdin: str | None = None, **env_extra) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=clean_env(home, **env_extra),
        input=stdin,
    )


# Rules are stated as what to do: a prohibition word in a prompt or a skill is a sign that a rule is left to prose. What a hook
# enforces is not repeated as a prohibition, and what only judgement can apply is still phrased as an instruction.
PROHIBITION = re.compile(r"\b(never|don't|do not|must not|cannot|can't|won't|shouldn't|forbidden|prohibited|not allowed)\b", re.I)


def prose_lines(text: str):
    """The lines of a markdown text outside its fenced code blocks."""
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced:
            yield line


def process_gone(pid: int, wait: float = 3.0) -> bool:
    """Has the process ended? A zombie that nobody has reaped yet counts as ended (a container may have no init to reap it)."""
    import time

    deadline = time.monotonic() + wait
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            state = ""
        if state == "Z":
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
