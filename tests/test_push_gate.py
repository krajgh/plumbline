"""The push gate holds where the 2b review found it open.

PG-CD (a cd that only seems to move the shell), PG-DOTGIT (git cannot say which repository: inside .git, or a repository it does not
trust), PG-REF (the gate looked at HEAD, never at the ref a push names), PG-ALIAS (`git -c alias.p=push p`, and aliases from the git
config), C-19 (the hook's own git calls take no optional locks), and the dry-run and `gh pr create` parsing that let a value such as
`-o -n` pass for a flag. Pushes that really happen go to a bare repository under tmp_path."""
import os
import subprocess
from pathlib import Path

import pytest

import plumbline as pl
import pre_tool_use as pre
from helpers import commit_all, git, write
from hookdata import add_origin, bash_payload, denial, remote_ref
from rundata import adopt, genuine_pass

KEY = "sk-" + "ant-" + "a" * 24


@pytest.fixture(autouse=True)
def hook_errors_surface(monkeypatch):
    monkeypatch.setenv("PLUMBLINE_HOOK_DEBUG", "1")


@pytest.fixture
def adopted(repo):
    adopt(repo)
    return repo


@pytest.fixture
def origin(adopted, tmp_path):
    return add_origin(adopted, tmp_path)


def head(repo):
    return git(repo, "rev-parse", "HEAD").strip()


def gate(command, cwd, **fields):
    """The hook's reason to deny `command` run in `cwd`, or None."""
    return pre.decide(bash_payload(cwd, command, **fields))


def commit_on(repo, branch, name, text="x\n", base="main"):
    """A commit on a new branch off `base`, unreviewed; leaves `feature` checked out again."""
    git(repo, "checkout", "-q", "-b", branch, base)
    write(repo / name, text)
    sha = commit_all(repo, f"work on {branch}")
    git(repo, "checkout", "-q", "feature")
    return sha


# ----------------------------------------------------------------------------------------- PG-CD

CD_FORMS = [
    "(cd /tmp && true); git push origin feature",
    "pushd /tmp >/dev/null; popd >/dev/null; git push origin feature",
    "cd /tmp; cd -; git push origin feature",
    "false && cd /tmp; git push origin feature",
    "true || cd /tmp; git push origin feature",
    "(cd /tmp; true) && git push origin feature",
    "{ cd /tmp; }; cd -; git push origin feature",
    "bash -c 'cd /tmp && true'; git push origin feature",
    "cd nowhere-at-all || true; git push origin feature",
    "cd /tmp; cd ~; cd - >/dev/null; git push origin feature",
]


@pytest.mark.parametrize("command", CD_FORMS)
def test_a_cd_that_does_not_really_move_the_shell_does_not_silence_the_gate(adopted, command):
    reason = gate(command, adopted)
    assert reason and f"HEAD {head(adopted)[:7]} has no pass or override record" in reason, command


@pytest.mark.parametrize("command", CD_FORMS[:5])
def test_the_reviewers_pg_cd_forms_really_push_when_nothing_stops_them(adopted, origin, command):
    # the reproduction is a real one: run without the hook, each form publishes HEAD
    result = subprocess.run(["bash", "-c", command], cwd=adopted, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert remote_ref(origin, "feature") == head(adopted)
    git(adopted, "push", "-q", "origin", ":feature")


@pytest.mark.parametrize("command", CD_FORMS)
def test_with_a_pass_for_head_every_form_goes_through(adopted, command):
    genuine_pass(adopted)
    assert gate(command, adopted) is None, command


def test_a_push_from_a_directory_outside_any_adopted_repository_stays_free(adopted, tmp_path, monkeypatch):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for command in ("git push origin feature", "cd /tmp; git push", "(cd /tmp && git push origin feature)", "cd /tmp && cd - && git push"):
        assert gate(command, outside) is None, command


def test_every_directory_the_line_may_run_in_counts_even_where_the_push_runs_elsewhere(adopted, tmp_path):
    # a cd may fail, be undone or come from a branch not taken: the gate takes each place the line may be in, and denies if any is uncovered
    reason = gate("cd /tmp && git push origin feature", adopted)
    assert reason and "has no pass or override record" in reason
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert gate(f"cd {adopted} && git push origin feature", outside)
    assert gate(f"cd {adopted}/src && git push", outside)
    assert gate(f"pushd {adopted} >/dev/null; git push", outside)
    assert gate(f"env -C {adopted} git push", outside)
    assert gate(f"git -C {adopted} push", outside)
    assert gate(f"(cd {adopted}; git push)", outside)
    assert gate(f"bash -c 'cd {adopted} && git push'", outside)
    assert gate(f"cd {adopted}; cd src; cd ..; git push", outside)
    assert gate(f"cd nothing; cd {adopted}; git push", outside)


def test_git_dir_and_work_tree_name_the_repository_a_push_acts_in(adopted, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for command in (
        f"git --git-dir={adopted}/.git --work-tree={adopted} push origin feature",
        f"git --git-dir {adopted}/.git push origin feature",
        f"GIT_DIR={adopted}/.git git push origin feature",
        f"GIT_WORK_TREE={adopted} GIT_DIR={adopted}/.git git push",
        f"env GIT_DIR={adopted}/.git git push",
    ):
        assert gate(command, outside), command


def test_a_git_dir_set_or_exported_earlier_in_the_line_names_the_repository_a_later_push_acts_in(adopted, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    for command in (
        f"export GIT_DIR={adopted}/.git; git push origin feature",
        f"export GIT_DIR={adopted}/.git GIT_WORK_TREE={adopted}; git push",
        f"GIT_DIR={adopted}/.git; export GIT_DIR; git push origin feature",
        f"declare -x GIT_WORK_TREE={adopted}; git push origin feature",
    ):
        assert gate(command, outside), command


def test_the_dashed_git_commands_and_windows_exe_names_are_git_and_gh(adopted):
    for command in (
        "git-push origin feature",
        "/usr/lib/git-core/git-push origin feature",
        "git.exe push origin feature",
        "GIT.EXE push origin feature",
        '"C:/Program Files/Git/cmd/git.exe" push',
        "gh.exe pr create --fill",
        "gh pr new --fill",
        "gh --repo o/r pr new",
    ):
        assert gate(command, adopted), command
    assert gate("git-status", adopted) is None and gate("gh pr new --dry-run", adopted) is None
    write(adopted / "k.py", f"KEY = '{KEY}'\n")
    git(adopted, "add", "k.py")
    assert "adds a key-shaped secret" in gate("git-commit -m x", adopted)


def test_the_commit_checks_follow_every_directory_too(adopted):
    write(adopted / "k.py", f"KEY = '{KEY}'\n")
    git(adopted, "add", "k.py")
    for command in ("(cd /tmp && true); git commit -m x", "cd /tmp; cd -; git commit -m x", "false && cd /tmp; git commit -m x"):
        reason = gate(command, adopted)
        assert reason and "adds a key-shaped secret: k.py:1" in reason, command


def test_the_directories_of_a_line_are_the_start_and_every_cd_target():
    (action,) = pre.analyze("(cd /tmp && true); git push origin feature", Path("/w/repo"))
    assert action.kind == "push" and Path("/w/repo") in action.dirs and Path("/tmp") in action.dirs
    (action,) = pre.analyze("cd a; cd b; git push", Path("/w/repo"))
    assert {str(d) for d in action.dirs} >= {"/w/repo", "/w/repo/a", "/w/repo/b", "/w/repo/a/b"}
    (action,) = pre.analyze("git -C ../x -C y push", Path("/w/repo"))
    assert str(action.cwd) == "/w/x/y" and [str(d) for d in action.dirs] == ["/w/x/y"]
    (action,) = pre.analyze("cd /elsewhere && git -C sub push", Path("/w/repo"))
    assert {str(d) for d in action.dirs} == {"/w/repo/sub", "/elsewhere/sub"}
    dirs = {str(d) for d in pre.analyze("bash -c 'cd deeper && git push'", Path("/w/repo"))[0].dirs}
    assert dirs == {"/w/repo", "/w/repo/deeper"}


def test_a_line_that_names_hundreds_of_directories_is_still_read():
    line = "; ".join(f"cd d{i}" for i in range(300)) + "; git push"
    (action,) = pre.analyze(line, Path("/w/repo"))
    assert Path("/w/repo") in action.dirs and len(action.dirs) <= pre.MAX_DIRS


# -------------------------------------------------------------------------------------- PG-DOTGIT


@pytest.mark.parametrize("where", [".git", ".git/hooks", ".git/refs/heads", ".git/objects/pack"])
def test_a_push_from_inside_the_git_directory_is_gated(adopted, where):
    assert (adopted / where).is_dir()
    reason = gate("git push origin feature", adopted / where)
    assert reason and f"HEAD {head(adopted)[:7]} has no pass or override record" in reason


def test_git_really_cannot_say_where_it_is_inside_dot_git_and_the_gate_still_can(adopted):
    result = subprocess.run(["git", "-C", str(adopted / ".git"), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    assert result.returncode != 0  # the premise of the finding
    assert pre.adopted_root(pl, adopted / ".git" / "hooks") == adopted


def test_a_push_from_inside_dot_git_passes_when_head_is_covered(adopted):
    genuine_pass(adopted)
    assert gate("git push origin feature", adopted / ".git") is None


def test_the_git_directory_of_a_repository_that_has_not_adopted_plumbline_is_nothing_to_it(repo):
    assert gate("git push origin feature", repo / ".git") is None
    assert pre.adopted_root(pl, repo / ".git") is None


def test_a_repository_git_will_not_trust_is_gated_by_its_plumbline_toml(adopted, monkeypatch):
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")  # git's own "dubious ownership" refusal
    if subprocess.run(["git", "-C", str(adopted), "rev-parse", "--show-toplevel"], capture_output=True).returncode == 0:
        pytest.skip("this git does not know GIT_TEST_ASSUME_DIFFERENT_OWNER")
    assert pre.adopted_root(pl, adopted) == adopted
    reason = gate("git push origin feature", adopted)
    assert reason and "git cannot read" in reason and "ownership" in reason


def test_the_hook_process_denies_under_dubious_ownership_too(run_pre, adopted):
    probe = subprocess.run(["git", "-C", str(adopted), "rev-parse", "--show-toplevel"], capture_output=True, env={**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"})
    if probe.returncode == 0:
        pytest.skip("this git does not know GIT_TEST_ASSUME_DIFFERENT_OWNER")
    payload = bash_payload(adopted, "git push origin feature")
    assert denial(run_pre(payload, adopted, GIT_TEST_ASSUME_DIFFERENT_OWNER="1"))
    assert "has no pass or override record" in denial(run_pre(bash_payload(adopted / ".git", "git push"), adopted / ".git"))


def test_a_nested_directory_without_a_plumbline_toml_above_it_is_not_adopted(tmp_path):
    (tmp_path / "plain" / ".git").mkdir(parents=True)
    assert pre._adopted_above(tmp_path / "plain" / ".git") is None


# ------------------------------------------------------------------------------------------ PG-REF


@pytest.fixture
def world(adopted, origin):
    """HEAD (feature) is covered by a pass; `other` and `other2` hold unreviewed commits; tag `v1` sits on `other`."""
    other = commit_on(adopted, "other", "secret.py", "TOKEN = 'unreviewed'\n")
    commit_on(adopted, "other2", "more.py", "X = 'also unreviewed'\n")
    git(adopted, "tag", "v1", other)
    git(adopted, "tag", "-a", "-m", "annotated", "v2", other)
    write(adopted / "README.md", "# demo\nreviewed docs change\n")
    commit_all(adopted, "reviewed docs")
    genuine_pass(adopted)
    git(adopted, "tag", "covered-tag")
    return adopted


def test_the_ref_a_push_names_is_what_needs_a_pass_not_only_head(world):
    reason = gate("git push origin other", world)
    assert reason and reason.startswith("plumbline: other (") and "has no pass or override record" in reason and "Check other out" in reason
    assert gate("git push origin feature", world) is None  # the covered one, as before


@pytest.mark.parametrize(
    "command",
    [
        "git push origin other",
        "git push --force origin other:main",
        "git push origin other:main",
        "git push origin +other:main",
        "git push origin refs/heads/other",
        "git push origin refs/heads/other:refs/heads/x",
        "git push origin feature other",
        "git push origin HEAD other2",
        "git push origin feature:x other:y",
        "git push -u origin other",
        "git push --force-with-lease=other:abc origin other",
        "git push origin v1",
        "git push origin tag v1",
        "git push origin v2",
        "git push origin refs/tags/v1",
        "git push origin other~0",
        "git push origin other -- -n",
    ],
)
def test_a_push_that_publishes_an_unreviewed_ref_is_denied_while_head_is_covered(world, command):
    assert gate(command, world), command


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin",
        "git push origin feature",
        "git push origin HEAD",
        "git push origin @",
        "git push origin feature:feature",
        "git push origin HEAD:refs/heads/x",
        "git push origin feature:main",
        "git push -u origin feature",
        "git push --force-with-lease origin feature",
        "git push origin +feature",
        "git push origin refs/heads/feature",
        "git push origin covered-tag",
        "git push origin tag covered-tag",
        "git push origin feature covered-tag",
        "git push --follow-tags origin feature",
        "git push --no-verify origin feature",
        "git push origin feature -o ci.skip",
        "git push origin feature --push-option=ci.skip",
    ],
)
def test_a_push_of_the_covered_ref_goes_through(world, command):
    assert gate(command, world) is None, command


@pytest.mark.parametrize("option", ["--all", "--mirror", "--tags", "--branches", "--al", "--mir", "--tag", "--branch"])
def test_a_push_of_many_refs_is_denied_with_the_way_out(world, option):
    reason = gate(f"git push {option} origin", world)
    assert reason and "push one reviewed branch at a time" in reason, option
    assert gate(f"git push origin {option}", world)
    assert gate(f"git push {option}", world)


def test_a_refspec_with_a_wildcard_is_denied_as_a_push_of_many(world):
    for refspec in ("refs/heads/*:refs/heads/*", "refs/heads/*", "+refs/heads/*:refs/remotes/x/*", "refs/heads/o?her", "refs/heads/[ab]"):
        reason = gate(f"git push origin '{refspec}'", world)
        assert reason and "push one reviewed branch at a time" in reason, refspec


def test_many_refs_are_denied_before_the_head_check_and_only_in_adopted_repositories(repo, adopted, tmp_path, monkeypatch):
    genuine_pass(adopted)
    assert gate("git push --all origin", adopted)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert gate("git push --all origin", outside) is None


@pytest.mark.parametrize(
    "command",
    [
        "git push origin :other",
        "git push origin :refs/heads/other",
        "git push origin :other :other2",
        "git push origin --delete other",
        "git push --delete origin other other2",
        "git push -d origin other",
        "git push origin -d other",
        "git push --del origin other",
        "git push origin --delete tag v1",
        "git push origin :refs/tags/v1",
    ],
)
def test_deleting_a_remote_ref_publishes_no_commit_and_is_allowed(world, command):
    assert gate(command, world) is None, command


def test_a_deletion_is_allowed_even_when_head_is_not_covered_and_in_a_line_that_commits(adopted):
    assert gate("git push origin :feature", adopted) is None
    assert gate("git commit -m x && git push origin --delete feature", adopted) is None
    assert gate("git push origin :feature feature", adopted)  # a deletion beside a push is still a push


def test_a_mixed_deletion_and_push_needs_the_pushed_ref_covered(world):
    assert gate("git push origin :other feature", world) is None
    assert gate("git push origin :feature other", world)


def test_a_dry_run_or_help_publishes_nothing(world):
    for command in ("git push --dry-run origin other", "git push -n origin other", "git push origin other --dry-run", "git push -fn origin other", "git push --dry origin other",
                    "git push -h", "git push --help", "git push origin other --help"):
        assert gate(command, world) is None, command


def test_a_value_that_looks_like_a_flag_is_a_value_not_a_flag(adopted):
    # `-o -n` sends the push option "-n": it was read as the dry-run flag and let a real push through
    for command in ("git push origin feature -o '-n'", "git push --push-option='-n' origin feature", "git push --push-option -n origin feature", "git push -o '- note' origin feature",
                    "git push origin feature -o -h", "git push --repo -n origin feature", "git push origin feature --receive-pack=-n", "git push origin feature --exec -n"):
        reason = gate(command, adopted)
        assert reason and "has no pass or override record" in reason, command
    assert gate("git push origin feature -- -n", adopted)  # after `--` every word is a ref


def test_the_hook_still_reads_a_push_it_cannot_resolve_by_head(adopted):
    for command in ("git push origin $BRANCH", "git push origin $(git branch --show-current)", "git push origin nonexistent-branch", "for b in a b; do git push origin $b; done"):
        reason = gate(command, adopted)
        assert reason and f"HEAD {head(adopted)[:7]}" in reason, command
    genuine_pass(adopted)
    assert gate("git push origin $BRANCH", adopted) is None


def test_another_branch_with_its_own_pass_may_be_pushed_while_head_is_not_covered(adopted, origin):
    git(adopted, "checkout", "-q", "-b", "reviewed", "main")
    write(adopted / "README.md", "# demo\nreviewed on its own branch\n")
    commit_all(adopted, "reviewed")
    genuine_pass(adopted)
    git(adopted, "checkout", "-q", "feature")  # HEAD is the adoption commit again, and has no pass
    assert gate("git push origin reviewed", adopted) is None
    assert gate("git push origin feature", adopted)
    assert gate("git push origin", adopted)
    assert gate("git push origin reviewed feature", adopted)


def test_an_override_for_the_ref_covers_it(world):
    other = git(world, "rev-parse", "other").strip()
    assert gate("git push origin other", world)
    record = pl.write_json_atomic
    from samples import sample

    override = sample("override_record")
    override["commit"] = other
    record(world / ".plumbline" / "pass" / f"{other}.override.json", override)
    assert gate("git push origin other", world) is None
    assert gate("git push origin other2", world)


def test_pushes_that_the_gate_lets_through_really_publish_and_the_denied_ones_are_the_ones_that_would_have_leaked(world, origin):
    assert gate("git push origin feature", world) is None
    git(world, "push", "-q", "origin", "feature")
    assert remote_ref(origin, "feature") == head(world)
    assert gate("git push origin other:main", world)  # would have replaced main with unreviewed work
    assert remote_ref(origin, "main") != git(world, "rev-parse", "other").strip()


def test_a_push_that_follows_a_commit_is_still_denied_whatever_it_names(world):
    reason = gate("git commit --allow-empty -m more && git push origin feature", world)
    assert reason and "changes HEAD" in reason
    assert gate("git commit --allow-empty -m more && git push origin other", world)


def test_the_head_the_message_names_is_head_and_another_ref_is_named_by_its_own_name(world):
    other = git(world, "rev-parse", "other").strip()
    assert f"other ({other[:7]}) has no pass or override record" in gate("git push origin other", world)
    write(world / "x.txt", "x\n")
    commit_all(world, "uncovered head")
    assert f"HEAD {head(world)[:7]} has no pass or override record" in gate("git push origin feature", world)


# ---------------------------------------------------------------------------------------- PG-ALIAS


def test_an_alias_given_with_dash_c_is_expanded(adopted):
    for command in (
        "git -c alias.p=push p origin feature",
        "git -c alias.P=push P origin feature",
        "git -c alias.ship='push origin HEAD' ship",
        "git -c alias.p='push --force' p origin feature",
        "git -c 'alias.p=!git push' p origin feature",
        "git -c 'alias.p=!f() { git push \"$@\"; }; f' p origin feature",
        "git -c alias.a=b -c alias.b=push a origin feature",
        "git --no-pager -c alias.p=push p origin feature",
        "git -C . -c alias.p=push p origin feature",
    ):
        reason = gate(command, adopted)
        assert reason and "has no pass or override record" in reason, command


def test_the_reviewers_alias_reproduction_really_pushes_when_nothing_stops_it(adopted, origin):
    result = subprocess.run(["git", "-c", "alias.p=push", "p", "origin", "feature"], cwd=adopted, capture_output=True, text=True)
    assert result.returncode == 0 and remote_ref(origin, "feature") == head(adopted)


def test_an_alias_from_the_repositorys_or_the_users_git_config_is_expanded(adopted, tmp_path, monkeypatch):
    git(adopted, "config", "alias.p", "push")
    assert gate("git p origin feature", adopted)
    assert gate("git p", adopted)
    git(adopted, "config", "alias.ship", "push origin HEAD")
    assert gate("git ship", adopted)
    git(adopted, "config", "alias.sh", "!git push")
    assert gate("git sh origin feature", adopted)
    git(adopted, "config", "alias.loop", "p")
    assert gate("git loop origin feature", adopted)  # an alias of an alias
    user = tmp_path / "user-gitconfig"
    write(user, "[alias]\n\tgo = push\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(user))
    assert gate("git go origin feature", adopted)


def test_the_alias_is_looked_up_in_the_repository_the_command_acts_in(adopted, tmp_path, monkeypatch):
    git(adopted, "config", "alias.zzz", "push")  # only this repository's own config has it
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert gate(f"git -C {adopted} zzz origin feature", outside)
    assert gate(f"cd {adopted} && git zzz origin feature", outside)
    assert gate("git zzz origin feature", outside) is None  # there `zzz` is no alias, and no adopted repository is in play


def test_a_shell_alias_that_mentions_push_is_a_push(adopted):
    for command in ("git -c 'alias.p=!echo push' p", "git -c 'alias.p=!sh -c \"exec git push\"' p", "git -c 'alias.p=!git push origin' p feature", "git -c 'alias.p=!true; git push' p"):
        assert gate(command, adopted), command
    assert gate("git -c 'alias.p=!echo hello' p", adopted) is None
    assert gate("git -c 'alias.p=!git status' p", adopted) is None


def test_aliases_that_are_not_a_push_are_not_denied(adopted):
    for command in ("git -c alias.st=status st", "git -c alias.d=diff d", "git -c alias.l='log --oneline' l", "git -c alias.p=pull p"):
        assert gate(command, adopted) is None, command


def test_an_alias_cannot_hide_a_builtin_command_and_git_says_so_too(adopted):
    assert gate("git -c alias.push=status push origin feature", adopted)  # git ignores an alias that hides one of its own commands
    assert gate("git -c alias.status=push status", adopted) is None


def test_an_alias_from_environment_config_variables_is_expanded(adopted):
    command = "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.p GIT_CONFIG_VALUE_0=push git p origin feature"
    assert gate(command, adopted)
    assert gate("env GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.p GIT_CONFIG_VALUE_0=push git p origin feature", adopted)
    assert gate("git --config-env=alias.p=WHATEVER p origin feature", adopted)  # the value is not known here: the worst is assumed
    assert gate("git --config-env alias.p=WHATEVER p origin feature", adopted)


def test_an_alias_that_commits_meets_the_commit_checks(adopted):
    write(adopted / "k.py", f"KEY = '{KEY}'\n")
    git(adopted, "add", "k.py")
    reason = gate("git -c alias.ci=commit ci -m x", adopted)
    assert reason and "adds a key-shaped secret" in reason


def test_an_alias_in_a_repository_that_has_not_adopted_plumbline_is_left_alone(repo):
    assert gate("git -c alias.p=push p origin feature", repo) is None


def test_looking_up_an_alias_is_done_only_for_a_command_git_does_not_have(adopted, tmp_path, monkeypatch):
    # a shim git that counts its `config --get` calls: builtin subcommands never ask
    shim = tmp_path / "shim"
    shim.mkdir()
    log = tmp_path / "asked.log"
    real = subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True).stdout.strip()
    (shim / "git").write_text(f'#!/bin/sh\ncase " $* " in *" config --get "*) echo "$*" >> "{log}" ;; esac\nexec {real} "$@"\n')
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    for command in ("git status", "git push origin feature", "git log --oneline", "git diff", "git commit -m x"):
        gate(command, adopted)
    assert not log.exists()
    gate("git zzz origin feature", adopted)
    assert log.exists() and "alias.zzz" in log.read_text()


# --------------------------------------------------------------------------------------- gh pr create


def test_gh_pr_create_is_a_push_of_head_however_its_body_reads(adopted):
    for command in (
        "gh pr create --fill",
        "gh pr create --title x --body '- item one'",
        "gh pr create -t x -b '- adds thing'",
        "gh pr create --title \"fix\" --body \"- Adds the thing\"",
        "gh pr create -t '-n'",
        "gh pr create --body --dry-run",
        "gh pr create --title --help",
        "gh pr create -B main -H feature -t 'x' -b '-- nothing'",
        "gh --repo o/r pr create --fill",
        "gh pr create --body-file -",
    ):
        reason = gate(command, adopted)
        assert reason and "opened for review" in reason, command


def test_gh_pr_create_is_not_a_push_when_it_is_a_dry_run_or_help(adopted):
    for command in ("gh pr create --dry-run", "gh pr create --fill --dry-run", "gh pr create --help", "gh pr create -h", "gh pr list", "gh pr view 3", "gh pr merge 3"):
        assert gate(command, adopted) is None, command


# --------------------------------------------------------------------------------------- C-19


def test_the_hooks_git_calls_take_no_optional_locks(adopted, tmp_path, monkeypatch):
    shim = tmp_path / "shim"
    shim.mkdir()
    log = tmp_path / "git-calls.log"
    real = subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True).stdout.strip()
    (shim / "git").write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexec {real} "$@"\n')
    (shim / "git").chmod(0o755)
    write(adopted / "k.py", "x = 1\n")
    git(adopted, "add", "k.py")
    write(adopted / "tests" / "t.py", "x = 1\n")
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")  # from here on, every git the hook starts is logged
    gate("git commit -am x", adopted)
    gate("git push origin feature", adopted)
    pre.decide(
        {"tool_name": "Grep", "agent_type": "plumbline:builder", "cwd": str(adopted), "tool_input": {"pattern": "x", "path": "src"}, "transcript_path": str(adopted / "t.jsonl")}
    )
    calls = [line for line in log.read_text().splitlines() if "rev-parse --show-toplevel" not in line]  # git_toplevel never refreshes an index
    assert calls
    for line in calls:
        assert "--no-optional-locks" in line, line


def test_a_look_by_the_hook_leaves_the_index_of_the_repository_alone(run_pre, adopted):
    (adopted / "src" / "app.py").touch()  # the file's stat data no longer matches the index: git would refresh it
    index = adopted / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    payload = bash_payload(adopted, "git commit -am x")
    assert denial(run_pre(payload, adopted)) is None
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    # the premise: a plain `git diff` does rewrite the index here
    (adopted / "src" / "app.py").touch()
    subprocess.run(["git", "-C", str(adopted), "diff", "HEAD"], capture_output=True)
    subprocess.run(["git", "-C", str(adopted), "status", "-s"], capture_output=True)
    assert (index.read_bytes(), index.stat().st_mtime_ns) != before


def test_the_hook_sets_the_environment_switch_for_whatever_git_it_starts(run_pre, adopted, tmp_path):
    shim = tmp_path / "shim"
    shim.mkdir()
    log = tmp_path / "env.log"
    real = subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True).stdout.strip()
    (shim / "git").write_text(f'#!/bin/sh\necho "$GIT_OPTIONAL_LOCKS" >> "{log}"\nexec {real} "$@"\n')
    (shim / "git").chmod(0o755)
    path = f"{shim}:{os.environ['PATH']}"
    assert denial(run_pre(bash_payload(adopted, "git push origin feature"), adopted, PATH=path))
    assert set(log.read_text().split()) == {"0"}


# ----------------------------------------------------------------------- the pieces, on their own


@pytest.mark.parametrize(
    "args,expect",
    [
        (["origin", "feature"], dict(repository="origin", refspecs=["feature"])),
        (["-u", "origin", "feature"], dict(repository="origin", refspecs=["feature"])),
        (["--force-with-lease=x:y", "origin", "a", "b"], dict(repository="origin", refspecs=["a", "b"])),
        (["-o", "n", "origin", "a"], dict(repository="origin", refspecs=["a"])),
        (["-on", "origin", "a"], dict(repository="origin", refspecs=["a"])),
        (["--push-option", "n", "origin", "a"], dict(repository="origin", refspecs=["a"])),
        (["--push-option=n", "origin", "a"], dict(repository="origin", refspecs=["a"])),
        (["--repo", "origin", "a"], dict(repository="a", refspecs=[])),
        (["--all", "origin"], dict(many="--all", repository="origin")),
        (["--mir", "origin"], dict(many="--mirror")),
        (["--tag"], dict(many="--tags")),
        (["-d", "origin", "a"], dict(delete=True, refspecs=["a"])),
        (["--del", "origin", "a"], dict(delete=True)),
        (["-n", "origin"], dict(dry_run=True)),
        (["-fn", "origin"], dict(dry_run=True)),
        (["--dry", "origin"], dict(dry_run=True)),
        (["-o", "-n", "origin"], dict(dry_run=False, repository="origin")),
        (["-h"], dict(help=True)),
        (["origin", "--", "-n"], dict(dry_run=False, refspecs=["-n"])),
        ([], dict(repository=None, refspecs=[])),
    ],
)
def test_parse_push_reads_flags_values_and_refs_apart(args, expect):
    spec = pre.parse_push(args)
    for name, value in expect.items():
        assert getattr(spec, name) == value, (args, name)


@pytest.mark.parametrize(
    "words,expect",
    [
        (["git", "-C", "a", "-c", "x.y=z", "push"], dict(sub="push", chdirs=["a"], config={"x.y": "z"})),
        (["git", "--git-dir=/g", "--work-tree", "/w", "status"], dict(sub="status", trees=["/g", "/w"])),
        (["git", "--config-env=alias.p=E", "p"], dict(sub="p", config_env=["alias.p"])),
        (["git", "--no-pager", "-P", "log"], dict(sub="log")),
        (["git"], dict(sub=None)),
        (["git", "-C"], dict(sub=None)),
    ],
)
def test_parse_git_reads_what_comes_before_the_subcommand(words, expect):
    call = pre.parse_git(words)
    for name, value in expect.items():
        assert getattr(call, name) == value, (words, name)


# ------------------------------------------------------------------- what a push publishes when the refspec does not say


def unset(repo, key):
    subprocess.run(["git", "-C", str(repo), "config", "--unset-all", key], capture_output=True)


@pytest.mark.parametrize("command", ["git push origin :", "git push origin +:", "git push --force origin :", "git push -u origin +:"])
def test_a_bare_colon_refspec_pushes_every_branch_both_sides_have_and_is_denied(world, command):
    reason = gate(command, world)
    assert reason and "push one reviewed branch at a time" in reason, command


def test_the_settings_that_make_a_bare_push_publish_more_than_head_are_read(world):
    # PG-MULTI: `git push` publishes what the git settings say, and they can say every branch
    git(world, "config", "push.default", "matching")
    for command in ("git push", "git push origin", "git push -u origin"):
        reason = gate(command, world)
        assert reason and "push.default=matching" in reason and "push one reviewed branch at a time" in reason, command
    assert gate("git push origin feature", world) is None  # a refspec of its own beats the setting
    unset(world, "push.default")
    assert gate("git push", world) is None
    git(world, "config", "remote.origin.mirror", "true")
    reason = gate("git push origin", world)
    assert reason and "mirror remote origin" in reason
    assert gate("git push origin feature", world) is None
    unset(world, "remote.origin.mirror")
    git(world, "config", "--add", "remote.origin.push", "refs/heads/*:refs/heads/*")
    assert "push one reviewed branch at a time" in gate("git push origin", world)
    assert "push one reviewed branch at a time" in gate("git push", world)
    unset(world, "remote.origin.push")
    git(world, "config", "--add", "remote.origin.push", "refs/heads/other:refs/heads/other")
    reason = gate("git push origin", world)
    assert reason and reason.startswith("plumbline: refs/heads/other (") and "has no pass or override record" in reason  # the configured refspec is a push of `other`
    unset(world, "remote.origin.push")
    git(world, "config", "--add", "remote.origin.push", "HEAD")
    assert gate("git push origin", world) is None  # HEAD is covered
    unset(world, "remote.origin.push")


def test_dash_c_push_default_and_a_remote_named_by_the_branch_count_too(world, tmp_path):
    assert "push.default=matching" in gate("git -c push.default=matching push origin", world)
    assert "push.default=matching" in gate("GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=push.default GIT_CONFIG_VALUE_0=matching git push", world)
    git(world, "config", "branch.feature.pushRemote", "elsewhere")
    git(world, "config", "remote.elsewhere.mirror", "true")
    assert "mirror remote elsewhere" in gate("git push", world)
    unset(world, "branch.feature.pushRemote")
    git(world, "config", "remote.pushDefault", "elsewhere")
    assert "mirror remote elsewhere" in gate("git push", world)
    assert gate(f"git push {tmp_path / 'origin.git'}", world) is None  # a path is no remote with settings of its own


def test_a_line_that_changes_the_push_settings_and_then_pushes_is_held_back(world):
    for command in (
        "git config push.default matching && git push",
        "git config --global remote.origin.mirror true; git push origin",
        "git config --add remote.origin.push 'refs/heads/*:refs/heads/*' && git push origin",
        "git config branch.feature.pushRemote other && git push",
    ):
        reason = gate(command, world)
        assert reason and "changes git's push settings" in reason and "push in another" in reason, command
    assert gate("git config user.name someone && git push origin feature", world) is None  # not a setting a push reads
    assert gate("git config --get push.default; git push origin feature", world) is None  # reading is not changing


def test_the_reviewers_multi_ref_pushes_really_publish_when_nothing_stops_them(world, origin):
    git(world, "config", "push.default", "matching")
    git(world, "push", "-q", "origin", "other")  # a branch both sides have, at its old place
    git(world, "checkout", "-q", "other")
    write(world / "newer.py", "x = 1\n")
    newer = commit_all(world, "newer unreviewed work on other")
    git(world, "checkout", "-q", "feature")
    subprocess.run(["git", "push", "-q", "origin"], cwd=world, capture_output=True)
    assert remote_ref(origin, "other") == newer  # the bare `git push` published it
    assert "push.default=matching" in gate("git push", world)


# ------------------------------------------------------------- refs made earlier in the same command line


def test_a_branch_or_tag_made_earlier_in_the_line_is_what_a_later_push_names(world):
    for command in (
        "git branch pub other && git push origin pub",
        "git branch -f feature other && git push origin feature",
        "git branch -m other pub && git push origin pub",
        "git branch -c other pub && git push origin pub",
        "git tag -f covered-tag other && git push origin covered-tag",
        "git tag -a v9 -m note other && git push origin v9",
        "git tag -am note v9 other && git push origin v9",
        "git update-ref refs/heads/feature other && git push origin feature",
        "git update-ref refs/tags/v9 other && git push origin v9",
        "git checkout -B feature other && git push origin feature",
        "git switch -C feature other && git push origin feature",
        "git checkout -b pub other && git push origin pub",
        "git switch -c pub other && git push -u origin pub",
        "git symbolic-ref HEAD refs/heads/other && git push",
        "git symbolic-ref HEAD refs/heads/other && git push origin HEAD",
    ):
        reason = gate(command, world)
        assert reason and "has no pass or override record" in reason, command


def test_the_reviewers_ref_creating_forms_really_publish_when_nothing_stops_them(world, origin):
    subprocess.run(["bash", "-c", "git branch pub other && git push -q origin pub"], cwd=world, capture_output=True, check=True)
    assert remote_ref(origin, "pub") == git(world, "rev-parse", "other").strip()
    assert gate("git branch pub2 other && git push origin pub2", world)


def test_a_branch_made_at_a_covered_commit_may_be_pushed_in_the_same_line(world):
    # the honest forms: a new branch or tag at the covered HEAD, or a switch to a covered branch, and its push
    for command in (
        "git switch -c newb && git push -u origin newb",
        "git checkout -b newb && git push origin newb",
        "git checkout -b newb feature && git push origin newb",
        "git switch -c newb HEAD && git push origin newb",
        "git branch pub && git push origin pub",
        "git branch pub feature && git push origin pub",
        "git tag v9 && git push origin v9",
        "git tag -a v9 -m 'release note' && git push origin v9",
        "git tag -am 'release note' v9 && git push origin v9",
        "git checkout feature && git push origin feature",
        "git switch feature && git push",
        "git checkout feature && git push origin HEAD",
        "git switch -c a && git switch -c b && git push origin b",
        "git branch -d old && git push origin feature",
        "git tag -d old && git push origin feature",
        "git branch --list && git push origin feature",
        "git tag -l && git push origin feature",
        "git checkout -- README.md && git push origin feature",
        "git checkout HEAD -- README.md && git push",
        "git checkout README.md && git push",
    ):
        assert gate(command, world) is None, command


def test_a_switch_to_something_uncovered_or_unknown_holds_back_a_push_of_head(world):
    for command in (
        "git checkout other && git push",
        "git checkout other && git push origin HEAD",
        "git switch other && git push",
        "git checkout main; git push",
        "git checkout other2 && git push origin",
        "git checkout - && git push",
        "git switch - && git push",
        "git checkout --orphan x && git push",
        "git checkout nonexistent-branch && git push",
        "git checkout -p && git push",
        "git checkout - && git push origin nonexistent-branch",  # what it names cannot be seen, and neither can where HEAD is
        "git checkout - && git push origin $BRANCH",
        "git switch - && git push origin HEAD~1",
    ):
        reason = gate(command, world)
        assert reason and "changes HEAD or a ref" in reason or "has no pass or override record" in reason, command
    reason = gate("git checkout - && git push", world)
    assert "changes HEAD or a ref" in reason
    assert "has no pass or override record" in gate("git checkout other && git push", world)  # known: it is `other`, and `other` has no pass


def test_a_push_of_an_explicit_ref_after_a_switch_is_judged_by_that_ref(world):
    assert gate("git checkout other && git push origin feature", world) is None  # `feature` is covered whatever HEAD does
    assert gate("git checkout feature && git push origin other", world)


def test_commits_and_ref_writers_by_stdin_still_hold_a_push_back(world):
    for command in ("git commit --allow-empty -m x && git push origin feature", "git reset --hard HEAD~1 && git push origin feature", "git merge other && git push origin feature",
                    "git update-ref --stdin < /dev/null && git push origin feature"):
        assert "changes HEAD or a ref" in gate(command, world), command


def test_ref_effects_are_worked_out_from_the_arguments(world):
    state = pre.RefState()
    for kind, args in (("branch", ["pub", "other"]), ("tag", ["-a", "v9", "-m", "msg", "feature"]), ("update-ref", ["refs/heads/x", "other"]), ("checkout", ["-b", "n", "main"])):
        pre._ref_effects(pl, world, pre.Action(kind, world, args), state)
    assert state.created["pub"] == "other" and state.created["refs/heads/pub"] == "other"
    assert state.created["v9"] == "feature" and state.created["refs/tags/v9"] == "feature"
    assert state.created["x"] == "other" and state.created["n"] == "main" and state.head == "main"
    assert not state.tips_moved and not state.config_changed
    state = pre.RefState()
    pre._ref_effects(pl, world, pre.Action("checkout", world, ["-"]), state)
    assert state.head is None
    pre._ref_effects(pl, world, pre.Action("branch", world, ["-d", "x"]), state)
    assert state.created == {}


# --------------------------------------------------------------------------------- negated flags, refspecs, send-pack


def test_a_negated_flag_takes_back_the_flag_before_it(world):
    for command in (
        "git push --dry-run --no-dry-run origin other",
        "git push -n --no-dry-run origin other",
        "git push --delete --no-delete origin other",
        "git push --all --no-all origin other",
        "git push --dry-run --no-dry origin other",
    ):
        assert gate(command, world), command
    assert gate("git push --no-dry-run --dry-run origin other", world) is None  # the later flag wins, and it is a dry run
    assert gate("git push --no-delete --delete origin other", world) is None


def test_a_refspec_is_cut_at_its_last_colon_so_a_commit_named_by_its_message_is_a_source(world):
    reason = gate("git push origin ':/work on other:refs/heads/x'", world)
    assert reason and "has no pass or override record" in reason
    assert gate("git push origin ':/work on other'", world) is None  # a colon in front and none after: deletes a ref of that name
    assert gate("git push origin :refs/heads/x", world) is None
    assert pre._refspec_sources([":/text:dst", "a:b:c", ":gone", "^x", "+HEAD:y"]) == ([":/text", "a:b", "HEAD"], False)
    assert pre._refspec_sources(["tag", "v1"]) == (["refs/tags/v1"], False)
    assert pre._refspec_sources([":", "a"]) == ([], True) and pre._refspec_sources(["+:"]) == ([], True)


def test_send_pack_and_subtree_push_publish_commits_and_are_pushes(world, tmp_path):
    origin = tmp_path / "origin.git"
    assert gate(f"git send-pack {origin} other:refs/heads/x", world)
    assert gate(f"git send-pack --all {origin}", world)
    assert gate("git subtree push --prefix=src origin other", world)
    assert gate(f"git send-pack {origin} feature:refs/heads/f", world) is None  # the covered ref
    assert gate(f"git send-pack --dry-run {origin} other:refs/heads/x", world) is None
    reason = gate("git subtree push --prefix=src origin feature", world)
    assert reason is None  # HEAD is covered
    (action,) = pre.analyze("git subtree push --prefix=src origin other", Path("/w"))
    assert action.kind == "push" and action.args == ["--prefix=src", "origin", "other"]


# ------------------------------------------------------------------------- aliases that the hook could not see before


def test_an_alias_defined_earlier_in_the_line_or_by_environment_variables_is_expanded(adopted, tmp_path):
    user = tmp_path / "gitconfig-of-the-line"
    write(user, "[alias]\n\tgo = push\n")
    for command in (
        "git config alias.p push && git p origin feature",
        "git config --global alias.p push; git p origin feature",
        "git config --add alias.ship 'push origin HEAD' && git ship",
        "git config alias.sh '!git push' && git sh origin feature",
        "GIT_CONFIG_PARAMETERS=\"'alias.p=push'\" git p origin feature",
        f"GIT_CONFIG_GLOBAL={user} git go origin feature",
        f"env GIT_CONFIG_GLOBAL={user} git go origin feature",
    ):
        reason = gate(command, adopted)
        assert reason and "has no pass or override record" in reason, command
    assert gate("git config alias.st status && git st", adopted) is None
    assert gate("git config alias.p push && git status", adopted) is None  # the alias is defined, and not used


# --------------------------------------------------------------------------------------- wrappers and shell constructs

WRAPPED = [
    "env --unset FOO git push origin feature",
    "env -u FOO git push origin feature",
    "timeout --signal KILL 10 git push origin feature",
    "timeout --kill-after=5 10 git push origin feature",
    "nice --adjustment 5 git push origin feature",
    "nice -n 5 git push origin feature",
    "stdbuf -oL git push origin feature",
    "stdbuf -o L -e0 git push origin feature",
    "flock /tmp/l git push origin feature",
    "flock -w 5 /tmp/l git push origin feature",
    "ionice -c3 git push origin feature",
    "ionice -c 2 -n 5 git push origin feature",
    "taskset -c 0 git push origin feature",
    "chrt -r 10 git push origin feature",
    "ssh-agent git push origin feature",
    "ssh-agent -t 60 git push origin feature",
    "watch -n 5 git push origin feature",
    "unbuffer git push origin feature",
    "chronic git push origin feature",
    "busybox sh -c 'git push origin feature'",
    "coproc git push origin feature",
    "trap 'git push origin feature' EXIT",
    "trap -- 'git push origin feature' EXIT ERR",
    "sudo --user root git push origin feature",
    "sudo --user=root git push origin feature",
    "time --format x git push origin feature",
    "command -p git push origin feature",
    "nohup nice ionice -c3 timeout 5 git push origin feature",
]


@pytest.mark.parametrize("command", WRAPPED)
def test_a_wrapper_or_shell_construct_does_not_hide_a_push(adopted, command):
    reason = gate(command, adopted)
    assert reason and "has no pass or override record" in reason, command


def test_the_wrapped_pushes_go_through_when_head_is_covered(adopted):
    genuine_pass(adopted)
    for command in WRAPPED:
        assert gate(command, adopted) is None, command
    assert gate("trap - EXIT; git status", adopted) is None


# ---------------------------------------------------------------------------- a check that fails does not let a push through


def test_an_unreadable_file_does_not_switch_the_push_check_off(adopted):
    if os.geteuid() == 0:
        pytest.skip("root reads any file")
    junk = adopted / "junk.bin"
    write(junk, "x\n")
    junk.chmod(0)
    try:
        reason = gate("git add a.txt; git commit -q -m x; git push origin feature", adopted)
        assert reason and "changes HEAD or a ref" in reason
        assert gate("git add -A && git commit -q -m x", adopted) is None  # no error, and nothing in what git can add
        assert "changes HEAD" in gate("git add -A && git commit -q -m x && git push", adopted)
    finally:
        junk.chmod(0o644)


def test_a_commit_check_that_raises_does_not_stop_the_push_check(adopted, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("cannot read")

    monkeypatch.delenv("PLUMBLINE_HOOK_DEBUG")
    monkeypatch.setattr(pre, "commit_problems", broken)
    reason = gate("git commit -q -m x && git push origin feature", adopted)
    assert reason and "changes HEAD or a ref" in reason


def test_a_push_the_gate_cannot_check_is_held_back(adopted, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("odd record")

    monkeypatch.delenv("PLUMBLINE_HOOK_DEBUG")
    monkeypatch.setattr(pl, "coverage", broken)
    reason = gate("git push origin feature", adopted)
    assert reason and "could not be checked (ValueError)" in reason and "/plumbline:override" in reason
    monkeypatch.setenv("PLUMBLINE_HOOK_DEBUG", "1")
    with pytest.raises(ValueError):
        gate("git push origin feature", adopted)


def test_a_pass_record_that_is_a_named_pipe_holds_the_push_not_the_hook(adopted, monkeypatch):
    import time

    monkeypatch.setattr(pre, "PASS_READ_SECONDS", 1)
    (adopted / ".plumbline" / "pass").mkdir(parents=True)
    os.mkfifo(adopted / ".plumbline" / "pass" / f"{head(adopted)}.json")
    started = time.time()
    reason = gate("git push origin feature", adopted)
    assert reason and "did not answer in time" in reason and "named pipe" in reason
    assert time.time() - started < 8


# ---------------------------------------------------------------------------------------- shells other than sh


def test_the_windows_shells_and_their_ways_to_run_a_string_do_not_hide_a_push(adopted):
    for command in (
        "cmd /c git push origin feature",
        'cmd.exe /C "git push origin feature"',
        "iex 'git push origin feature'",
        'Invoke-Expression "git push origin feature"',
        "pwsh -Command 'git push origin feature'",
        'powershell -c "git push origin feature"',
        "powershell.exe -Command git push origin feature",
        "pwsh -NoProfile -Command 'cd /tmp; git push origin feature'",
    ):
        reason = gate(command, adopted)
        assert reason and "has no pass or override record" in reason, command
    from hookdata import tool_payload

    assert pre.decide(tool_payload(adopted, "PowerShell", {"command": "iex 'git push origin feature'"}))
    assert gate("cmd /c dir", adopted) is None and gate("pwsh -Command 'Get-ChildItem'", adopted) is None


# ------------------------------------------------ the second adversarial pass: names, objects, nested shells, settings a command carries


def test_other_spellings_of_a_ref_moved_earlier_in_the_line_are_not_worked_out_they_are_held_back(world):
    for command in (
        "git branch pub other && git push origin 'pub^{}:refs/heads/pub'",
        "git branch pub other && git push origin heads/pub:refs/heads/pub",
        "git branch pub other && git push origin 'pub~0:refs/heads/pub'",
        "git branch pub other && git push origin 'refs/heads/pub@{0}:refs/heads/pub'",
        "git tag -f covered-tag other && git push origin tags/covered-tag:refs/tags/covered-tag",
        "git tag -f covered-tag other && git push origin refs/tags/covered-tag",
        "git checkout -q other && git push origin 'HEAD~0:refs/heads/x'",
        "git checkout -q other && git push origin 'HEAD^{}:refs/heads/x'",
        "git checkout -q other && git push origin '@^0:refs/heads/x'",
        "git checkout -q other && git push origin 'HEAD@{0}:refs/heads/x'",
    ):
        reason = gate(command, world)
        assert reason and ("changes HEAD or a ref" in reason or "has no pass or override record" in reason), command
    # nothing moved: the same spellings are worked out as they are
    for command in ("git push origin 'feature~0:refs/heads/x'", "git push origin 'HEAD~0:refs/heads/x'", "git push origin 'HEAD^{}:refs/heads/x'", "git push origin heads/feature"):
        assert gate(command, world) is None, command
    assert gate("git push origin 'other~0:refs/heads/x'", world)


def test_a_source_that_is_a_tree_or_a_blob_is_not_a_commit_and_is_refused(world):
    for command in ("git push origin 'other^{tree}:refs/tags/t'", "git push origin 'other:secret.py:refs/tags/b'", "git push origin 'HEAD^{tree}:refs/tags/t'"):
        reason = gate(command, world)
        assert reason and "not a commit" in reason and "push one reviewed branch at a time" in reason, command
    assert gate("git push origin 'other^{commit}:refs/heads/x'", world)  # a commit, and an unreviewed one


def test_a_double_dash_after_a_branch_name_still_switches_to_it(world):
    for command in ("git checkout other -- && git push origin HEAD", "git switch -- other && git push origin HEAD", "git switch -- other && git push"):
        assert gate(command, world), command
    for command in (
        "git checkout HEAD -- README.md && git push",
        "git checkout -- README.md && git push origin HEAD",
        "git checkout feature -- README.md && git push",
        # restoring a file from an unreviewed branch leaves HEAD where it was: what is pushed is still the reviewed branch
        "git checkout other -- README.md && git push",
        "git checkout other -- README.md && git push origin HEAD",
        "git checkout other -- README.md && git push origin feature",
    ):
        assert gate(command, world) is None, command


def test_update_ref_of_head_worktrees_fetches_into_local_refs_and_stashes_hold_a_push_back(world):
    for command in (
        "git update-ref HEAD other && git push origin feature",
        "git worktree add -b pub ../wtpub other && git push origin pub",
        "git worktree add ../wt other && cd ../wt && git push origin HEAD",
        "git worktree add ../wt other && git -C ../wt push origin HEAD",
        "git fetch . other:pub && git push origin pub",
        "git fetch . +other:pub && git push origin pub",
        "git fetch . other:refs/tags/t && git push origin t",
        "git stash && git push origin 'stash@{0}:refs/heads/s'",
        "git stash push -m x && git push",
    ):
        reason = gate(command, world)
        assert reason and ("changes HEAD or a ref" in reason or "has no pass or override record" in reason), command
    for command in (
        "git fetch origin && git push origin feature",
        "git fetch --all --prune && git push",
        "git fetch origin 'refs/heads/*:refs/remotes/origin/*' && git push origin feature",
        "git stash list && git push origin feature",
        "git stash pop && git push origin feature",
        "git worktree list && git push origin feature",
    ):
        assert gate(command, world) is None, command


def test_a_reset_that_names_paths_does_not_move_the_branch(world):
    for command in ("git reset HEAD README.md && git push origin feature", "git reset -- README.md && git push origin feature", "git reset -p && git push origin feature"):
        assert gate(command, world) is None, command
    for command in ("git reset --hard HEAD~1 && git push origin feature", "git reset HEAD~1 && git push origin feature", "git reset --soft HEAD^ && git push", "git reset --mixed && git push"):
        assert "changes HEAD or a ref" in gate(command, world), command


def test_a_push_may_not_name_more_refs_than_are_looked_up(world):
    names = " ".join(f"feature:refs/heads/x{i}" for i in range(pre.MAX_SOURCES + 5))
    reason = gate(f"git push origin {names}", world)
    assert reason and "push one reviewed branch at a time" in reason
    few = " ".join(f"other:refs/heads/x{i}" for i in range(3))
    assert "other (" in gate(f"git push origin {few}", world)


# ----------------------------------------------------------------- settings that ride on the command, not in the repository


def test_push_settings_given_to_the_command_itself_are_read(world, tmp_path):
    refs = tmp_path / "refs-config"
    write(refs, '[remote "origin"]\n\tpush = +other:other\n')
    matching = tmp_path / "matching-config"
    write(matching, "[push]\n\tdefault = matching\n")
    for command in (
        "git -c remote.origin.push=+other:other push origin",
        "git -c remote.origin.push=+other:other push",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0=+other:other git push origin",
        "GIT_CONFIG_PARAMETERS=\"'remote.origin.push=+other:other'\" git push origin",
        f"GIT_CONFIG_GLOBAL={refs} git push origin",
        f"env GIT_CONFIG_GLOBAL={refs} git push origin",
        f"git -c include.path={refs} push origin",
        f"GIT_CONFIG_GLOBAL={matching} git push",
        f"git -c include.path={matching} push origin",
    ):
        reason = gate(command, world)
        assert reason and ("has no pass or override record" in reason or "push one reviewed branch at a time" in reason), command
    assert gate("git -c remote.origin.push=HEAD push origin", world) is None  # HEAD is covered


def test_the_repo_option_names_the_remote_when_no_word_does(world):
    git(world, "config", "remote.m.mirror", "true")
    for command in ("git push --repo=m", "git push --repo m", "git push -q --repo=m"):
        reason = gate(command, world)
        assert reason and "mirror remote m" in reason, command
    assert gate("git push --repo=origin", world) is None
    assert gate("git push origin feature", world) is None


def test_a_mirror_remote_made_in_the_line_is_a_change_of_push_settings(world, tmp_path):
    for command in (f"git remote add --mirror=push m {tmp_path}/x && git push m", f"git remote add --mirror m {tmp_path}/x; git push m", "git remote set-url --mirror m x && git push m"):
        reason = gate(command, world)
        assert reason and "changes git's push settings" in reason, command
    assert gate(f"git remote add origin2 {tmp_path}/x && git push origin2 feature", world) is None  # the ordinary set-up of a remote


def test_git_config_with_a_file_option_is_read_like_git_reads_it(world):
    for command in (
        "git config --file .git/config remote.origin.push +other:other && git push origin",
        "git config -f .git/config remote.origin.push +other:other && git push origin",
        "git config -f .git/config push.default matching && git push",
        "git config -f .git/config alias.p push && git p origin other",
        "git config --file=.git/config alias.p push && git p origin other",
    ):
        reason = gate(command, world)
        assert reason and ("changes git's push settings" in reason or "has no pass or override record" in reason), command


def test_an_alias_is_looked_up_in_the_repository_a_git_dir_or_work_tree_names(adopted, tmp_path):
    git(adopted, "config", "alias.zzz", "push")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    extra = tmp_path / "alias-config"
    write(extra, "[alias]\n\tyyy = push\n")
    for command in (
        f"git --git-dir={adopted}/.git zzz origin feature",
        f"git --git-dir {adopted}/.git zzz origin feature",
        f"GIT_DIR={adopted}/.git git zzz origin feature",
        f"git --work-tree={adopted} zzz origin feature",
        f"export GIT_DIR={adopted}/.git; git zzz origin feature",
    ):
        reason = gate(command, outside)
        assert reason and "has no pass or override record" in reason, command
    for command in (f"git -c include.path={extra} yyy origin feature", f"GIT_CONFIG_GLOBAL={extra} git yyy origin feature"):  # where the alias is in a file the command names
        reason = gate(command, adopted)
        assert reason and "has no pass or override record" in reason, command
        assert gate(command, outside) is None  # and from a directory that is no adopted repository there is nothing to gate


def test_a_shell_alias_that_hands_its_arguments_on_is_read_with_them(adopted):
    for command in (
        "git -c 'alias.p=!f(){ git push \"$@\"; };f' p origin feature",
        "git -c 'alias.p=!f(){ git push $@; };f' p origin feature",
        "git -c 'alias.p=!sh -c \"git push $*\"' p origin feature",
        "git -c 'alias.p=!git push $1 $2' p origin feature",
        "git -c 'alias.p=!git push' p origin feature",
    ):
        reason = gate(command, adopted)
        assert reason and "has no pass or override record" in reason, command
    (action,) = [a for a in pre.analyze("git -c 'alias.p=!f(){ git push \"$@\"; };f' p origin other", Path("/w")) if a.kind == "push"]
    assert action.args == ["origin", "other"]


def test_a_shell_alias_is_read_with_the_arguments_it_is_given_so_the_branch_it_pushes_is_the_one_checked(world):
    # `feature` is reviewed and `other` is not: only reading the arguments into the alias tells the two pushes apart
    for alias in (
        "!git push",
        "!f(){ git push \"$@\"; };f",
        "!f(){ git push $@; };f",
        "!git push $1 $2",
        "!f() { git push \"$1\" \"$2\"; }; f",
        "!sh -c \"git push $*\"",
    ):
        reason = gate(f"git -c 'alias.p={alias}' p origin other", world)
        assert reason and "has no pass or override record" in reason and "other" in reason, alias
        assert gate(f"git -c 'alias.p={alias}' p origin feature", world) is None, alias


def test_a_reset_with_a_mode_counts_as_moving_head_whatever_else_is_written():
    for args in (["--hard", "a", "b"], ["--soft", "a", "b"], ["--mixed", "a", "b"], ["--merge", "a", "b"], ["--keep", "a", "b"], ["--hard", "--", "x"], ["--hard"]):
        assert pre._reset_moves(args) is True, args
    for args in (["HEAD", "file"], ["--", "file"], ["-p"], ["--patch", "HEAD", "file"], ["HEAD", "--", "file"]):
        assert pre._reset_moves(args) is False, args
    for args in ([], ["HEAD~1"], ["other"], ["HEAD", "--"]):
        assert pre._reset_moves(args) is True, args


# ------------------------------------------------------------------------------------ nested shells, spelled the odd ways

NESTED = [
    "bash -c -e 'git push origin feature'",
    "sh -c -- 'git push origin feature'",
    "bash -c -O extglob 'git push origin feature'",
    "dash -c -x 'git push origin feature'",
    "bash -o pipefail -c 'git push origin feature'",
    "bash -lc 'git push origin feature'",
    "eval -- 'git push origin feature'",
    "fish -c 'git push origin feature'",
    "ash -c 'git push origin feature'",
    "csh -c 'git push origin feature'",
    "mksh -c 'git push origin feature'",
    "flock /tmp/lk -c 'git push origin feature'",
    "flock /tmp/lk --command 'git push origin feature'",
    "watch -n1 -g 'git push origin feature'",
    "watch 'git push origin feature'",
    "watch -n 5 git push origin feature",
    "coproc NAME { git push origin feature; }",
    "pwsh -EncodedCommand ZwBpAHQAIABwAHUAcwBoACAAbwByAGkAZwBpAG4AIABmAGUAYQB0AHUAcgBlAA==",
    "powershell -e ZwBpAHQAIABwAHUAcwBoACAAbwByAGkAZwBpAG4AIABmAGUAYQB0AHUAcgBlAA==",
]


@pytest.mark.parametrize("command", NESTED)
def test_a_push_inside_a_nested_shell_is_seen_however_the_options_are_spelled(adopted, command):
    reason = gate(command, adopted)
    assert reason and "has no pass or override record" in reason, command


def test_the_nested_forms_go_through_when_head_is_covered(adopted):
    genuine_pass(adopted)
    for command in NESTED:
        assert gate(command, adopted) is None, command


def test_the_encoded_command_is_what_the_push_says():
    import base64

    encoded = base64.b64encode("git push origin feature".encode("utf-16-le")).decode()
    assert encoded == "ZwBpAHQAIABwAHUAcwBoACAAbwByAGkAZwBpAG4AIABmAGUAYQB0AHUAcgBlAA=="
    assert pre._nested_line("pwsh", ["pwsh", "-EncodedCommand", encoded]) == "git push origin feature"
    assert pre._nested_line("pwsh", ["pwsh", "-EncodedCommand", "not base64!!"]) is None


def test_the_shell_command_is_the_first_word_after_the_options_that_follow_dash_c():
    assert pre._shell_command(["bash", "-c", "x"]) == "x"
    assert pre._shell_command(["bash", "-c", "-e", "x"]) == "x"
    assert pre._shell_command(["bash", "-c", "--", "x"]) == "x"
    assert pre._shell_command(["bash", "-c", "-O", "extglob", "x"]) == "x"
    assert pre._shell_command(["bash", "-lc", "x", "arg0"]) == "x"
    assert pre._shell_command(["bash", "-o", "pipefail", "-c", "x"]) == "x"
    assert pre._shell_command(["bash", "script.sh"]) is None and pre._shell_command(["bash", "-c"]) is None
