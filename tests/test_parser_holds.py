"""The command parser sees what the shell will run, and no odd input makes the hook look away.

The reviewer's scratch work found lines whose later commands the parser could not see: a commit message with an apostrophe in a
here-document inside `$( )`, `$'it\\'s'`, `<<\\EOF`, nested backticks. A push or an `rm` after such a construct was invisible to the
gate and to every role's allow-list. Real bash is the judge here: a command that bash runs must be one the parser lists.
It also covers the fail-open holes those inputs shared with NUL characters and over-long names: the hook must not crash, and one rule that meets
input it cannot handle must not take the others down."""
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

import pre_tool_use as pre
from hookdata import bash_payload, denial, tool_payload
from rundata import adopt

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is needed to say what a shell runs")


@pytest.fixture(autouse=True)
def hook_errors_surface(monkeypatch):
    monkeypatch.setenv("PLUMBLINE_HOOK_DEBUG", "1")


@pytest.fixture
def adopted(repo):
    adopt(repo)
    return repo


def gate(repo, command, role=None):
    return pre.decide(bash_payload(repo, command, agent_type=f"plumbline:{role}" if role else None))


# --------------------------------------------------------------------------- what the parser makes of the tricky forms


@pytest.mark.parametrize(
    "text,expected",
    [
        ("echo $'it\\'s'; rm x", [["echo", "it's"], ["rm", "x"]]),
        ("echo $'a\\'b' && rm x", [["echo", "a'b"], ["rm", "x"]]),
        ("cat README.md $'\\'' ; rm x", [["cat", "README.md", "'"], ["rm", "x"]]),
        ("$'git' push origin main", [["git", "push", "origin", "main"]]),
        ("$'\\x67it' push", [["git", "push"]]),
        ("$'\\147it' push", [["git", "push"]]),
        ("$'\\u0067it' push", [["git", "push"]]),
        ("$'a\\tb\\nc' x", [["a\tb\nc", "x"]]),
        ("$'\\x' x", [["\\x", "x"]]),
        ("$\"git\" push", [["git", "push"]]),
        ("echo 'plain\\' ; rm x", [["echo", "plain\\"], ["rm", "x"]]),
        ("cat <<\\EOF\nhello\nEOF\nrm x", [["cat"], ["rm", "x"]]),
        ("cat <<'EOF'\nhello\nEOF\nrm x", [["cat"], ["rm", "x"]]),
        ('cat <<"EOF"\nhello\nEOF\nrm x', [["cat"], ["rm", "x"]]),
        ("cat <<E'O'F\nhello\nEOF\nrm x", [["cat"], ["rm", "x"]]),
        ("cat <<'END OF'\nhello\nEND OF\nrm x", [["cat"], ["rm", "x"]]),
        ("cat <<-EOF\n\thello\n\tEOF\nrm x", [["cat"], ["rm", "x"]]),
        ("cat << EOF\nhello\nEOF\nrm x", [["cat"], ["rm", "x"]]),
        ("cat <<EOF\nEOFX\nEOF\nrm x", [["cat"], ["rm", "x"]]),
        ("cat <<EOF; rm x\nbody\nEOF", [["cat"], ["rm", "x"]]),
        ("cat <<A <<B\none\nA\ntwo\nB\nrm x", [["cat"], ["rm", "x"]]),
        ("git commit -m \"$(cat <<'EOF'\nFix: don't crash\nEOF\n)\" && git push origin feature", [["cat"], ["git", "commit", "-m", "$(...)"], ["git", "push", "origin", "feature"]]),
        ("echo \"$(cat <<'EOF'\nSteps: 1) do x\nEOF\n)\" && rm x", [["cat"], ["echo", "$(...)"], ["rm", "x"]]),
        ("echo \"$(cat <<'EOF'\nsteps (1 do x\nEOF\n)\" && rm x", [["cat"], ["echo", "$(...)"], ["rm", "x"]]),
        ("echo \"$(cat <<'EOF'\nsay \"hi\" and `code`\nEOF\n)\" && rm x", [["cat"], ["echo", "$(...)"], ["rm", "x"]]),
        ("echo $(cat <<'EOF'\ndon't\nEOF\n); rm x", [["cat"], ["echo", "$(...)"], ["rm", "x"]]),
        ("cat <<EOF\n$(git push)\nEOF", [["cat"], ["git", "push"]]),
        ("cat <<EOF\n`git push`\nEOF", [["cat"], ["git", "push"]]),
        ("cat <<EOF\n\\$(git push)\nEOF", [["cat"]]),
        ("cat <<'EOF'\n$(git push)\nEOF", [["cat"]]),
        ("cat <<\\EOF\n$(git push)\nEOF", [["cat"]]),
        ("echo `echo \\`true\\``; rm x", [["true"], ["echo", "`...`"], ["echo", "`...`"], ["rm", "x"]]),
        ("echo $(( 1 + 2 )); rm x", [["1", "+", "2"], ["echo", "$(...)"], ["rm", "x"]]),
        ("echo $( (rm x) ); ls", [["rm", "x"], ["echo", "$(...)"], ["ls"]]),
        ("function p { git push; }; p", [["function", "p", "{", "git", "push"], ["}"], ["p"]]),
    ],
)
def test_the_parser_sees_past_the_forms_that_used_to_hide_a_command(text, expected):
    assert pre.split_commands(text) == expected


def test_a_command_after_each_hiding_form_is_a_step_and_a_push_after_it_is_an_action():
    for prefix in (
        "echo $'it\\'s'; ",
        "cat <<\\EOF\nx\nEOF\n",
        "echo \"$(cat <<'EOF'\ndon't\nEOF\n)\" && ",
        "echo \"$(cat <<'EOF'\nSteps: 1) do x\nEOF\n)\" && ",
        "cat <<EOF\n$(echo hi)\nEOF\n",
        "echo `echo \\`true\\``; ",
        "git commit -q -m $'it\\'s done' && ",
    ):
        actions = pre.analyze(prefix + "git push origin feature", Path("/w/repo"))
        assert [a.kind for a in actions if a.kind == "push"] == ["push"], prefix
        steps = pre.walk(prefix + "rm canary", Path("/w/repo"))
        assert any(s.argv[:1] == ["rm"] for s in steps), prefix


def test_the_reviewers_hidden_push_forms_are_denied_and_the_hidden_rm_forms_are_denied_to_the_verifier(adopted):
    pushes = [
        "git commit -q -m \"$(cat <<'EOF'\nFix: don't crash\nEOF\n)\" && git push origin feature",
        "git commit -q -m $'it\\'s done' && git push origin feature",
        "cat <<\\EOF\nx\nEOF\ngit push origin feature",
        "echo `echo \\`true\\``; git push origin feature",
        "$'\\x67it' push origin feature",
        "cat <<EOF\n$(git push origin feature)\nEOF",
    ]
    for command in pushes:
        reason = gate(adopted, command)
        # a line that commits and then pushes is refused for that, whatever the commit's pass; the others for HEAD's missing pass
        assert reason and ("has no pass or override record" in reason or "changes HEAD or a ref and then tries to push" in reason), command
        if "git commit" not in command:
            assert "has no pass or override record" in reason, command
    removals = [
        "echo $'it\\'s'; rm canary1",
        "echo \"$(cat <<'EOF'\ndon't\nEOF\n)\"; rm canary4",
        "cat <<\\EOF\nhello\nEOF\nrm canary2",
        "echo 'ok'; rm canary1",
        "echo \"$(cat <<E\n)\nE\n)\"; rm canary3",
        "$'\\x72m' canary",
        "cat <<EOF\n$(rm canary)\nEOF",
    ]
    for command in removals:
        reason = gate(adopted, command, "verifier")
        assert reason and reason.startswith("plumbline: the verifier's Bash may run only"), command


def test_a_here_document_body_is_not_a_command_when_its_delimiter_is_quoted(adopted):
    assert gate(adopted, "cat <<'EOF'\ngit push origin feature\nEOF") is None
    assert gate(adopted, "cat <<EOF\ngit push origin feature\nEOF") is None  # unquoted: the text is expanded, but `git push` written out is text
    assert gate(adopted, "cat <<'EOF' > notes.md\ngit push origin main\nEOF") is None


# ------------------------------------------------------------------------ real bash as the judge

NAMES = ("git", "rm", "curl", "evil")
STUBS = "".join(f'{n}() {{ echo "RAN {n} $1"; }}\n' for n in NAMES)


def parsed(line):
    found = set()
    for step in pre.walk(line, Path("/nonexistent")):
        if step.argv and os.path.basename(step.argv[0]) in NAMES:
            found.add((os.path.basename(step.argv[0]), step.argv[1] if len(step.argv) > 1 else ""))
    return found


def executed(line):
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", STUBS + line], capture_output=True, text=True, timeout=10, cwd="/tmp", stdin=subprocess.DEVNULL
    )
    found = set()
    for out in result.stdout.splitlines():
        if out.startswith("RAN "):
            parts = out.split(" ", 3)
            found.add((parts[1], parts[2] if len(parts) > 2 else ""))
    return found


CORPUS = [
    "echo $'it\\'s'; rm x",
    "cat <<\\EOF\nhello\nEOF\nrm x",
    "echo \"$(cat <<'EOF'\ndon't\nEOF\n)\" && git push",
    "echo \"$(cat <<'EOF'\nSteps: 1) do x\nEOF\n)\" && git push",
    "echo \"$(cat <<'EOF'\nsteps (1 do x\nEOF\n)\" && curl y",
    "cat <<EOF\n$(rm x)\nEOF",
    "cat <<EOF\n`evil z`\nEOF",
    "echo `echo \\`git push\\``",
    "git commit -m $'it\\'s' && git push",
    "$'git' push",
    "$'\\x67it' push",
    "\"git\" push; 'rm' x; \\curl y",
    "g\"\"it push; r''m x",
    "{ git push; } && ( rm x )",
    "( ( evil z ) )",
    "bash -c 'git push'; eval 'rm x'",
    "echo \"$(echo \"$(git push)\")\"",
    "echo $(echo $(echo $(rm x)))",
    "cat <<-EOF\n\tgit push\n\tEOF\nrm x",
    "cat <<'A B'\nx\nA B\nrm x",
    "cat <<A <<B\n1\nA\n2\nB\ngit push",
    "echo a # rm x\ngit push",
    "echo a#b; git push",
    "echo \"a # b\"; git push",
    "echo $'\\'' ; rm x",
    "echo \"it's\"; rm x",
    "echo 'say \"hi\"'; rm x",
    "true && false || git push",
    "false; git push | cat",
    "for i in 1 2; do git push; done",
    "if true; then rm x; fi",
    "while false; do curl y; done; evil z",
    "function f { rm x; }; f",
    "f() { evil z; }; f",
    "echo $(( 1 + 2 )); git push",
    "echo \"$(( 1 + 2 ))\"; rm x",
    "echo $( (rm x) )",
    "git push\\\n --dry-run",
    "cat <<EOF\n\\$(rm x)\nEOF\ngit push",
    "cat <<\"EOF\"\n$(rm x)\nEOF\ngit push",
]


@needs_bash
@pytest.mark.parametrize("line", CORPUS)
def test_every_command_bash_runs_is_one_the_parser_lists(line):
    missed = executed(line) - parsed(line)
    assert not missed, (missed, line)


def random_line(rng):
    def body():
        return rng.choice(["hello", "don't", "it's ok", "a (b", "c) d", 'say "hi"', "back`tick", "$HOME", "x; y", "# c", "a & b", "{ x }", "EOF2", "it\\'s"])

    def name(command):
        head, _, rest = command.partition(" ")
        style = rng.choice(["plain", "dq", "sq", "split", "bs", "ansi", "hex"])
        head = {"dq": f'"{head}"', "sq": f"'{head}'", "split": head[0] + '""' + head[1:], "bs": "\\" + head, "ansi": f"$'{head}'", "hex": "$'\\x%02x" % ord(head[0]) + head[1:] + "'"}.get(style, head)
        return f"{head} {rest}"

    def simple():
        command = rng.choice(["git push", "rm x", "curl y", "evil z", "git status"])
        return name(command) if rng.random() < 0.5 else command

    def heredoc():
        delimiter = rng.choice(["EOF", "'EOF'", '"EOF"', "\\EOF", "E'O'F"])
        inner = simple()
        sub = rng.choice(["", f"$({inner})", f"`{inner}`"])
        return f"cat <<{delimiter}\n{body()}\n{sub}\nEOF\n"

    def piece(depth=0):
        r = rng.random()
        if r < 0.25:
            return simple()
        if r < 0.40 and depth < 2:
            return f'echo "$({piece(depth + 1)})"'
        if r < 0.50 and depth < 1:
            return f"echo `{piece(depth + 1)}`"
        if r < 0.60 and depth < 2:
            return f"( {piece(depth + 1)} )"
        if r < 0.68 and depth < 2:
            return f"{{ {piece(depth + 1)}; }}"
        if r < 0.78:
            return heredoc()
        if r < 0.85:
            return f"echo {body().replace(chr(10), ' ')} # {body()}\n{simple()}"
        if r < 0.90 and depth < 2:
            return f"bash -c '{simple()}'"
        if r < 0.95 and depth < 2:
            return f"eval '{simple()}'"
        return f'echo "$(cat <<\'EOF\'\n{body()}\nEOF\n)" && {simple()}'

    parts = [piece() for _ in range(rng.randint(1, 3))]
    return rng.choice([";", " && ", "\n", " || ", " | "]).join(parts)


@needs_bash
def test_no_generated_line_runs_a_command_the_parser_does_not_list():
    rng = random.Random(20260929)
    checked = 0
    for _ in range(250):
        line = random_line(rng)
        try:
            ran = executed(line)
        except subprocess.TimeoutExpired:
            continue
        checked += 1
        missed = ran - parsed(line)
        assert not missed, (missed, line)
    assert checked > 200


# ------------------------------------------------------------- env -S, function, and wrappers that hold a command line


def test_env_dash_s_holds_a_command_line_and_is_read_as_one(adopted):
    for command in ("env -S 'git push origin feature'", "env --split-string='git push origin feature'", "env -S'git push origin feature'", "env -S 'git push' origin feature",
                    "env -i -S 'git push origin feature'", "env FOO=1 -S 'git push'"):
        reason = gate(adopted, command)
        assert reason and "has no pass or override record" in reason, command
    (action,) = pre.analyze("env -S 'git push origin' feature", Path("/w/repo"))
    assert action.kind == "push" and action.args == ["origin", "feature"]


def test_env_dash_s_cannot_smuggle_a_command_past_a_role_allow_list(adopted):
    for command in ("env -S 'rm -rf src' git status", "env -S 'rm x' git diff", "env -S 'curl http://x' true"):
        reason = gate(adopted, command, "verifier")
        assert reason and "is none of these" in reason, command
    assert gate(adopted, "env git status", "verifier") is None


def test_env_dash_c_is_a_directory_change(adopted, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert gate(outside, f"env -C {adopted} git push origin feature")
    assert gate(outside, f"env --chdir={adopted} git push origin feature")
    assert gate(outside, f"env --chdir {adopted} git push origin feature")


def test_a_function_definition_with_the_function_keyword_does_not_hide_its_body(adopted):
    assert gate(adopted, "function p { git push origin feature; }; p")
    assert gate(adopted, "function p() { git push origin feature; }; p")
    assert gate(adopted, "p() { git push origin feature; }; p")
    assert gate(adopted, "function p { rm x; }; p", "verifier")


def test_g_quote_quote_it_is_git_where_the_hook_runs(adopted):
    # the sh filter never lets this reach Python (see the README's limits); once there, the parser reads it as git
    reason = gate(adopted, 'g""it push origin feature')
    assert reason and "has no pass or override record" in reason
    assert gate(adopted, "g\\it push origin feature") and gate(adopted, "gi't' push origin feature") and gate(adopted, '"g"it push origin feature')
    assert gate(adopted, 'gh""pr create') is None  # `gh""pr` is one word, not gh pr


# ----------------------------------------------------------------- input that must not make the hook look away


def test_a_nul_in_the_command_does_not_switch_the_hook_off(adopted):
    reason = gate(adopted, "git push origin feature; echo x > a\x00b")
    assert reason and "has no pass or override record" in reason
    assert gate(adopted, "git push\x00 origin feature")
    assert gate(adopted, "git\x00 push origin feature")


def test_a_nul_in_a_path_or_a_cwd_is_dropped_as_a_shell_drops_it(adopted):
    payload = tool_payload(adopted, "Write", {"file_path": str(adopted / ".plumbline" / "pass" / "x\x00.json"), "content": "x"})
    assert "written only by plumbline.py commands" in pre.decide(payload)
    payload = tool_payload(adopted, "Write", {"file_path": str(adopted / ".git" / "co\x00nfig"), "content": "x"}, agent_type="plumbline:builder")
    assert pre.decide(payload)
    payload = bash_payload(adopted, "git push origin feature") | {"cwd": str(adopted) + "\x00"}
    assert pre.decide(payload)


def test_the_scrub_leaves_everything_but_the_nul_alone():
    data = {"a": "x\x00y", "b": ["p\x00", {"c": "q\x00\x00r"}], "d": 5, "e": None, "f": True}
    assert pre._scrub(data) == {"a": "xy", "b": ["p", {"c": "qr"}], "d": 5, "e": None, "f": True}


def test_an_over_long_name_does_not_switch_the_hook_off(adopted):
    long_name = "a" * 300
    for command in (f"git push origin feature; echo x > {long_name}", f"cd {long_name}; git push origin feature", f"git push origin feature; rm {long_name}/{long_name}"):
        reason = gate(adopted, command)
        assert reason and "has no pass or override record" in reason, command[:60]
    payload = tool_payload(adopted, "Write", {"file_path": str(adopted / long_name / "x"), "content": "x"}, agent_type="plumbline:builder")
    assert pre.decide(payload) is None  # nothing wrong with it as far as plumbline goes: the write itself fails, if it must
    for tool in ("Read", "Grep"):
        payload = tool_payload(adopted, tool, {"file_path": long_name, "path": long_name, "pattern": "x"}, agent_type="plumbline:builder")
        pre.decide(payload)  # no exception (PLUMBLINE_HOOK_DEBUG is on)


def test_an_event_whose_tool_input_is_not_an_object_is_ignored_not_a_crash(adopted):
    for tool in ("Bash", "Monitor", "PowerShell", "Read", "Grep", "Write", "Edit", "NotebookEdit", "Agent", "Task"):
        for tool_input in ("git push", None, 5, ["git push"]):
            assert pre.decide({"tool_name": tool, "tool_input": tool_input, "cwd": str(adopted)}) is None, (tool, tool_input)
    for event in ("git push", None, 5, ["git push"], {"tool_name": ["Bash"], "tool_input": {"command": "git push"}}):
        assert pre.decide(event) is None, event


def test_an_event_with_a_byte_order_mark_is_read(run_pre, adopted):
    import json

    text = "\ufeff" + json.dumps(bash_payload(adopted, "git push origin feature"))
    assert "has no pass or override record" in denial(run_pre(text, adopted))


def test_one_rule_that_meets_input_it_cannot_handle_does_not_take_the_others_down(adopted, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("a system call failed")

    monkeypatch.delenv("PLUMBLINE_HOOK_DEBUG")
    monkeypatch.setattr(pre, "protected_command_reason", broken)
    monkeypatch.setattr(pre, "override_reason", broken)
    reason = pre.decide(bash_payload(adopted, "git push origin feature"))
    assert reason and "has no pass or override record" in reason  # the push gate still speaks
    monkeypatch.setattr(pre, "gate_reason", broken)
    monkeypatch.setattr(pre, "role_command_reason", broken)
    assert pre.decide(bash_payload(adopted, "git push origin feature")) is None  # every rule broken: nothing is said, as before
    monkeypatch.setenv("PLUMBLINE_HOOK_DEBUG", "1")
    with pytest.raises(OSError):
        pre.decide(bash_payload(adopted, "git push origin feature"))


def test_a_rule_that_breaks_on_one_place_does_not_hide_the_others_places(adopted, monkeypatch):
    real = pre.adopted_root

    def picky(pl, directory):
        if str(directory).endswith("poison"):
            raise OSError("cannot look there")
        return real(pl, directory)

    monkeypatch.delenv("PLUMBLINE_HOOK_DEBUG")
    monkeypatch.setattr(pre, "adopted_root", picky)
    assert pre.decide(bash_payload(adopted, "cd poison; git push origin feature"))


def test_the_hook_process_survives_the_inputs_and_still_denies(run_pre, adopted):
    payload = bash_payload(adopted, "git push origin feature; echo x > a\x00b")
    assert "has no pass or override record" in denial(run_pre(payload, adopted))
    payload = bash_payload(adopted, "git push origin feature; echo x > " + "a" * 300)
    assert "has no pass or override record" in denial(run_pre(payload, adopted))


@pytest.mark.parametrize(
    "text",
    ["", " ", "\n", ";", "&&", "||", "(", ")", "$(", "`", "'", '"', "\\", "<<", "<<EOF", "<<'", "$'", "$'\\", "$'\\x", "$\"", "echo $(", "echo $(echo $(", "cat <<EOF\n$(", "cat <<EOF\n`",
     "cat <<\\", "cat <<-", "echo `\\`", "echo \"$(cat <<'EOF'\nx", "((((((((((", "))))))))))", "$(" * 30 + "git push" + ")" * 30, "`" * 41, "\x00", "é", "#", "a=", "=", "<>", ">|", "2>&"],
)
def test_the_parser_survives_odd_and_unbalanced_input(text):
    pre.split_commands(text)
    pre.walk(text, Path("/w"))
    pre.analyze(text, Path("/w"))


def test_a_deeply_nested_line_is_cut_off_not_followed_into_the_ground():
    text = "git push"
    for _ in range(12):
        text = f"echo $({text})"
    pre.split_commands(text)  # no exception, no hang
    assert pre.analyze(text, Path("/w")) == []  # nested deeper than the parser follows


def test_a_long_line_is_parsed_in_reasonable_time():
    import time

    text = "; ".join(["echo 'a b' \"c d\" $(echo x) `echo y` > /dev/null"] * 3000)
    started = time.time()
    pre.walk(text, Path("/w"))
    assert time.time() - started < 5
