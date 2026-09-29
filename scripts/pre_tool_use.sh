#!/bin/sh
# plumbline's PreToolUse hook: a filter in front of pre_tool_use.py.
#
# Claude Code runs a plugin's PreToolUse hook for every tool call that matches, in every session, and a plugin
# agent cannot carry a hook of its own, so this hook sees calls that have nothing to do with plumbline. Starting
# Python for each of them costs 25-35 ms. This script reads the input once and starts Python only when the input
# could concern plumbline; every other call exits 0 at once, with no output.
#
# It could concern plumbline when the input mentions
#   plumbline   every plumbline agent's agent_type, the .plumbline/ paths, plumbline.toml and plumbline.py;
#   override    a command that runs `plumbline.py override` through a variable;
#   git         followed by a space (so paths such as /github/ do not match), or by what the shell also takes
#               as a separator or a quote: a tab or a newline as JSON writes them (\t, \n, \r), a backslash, a quote;
#   gh          the same way (`gh --repo owner/name pr create` is a pull request, so `gh pr` alone would miss it).
# Python's own answer, and its exit status, pass through unchanged.
input=$(cat)
case $input in
  *plumbline*|*override*) ;;
  *'git '*|*'git\t'*|*'git\n'*|*'git\r'*|*'git\\'*|*"git'"*|*'git\"'*) ;;
  *'gh '*|*'gh\t'*|*'gh\n'*|*'gh\r'*|*'gh\\'*|*"gh'"*|*'gh\"'*) ;;
  *) exit 0 ;;
esac
case $0 in
  */*) here=${0%/*} ;;
  *) here=. ;;
esac
printf '%s\n' "$input" | python3 "$here/pre_tool_use.py"
exit $?
