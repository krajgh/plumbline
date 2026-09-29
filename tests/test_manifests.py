"""The plugin's manifests, hooks, skills and licence files."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers import REPO, clean_env

PLUGIN = REPO / ".claude-plugin" / "plugin.json"
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"
HOOKS = REPO / "hooks" / "hooks.json"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def frontmatter(path):
    """A SKILL.md's frontmatter as a dict of single-line `key: value` pairs, and its body."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: the frontmatter must open on the first line"
    head, sep, body = text[4:].partition("\n---\n")
    assert sep, f"{path}: the frontmatter is never closed"
    fields = {}
    for line in head.splitlines():
        key, colon, value = line.partition(": ")
        assert colon and key and not key.startswith(" "), f"{path}: not a simple 'key: value' line: {line!r}"
        assert key not in fields, f"{path}: {key} appears twice"
        fields[key] = value
    return fields, body


# --- plugin.json and marketplace.json


def test_plugin_json_says_what_the_task_specified():
    plugin = load(PLUGIN)
    assert plugin["name"] == "plumbline"
    assert plugin["version"] == "0.1.0"
    assert plugin["author"] == {"name": "krajgh", "url": "https://github.com/krajgh"}
    assert plugin["homepage"] == "https://github.com/krajgh/plumbline"
    assert plugin["repository"] == "https://github.com/krajgh/plumbline"
    assert plugin["license"] == "Apache-2.0"
    assert plugin["dependencies"] == [{"name": "ponytail", "marketplace": "ponytail"}]
    assert isinstance(plugin["description"], str) and plugin["description"].strip()


def test_the_plugin_description_is_one_sentence():
    description = load(PLUGIN)["description"]
    assert description.endswith(".") and len(re.findall(r"[.!?](?:\s|$)", description)) == 1


def test_the_ponytail_dependency_is_unpinned():
    # ponytail tags vX.Y.Z, not ponytail--vX.Y.Z, so a version range could not resolve
    [dependency] = load(PLUGIN)["dependencies"]
    assert "version" not in dependency


def test_marketplace_json_is_the_plumbline_marketplace_with_the_plugin_at_its_root():
    market = load(MARKETPLACE)
    assert market["name"] == "plumbline"
    assert market["owner"] == {"name": "krajgh"}
    assert market["allowCrossMarketplaceDependenciesOn"] == ["ponytail"]
    assert market["description"].strip()
    [entry] = market["plugins"]
    assert entry["name"] == load(PLUGIN)["name"]  # an entry name that differs from the manifest name breaks installs
    assert entry["source"] == "./"
    assert entry["description"].strip()


def test_the_dependency_marketplace_is_on_the_allowlist():
    dependency = load(PLUGIN)["dependencies"][0]
    assert dependency["marketplace"] in load(MARKETPLACE)["allowCrossMarketplaceDependenciesOn"]


# --- hooks


def hook_commands():
    for event, groups in load(HOOKS)["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                yield event, group, hook


def test_hooks_json_registers_session_start_with_no_matcher_and_a_10_second_timeout():
    hooks = load(HOOKS)["hooks"]
    assert list(hooks) == ["SessionStart"]
    [group] = hooks["SessionStart"]
    assert "matcher" not in group
    [hook] = group["hooks"]
    assert hook["type"] == "command" and hook["timeout"] == 10


def test_every_hook_command_ends_in_or_true():
    commands = [hook["command"] for _, _, hook in hook_commands()]
    assert commands
    for command in commands:
        assert command.endswith(" || true"), command


def test_the_session_start_command_runs_the_script_from_the_plugin_root_quoted():
    [(_, _, hook)] = list(hook_commands())
    assert hook["command"] == 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_start.py" || true'
    assert (REPO / "scripts" / "session_start.py").is_file()


def test_the_hook_command_runs_under_sh_from_a_plugin_root_with_a_space(tmp_path, home):
    # exactly as Claude Code runs it: the command string through a shell, the plugin root in the environment
    root = tmp_path / "with space" / "plumbline"
    for name in ("scripts", "schemas", "pipeline"):
        shutil.copytree(REPO / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    project = tmp_path / "project"
    project.mkdir()
    [(_, _, hook)] = list(hook_commands())
    result = subprocess.run(
        hook["command"], shell=True, cwd=project, capture_output=True, text=True,
        env=clean_env(home, CLAUDE_PLUGIN_ROOT=str(root), CLAUDE_PROJECT_DIR=str(project), GIT_CEILING_DIRECTORIES=str(tmp_path)),
        input='{"hook_event_name": "SessionStart", "source": "startup"}',
    )
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "plumbline:subagent-discipline" in context


def test_the_or_true_keeps_a_broken_hook_from_failing_the_session(tmp_path, home):
    [(_, _, hook)] = list(hook_commands())
    result = subprocess.run(
        hook["command"], shell=True, cwd=tmp_path, capture_output=True, text=True,
        env=clean_env(home, CLAUDE_PLUGIN_ROOT=str(tmp_path / "nowhere")),
    )
    assert result.returncode == 0  # the script was not found, and the hook still exits 0


# --- skills


def skill_dirs():
    return sorted(p.parent for p in (REPO / "skills").glob("*/SKILL.md"))


def test_the_two_skills_exist():
    assert [d.name for d in skill_dirs()] == ["init", "subagent-discipline"]


@pytest.mark.parametrize("directory", skill_dirs(), ids=lambda d: d.name)
def test_skills_have_valid_frontmatter(directory):
    fields, body = frontmatter(directory / "SKILL.md")
    assert fields["name"] == directory.name
    assert fields["description"].strip()
    assert ": " not in fields["description"], "a colon and space in a plain YAML scalar breaks the frontmatter"
    assert not fields["description"].startswith(("'", '"', "[", "{", "&", "*", "!", "|", ">", "%", "@", "`", "#"))
    assert " #" not in fields["description"]
    assert body.strip()


def test_the_init_skill_is_user_invocable_only_and_may_run_python():
    fields, body = frontmatter(REPO / "skills" / "init" / "SKILL.md")
    assert fields["disable-model-invocation"] == "true"
    assert fields["allowed-tools"] == "Bash(python3 *)"
    assert set(fields) == {"name", "description", "disable-model-invocation", "allowed-tools"}
    assert 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plumbline.py" init' in body
    assert "plumbline.toml" in body and ".gitignore" in body and "commit" in body.lower()
    assert "!`" not in body, "a bang and backtick would run a command when the skill loads"


def test_the_discipline_skill_keeps_the_wording_it_moved_with():
    fields, body = frontmatter(REPO / "skills" / "subagent-discipline" / "SKILL.md")
    assert fields["name"] == "subagent-discipline"
    assert "disable-model-invocation" not in fields  # Claude loads it when the work begins
    for heading in ("# Appropriately sized subagents, always", "## Sizing", "## Briefs", "## Shared files: map and reduce", "## Running them"):
        assert heading in body
    assert "smaller models substitute their own model name" in body


# --- licence and notice


def test_license_is_the_canonical_apache_text():
    text = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert text.lstrip().startswith("Apache License\n")
    assert "Version 2.0, January 2004" in text
    assert "http://www.apache.org/licenses/" in text
    assert text.count("END OF TERMS AND CONDITIONS") == 1
    assert text.endswith("limitations under the License.\n")
    assert len(text) == 11357


def test_notice_is_exactly_the_specified_text():
    assert (REPO / "NOTICE").read_text(encoding="utf-8") == (
        "plumbline\n"
        "Copyright 2026 Krishnaraj Ajay Gharpure\n"
        "\n"
        "This product includes software developed by Krishnaraj Ajay Gharpure (https://github.com/krajgh).\n"
    )


def test_gitignore_ignores_python_caches():
    lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "__pycache__/" in lines and ".pytest_cache/" in lines


# --- layout and hygiene


def test_the_layout_matches_the_spec():
    for path in (
        ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "LICENSE", "NOTICE", "README.md",
        "hooks/hooks.json", "scripts/plumbline.py", "scripts/session_start.py", "pipeline/default.toml",
        "skills/subagent-discipline/SKILL.md", "skills/init/SKILL.md",
    ):
        assert (REPO / path).is_file(), path
    assert not (REPO / "agents").exists()  # agents arrive in phase 2
    assert sorted(p.name for p in (REPO / "schemas").glob("*.json")) == sorted(
        f"{n}.json" for n in ("change_class", "spec", "tests_record", "build_note", "verify_record", "review_record", "pass_record", "override_record")
    )


def tracked_or_untracked_text_files():
    listed = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True, text=True,
    )
    if listed.returncode != 0:  # not a checkout: walk the tree instead
        return [p for p in REPO.rglob("*") if p.is_file() and "__pycache__" not in p.parts and ".git" not in p.parts]
    return [REPO / name for name in listed.stdout.split("\0") if name]


def test_nothing_names_a_private_project_person_or_path():
    # built from pieces so that this file does not contain what it forbids; a maintainer's own
    # private words, one per line, are read from a file outside the repository when it exists
    forbidden = ["/ho" + "me/", "@" + "gmail"]
    private = Path(os.environ.get("PLUMBLINE_PRIVATE_WORDS") or Path.home() / ".config" / "plumbline" / "private-words.txt")
    if private.is_file():
        forbidden += [w.strip() for w in private.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")]
    offenders = []
    for path in tracked_or_untracked_text_files():
        if path.suffix in (".pyc",) or path == REPO / "tests" / "test_manifests.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        offenders += [f"{path.relative_to(REPO)}: {word}" for word in forbidden if word in text]
    assert offenders == []


def test_no_file_uses_an_absolute_home_path_or_an_env_file():
    names = [p.name for p in tracked_or_untracked_text_files()]
    assert not [n for n in names if n == ".env" or n.startswith(".env.")]
