#!/usr/bin/env bash
# One-shot map data refresh: fresh GTFS (and optionally OSM) → full
# transit pipeline → routing stack (Valhalla tiles, footpath matrix,
# MOTIS import) → deploy map assets, Valhalla, and MOTIS data.
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
# Stops at the first failing stage. Deploys run only after the local
# MOTIS import succeeded and answered setup_routing.sh's smoke test, so
# the server never receives a half-updated dataset.
#
# Wall time is dominated by pfaedle (~30 min) and the footpath matrix
# (2-4 h); Valhalla tiles add ~10 min when OSM changed. Run under
# `tee` to keep the log:  ./scripts/update_map.sh 2>&1 | tee update.log
set -euo pipefail
cd "$(dirname "$0")/.."

WITH_OSM=0
DEPLOY=1
for a in "$@"; do
  case "$a" in
    --osm)         WITH_OSM=1 ;;
    --skip-deploy) DEPLOY=0 ;;
    -h|--help)     sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; echo "usage: $0 [--osm] [--skip-deploy]" >&2; exit 2 ;;
  esac
done

T0=$(date +%s)
stage() {
  echo ""
  echo "══════════════════════════════════════════"
  echo "  $1   ($(( ($(date +%s) - T0) / 60 )) min elapsed)"
  echo "══════════════════════════════════════════"
}

# ── Preflight: fail in seconds, not after hours ───────────────────────
stage "Preflight"
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

# ── 1. Transit pipeline with fresh source data ───────────────────────
# --start 1 (not bare: glyphs are a bootstrap concern, not a refresh).
# GTFS + atlas re-download always; OSM only on request.
stage "Transit pipeline"
FORCE_ARGS=(--force-gtfs --force-atlas)
if [[ $WITH_OSM -eq 1 ]]; then FORCE_ARGS+=(--force-osm); fi
./scripts/rebuild_transit.sh --start 1 "${FORCE_ARGS[@]}"

# ── 2. Valhalla tile staleness ───────────────────────────────────────
# setup_routing.sh re-patches the walkable PBF when the pipeline's OSM
# extract is newer, but Valhalla itself never notices: with
# use_tiles_ignore_pbf=True it serves whatever tiles exist. So when the
# extract is newer than the tile tarball, wipe the tiles (keeping the
# slow-to-fetch elevation and admin data) and let step 4 rebuild them.
# The tiles are owned by the container's user, hence the docker-side rm.
stage "Valhalla tile check"
OSM_EXTRACT=data/osm/ch_pfaedle.osm.pbf
TILES_TAR=valhalla/data/valhalla_tiles.tar
if [[ ! -f "$TILES_TAR" ]]; then
  echo "  no tiles yet — setup_routing.sh will build them"
elif [[ "$OSM_EXTRACT" -nt "$TILES_TAR" ]]; then
  echo "  OSM extract is newer than the tiles — rebuilding tiles"
  (cd valhalla && docker compose down) >/dev/null 2>&1 || true
  docker run --rm -v "$PWD/valhalla/data:/d" alpine \
    sh -c 'rm -rf /d/valhalla_tiles /d/valhalla_tiles.tar /d/file_hashes.txt'
  echo "  stale tiles removed (elevation + admin data kept)"
else
  echo "  tiles up to date — kept"
fi

# ── 3. Routing stack: sidecar, matrix, import, local smoke test ──────
# Stops change with every GTFS refresh, so the matrix and the import are
# always redone. setup_routing.sh's step 8 starts the local server and
# fails the run if it does not answer a plan query — that is the gate
# in front of every deploy below.
stage "Routing stack (matrix + import)"
export MATRIX_WORKERS="${MATRIX_WORKERS:-20}"
./scripts/setup_routing.sh --force-matrix --force-import

# ── 4. Deploy ────────────────────────────────────────────────────────
if [[ $DEPLOY -eq 0 ]]; then
  stage "Done (deploy skipped)"
  exit 0
fi

stage "Deploy Valhalla"
./scripts/deploy_valhalla.sh

stage "Deploy MOTIS data"
./scripts/deploy_motis.sh --data-only

stage "Deploy map assets"
./scripts/deploy_map_assets.sh

# ── 5. External smoke test ───────────────────────────────────────────
stage "Production smoke test"
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

stage "Done"
echo "  total: $(( ($(date +%s) - T0) / 60 )) min"
