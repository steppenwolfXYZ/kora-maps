#!/usr/bin/env bash
# Build on the data machine (Kranich), from anywhere, and leave this Mac
# serving the result. One command, four phases:
#
#   launch     start the build over SSH, detached, and return
#   watch      stream its log here until the completion stamp appears
#   fetch      pull the artifacts back (scripts/fetch_build.sh)
#   post-sync  make them serve (scripts/post_sync.sh)
#
# The build is parented to a tmux server on Kranich, not to the SSH
# session, so a closed lid or a network change costs you the view and
# nothing else. Losing the stream is never itself a failure: success is
# decided solely by the exit-code stamp the build writes when it ends.
#
# This Mac may sleep during the build. It must stay awake for the fetch
# and post-sync legs (Amphetamine).
#
# Usage:
#   ./scripts/remote_build.sh [orchestrator flags] [build flags…]
#
#   --watch-only     do not launch; attach to a build already running
#   --fetch-only     skip launch and watch; fetch and post-sync only
#   --no-post-sync   stop after fetching
#   --no-fetch       stop after the build finishes
#   --no-pull        do not `git pull` on Kranich before building
#   -n, --dry-run    print the plan and the phase decisions, do nothing
#
# Every unrecognised argument is forwarded verbatim to update_map.sh
# (--osm, --skip-gtfs, --skip-deploy, --only-pipeline, --only-routing).
# The build's flag surface deliberately lives on the build script; this
# one is transport and does not interpret it.
#
# Resuming: each phase is independently entered, so a run interrupted
# anywhere is picked up with --watch-only or --fetch-only rather than
# started over.
#
# Env:
#   KRANICH_REMOTE  SSH alias of the data machine   (default: kranich)
#   KRANICH_PATH    repo path over there            (default: ~/Prog/kora-maps)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

REMOTE="${KRANICH_REMOTE:-kranich}"
REMOTE_PATH="${KRANICH_PATH:-~/Prog/kora-maps}"
REMOTE_PATH="${REMOTE_PATH%/}"
SESSION=kora-build

DO_LAUNCH=1; DO_WATCH=1; DO_FETCH=1; DO_POST=1
PULL_ARGS=(); DRY_RUN=0
BUILD_ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --watch-only)   DO_LAUNCH=0; shift ;;
    --fetch-only)   DO_LAUNCH=0; DO_WATCH=0; shift ;;
    --no-post-sync) DO_POST=0; shift ;;
    --no-fetch)     DO_FETCH=0; DO_POST=0; shift ;;
    --no-pull)      PULL_ARGS+=(--no-pull); shift ;;
    -n|--dry-run)   DRY_RUN=1; shift ;;
    *)              BUILD_ARGS+=("$1"); shift ;;
  esac
done

T0=$(date +%s)
banner() {
  printf '\n\033[1m══ %s\033[0m  (%s min elapsed)\n' "$1" "$(( ($(date +%s) - T0) / 60 ))"
}
die() { echo "error: $*" >&2; exit 1; }

rssh() { ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=6 "$REMOTE" "$@"; }

# ── Preflight ────────────────────────────────────────────────────────
banner "Preflight"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE" true 2>/dev/null \
  || die "cannot reach '$REMOTE' over SSH — is Tailscale up?"
# Resolve to an absolute path once: a leading ~ stays literal inside the
# single-quoted remote commands below. `cd && pwd` expands it and proves
# it exists in one step.
REMOTE_PATH="$(rssh "cd $REMOTE_PATH 2>/dev/null && pwd")" \
  || die "${KRANICH_PATH:-~/Prog/kora-maps} not found on $REMOTE (set KRANICH_PATH)"

LIVE=0
if rssh "tmux has-session -t '$SESSION' 2>/dev/null"; then LIVE=1; fi

echo "  remote:     $REMOTE:$REMOTE_PATH"
echo "  build args: ${BUILD_ARGS[*]-<none>}"
echo "  phases:     launch=$DO_LAUNCH watch=$DO_WATCH fetch=$DO_FETCH post-sync=$DO_POST"
echo "  build live: $([ $LIVE -eq 1 ] && echo "yes (tmux $SESSION)" || echo no)"

if [ "$DRY_RUN" -eq 1 ]; then
  echo ""
  echo "(dry run — nothing was started, fetched or restarted)"
  exit 0
fi

if [ $DO_LAUNCH -eq 1 ] && [ $LIVE -eq 1 ]; then
  die "a build is already running on $REMOTE — use --watch-only to follow it"
fi

# ── Phase 1: launch ──────────────────────────────────────────────────
# The remote wrapper resets the stamp, starts tmux and returns; nothing
# here holds a connection open for the duration of the build.
if [ $DO_LAUNCH -eq 1 ]; then
  banner "Launch"
  rssh "cd '$REMOTE_PATH' && ./scripts/run_build_detached.sh \
        --session '$SESSION' ${PULL_ARGS[*]-} ${BUILD_ARGS[*]-}" \
    || die "could not start the build"
fi

# ── Phase 2: watch ───────────────────────────────────────────────────
# Two independent things happen here. The log tail is cosmetic and may
# drop; the stamp poll is authoritative and reconnects freely. A build
# that takes hours will outlive several tails without anyone noticing.
if [ $DO_WATCH -eq 1 ]; then
  banner "Watch"
  echo "  streaming $REMOTE:$REMOTE_PATH/build.log — Ctrl-C detaches, the build keeps running"
  echo ""
  LOCAL_LOG="$ROOT/data/transit/logs/remote-build.log"
  mkdir -p "$(dirname "$LOCAL_LOG")"
  : > "$LOCAL_LOG"

  while :; do
    # Stamp first: a build that finished while we were disconnected must
    # not start another tail.
    if rssh "[ -f '$REMOTE_PATH/build.status' ]" 2>/dev/null; then break; fi

    # Tail until it dies (connection drop) or the stamp appears. The
    # remote side stops the tail itself so we do not have to kill it.
    # Absolute paths throughout, deliberately. Written as
    # `cd … && tail … &` the ampersand backgrounds the whole cd+tail list,
    # so the cd takes effect only inside the subshell and the poll below
    # then tests build.status relative to $HOME — and waits forever.
    rssh "tail -n +1 -F '$REMOTE_PATH/build.log' 2>/dev/null &
          TP=\$!
          while [ ! -f '$REMOTE_PATH/build.status' ]; do sleep 5; done
          sleep 2
          kill \$TP 2>/dev/null" 2>/dev/null | tee -a "$LOCAL_LOG" || true

    if rssh "[ -f '$REMOTE_PATH/build.status' ]" 2>/dev/null; then break; fi
    echo ""
    printf '\033[33m  … stream dropped, reconnecting in 15 s (the build is unaffected)\033[0m\n'
    sleep 15
  done

  STATUS="$(rssh "cat '$REMOTE_PATH/build.status'" 2>/dev/null || echo "")"
  [ -n "$STATUS" ] || die "build finished but wrote no exit code — inspect $REMOTE:$REMOTE_PATH/build.log"

  banner "Build finished — exit $STATUS"
  if [ "$STATUS" != "0" ]; then
    # Bring the whole log down so the failure is readable offline.
    scp -q "$REMOTE:$REMOTE_PATH/build.log" "$ROOT/data/transit/logs/remote-build-failed.log" \
      2>/dev/null && echo "  full log: data/transit/logs/remote-build-failed.log"
    die "the build failed on $REMOTE — not fetching"
  fi
fi

# ── Phase 3: fetch ───────────────────────────────────────────────────
if [ $DO_FETCH -eq 1 ]; then
  banner "Fetch"
  KRANICH_REMOTE="$REMOTE" KRANICH_PATH="$REMOTE_PATH" ./scripts/fetch_build.sh \
    || die "fetch failed — rerun with --fetch-only once the link is back"
fi

# ── Phase 4: post-sync ───────────────────────────────────────────────
# Unchanged: rebuilds the sidecar hardlink farm, decides whether to
# re-import, restarts Valhalla then MOTIS, and smoke-tests a real query.
# Success therefore means "this Mac serves the new data", not "files
# arrived".
if [ $DO_POST -eq 1 ]; then
  banner "Post-sync"
  ./scripts/post_sync.sh || die "post_sync failed — see its output above"
fi

banner "Done — $(( ($(date +%s) - T0) / 60 )) min total"
