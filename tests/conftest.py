"""Fixtures: an isolated environment, a throw-away git repository, and CLI runners."""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import helpers  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """Nothing a test does may see the developer's home, git config or Claude
    Code session: HOME is a throw-away directory, git has a fixed identity, and
    the variables Claude Code sets are removed."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    for key, value in helpers.GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    for key in ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_ENV_FILE", "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture
def home(isolated_environment):
    return isolated_environment


@pytest.fixture
def repo(tmp_path):
    """A git repository on branch `feature`. `main` and `feature` both point at the
    base commit, which holds README.md and src/app.py."""
    root = tmp_path / "repo"
    root.mkdir()
    helpers.git(root, "init", "-q", "-b", "main")
    helpers.write(root / "README.md", "# demo\n")
    helpers.write(root / "src" / "app.py", "def main():\n    return 1\n")
    helpers.commit_all(root, "base")
    helpers.git(root, "checkout", "-q", "-b", "feature")
    return root


@pytest.fixture
def run_cli(home):
    """Run scripts/plumbline.py in a subprocess with a clean environment."""

    def run(*args, cwd, **env_extra):
        return helpers.run_script(helpers.CLI, args, cwd, home, **env_extra)

    return run


@pytest.fixture
def run_hook(home):
    """Run scripts/session_start.py in a subprocess with a clean environment."""

    def run(cwd, **env_extra):
        return helpers.run_script(helpers.SESSION_START, [], cwd, home, **env_extra)

    return run


def _as_stdin(payload) -> str:
    return payload if isinstance(payload, str) else json.dumps(payload)


@pytest.fixture
def run_stop(home):
    """Run scripts/subagent_stop.py with a hook payload (a dict, or a string as it is) on stdin."""

    def run(payload, cwd, **env_extra):
        return helpers.run_script(helpers.SUBAGENT_STOP, [], cwd, home, stdin=_as_stdin(payload), **env_extra)

    return run


@pytest.fixture
def run_pre(home):
    """Run scripts/pre_tool_use.py with a hook payload (a dict, or a string as it is) on stdin."""

    def run(payload, cwd, **env_extra):
        return helpers.run_script(helpers.PRE_TOOL_USE, [], cwd, home, stdin=_as_stdin(payload), **env_extra)

    return run
