#!/bin/sh
# plumbline's PreToolUse hook: a filter in front of pre_tool_use.py.
#
# Claude Code runs a plugin's PreToolUse hook for every tool call that matches, in every session, and a plugin
# agent cannot carry a hook of its own, so this hook sees calls that have nothing to do with plumbline. Starting
# Python for each of them costs 25-35 ms. This script reads the input once and starts Python only when the input
# could concern plumbline; every other call exits 0 at once, with no output.
#
# It could concern plumbline when the input mentions
#   plumbline   every plumbline agent's agent_type, the .plumbline/ paths, plumbline.toml and plumbline.py (in capitals too: some file systems do not tell them apart);
#   override    a command that runs `plumbline.py override` through a variable;
#   git         followed by a space (so paths such as /github/ do not match), or by what the shell also takes
#               as a separator or a quote: a tab or a newline as JSON writes them (\t, \n, \r), a backslash, a quote;
#   gh          the same way (`gh --repo owner/name pr create` is a pull request, so `gh pr` alone would miss it);
#   git-, git.  the dashed program (`git-push`) and a Windows `git.exe` (`gh.exe` too); a redirection right after the name (`git>/dev/null push`).
# Python's own answer, and its exit status, pass through unchanged.
#
# Without python3 the hook cannot look at the call, and the `|| true` that hooks.json appends would turn the
# failure into silence. So when the input passed the filter and python3 cannot be found (or, as a stripped shim does,
# answers "not found"), this script looks for plumbline itself: in a repository whose git top level holds a
# plumbline.toml it prints a PreToolUse deny that says what is missing; anywhere else it exits 0 without a word.
input=$(cat)
case $input in
  *plumbline*|*PLUMBLINE*|*Plumbline*|*override*) ;;
  *'git '*|*'git\t'*|*'git\n'*|*'git\r'*|*'git\\'*|*"git'"*|*'git\"'*) ;;
  *'gh '*|*'gh\t'*|*'gh\n'*|*'gh\r'*|*'gh\\'*|*"gh'"*|*'gh\"'*) ;;
  *'git-'*|*'git.'*|*'git>'*|*'git<'*|*'gh.'*|*'gh>'*|*'gh<'*) ;;
  *) exit 0 ;;
esac
case $0 in
  */*) here=${0%/*} ;;
  *) here=. ;;
esac

# No python3: deny where plumbline is adopted, stay silent elsewhere. Only shell builtins are needed to find the
# directory and the plumbline.toml; git is asked first, and the walk upwards stands in for it where git is missing
# or will not answer (a working directory inside .git, a repository owned by someone else).
no_python() {
  cwd=
  case $input in
    *'"cwd"'*)
      rest=${input#*'"cwd"'}
      rest=${rest#*:}
      while :; do
        case $rest in
          ' '*|'	'*) rest=${rest#?} ;;
          *) break ;;
        esac
      done
      case $rest in
        '"'*) rest=${rest#'"'}; cwd=${rest%%'"'*} ;;
      esac
      ;;
  esac
  [ -n "$cwd" ] && [ -d "$cwd" ] || cwd=${CLAUDE_PROJECT_DIR:-$PWD}
  top=
  if [ -n "$cwd" ]; then
    top=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null) || top=
  fi
  if [ -z "$top" ]; then
    dir=$cwd
    while :; do
      case $dir in
        /*) ;;
        *) break ;;
      esac
      if [ -f "$dir/plumbline.toml" ] && [ -e "$dir/.git" ]; then
        top=$dir
        break
      fi
      [ "$dir" = / ] && break
      parent=${dir%/*}
      dir=${parent:-/}
    done
  fi
  if [ -n "$top" ] && [ -f "$top/plumbline.toml" ]; then
    printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"plumbline needs python3 on PATH: this repository has adopted plumbline, and its PreToolUse hook cannot look at this call without it. Install python3 (3.11 or newer) or add it to PATH, then retry."}}'
  fi
  exit 0
}

command -v python3 >/dev/null 2>&1 || no_python
printf '%s\n' "$input" | python3 "$here/pre_tool_use.py"
status=$?
case $status in
  126|127) no_python ;;
esac
exit $status
