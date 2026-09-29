#!/bin/sh
# plumbline's SubagentStop hook: a filter in front of subagent_stop.py.
#
# Every subagent that stops runs this hook, plumbline's or not. Only a plumbline agent's stop concerns plumbline
# (its agent_type is `plumbline:<name>`), so every other input exits 0 at once, with no output, and Python is not
# started. For a plumbline agent, Python's answer and its exit status (2 blocks the stop) pass through unchanged.
input=$(cat)
case $input in
  *'plumbline:'*) ;;
  *) exit 0 ;;
esac
case $0 in
  */*) here=${0%/*} ;;
  *) here=. ;;
esac
printf '%s\n' "$input" | python3 "$here/subagent_stop.py"
exit $?
