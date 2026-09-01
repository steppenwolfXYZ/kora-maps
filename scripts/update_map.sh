#!/usr/bin/env bash
# One-shot map data refresh: fresh GTFS (and optionally OSM) → transit
# pipeline → routing stack (Valhalla tiles, footpath matrix, MOTIS
# import) → deploy map assets, Valhalla, and MOTIS data.
#
# This is the data-refresh machine's routine (see the Mac / data-machine
# split in .claude/rules/deployment.md). It never touches app code and
# never ships a MOTIS image — the server keeps the arm64 image the dev
# Mac built; only indexes go out (`deploy_motis.sh --data-only`).
#
# Usage: ./scripts/update_map.sh [--osm] [--skip-deploy]
#
#   --osm          also re-download the country PBFs (~12 GB) and rebuild
#                  the Valhalla tiles. Default refreshes GTFS + atlas only
#                  — OSM changes slowly, GTFS twice a week.
#   --skip-deploy  run everything locally, deploy nothing.
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
for a in "$@"; do
  case "$a" in
    --osm)         WITH_OSM=1 ;;
    --skip-deploy) DEPLOY=0 ;;
    -h|--help)     sed -n '2,43p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; echo "usage: $0 [--osm] [--skip-deploy]" >&2; exit 2 ;;
  esac
done

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
echo "  OSM refresh: $([[ $WITH_OSM -eq 1 ]] && echo yes || echo no)"
echo "  deploy:      $([[ $DEPLOY -eq 1 ]] && echo yes || echo no)"
echo "  PFAEDLE_JOBS=$PFAEDLE_JOBS TIPPECANOE_JOBS=$TIPPECANOE_JOBS" \
     "VALHALLA_THREADS=$VALHALLA_THREADS MATRIX_WORKERS=$MATRIX_WORKERS"

# ── Phase 1: downloads (GTFS ∥ OSM) ──────────────────────────────────
banner "Phase 1 — downloads"
run_bg gtfs_dl ./scripts/rebuild_transit.sh --only 1 --force-gtfs --force-atlas
if [[ $WITH_OSM -eq 1 ]]; then
  run_bg osm_dl ./scripts/rebuild_transit.sh --only 2 --force-osm
else
  run_bg osm_dl ./scripts/rebuild_transit.sh --only 2
fi
wait_all gtfs_dl osm_dl

# ── Phase 2: extracts (bbox/OSM ∥ GTFS preprocess) ───────────────────
# Step 4 needs only GTFS + the config bbox; step 3 needs OSM + stop
# coords. Neither needs the other.
banner "Phase 2 — OSM extracts ∥ GTFS preprocess"
run_bg osm_extract ./scripts/rebuild_transit.sh --only 3
run_bg gtfs_prep   ./scripts/rebuild_transit.sh --only 4
wait_all osm_extract gtfs_prep

# ── Valhalla tile staleness ──────────────────────────────────────────
# setup_routing.sh re-patches the walkable PBF when the pipeline's OSM
# extract is newer, but Valhalla itself never notices: with
# use_tiles_ignore_pbf=True it serves whatever tiles exist. So when the
# extract is newer than the tile tarball, wipe the tiles (keeping the
# slow-to-fetch elevation and admin data) and let the routing prep
# rebuild them. The tiles are owned by the container's user, hence the
# docker-side rm.
# The station walk network is checked too: it is baked into the same
# walkable PBF, so a rebuilt overlay invalidates the tiles even when the
# OSM extract itself has not moved.
OSM_EXTRACT=data/osm/ch_pfaedle.osm.pbf
WALK_OVERLAY=data/osm/station_walk_network.osm.pbf
TILES_TAR=valhalla/data/valhalla_tiles.tar
if [[ ! -f "$TILES_TAR" ]]; then
  echo "  Valhalla: no tiles yet — routing prep will build them"
elif [[ "$OSM_EXTRACT" -nt "$TILES_TAR" || "$WALK_OVERLAY" -nt "$TILES_TAR" ]]; then
  echo "  Valhalla: inputs are newer than the tiles — rebuilding tiles"
  (cd valhalla && docker compose down) >/dev/null 2>&1 || true
  docker run --rm -v "$PWD/valhalla/data:/d" alpine \
    sh -c 'rm -rf /d/valhalla_tiles /d/valhalla_tiles.tar /d/file_hashes.txt'
  echo "  Valhalla: stale tiles removed (elevation + admin data kept)"
else
  echo "  Valhalla: tiles up to date — kept"
fi

# ── Phase 3: pfaedle ∥ routing prep ──────────────────────────────────
# pfaedle (sharded across PFAEDLE_JOBS containers) is the long serial
# stage. The routing prerequisites that depend only on the OSM extracts
# — docker network, fork image, OSM patching, Valhalla tiles — run
# alongside it instead of after everything.
banner "Phase 3 — pfaedle ∥ routing prep (OSM patch, Valhalla)"
run_bg pfaedle      ./scripts/rebuild_transit.sh --only 5
run_bg routing_prep ./scripts/setup_routing.sh --steps 1,2,3,4
wait_all pfaedle routing_prep

# ── Phase 4: matrix ∥ emit (6 → 7 → 8) ───────────────────────────────
# The matrix needs pfaedle's routed feed and a serving Valhalla — not
# the map emission. So the hours-long matrix (Valhalla-bound) overlaps
# the single-core 6 → 7 → 8 chain. Stops change with every GTFS
# refresh, so the matrix is always recomputed.
banner "Phase 4 — footpath matrix ∥ map emission (6 → 7 → 8)"
run_bg matrix ./scripts/setup_routing.sh --steps 5,6 --force-matrix
run_bg emit   ./scripts/rebuild_transit.sh --only 6,7,8
wait_all matrix emit

# ── Phase 5: MOTIS import + local smoke test ─────────────────────────
# setup_routing's step 8 starts the local server and fails if it does
# not answer a plan query — the gate in front of every deploy.
banner "Phase 5 — MOTIS import + local smoke test"
run_fg import ./scripts/setup_routing.sh --steps 7,8 --force-import

# ── Phase 6: deploy ──────────────────────────────────────────────────
if [[ $DEPLOY -eq 1 ]]; then
  banner "Phase 6 — deploy"
  run_fg deploy_valhalla ./scripts/deploy_valhalla.sh
  run_fg deploy_motis    ./scripts/deploy_motis.sh --data-only
  run_fg deploy_assets   ./scripts/deploy_map_assets.sh

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
fi

# ── Timing summary ───────────────────────────────────────────────────
banner "Done — $(( ($(now) - T0) / 60 )) min total"
printf "  %-16s %6s\n" "stage" "min"
for name in "${STAGE_ORDER[@]}"; do
  printf "  %-16s %6d\n" "$name" "$(( ${STAGE_SECS[$name]:-0} / 60 ))"
done
echo "  (overlapping stages: the longest in each phase is the critical path)"
