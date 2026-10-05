"""The orchestrator's lane, enforced by the hooks: what it reads (the active run's directory), what its Bash runs (the plumbline-run and git-meta
classes), which agents it launches (the stage agents, under the launch pins), and the ledger entry its stop leaves. The main session may still
launch the stage agents itself, as the fallback when the orchestrator is not used."""
import json
import re

import pytest

import plumbline as pl
import pre_tool_use as pre
from helpers import CLI, REPO, commit_all, write
from hookdata import bash_payload, denial, start_run, stop_payload, tool_payload
from rundata import RUN, adopt, adopt_base, ledger, put

ORCHESTRATOR = "plumbline:orchestrator"
STAGE_ROLES = ("planner", "test-writer", "builder", "verifier", "prosecutor", "defender", "detective", "canary")
PLUMBLINE = f"python3 {CLI}"


@pytest.fixture(autouse=True)
def hook_errors_surface(monkeypatch):
    monkeypatch.setenv("PLUMBLINE_HOOK_DEBUG", "1")


@pytest.fixture
def started(repo):
    """An adopted repository with run r1 in progress, a source file and a test file."""
    adopt(repo)
    write(repo / "src" / "app.py", "def main():\n    return 2\n")
    write(repo / "tests" / "test_app.py", "def test_main():\n    assert True\n")
    commit_all(repo, "source and tests")
    start_run(repo)
    return repo


def as_orchestrator(repo, tool, tool_input, cwd=None):
    return pre.decide(tool_payload(cwd or repo, tool, tool_input, agent_type=ORCHESTRATOR))


def read(repo, path, **kw):
    return as_orchestrator(repo, "Read", {"file_path": str(path)}, **kw)


def grep(repo, path=None, pattern="x", **kw):
    return as_orchestrator(repo, "Grep", {"pattern": pattern, **({"path": str(path)} if path is not None else {})}, **kw)


def glob(repo, pattern="*.json", path=None, **kw):
    return as_orchestrator(repo, "Glob", {"pattern": pattern, **({"path": str(path)} if path is not None else {})}, **kw)


def run_dir(repo, run=RUN):
    return repo / ".plumbline" / "runs" / run


# --------------------------------------------------------------- Read, Grep and Glob: inside the active run's directory only


def test_the_orchestrator_reads_what_is_in_the_active_runs_directory(started):
    put(started, "plan", {"goal": "g"}, agent=False)
    write(run_dir(started) / "review" / "round-1" / "prosecutor-correctness.json", "{}")
    for path in (run_dir(started) / "intake.json", run_dir(started) / "plan.json", run_dir(started) / "ledger.jsonl", run_dir(started) / "review" / "round-1" / "prosecutor-correctness.json"):
        assert read(started, path) is None, path
    assert read(started, ".plumbline/runs/r1/plan.json") is None  # relative to the working directory
    assert read(started, run_dir(started) / "review" / "round-2" / "nothing-yet.json") is None  # what is not there yet is still inside


def test_the_orchestrator_does_not_read_source_tests_or_the_repositorys_own_files(started):
    for path in ("src/app.py", "tests/test_app.py", "plumbline.toml", "README.md", ".gitignore", ".git/config", "src", "."):
        reason = read(started, started / path)
        assert reason and "the orchestrator reads the active run's directory, .plumbline/runs/r1/ and nothing else" in reason, path
    assert "source and tests stay with the stage agents" in read(started, started / "src" / "app.py")


def test_the_orchestrator_does_not_read_what_else_lies_in_plumbline(started):
    write(started / ".plumbline" / "pass" / "abc.json", "{}")
    write(run_dir(started, "r0") / "plan.json", "{}")
    for path in (started / ".plumbline" / "pass" / "abc.json", started / ".plumbline" / "runs" / "ACTIVE", run_dir(started, "r0") / "plan.json", started / ".plumbline", started / ".plumbline" / "runs"):
        assert read(started, path), path
    assert read(started, "../outside.txt")
    assert read(started, "/etc/hostname")


def test_the_active_run_is_the_one_in_progress_so_another_runs_files_are_not_read(started):
    start_run(started, "r2", activate=False)
    assert read(started, run_dir(started) / "intake.json") is None
    assert "r2" in (read(started, run_dir(started, "r2") / "intake.json") or "r2") and read(started, run_dir(started, "r2") / "intake.json")
    (started / ".plumbline" / "runs" / "ACTIVE").write_text("r2\n", encoding="utf-8")
    assert read(started, run_dir(started, "r2") / "intake.json") is None
    assert read(started, run_dir(started) / "intake.json")


def test_with_no_run_in_progress_the_orchestrator_reads_nothing_of_the_repository(repo):
    adopt(repo)
    reason = read(repo, repo / "src" / "app.py")
    assert reason and "no run is in progress, so there is none" in reason


def test_a_symbolic_link_in_the_run_leads_where_it_leads(started):
    (run_dir(started) / "link.py").symlink_to(started / "src" / "app.py")
    (started / "alias").symlink_to(run_dir(started))
    assert read(started, run_dir(started) / "link.py")  # inside by name, outside by where it goes
    assert read(started, started / "alias" / "intake.json") is None  # outside by name, inside by where it goes


def test_a_grep_or_glob_names_a_path_inside_the_run_and_a_search_from_where_it_stands_is_refused(started):
    inside = run_dir(started)
    assert grep(started, inside, pattern='"severity"') is None
    assert grep(started, inside / "ledger.jsonl") is None
    assert grep(started, ".plumbline/runs/r1/review") is None
    assert glob(started, "**/*.json", inside) is None
    assert glob(started, "review/round-*/*.json", ".plumbline/runs/r1") is None
    for tool in (grep, glob):
        for path in (None, "", "  "):
            reason = tool(started, path=path) if path is not None else tool(started)
            assert reason and "needs an explicit path inside the active run's directory" in reason, (tool.__name__, path)
        assert tool(started, started)  # the repository
        assert tool(started, started / "src")
        assert tool(started, started / ".plumbline")
        assert tool(started, "/")


def test_a_glob_pattern_that_leaves_the_path_it_names_is_refused(started):
    inside = run_dir(started)
    for pattern in ("../../../src/*.py", "../*", "review/../../*", "/etc/*", "~/x/*", "C:/x/*", "**/../../../src/*.py", "..", "a/../../b"):
        reason = glob(started, pattern, inside)
        assert reason and "reaches outside it" in reason, pattern
    assert glob(started, "src/**/*.py", started) and glob(started, str(started / "src" / "*.py"), inside)
    assert glob(started, "*", inside / ".." / ".." / "..")  # the path itself leads out


def test_the_lane_is_the_orchestrators_alone(started):
    for role in (*STAGE_ROLES, None):
        payload = tool_payload(started, "Read", {"file_path": str(started / "src" / "app.py")}, agent_type=f"plumbline:{role}" if role else None)
        assert pre.decide(payload) is None, role  # the main session and the stage agents read source
    assert pre.decide(tool_payload(started, "Grep", {"pattern": "x"}, agent_type="plumbline:prosecutor")) is None


def test_the_lane_is_silent_where_plumbline_is_not_adopted(repo):
    assert read(repo, repo / "src" / "app.py") is None
    assert grep(repo) is None and glob(repo) is None


def test_the_other_tools_are_not_read_by_this_rule(started):
    assert as_orchestrator(started, "WebFetch", {"url": "x"}) is None
    assert as_orchestrator(started, "SendMessage", {"to": "a", "message": "b"}) is None


def test_the_lane_through_the_hook_process(run_pre, started):
    payload = tool_payload(started, "Read", {"file_path": str(started / "src" / "app.py")}, agent_type=ORCHESTRATOR)
    assert "the orchestrator reads the active run's directory" in denial(run_pre(payload, started))
    payload = tool_payload(started, "Read", {"file_path": str(run_dir(started) / "intake.json")}, agent_type=ORCHESTRATOR)
    assert denial(run_pre(payload, started)) is None


# ---------------------------------------------------------------------------------------------------- Bash: two classes


def runs(repo, command, role="orchestrator", cwd=None):
    payload = bash_payload(cwd or repo, command, agent_type=f"plumbline:{role}" if role else None)
    return pre.decide(payload)


@pytest.mark.parametrize(
    "command",
    [
        f"{PLUMBLINE} status --run r1",
        f"{PLUMBLINE} status",
        f"{PLUMBLINE} plan --run r1 --json",
        f"{PLUMBLINE} plan --run r1",
        f"{PLUMBLINE} plan --run=r1 --json",
        f"{PLUMBLINE} plan --json --run r1 --project .",
        f"{PLUMBLINE} plan --run r1 --project=.",
        f"{PLUMBLINE} check-diff --run r1",
        f"{PLUMBLINE} gate r1 verify",
        f"{PLUMBLINE} merge-review r1 review --round 2",
        f"{PLUMBLINE} tokens r1",
        f"{PLUMBLINE} check-record spec .plumbline/runs/r1/plan.json",
        f"{PLUMBLINE} open",
        f"{PLUMBLINE} open r1 --json",
        f"python3.12 {CLI} gate r1 review",
        f"{PLUMBLINE} status --run r1\n{PLUMBLINE} plan --run r1 --json\n{PLUMBLINE} check-diff --run r1",  # a batch, one command to a line
        f"{PLUMBLINE} status --run r1 && {PLUMBLINE} check-diff --run r1; {PLUMBLINE} tokens r1",
        f"{PLUMBLINE} status --run r1 2>/dev/null",
        f"cd . && {PLUMBLINE} status --run r1",
    ],
)
def test_the_orchestrator_runs_plumbline_s_commands_for_a_run(started, command):
    assert runs(started, command) is None, command


@pytest.mark.parametrize(
    "command",
    [
        "git status", "git status --short", "git status --porcelain=v1 -b", "git rev-parse HEAD", "git rev-parse --short HEAD", "git rev-parse --show-toplevel",
        "git log", "git log --oneline -5", "git log --oneline origin/main..HEAD", "git log --stat", "git log --numstat -3", "git log --name-only --format=%h",
        "git log --stat=120 -n 2", "git log --since=1.day --author=x --no-merges", "git log -S foo --oneline", "git log --pretty=format:%H%x20%s", "git log --graph --decorate --all",
        "git log --color --oneline", "git log --diff-filter=A --name-only", "git log -- src/app.py",
        "git branch --show-current",
        "git diff --stat", "git diff --stat=100", "git diff --numstat main...HEAD", "git diff --name-only", "git diff --cached --stat", "git diff --stat -- src/", "git diff HEAD~1 --name-only",
        "git diff --stat --name-only --numstat", "git --no-pager log --oneline", "git -C . status", "git status && git rev-parse HEAD",
        "git status -uno", "git status -uall -s", "git status --untracked-files=no", "git status -sb", "git log -n5 --oneline", "git log -5", "git log --max-count=3",
    ],
)
def test_the_orchestrator_looks_at_the_repository_through_git_s_summary_views(started, command):
    assert runs(started, command) is None, command


@pytest.mark.parametrize(
    "command,said",
    [
        (f"{PLUMBLINE} plan --intent feature", "may run only"),
        (f"{PLUMBLINE} plan --intent feature --request-file x", "may run only"),
        (f"{PLUMBLINE} plan", "may run only"),
        (f"{PLUMBLINE} plan --json", "may run only"),
        (f"{PLUMBLINE} plan --run r1 --intent feature", "may run only"),
        (f"{PLUMBLINE} plan --run r1 --calibrate", "may run only"),
        (f"{PLUMBLINE} plan --run r1 --row code.M", "may run only"),
        (f"{PLUMBLINE} plan --run r1 --base main", "may run only"),
        (f"{PLUMBLINE} plan --run r1 --run-id r2", "may run only"),
        (f"{PLUMBLINE} plan --run r1 --spec x.json --json", "may run only"),
        (f"{PLUMBLINE} plan --run", "may run only"),
        (f"{PLUMBLINE} plan --ru r1", "may run only"),
        (f"{PLUMBLINE} plan r1", "may run only"),
        (f"{PLUMBLINE} pass r1", "may run only"),
        (f"{PLUMBLINE} init", "may run only"),
        (f"{PLUMBLINE} classify", "may run only"),
        (f"{PLUMBLINE} render x.json", "may run only"),
        (f"{PLUMBLINE} validate-pipeline", "may run only"),
        (f"{PLUMBLINE} override --reason 'a reason that is long enough'", "the builder's command"),
        (f"{PLUMBLINE}", "may run only"),
        ("python3 -c 'import plumbline'", "may run only"),
        ("python3 other/plumbline.py status", "may run only"),
        (f"FOO=1 {PLUMBLINE} status --run r1", "no VAR=value before them"),
        (f"PYTHONPATH=/tmp {PLUMBLINE} gate r1 verify", "no VAR=value before them"),
        ("GIT_PAGER=x git status", "no VAR=value before them"),
        ("git log -p", "may run only"), ("git log --patch", "may run only"), ("git log -p -3", "may run only"), ("git log --stat -p", "may run only"),
        ("git log -u", "may run only"), ("git log -U5", "may run only"), ("git log -c", "may run only"), ("git log --cc", "may run only"), ("git log -m -p", "may run only"),
        ("git log --patch-with-stat", "may run only"), ("git log --word-diff", "may run only"), ("git log --color-words", "may run only"), ("git log -L1,5:src/app.py", "may run only"),
        ("git log --pat", "may run only"), ("git log --unif=3", "may run only"), ("git log --diff-merges=on", "may run only"), ("git log --binary", "may run only"),
        ("git log --output=x.txt", "may run only"), ("git log --ext-diff", "may run only"), ("git log --textconv", "may run only"), ("git log -O x", "may run only"),
        ("git diff", "may run only"), ("git diff HEAD", "may run only"), ("git diff -- src/app.py", "may run only"), ("git diff --cached", "may run only"),
        ("git diff -p", "may run only"), ("git diff --patch", "may run only"), ("git diff --stat -p", "may run only"), ("git diff --stat --patch", "may run only"),
        ("git diff --stat -U0", "may run only"), ("git diff --name-only --word-diff", "may run only"), ("git diff --stat --binary", "may run only"),
        ("git diff --raw", "may run only"), ("git diff --name-status", "may run only"), ("git diff --shortstat", "may run only"), ("git diff --sta", "may run only"),
        ("git diff --stat --ext-diff", "may run only"), ("git diff --stat --output=x", "may run only"),
        ("git status -v", "may run only"), ("git status --verbose", "may run only"), ("git status -sv", "may run only"), ("git status -vv", "may run only"),
        ("git branch", "may run only"), ("git branch -a", "may run only"), ("git branch -D x", "may run only"), ("git branch new", "may run only"), ("git branch --show-current x", "may run only"),
        ("git branch --show-current --list", "may run only"),
        ("git show", "may run only"), ("git show HEAD", "may run only"), ("git ls-files", "may run only"), ("git grep x", "may run only"), ("git blame src/app.py", "may run only"),
        ("git add -A", "may run only"), ("git commit -m x", "may run only"), ("git checkout -b x", "may run only"), ("git reset --hard", "may run only"), ("git stash", "may run only"),
        ("git merge-base main HEAD", "may run only"), ("git tag x", "may run only"), ("git -c core.pager=x log", "may run only"), ("git --git-dir=/x log", "may run only"),
        ("git", "may run only"), ("git -C", "may run only"),
        ("cat src/app.py", "may run only"), ("grep -rn main src", "may run only"), ("rg main", "may run only"), ("head -5 src/app.py", "may run only"), ("tail src/app.py", "may run only"),
        ("ls", "may run only"), ("ls src", "may run only"), ("find . -name '*.py'", "may run only"), ("sed -n 1,5p src/app.py", "may run only"), ("wc -l src/app.py", "may run only"),
        ("python3 -m pytest", "may run only"), ("pytest", "may run only"), ("sh -c 'cat src/app.py'", "may run only"), ("bash -c 'git log -p'", "may run only"),
        ("rm -rf src", "may run only"), ("touch x", "may run only"), ("mv a b", "may run only"), ("cp a b", "may run only"), ("curl http://x", "may run only"), ("sudo git status", "another user"),
        (f"{PLUMBLINE} status --run r1 > out.txt", "does not write files"), (f"{PLUMBLINE} status --run r1 | tee out.txt", "may run only"),
        (f"{PLUMBLINE} status --run r1 && cat src/app.py", "may run only"), ("git status; cat tests/test_app.py", "may run only"),
        ("echo $(cat src/app.py)", "may run only"), (f"{PLUMBLINE} check-record spec $(cat src/app.py)", "may run only"),
        ("git push origin feature", "may run only"),  # the role's classes answer before the push gate would
    ],
)
def test_the_orchestrator_runs_nothing_else(started, command, said):
    reason = runs(started, command)
    assert reason and said in reason, (command, reason)


def test_the_denial_names_what_the_orchestrator_may_run(started):
    reason = runs(started, "cat src/app.py")
    assert reason == (
        "plumbline: the orchestrator's Bash may run only `plumbline.py` check-diff, gate, merge-review, status, tokens, check-record and open, and `plan --run RUN --json`; "
        "git's summary views (status, rev-parse, log with no patch, branch --show-current, and diff with --stat, --numstat or --name-only); "
        "`plumbline.py check-record TYPE FILE` to check your record. `cat src/app.py` is none of these."
    )


def test_plumbline_run_is_not_open_to_the_stage_agents_because_their_policies_do_not_name_it(started):
    for role in STAGE_ROLES:
        reason = runs(started, f"{PLUMBLINE} gate r1 verify", role)
        assert reason and ("has no Bash" if role == "builder" else "may run only") in reason, role
        assert runs(started, f"{PLUMBLINE} check-record spec x.json", role) is None or role == "builder", role  # check-record is every role's own
    assert runs(started, f"{PLUMBLINE} check-diff --run r1", "verifier") is None  # the verifier's own class, plumbline-check


def test_the_main_session_runs_all_of_it(started):
    for command in (f"{PLUMBLINE} plan --intent feature --row code.S --run-id r9", f"{PLUMBLINE} pass r1", "cat src/app.py", "git log -p", "git diff"):
        assert runs(started, command, None) is None, command  # nothing here concerns the main session's Bash but the push gate and override


def test_a_pipeline_file_can_widen_the_orchestrators_classes_because_the_hook_follows_the_data(repo):
    text = (pl.PIPELINE_DIR / "default.toml").read_text(encoding="utf-8")
    widened = text.replace('commands = ["plumbline-run", "git-meta"]', 'commands = ["plumbline-run", "git-meta", "search"]')
    assert widened != text
    write(repo / "wide.toml", widened)
    write(repo / "plumbline.toml", 'schema = 1\npipeline = "wide.toml"\n')
    write(repo / ".gitignore", ".plumbline/\n")
    commit_all(repo, "adopt with a pipeline of its own")
    start_run(repo)
    assert pl.load_project(repo).errors == [] and pl.load_project(repo).roles["orchestrator"]["commands"] == ["plumbline-run", "git-meta", "search"]
    assert runs(repo, "cat src/app.py") is None  # the repository's own choice
    assert "may run only" in runs(repo, "git log -p")  # the other classes hold as they were
    assert "reaches outside it" in read(repo, repo / "src" / "app.py")  # and the read lane is the hook's own, not the data's


def test_the_classes_work_through_the_hook_process(run_pre, started):
    payload = bash_payload(started, "cat src/app.py", agent_type=ORCHESTRATOR)
    assert "the orchestrator's Bash may run only" in denial(run_pre(payload, started))
    payload = bash_payload(started, f"{PLUMBLINE} status --run r1", agent_type=ORCHESTRATOR)
    assert denial(run_pre(payload, started)) is None


def test_the_bash_rule_is_silent_where_plumbline_is_not_adopted(repo):
    assert runs(repo, "cat src/app.py") is None


def test_the_orchestrator_is_a_role_of_the_hook_beside_the_stage_roles():
    assert pre.STAGE_ROLES == STAGE_ROLES and pre.ORCHESTRATOR == "orchestrator" and pre.ROLES == (*STAGE_ROLES, "orchestrator") == pl.AGENT_ROLES
    assert pre.agent_role({"agent_type": ORCHESTRATOR}) == "orchestrator" and pre.agent_role({"agent_type": "plumbline:wizard"}) is None


@pytest.mark.parametrize("tool", ["Edit", "Write", "NotebookEdit"])
def test_the_orchestrator_writes_no_file_even_though_it_has_no_tool_for_it(started, tool):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    for path in (started / "src" / "app.py", run_dir(started) / "plan.json", run_dir(started) / "review" / "round-1" / "prosecutor-x.json", started / "x.txt"):
        reason = pre.decide(tool_payload(started, tool, {key: str(path), "content": "x"}, agent_type=ORCHESTRATOR))
        assert reason == "plumbline: the orchestrator writes no file: the stage agents write the records, and the main session commits. Launch the agent whose work this is.", path


def test_what_the_orchestrator_may_run_matches_what_its_prompt_names():
    prompt = (REPO / "agents" / "orchestrator.md").read_text(encoding="utf-8")
    named = {m.group(1) for m in re.finditer(r"PLUMBLINE ([a-z][a-z-]+)", prompt)}
    assert named <= {*pre.PLUMBLINE_RUN, "plan"}
    for line in re.findall(r"^PLUMBLINE (.*)$", prompt, re.M):
        command = f"{PLUMBLINE} " + re.sub(r"<[^>]*>", "x", line)
        assert pre._plumbline_run(pl, pre.split_commands(command)[0]), command


# ------------------------------------------------------------------------------------------------------------ Agent launches


def launch(repo, subagent="plumbline:builder", launcher=ORCHESTRATOR, cwd=None, **fields):
    tool_input = {"description": "x", "prompt": "write the record to .plumbline/runs/r1/build.json", **fields}
    if subagent is not False:
        tool_input["subagent_type"] = subagent
    return pre.decide(tool_payload(cwd or repo, "Agent", tool_input, agent_type=launcher))


@pytest.mark.parametrize("role", STAGE_ROLES)
def test_the_orchestrator_launches_each_stage_agent(started, role):
    assert launch(started, f"plumbline:{role}") is None
    assert launch(started, f"plumbline:{role}", run_in_background=False) is None
    assert launch(started, f"plumbline:{role}", model=None, isolation=None) is None


@pytest.mark.parametrize("subagent", ["plumbline:orchestrator", "general-purpose", "Explore", "Plan", "claude", "claude-code-guide", "probe:echo", "fork", "plumbline:wizard", "plumbline:", "plumbline:Planner", "", None, False, 5])
def test_the_orchestrator_launches_no_other_agent_and_not_itself(started, subagent):
    reason = launch(started, subagent, prompt="do something unrelated")
    assert reason and "the orchestrator launches the stage agents (plumbline:planner, test-writer, builder, verifier, prosecutor, defender, detective or canary) and no other agent, itself included" in reason, subagent
    assert "is not one of them" in reason


def test_the_denial_names_what_was_asked_for(started):
    assert "itself included: plumbline:orchestrator is not one of them" in launch(started, "plumbline:orchestrator")
    assert "itself included: general-purpose is not one of them" in launch(started, "general-purpose")
    assert "itself included: a general agent is not one of them" in launch(started, False)


def test_a_plain_brief_to_a_general_agent_is_still_the_orchestrators_denial(started):
    assert "the orchestrator launches the stage agents" in launch(started, "general-purpose", prompt="summarise the repository", description="x")
    assert "the orchestrator launches the stage agents" in launch(started, "general-purpose", prompt="read .plumbline/runs/r1/plan.json")


@pytest.mark.parametrize("role", STAGE_ROLES)
def test_the_launch_pins_hold_for_the_orchestrators_launches(started, role):
    assert launch(started, f"plumbline:{role}", isolation="worktree") == f"plumbline: a run's records live in the main checkout, so plumbline:{role} runs there. Launch it without `isolation`."
    pinned = re.search(r"^model:\s*(\S+)", (REPO / "agents" / f"{role}.md").read_text(encoding="utf-8"), re.M).group(1)
    other = "opus" if pinned != "opus" else "haiku"
    assert launch(started, f"plumbline:{role}", model=other) == f"plumbline: plumbline:{role} is pinned to the {pinned} model. Launch it without `model`, or with `model: {pinned}`."
    assert launch(started, f"plumbline:{role}", model=pinned.upper()) is None


def test_the_canary_rule_for_a_defenders_brief_holds_for_the_orchestrators_launch(repo, run_cli):
    adopt_base(repo)
    assert run_cli("plan", "--run-id", RUN, "--intent", "feature", "--row", "code.S", "--calibrate", cwd=repo).returncode == 0
    reason = launch(repo, "plumbline:defender", prompt="Answer the findings, the canary's among them. .plumbline/runs/r1/review/round-1/prosecutor-correctness-b.json")
    assert reason and "names neither the canary nor its key" in reason
    assert launch(repo, "plumbline:defender", prompt="Answer the findings in .plumbline/runs/r1/review/round-1/prosecutor-correctness-b.json") is None
    assert launch(repo, "plumbline:canary", prompt="You are the canary of this round.") is None


def test_the_main_session_still_launches_the_stage_agents_itself_as_the_fallback(started):
    for role in STAGE_ROLES:
        assert launch(started, f"plumbline:{role}", launcher=None) is None, role
    assert "pinned to the sonnet model" in launch(started, "plumbline:builder", launcher=None, model="haiku")
    assert "Launch it without `isolation`" in launch(started, "plumbline:builder", launcher=None, isolation="remote")
    assert "plumbline stages run through the plumbline:* agents" in launch(started, "general-purpose", launcher=None, prompt="see .plumbline/runs/r1/plan.json")
    assert launch(started, "general-purpose", launcher=None, prompt="summarise the repository") is None


def test_the_main_session_launches_the_orchestrator_under_the_same_pins(started):
    assert launch(started, ORCHESTRATOR, launcher=None, prompt="Run r1.") is None
    assert launch(started, ORCHESTRATOR, launcher=None, prompt="Run r1.", run_in_background=True) is None
    assert launch(started, ORCHESTRATOR, launcher=None, prompt="Run r1.", model="sonnet") is None
    assert "pinned to the sonnet model" in launch(started, ORCHESTRATOR, launcher=None, prompt="Run r1.", model="opus")
    assert "pinned to the sonnet model" in launch(started, ORCHESTRATOR, launcher=None, prompt="Run r1.", model="haiku")
    assert "Launch it without `isolation`" in launch(started, ORCHESTRATOR, launcher=None, prompt="Run r1.", isolation="worktree")
    assert pre.pinned_model(pl, "orchestrator") == "sonnet"


def test_another_stage_agent_launching_is_no_part_of_this_rule(started):
    assert launch(started, "general-purpose", launcher="plumbline:builder", prompt="summarise the repository") is None  # only the orchestrator's launches are held to the stage agents
    assert launch(started, "plumbline:orchestrator", launcher="plumbline:planner") is None


def test_the_launch_rule_is_silent_where_plumbline_is_not_adopted(repo):
    assert launch(repo, "general-purpose") is None and launch(repo, "plumbline:orchestrator") is None
    assert launch(repo, "plumbline:builder", isolation="remote") is None


def test_the_launch_rule_through_the_hook_process(run_pre, started):
    payload = tool_payload(started, "Agent", {"description": "x", "prompt": "y", "subagent_type": "general-purpose"}, agent_type=ORCHESTRATOR)
    assert "the orchestrator launches the stage agents" in denial(run_pre(payload, started))
    payload = tool_payload(started, "Agent", {"description": "x", "prompt": "y", "subagent_type": "plumbline:planner"}, agent_type=ORCHESTRATOR)
    assert denial(run_pre(payload, started)) is None


# --------------------------------------------------------------------------------------------------------------- SubagentStop


def stop(run_stop, repo, agent_type=ORCHESTRATOR, agent_id="orch-1", message="every gate passed", **fields):
    return run_stop(stop_payload(repo, agent_type, message, agent_id=agent_id, **fields), repo)


def legs(repo, run=RUN):
    return [e for e in ledger(repo, run) if e["kind"] == "leg"]


def test_an_orchestrators_stop_enters_a_leg_in_the_ledger_and_asks_for_no_record(run_stop, started):
    result = stop(run_stop, started)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    [entry] = legs(started)
    assert entry["agent_id"] == "orch-1" and entry["agent_transcript_path"].endswith("/subagents/agent-orch-1.jsonl") and entry["at"]
    assert entry["kind"] == "leg" and "agent_type" not in entry and "record" not in entry
    assert not [e for e in ledger(started) if e["kind"] == "agent" and e.get("agent_type") == ORCHESTRATOR]  # it is no stage's agent
    assert "unknown plumbline role" not in json.dumps(ledger(started))


def test_each_leg_is_one_entry_however_often_its_agent_stops(run_stop, started):
    stop(run_stop, started)
    stop(run_stop, started)
    assert len(legs(started)) == 1
    stop(run_stop, started, agent_id="orch-2")
    assert [e["agent_id"] for e in legs(started)] == ["orch-1", "orch-2"]


def test_a_stop_with_no_agent_id_is_a_leg_of_its_own_each_time(run_stop, started):
    payload = stop_payload(started, ORCHESTRATOR, "done")
    del payload["agent_id"]
    run_stop(payload, started)
    run_stop(payload, started)
    assert len(legs(started)) == 2


def test_the_leg_is_credited_to_the_run_in_progress(run_stop, started):
    start_run(started, "r2", activate=False)
    stop(run_stop, started)
    assert len(legs(started, "r1")) == 1 and legs(started, "r2") == []
    (started / ".plumbline" / "runs" / "ACTIVE").write_text("r2\n", encoding="utf-8")
    stop(run_stop, started, agent_id="orch-2")
    assert [e["agent_id"] for e in legs(started, "r2")] == ["orch-2"]


def test_a_stop_with_no_run_in_progress_enters_nothing_and_lets_the_agent_go(run_stop, repo):
    adopt(repo)
    result = stop(run_stop, repo)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "") and not (repo / ".plumbline").exists()


def test_a_stop_where_plumbline_is_not_adopted_is_let_go_silently(run_stop, repo):
    result = stop(run_stop, repo)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "") and not (repo / ".plumbline").exists()


@pytest.mark.parametrize("agent_type", ["", None, "general-purpose", "Explore", "probe:outer", "plumbline", "PLUMBLINE:orchestrator", " plumbline:orchestrator", 5])
def test_an_empty_or_another_agent_type_is_ignored_as_a_harness_helper_is(run_stop, started, agent_type):
    result = stop(run_stop, started, agent_type=agent_type)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert legs(started) == [] and not [e for e in ledger(started) if e["kind"] == "agent"]
    assert not (started / ".plumbline" / "blocks").exists()


def test_an_unknown_plumbline_role_is_still_entered_as_invalid(run_stop, started):
    result = stop(run_stop, started, agent_type="plumbline:wizard", agent_id="w-1")
    assert (result.returncode, result.stdout) == (0, "")
    [entry] = [e for e in ledger(started) if e["kind"] == "agent"]
    assert entry["valid"] is False and entry["errors"] == ["unknown plumbline role"] and legs(started) == []


def test_a_stage_agent_below_the_orchestrator_is_traced_as_it_always_was(run_stop, started):
    stop(run_stop, started, agent_type="plumbline:planner", agent_id="p-1", message="RECORD: .plumbline/runs/r1/plan.json")  # no record: held back, as ever
    result = stop(run_stop, started, agent_type="plumbline:planner", agent_id="p-1", message="no line")
    assert result.returncode == 2 and legs(started) == []


def test_the_stop_hook_enters_the_leg_through_the_shell_filter_too(home, started):
    import subprocess

    from helpers import clean_env

    payload = stop_payload(started, ORCHESTRATOR, "done", agent_id="orch-sh")
    result = subprocess.run(["sh", str(REPO / "scripts" / "subagent_stop.sh")], input=json.dumps(payload), capture_output=True, text=True, cwd=started, env=clean_env(home))
    assert (result.returncode, result.stdout) == (0, "") and [e["agent_id"] for e in legs(started)] == ["orch-sh"]
    empty = stop_payload(started, "", "done", agent_id="helper")
    result = subprocess.run(["sh", str(REPO / "scripts" / "subagent_stop.sh")], input=json.dumps(empty), capture_output=True, text=True, cwd=started, env=clean_env(home))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "") and len(legs(started)) == 1  # the filter lets an empty agent_type through at once


# ------------------------------------------------------------------------------------------------ tokens: the legs, apart


from rundata import HAIKU, SONNET, agent_row, assistant_record, genuine_pass, write_ledger, write_transcript  # noqa: E402


def leg_row(agent_id, transcript=None, **fields):
    """A ledger row of an orchestrator's leg, as the SubagentStop hook enters it."""
    return {"kind": "leg", "agent_id": agent_id, "agent_transcript_path": str(transcript) if transcript else None, "session_id": None, "session_transcript": None, **fields}


@pytest.fixture
def usage(tmp_path):
    """A stage agent's transcript (Haiku: output 9, fresh input 10, cache read 50) and a leg's (Sonnet: output 300, fresh input 120, cache read 9000)."""
    stage = write_transcript(tmp_path / "t" / "agent-s1.jsonl", [assistant_record("msg_s", HAIKU, output=9, inp=7, cache_write=3, cache_read=50)])
    leg = write_transcript(
        tmp_path / "t" / "agent-o1.jsonl",
        [assistant_record("msg_o1", SONNET, output=100, inp=20, cache_write=100, cache_read=3000), assistant_record("msg_o2", SONNET, output=200, inp=0, cache_write=0, cache_read=6000)],
    )
    return stage, leg


def test_the_legs_are_reported_apart_from_the_stage_agents_per_model(run_cli, started, usage):
    stage, leg = usage
    write_ledger(started, [agent_row("s1", transcript=str(stage)), leg_row("o1", leg)])
    result = run_cli("tokens", RUN, cwd=started)
    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout) == {
        "by_model": {HAIKU: {"output": 9, "fresh_input": 10, "cache_read": 50, "output_lower_bound": 0}},
        "orchestration": {SONNET: {"output": 300, "fresh_input": 120, "cache_read": 9000, "output_lower_bound": 0}},
    }


def test_a_run_with_no_leg_has_no_orchestration_key_and_is_as_it_was(run_cli, started, usage):
    stage, _ = usage
    write_ledger(started, [agent_row("s1", transcript=str(stage))])
    assert list(json.loads(run_cli("tokens", RUN, cwd=started).stdout)) == ["by_model"]


def test_two_legs_add_up_per_model_and_a_leg_entered_twice_is_read_once(run_cli, started, usage, tmp_path):
    _, leg = usage
    second = write_transcript(tmp_path / "t" / "agent-o2.jsonl", [assistant_record("msg_o3", SONNET, output=5, inp=1, cache_write=2, cache_read=7)])
    write_ledger(started, [leg_row("o1", leg), leg_row("o1", leg), leg_row("o2", second)])
    orchestration = json.loads(run_cli("tokens", RUN, cwd=started).stdout)["orchestration"]
    assert orchestration == {SONNET: {"output": 305, "fresh_input": 123, "cache_read": 9007, "output_lower_bound": 0}}


def test_a_stage_agents_transcript_is_never_counted_as_a_leg_and_a_leg_never_as_an_agent(run_cli, started, usage):
    stage, leg = usage
    write_ledger(started, [agent_row("s1", transcript=str(stage)), leg_row("o1", leg), {"kind": "gate", "stage": "plan", "gate": "spec_complete", "passed": True}])
    tokens = json.loads(run_cli("tokens", RUN, cwd=started).stdout)
    assert set(tokens["by_model"]) == {HAIKU} and set(tokens["orchestration"]) == {SONNET}


def test_a_leg_whose_transcript_is_not_found_is_a_note_and_the_orchestration_is_empty(run_cli, started):
    write_ledger(started, [leg_row("o1", "/nowhere/agent-o1.jsonl"), leg_row("o1", "/nowhere/agent-o1.jsonl")])
    result = run_cli("tokens", RUN, cwd=started)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"by_model": {}, "orchestration": {}}
    assert result.stderr == "note: no transcript found for orchestrator leg o1\n"


def test_a_leg_transcript_is_found_from_the_session_when_its_own_path_is_missing(run_cli, started, tmp_path):
    session = tmp_path / "proj" / "sess.jsonl"
    write_transcript(tmp_path / "proj" / "sess-1" / "subagents" / "agent-o9.jsonl", [assistant_record("m", SONNET, output=4, inp=1)])
    write_ledger(started, [{"kind": "leg", "agent_id": "o9", "agent_transcript_path": None, "session_id": "sess-1", "session_transcript": str(session)}])
    assert json.loads(run_cli("tokens", RUN, cwd=started).stdout)["orchestration"][SONNET]["output"] == 4


def test_a_leg_whose_output_is_a_lower_bound_says_so_in_its_own_note(run_cli, started, tmp_path):
    snapshot = write_transcript(tmp_path / "t" / "agent-o1.jsonl", [assistant_record("m1", SONNET, output=8, stop_reason=None)])
    write_ledger(started, [leg_row("o1", snapshot)])
    result = run_cli("tokens", RUN, cwd=started)
    assert json.loads(result.stdout)["orchestration"][SONNET]["output_lower_bound"] == 1
    assert result.stderr == f"note: the output tokens of {SONNET} (orchestration) are a lower bound: 1 message had no final usage entry, only the snapshot taken as a message starts\n"


def test_the_pass_record_of_a_run_in_legs_carries_the_orchestration_and_validates(run_cli, repo, usage):
    stage, leg = usage
    adopt(repo)
    record = genuine_pass(repo)
    assert "orchestration" not in record["tokens"] and pl.check_record("pass_record", record) == []  # no leg: as before
    write_ledger(repo, [agent_row("s1", transcript=str(stage)), leg_row("o1", leg)])
    again, problems, _copy = pl.make_pass_record(pl.load_project(repo), RUN)
    assert problems == [] and again["tokens"]["orchestration"] == {SONNET: {"output": 300, "fresh_input": 120, "cache_read": 9000, "output_lower_bound": 0}}
    assert pl.check_record("pass_record", again) == []
    rendered = pl.render_record("pass_record", again)
    assert "Orchestration, the legs of the orchestrator, apart from the agents above:" in rendered and f"| {SONNET} | 300 | 120 | 9000 |" in rendered
    assert "Orchestration" not in pl.render_record("pass_record", record)


def test_pass_prints_the_orchestration_tokens_apart(run_cli, repo, usage):
    stage, leg = usage
    adopt(repo, commands={"test": "true"})
    from rundata import write_docs_run

    write_docs_run(repo)
    write_ledger(repo, [agent_row("s1", transcript=str(stage)), leg_row("o1", leg)])
    result = run_cli("pass", RUN, cwd=repo)
    assert result.returncode == 0, result.stdout
    assert f"  tokens {HAIKU}: output 9, fresh input 10, cache read 50" in result.stdout
    assert f"  tokens orchestration {SONNET}: output 300, fresh input 120, cache read 9000" in result.stdout


def test_orchestration_is_an_optional_object_of_the_pass_records_tokens():
    schema = pl.load_schema("pass_record")["properties"]["tokens"]
    assert schema["required"] == ["by_model"] and schema["properties"]["orchestration"] == {"type": "object"}
    assert "orchestration, present when the orchestrator ran the run in legs" in schema["description"]
    from samples import sample

    record = sample("pass_record")
    record["tokens"]["orchestration"] = {"sonnet": {"output": 1, "fresh_input": 2, "cache_read": 3}}
    assert pl.check_record("pass_record", record) == []
    record["tokens"]["orchestration"] = []
    assert any(e.startswith("$.tokens.orchestration:") for e in pl.check_record("pass_record", record))


# ------------------------------------------------------------------------------------------------------------------ README


def test_the_readme_documents_the_orchestrators_lane_the_leg_entry_and_the_tokens():
    from test_readme import README, rows, section

    hooks = section("Hooks")
    assert "**On Read, Grep and Glob, for `plumbline:orchestrator`:** only inside `.plumbline/runs/<the active run>/`" in hooks
    assert "a Glob pattern that leaves that path by `..` or by an absolute or `~` pattern is refused" in hooks and "With no run in progress there is no directory, and nothing is read" in hooks
    assert "- The orchestrator's classes are `plumbline-run` and `git-meta`, and no `VAR=value` goes before either." in hooks
    assert "Its `plan` is `plan --run RUN` with `--json` and `--project` and no option that starts a run" in hooks
    assert "`git status` refuses `-v`, and `git branch` runs only as `git branch --show-current`" in hooks and "It has no search class, so `cat` and `grep` of source are refused" in hooks
    assert "and the orchestrator, whose policy lists none, writes no file" in hooks
    assert "the orchestrator launches the stage agents (`plumbline:planner`, `test-writer`, `builder`, `verifier`, `prosecutor`, `defender`, `detective` and `canary`) and no other agent" in hooks
    assert "which may launch the stage agents itself as the fallback when it does not use the orchestrator" in hooks
    assert "The orchestrator (`plumbline:orchestrator`) is no stage's agent and ends with a report, so no record is asked of it: its stop is let go and enters a `leg` in the ledger" in hooks
    assert "A stop with an empty `agent_type` (a harness helper makes one)" in hooks
    ledger_rows = {r[0].strip("`"): r for r in rows(section("Runs, gates and the pass record").split("**The ledger**", 1)[1].split("**Provenance.**", 1)[0])}
    assert ledger_rows["leg"][1] == "the SubagentStop hook, for the orchestrator" and "Once per agent id" in ledger_rows["leg"][2]
    commands = {r[0].split("`")[1].split(" ")[0]: r for r in rows(section("The command line", 3))}
    assert "`orchestration`, present when the run had legs" in commands["tokens"][1]
    assert "tokens (and, apart, the orchestration's)" in {r[0].strip("`"): r for r in rows(section("Records"))}["pass_record"][2]
    assert "pass_record" in README
