#!/usr/bin/env bash
# Map data build: fresh GTFS (and optionally OSM) → transit pipeline →
# routing stack (Valhalla tiles, footpath matrix, MOTIS import) → deploy
# map assets, Valhalla, and MOTIS data.
#
# This is the data machine's routine (see the Mac / data-machine split in
# .claude/rules/deployment.md). It never touches app code and never ships
# a MOTIS or Valhalla image — the server keeps the arm64 images the dev
# Mac built; only indexes and tiles go out (`deploy_motis.sh --data-only`,
# `deploy_valhalla.sh --data-only`).
#
# Usage: ./scripts/update_map.sh [--osm] [--skip-gtfs] [--skip-deploy]
#                                [--only-pipeline | --only-routing]
#
# Two independent axes. Which branch to build:
#
#   --only-pipeline  transit pipeline and map emission only. Skips routing
#                    prep, the footpath matrix, the MOTIS import and the
#                    local routing smoke test.
#   --only-routing   routing prep, matrix, MOTIS import and smoke test
#                    only. Skips map emission — and the whole GTFS chain
#                    with it, building on the routed feed already on disk,
#                    since not re-shaping it is the point of the flag.
#   (neither)        both branches, the full build.
#
# And what to refresh first:
#
#   --osm            also re-download the country PBFs (~12 GB) and
#                    rebuild the Valhalla tiles. Default refreshes GTFS +
#                    atlas only — OSM changes slowly, GTFS twice a week.
#   --skip-gtfs      do not download GTFS/atlas at all; build on the feed
#                    already on disk.
#
# And where the pipeline starts:
#
#   --pipeline-from N  skip every pipeline step below N, reusing what those
#                    steps produced last time. N is a rebuild_transit.sh
#                    step number:
#
#                      4  gtfs_prep → pfaedle → emit  (skips downloads+extracts)
#                      5  pfaedle → emit
#                      6  emit (6,7,8)     — the common iteration, ~11 min
#                      7  emit (7,8)       — stops/pills/style + tiles
#                      8  emit (8)         — pmtiles only
#
#                    Not the same as --skip-gtfs, which skips only the
#                    download and still runs steps 2-4. Meaningless with
#                    --only-routing, and rejected there rather than ignored.
#                    Every artifact a skipped step would have produced is
#                    checked in preflight, named by file rather than flag.
#
#   --skip-deploy    run everything locally, deploy nothing.
#
# Deploy scope follows the branch: --only-pipeline deploys map assets,
# --only-routing deploys Valhalla and MOTIS data. Nothing to name by hand.
#
# Preconditions a branch cannot satisfy itself are checked in preflight,
# so a wrong flag fails in seconds rather than hours in.
#
# Scheduling: the stages form a DAG, not a line, and run as such —
# independent stages overlap as background jobs:
#
#   downloads   GTFS ∥ OSM
#   extracts    bbox/OSM extracts (3) ∥ GTFS preprocess (4)
#   routing     pfaedle (5, sharded) ∥ OSM patch + Valhalla tiles
#   emit        matrix (needs 5 + Valhalla) ∥ steps 6 → 7 → 8
#   import      MOTIS import + local smoke test
#   deploy      Valhalla, MOTIS data, map assets → production smoke test
#
# A branch selection prunes stages from that graph; what remains keeps
# its overlap.
#
# Any stage failure aborts the run before the deploy phase, so the
# server never receives a half-updated dataset. Per-stage wall times are
# printed at the end — that table is what tells you where the critical
# path is. Run under `tee` to keep the log:
#   ./scripts/update_map.sh 2>&1 | tee update.log
#
# Sizing (env, defaults tuned for the 24-thread / 62 GB data machine —
# lower them on smaller boxes):
#   PFAEDLE_JOBS     parallel pfaedle containers (each loads the OSM
#                    graph, several GB RAM)                        default 6
#   TIPPECANOE_JOBS  concurrent tippecanoe builds                  default 8
#   VALHALLA_THREADS Valhalla serving pool                         default nproc
#   MATRIX_WORKERS   matrix builder client threads                 default VALHALLA_THREADS+4
set -euo pipefail
cd "$(dirname "$0")/.."

WITH_OSM=0
DEPLOY=1
SKIP_GTFS=0
BRANCH=both          # both | pipeline | routing
FROM=1               # lowest pipeline step to run (1 = everything)
while [ $# -gt 0 ]; do
  case "$1" in
    --osm)              WITH_OSM=1 ;;
    --skip-gtfs)        SKIP_GTFS=1 ;;
    --skip-deploy)      DEPLOY=0 ;;
    --only-pipeline)    BRANCH=pipeline ;;
    --only-routing)     BRANCH=routing ;;
    --pipeline-from)    shift; FROM="${1:-}" ;;
    --pipeline-from=*)  FROM="${1#*=}" ;;
    -h|--help)          sed -n '2,84p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2
       echo "usage: $0 [--osm] [--skip-gtfs] [--skip-deploy]" >&2
       echo "          [--only-pipeline | --only-routing] [--pipeline-from N]" >&2
       exit 2 ;;
  esac
  shift
done

if ! [[ "$FROM" =~ ^[1-8]$ ]]; then
  echo "--pipeline-from must be a step between 1 and 8 (got '$FROM')" >&2; exit 2
fi
if [[ $FROM -gt 1 && "$BRANCH" == "routing" ]]; then
  echo "--pipeline-from is meaningless with --only-routing (no pipeline steps run)" >&2
  exit 2
fi

# Branch selection drives everything downstream: which stages run, and
# therefore which artifacts exist to deploy. The user never names deploy
# targets — choosing a branch already says what was built.
case "$BRANCH" in
  both)     DO_PIPELINE=1; DO_ROUTING=1 ;;
  pipeline) DO_PIPELINE=1; DO_ROUTING=0 ;;
  routing)  DO_PIPELINE=0; DO_ROUTING=1 ;;
esac

# --only-routing builds on the routed feed already on disk. Re-shaping it
# with pfaedle is exactly the work that flag exists to avoid, so the
# whole GTFS-side chain (download → preprocess → pfaedle) is skipped too.
RUN_GTFS_CHAIN=$DO_PIPELINE

# --pipeline-from N prunes every pipeline step below N, on top of whatever
# the branch already decided. Each stage below is simply "my step is >= N
# and my branch is on"; the artifacts the skipped steps would have
# produced are then required to exist, which preflight checks.
step_due() { [[ $1 -ge $FROM ]]; }

RUN_GTFS_DL=0; RUN_OSM_DL=0; RUN_OSM_EXTRACT=0; RUN_GTFS_PREP=0; RUN_PFAEDLE=0
if [[ $RUN_GTFS_CHAIN -eq 1 ]]; then
  if step_due 1 && [[ $SKIP_GTFS -eq 0 ]]; then RUN_GTFS_DL=1; fi
  if step_due 4; then RUN_GTFS_PREP=1; fi
  if step_due 5; then RUN_PFAEDLE=1; fi
fi
# Steps 2 and 3 serve both branches: the routing branch needs the bbox PBF
# behind the Valhalla tiles just as pfaedle does. They are pruned by
# --pipeline-from but not by the branch.
if step_due 2; then RUN_OSM_DL=1; fi
if step_due 3; then RUN_OSM_EXTRACT=1; fi

# Step 6/7/8 all live in the single `emit` stage; narrow its step list.
EMIT_STEPS=""
if [[ $DO_PIPELINE -eq 1 ]]; then
  for n in 6 7 8; do
    if step_due $n; then EMIT_STEPS="${EMIT_STEPS:+$EMIT_STEPS,}$n"; fi
  done
fi

export PFAEDLE_JOBS="${PFAEDLE_JOBS:-6}"
export TIPPECANOE_JOBS="${TIPPECANOE_JOBS:-8}"
if [[ -z "${VALHALLA_THREADS:-}" ]]; then
  export VALHALLA_THREADS="$(nproc 2>/dev/null || echo 8)"
fi
export MATRIX_WORKERS="${MATRIX_WORKERS:-$(( VALHALLA_THREADS + 4 ))}"

T0=$(date +%s)
LOGDIR=data/transit/logs
mkdir -p "$LOGDIR"
declare -A STAGE_START STAGE_SECS
declare -a STAGE_ORDER

now() { date +%s; }
banner() {
  echo ""
  echo "══════════════════════════════════════════"
  echo "  $1   ($(( ($(now) - T0) / 60 )) min elapsed)"
  echo "══════════════════════════════════════════"
}

# ── Background job helpers ───────────────────────────────────────────
# run_bg NAME cmd… — start a stage in the background; its output is
# prefixed with [NAME] so interleaved logs stay readable, and copied to
# $LOGDIR/NAME.log. wait_all NAME… — wait for those stages; any failure
# prints the failing stage's log tail and aborts the whole run.
declare -A PID_OF
run_bg() {
  local name="$1"; shift
  STAGE_START[$name]=$(now)
  STAGE_ORDER+=("$name")
  echo "  ▶ $name: $*"
  (
    set -o pipefail
    "$@" 2>&1 | tee "$LOGDIR/$name.log" | sed -u "s/^/[$name] /"
  ) &
  PID_OF[$name]=$!
}
wait_all() {
  local failed=()
  for name in "$@"; do
    if wait "${PID_OF[$name]}"; then
      STAGE_SECS[$name]=$(( $(now) - STAGE_START[$name] ))
      echo "  ✓ $name done in $(( STAGE_SECS[$name] / 60 )) min"
    else
      STAGE_SECS[$name]=$(( $(now) - STAGE_START[$name] ))
      failed+=("$name")
      echo "  ✗ $name FAILED after $(( STAGE_SECS[$name] / 60 )) min"
    fi
  done
  if (( ${#failed[@]} )); then
    # Let sibling stages finish cleanly rather than leaving half-written
    # outputs behind, then abort.
    wait || true
    for name in "${failed[@]}"; do
      echo ""; echo "--- tail of $LOGDIR/$name.log ---"
      tail -n 30 "$LOGDIR/$name.log" || true
    done
    echo ""; echo "update aborted in stage(s): ${failed[*]}" >&2
    exit 1
  fi
}
# run_fg NAME cmd… — foreground stage with the same bookkeeping.
run_fg() {
  local name="$1"; shift
  STAGE_START[$name]=$(now); STAGE_ORDER+=("$name")
  echo "  ▶ $name: $*"
  if ! "$@" 2>&1 | tee "$LOGDIR/$name.log" | sed -u "s/^/[$name] /"; then
    STAGE_SECS[$name]=$(( $(now) - STAGE_START[$name] ))
    echo "  ✗ $name FAILED — see $LOGDIR/$name.log" >&2
    exit 1
  fi
  STAGE_SECS[$name]=$(( $(now) - STAGE_START[$name] ))
  echo "  ✓ $name done in $(( STAGE_SECS[$name] / 60 )) min"
}

# ── Preflight: fail in seconds, not after hours ───────────────────────
banner "Preflight"
if ! docker info >/dev/null 2>&1; then
  echo "docker is not running" >&2; exit 1
fi
if [[ $DEPLOY -eq 1 ]]; then
  if ! ssh -o BatchMode=yes -o ConnectTimeout=10 koramaps true 2>/dev/null; then
    echo "cannot reach the server over the 'koramaps' SSH alias — fix" >&2
    echo "~/.ssh/config or pass --skip-deploy" >&2
    exit 1
  fi
  echo "  server reachable"
fi

# Branch preconditions. A pruned stage graph consumes artifacts it no
# longer produces; if those are absent, say so now rather than three
# hours in.
need() {  # need <path> <why>
  if [[ ! -e "$1" ]]; then
    echo "missing $1 — $2" >&2; exit 1
  fi
}
if [[ $RUN_GTFS_CHAIN -eq 0 ]]; then
  need data/gtfs_routed/shapes.txt \
       "--only-routing builds on the routed feed already on disk; run a full build first"
  need data/gtfs_filtered/stops.txt \
       "--only-routing reuses the filtered feed for quay anchors; run a full build first"
fi
if [[ $RUN_GTFS_PREP -eq 1 && $RUN_GTFS_DL -eq 0 ]]; then
  need data/gtfs/stop_times.txt \
       "the GTFS download is skipped, so step 4 needs a feed already on disk"
fi

# Every step --pipeline-from skipped still owes its outputs to the steps
# that follow. Guards are scoped to the actual consumer, not to "something
# later runs" — step 8 reads none of the OSM or GTFS artifacts, and
# demanding them would fail a legitimate pmtiles-only rebuild. Name the
# missing artifact, not the flag.
emit_has() { case ",$EMIT_STEPS," in *",$1,"*) return 0 ;; esac; return 1; }

if [[ $RUN_OSM_EXTRACT -eq 0 ]]; then
  if [[ $RUN_PFAEDLE -eq 1 ]]; then
    need data/osm/ch_pfaedle.osm.pbf \
         "step 3 is skipped; pfaedle needs its bbox extract"
  fi
  if emit_has 7; then
    need data/osm/rail_ways.geojson \
         "step 3 is skipped; step 7's stop-extent walks need its way networks"
  fi
fi
if [[ $RUN_GTFS_PREP -eq 0 && $RUN_PFAEDLE -eq 1 ]]; then
  need data/gtfs_filtered/stops.txt \
       "step 4 is skipped; pfaedle needs the filtered feed already on disk"
fi
if [[ $RUN_PFAEDLE -eq 0 ]] && emit_has 6; then
  need data/gtfs_routed/shapes.txt \
       "step 5 is skipped; step 6 needs pfaedle's routed feed"
fi
if ! emit_has 6 && emit_has 7; then
  need data/transit/transit_lines.geojson \
       "step 6 is skipped; step 7 needs its emitted lines"
  need data/transit/line_stops.json \
       "step 6 is skipped; step 7 needs its stop lists"
fi
if ! emit_has 7 && emit_has 8; then
  need data/transit/transit_lines_extended.geojson \
       "step 7 is skipped; tippecanoe needs its extended lines"
  need data/transit/transit_stops.geojson \
       "step 7 is skipped; tippecanoe needs its stop dots"
fi

stages=()
if [[ $RUN_GTFS_DL     -eq 1 ]]; then stages+=("1:gtfs_dl"); fi
if [[ $RUN_OSM_DL      -eq 1 ]]; then stages+=("2:osm_dl"); fi
if [[ $RUN_OSM_EXTRACT -eq 1 ]]; then stages+=("3:osm_extract"); fi
if [[ $RUN_GTFS_PREP   -eq 1 ]]; then stages+=("4:gtfs_prep"); fi
if [[ $RUN_PFAEDLE     -eq 1 ]]; then stages+=("5:pfaedle"); fi
if [[ -n "$EMIT_STEPS"       ]]; then stages+=("$EMIT_STEPS:emit"); fi
if [[ $DO_ROUTING      -eq 1 ]]; then stages+=("routing+matrix+import"); fi
echo "  branch:      $BRANCH$([[ $FROM -gt 1 ]] && echo "  (pipeline from step $FROM)")"
echo "  stages:      ${stages[*]:-<none>}"
echo "  OSM refresh: $([[ $WITH_OSM -eq 1 ]] && echo yes || echo no)"
if [[ $DEPLOY -eq 1 ]]; then
  targets=()
  if [[ $DO_ROUTING  -eq 1 ]]; then targets+=(valhalla motis-data); fi
  if [[ $DO_PIPELINE -eq 1 ]]; then targets+=(map-assets); fi
  echo "  deploy:      ${targets[*]}"
else
  echo "  deploy:      no"
fi
echo "  PFAEDLE_JOBS=$PFAEDLE_JOBS TIPPECANOE_JOBS=$TIPPECANOE_JOBS" \
     "VALHALLA_THREADS=$VALHALLA_THREADS MATRIX_WORKERS=$MATRIX_WORKERS"

# ── Phase 1: downloads (GTFS ∥ OSM) ──────────────────────────────────
# OSM feeds both branches (pipeline: stop-extent walks; routing: the
# patched PBF behind the Valhalla tiles), so it is never branch-gated.
banner "Phase 1 — downloads"
phase1=()
if [[ $RUN_GTFS_DL -eq 1 ]]; then
  run_bg gtfs_dl ./scripts/rebuild_transit.sh --only 1 --force-gtfs --force-atlas
  phase1+=(gtfs_dl)
fi
if [[ $RUN_OSM_DL -eq 1 ]]; then
  if [[ $WITH_OSM -eq 1 ]]; then
    run_bg osm_dl ./scripts/rebuild_transit.sh --only 2 --force-osm
  else
    run_bg osm_dl ./scripts/rebuild_transit.sh --only 2
  fi
  phase1+=(osm_dl)
fi
if [[ ${#phase1[@]} -gt 0 ]]; then wait_all "${phase1[@]}"; fi

# ── Phase 2: extracts (bbox/OSM ∥ GTFS preprocess) ───────────────────
# Step 4 needs only GTFS + the config bbox; step 3 needs OSM + stop
# coords. Neither needs the other. Step 3 serves both branches; step 4
# is part of the GTFS chain.
banner "Phase 2 — OSM extracts ∥ GTFS preprocess"
phase2=()
if [[ $RUN_OSM_EXTRACT -eq 1 ]]; then
  run_bg osm_extract ./scripts/rebuild_transit.sh --only 3
  phase2+=(osm_extract)
fi
if [[ $RUN_GTFS_PREP -eq 1 ]]; then
  run_bg gtfs_prep ./scripts/rebuild_transit.sh --only 4
  phase2+=(gtfs_prep)
fi
if [[ ${#phase2[@]} -gt 0 ]]; then wait_all "${phase2[@]}"; fi

# ── Valhalla tile staleness ──────────────────────────────────────────
# Owned by setup_routing.sh step 3, which wipes stale tiles immediately
# after regenerating the walkable PBF. Deciding it here would read the
# station walk network's timestamp from before step 3 rebuilds it, so a
# walk-network change with unchanged OSM data would never reach the tiles.

# ── Phase 3: pfaedle ∥ routing prep ──────────────────────────────────
# pfaedle (sharded across PFAEDLE_JOBS containers) is the long serial
# stage. The routing prerequisites that do not depend on pfaedle's output
# — docker network, fork image, OSM patching, Valhalla tiles — run
# alongside it instead of after everything.
#
# Step 3 also builds the quay anchors, which need GTFS stops. It reads
# them from data/gtfs_filtered/ (finished in Phase 2), never from
# data/gtfs_routed/, which pfaedle is rewriting right here. Reading the
# routed feed anchored against the previous run's stops, so a renumbered
# quay silently lost its platform snap.
banner "Phase 3 — pfaedle ∥ routing prep (OSM patch, Valhalla)"
phase3=()
if [[ $RUN_PFAEDLE -eq 1 ]]; then
  run_bg pfaedle ./scripts/rebuild_transit.sh --only 5
  phase3+=(pfaedle)
fi
if [[ $DO_ROUTING -eq 1 ]]; then
  run_bg routing_prep ./scripts/routing/setup_routing.sh --steps 1,2,3,4
  phase3+=(routing_prep)
fi
if [[ ${#phase3[@]} -gt 0 ]]; then wait_all "${phase3[@]}"; fi

# ── Phase 4: matrix ∥ emit (6 → 7 → 8) ───────────────────────────────
# The matrix needs pfaedle's routed feed and a serving Valhalla — not
# the map emission. So the hours-long matrix (Valhalla-bound) overlaps
# the single-core 6 → 7 → 8 chain. Stops change with every GTFS
# refresh, so the matrix is always recomputed.
banner "Phase 4 — footpath matrix ∥ map emission (6 → 7 → 8)"
phase4=()
if [[ $DO_ROUTING -eq 1 ]]; then
  run_bg matrix ./scripts/routing/setup_routing.sh --steps 5,6 --force-matrix
  phase4+=(matrix)
fi
if [[ -n "$EMIT_STEPS" ]]; then
  run_bg emit ./scripts/rebuild_transit.sh --only "$EMIT_STEPS"
  phase4+=(emit)
fi
if [[ ${#phase4[@]} -gt 0 ]]; then wait_all "${phase4[@]}"; fi

# ── Phase 5: MOTIS import + local smoke test ─────────────────────────
# setup_routing's step 8 starts the local server and fails if it does
# not answer a plan query — the gate in front of every deploy.
if [[ $DO_ROUTING -eq 1 ]]; then
  banner "Phase 5 — MOTIS import + local smoke test"
  run_fg import ./scripts/routing/setup_routing.sh --steps 7,8 --force-import
fi

# ── Phase 6: deploy ──────────────────────────────────────────────────
if [[ $DEPLOY -eq 1 ]]; then
  banner "Phase 6 — deploy"
  if [[ $DO_ROUTING -eq 1 ]]; then
    run_fg deploy_valhalla ./scripts/deploy/deploy_valhalla.sh --data-only
    run_fg deploy_motis    ./scripts/deploy/deploy_motis.sh --data-only
  fi
  if [[ $DO_PIPELINE -eq 1 ]]; then
    run_fg deploy_assets ./scripts/deploy/deploy_map_assets.sh
  fi

  # Smoke-test what was actually shipped.
  if [[ $DO_ROUTING -eq 1 ]]; then
    banner "Production smoke test"
    SMOKE='https://koramaps.app/routing/api/v1/plan?fromPlace=47.378,8.540&toPlace=47.424,8.508&arriveBy=false&numItineraries=1&directModes=WALK'
    ok=0
    for i in $(seq 1 12); do
      if curl -sf "$SMOKE" | grep -q '"itineraries"'; then ok=1; break; fi
      sleep 5
    done
    if [[ $ok -eq 1 ]]; then
      echo "  production routing answers"
    else
      echo "  production routing did NOT answer within 60 s — check:" >&2
      echo "    ssh koramaps 'docker logs kora-motis --tail 50'" >&2
      exit 1
    fi
  else
    banner "Production smoke test"
    if curl -sf 'https://koramaps.app/map-assets/style.json' | grep -q '"layers"'; then
      echo "  production map assets serve"
    else
      echo "  production style.json did NOT serve — check nginx" >&2
      exit 1
    fi
  fi
fi

# ── Timing summary ───────────────────────────────────────────────────
banner "Done — $(( ($(now) - T0) / 60 )) min total"
printf "  %-16s %6s\n" "stage" "min"
for name in "${STAGE_ORDER[@]}"; do
  printf "  %-16s %6d\n" "$name" "$(( ${STAGE_SECS[$name]:-0} / 60 ))"
done
echo "  (overlapping stages: the longest in each phase is the critical path)"
