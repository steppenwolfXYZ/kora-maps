#!/usr/bin/env bash
# Start a build on THIS machine (the data machine) detached from the
# calling SSH session, and return immediately.
#
# Run over SSH by the Mac's scripts/remote_build.sh; also fine by hand.
# The point is that the build must outlive the connection that started
# it — a closed lid or a WiFi switch must cost you the view, not the run.
#
# Usage: ./scripts/run_build_detached.sh [build args…]
#
#   --no-pull   skip the `git pull --ff-only` that normally precedes the
#               build (use when testing uncommitted local changes).
#   --session N tmux session name (default: kora-build).
#   everything else is forwarded verbatim to scripts/update_map.sh.
#
# Contract with the caller — three files in the repo root:
#
#   build.log     the run's full output, live.
#   build.status  written ONLY when the run finishes; contains the build's
#                 real exit code. This is the sole completion signal. The
#                 log is for humans; never infer success from its tail.
#   build.args    what was launched, for the record.
#
# build.status is deleted before the build starts, so a previous run's
# stamp can never be read as this one's result.
#
# The exit code has to survive the pipe into tee — hence PIPESTATUS.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

SESSION=kora-build
PULL=1
BUILD_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-pull)  PULL=0; shift ;;
    --session)  SESSION="$2"; shift 2 ;;
    --session=*) SESSION="${1#*=}"; shift ;;
    *)          BUILD_ARGS+=("$1"); shift ;;
  esac
done

command -v tmux >/dev/null 2>&1 || { echo "tmux not installed" >&2; exit 1; }

# One build at a time. Two concurrent runs would fight over the same
# working tree and the same artifacts.
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "error: a build is already running in tmux session '$SESSION'." >&2
  echo "       attach with: tmux attach -t $SESSION" >&2
  exit 1
fi

rm -f "$ROOT/build.status"
printf '%s\n' "${BUILD_ARGS[*]-}" > "$ROOT/build.args"

# `exec` is deliberately absent: the shell must outlive update_map.sh to
# write the stamp. PIPESTATUS[0] is the build's status, not tee's.
tmux new-session -d -s "$SESSION" -c "$ROOT" bash -c "
  set -o pipefail
  {
    if [ $PULL -eq 1 ]; then
      echo '── git pull --ff-only'
      git pull --ff-only || exit 1
    fi
    ./scripts/update_map.sh ${BUILD_ARGS[*]-}
  } 2>&1 | tee '$ROOT/build.log'
  echo \${PIPESTATUS[0]} > '$ROOT/build.status'
"

echo "started: tmux session '$SESSION'"
echo "  args:   ${BUILD_ARGS[*]-<none>}"
echo "  log:    $ROOT/build.log"
echo "  stamp:  $ROOT/build.status (written on completion)"
