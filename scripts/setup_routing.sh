#!/usr/bin/env bash
# Local routing backend bring-up (Valhalla + MOTIS fork).
# Run from the project root: ./scripts/setup_routing.sh
#
# Turns a finished map pipeline run (./scripts/rebuild_transit.sh) into a
# working local routing stack. Every step is idempotent — re-running the
# script skips whatever is already in place, so it doubles as a repair /
# refresh tool after a pipeline rebuild.
#
# Steps:
#   1  Create docker network `koramaps`                 (instant; skipped if present)
#   2  Build the MOTIS fork image                       (~30-60 min compile; skipped if present)
#   3  Patch OSM PBFs (foot=yes on alp/forest roads)    (~minutes each; skipped if up to date)
#   4  Start Valhalla (first run builds tiles)          (first run ~20-40 min, later instant)
#   5  Preprocess GTFS for MOTIS (platform snap)        (~1 min; always runs, cheap)
#   6  Build the Valhalla footpath matrix               (hours on a laptop; skipped when
#                                                        complete, resumes partial runs)
#   7  MOTIS import                                     (~10 min; skipped if index present)
#   8  Start MOTIS server + smoke test                  (seconds)
#
# Force flags (redo a step whose output already exists):
#
#   --force-image     rebuild the MOTIS fork docker image
#   --force-osm       re-patch both preprocessed OSM PBFs
#   --force-matrix    delete matrix CSV + checkpoint, recompute from scratch
#   --force-import    re-run the MOTIS import
#
# Step selection (for orchestrators that overlap this script's phases
# with the map pipeline — see scripts/update_map.sh):
#
#   --steps 1,2,3,4   run only the listed steps, in order, skip the rest.
#                     Prerequisite checks still run. Default: all 1-8.
#
# Sizing knobs (env): VALHALLA_THREADS (Valhalla serving pool; Linux
# defaults to nproc, elsewhere 8) and MATRIX_WORKERS (builder-side
# concurrency; defaults to VALHALLA_THREADS + 4 when unset).
#
# Prerequisites: ./scripts/rebuild_transit.sh has run at least once
# (needs data/gtfs_routed/ and the step-02 OSM downloads), docker is
# running, and the pyosmium package is installed
# (python3 -m pip install --user --break-system-packages osmium).

set -euo pipefail
cd "$(dirname "$0")/.."

FORCE_IMAGE=0
FORCE_OSM=0
FORCE_MATRIX=0
FORCE_IMPORT=0
STEPS="1,2,3,4,5,6,7,8"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force-image)  FORCE_IMAGE=1 ;;
    --force-osm)    FORCE_OSM=1 ;;
    --force-matrix) FORCE_MATRIX=1 ;;
    --force-import) FORCE_IMPORT=1 ;;
    --steps)        shift; STEPS="$1" ;;
    --steps=*)      STEPS="${1#--steps=}" ;;
    -h|--help)
      sed -n '2,31p' "$0"; exit 0 ;;
    *)
      echo "unknown arg: $1" >&2
      echo "usage: $0 [--force-image] [--force-osm] [--force-matrix] [--force-import] [--steps LIST]" >&2
      exit 2 ;;
  esac
  shift
done

# want N — true when step N is selected.
want() { [[ ",$STEPS," == *",$1,"* ]]; }

# Sizing: Valhalla's serving pool follows the core count on Linux (the
# fd-limit that used to cap it at 16 is lifted by valhalla/nofile.conf);
# the matrix builder runs a few more client threads than that so the
# server queue never idles. Both are plain env overrides.
if [[ -z "${VALHALLA_THREADS:-}" && "$(uname -s)" == "Linux" ]]; then
  export VALHALLA_THREADS="$(nproc)"
fi
if [[ -z "${MATRIX_WORKERS:-}" ]]; then
  export MATRIX_WORKERS="$(( ${VALHALLA_THREADS:-8} + 4 ))"
fi

echo "══════════════════════════════════════════"
echo "  Routing Backend Setup (Valhalla + MOTIS)"
echo "══════════════════════════════════════════"

# ── Prerequisite checks ─────────────────────────────────────────────
if ! docker info >/dev/null 2>&1; then
  echo "docker is not running — start Docker and retry" >&2
  exit 1
fi
# Steps 1-4 (network, image, OSM patch, Valhalla) need the OSM extracts
# plus — for step 3's quay anchors — step 04's filtered stops; the routed
# GTFS is required from step 5 on. Orchestrators start steps 1-4 while
# pfaedle is still running, which is exactly why step 3 reads the filtered
# feed and never the routed one (see build_station_walk_network.py).
PREREQS=(data/osm/ch_pfaedle.osm.pbf data/osm/switzerland-latest.osm.pbf)
for n in 3 4; do
  if want "$n"; then PREREQS+=(data/gtfs_filtered/stops.txt); break; fi
done
for n in 5 6 7 8; do
  if want "$n"; then PREREQS+=(data/gtfs_routed/stops.txt); break; fi
done
for f in "${PREREQS[@]}"; do
  if [[ ! -f "$f" ]]; then
    echo "missing $f — run ./scripts/rebuild_transit.sh first" >&2
    exit 1
  fi
done
if ! python3 -c "import osmium" 2>/dev/null; then
  echo "pyosmium missing — install with:" >&2
  echo "  python3 -m pip install --user --break-system-packages osmium" >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "created .env from .env.example (PUBLIC_MOTIS_URL=http://localhost:8080)"
fi
mkdir -p motis/data valhalla/data

if want 1; then
# ── Step 1: docker network ──────────────────────────────────────────
echo ""
echo "▶ Step 1 — Docker network 'koramaps'"
if docker network inspect koramaps >/dev/null 2>&1; then
  echo "  already exists — skipped"
else
  docker network create koramaps
fi
fi

if want 2; then
# ── Step 2: MOTIS fork image ────────────────────────────────────────
echo ""
echo "▶ Step 2 — MOTIS fork image (koramaps/motis:footpath-matrix)"
if [[ $FORCE_IMAGE -eq 0 ]] && docker image inspect koramaps/motis:footpath-matrix >/dev/null 2>&1; then
  echo "  image present — skipped (--force-image to rebuild)"
else
  time docker build -t koramaps/motis:footpath-matrix -f motis/fork/Dockerfile motis/fork
fi
fi

if want 3; then
# ── Step 3: preprocessed OSM PBFs ───────────────────────────────────
# foot=yes patch on access=agricultural/forestry ways so alp / forest
# roads route for pedestrians (see scripts/preprocess_osm_for_motis.py),
# plus the synthetic station walk network merged into the Valhalla input
# (see .claude/concepts/station-walk-network.md).
echo ""
echo "▶ Step 3 — Patch OSM PBFs"
# Each artifact is compared against the script that produces it as well as
# against its data inputs. A code change moves no file in data/, so a
# data-only check reports "up to date" and silently serves the old result —
# which is how a walk-network change once reached neither the overlay nor
# the tiles. git rewrites a script's mtime only when its content changed,
# so this triggers on real edits and not on every pull.
if [[ $FORCE_OSM -eq 0 \
      && data/osm/switzerland-motis.osm.pbf -nt data/osm/switzerland-latest.osm.pbf \
      && data/osm/switzerland-motis.osm.pbf -nt scripts/preprocess_osm_for_motis.py ]]; then
  echo "  switzerland-motis.osm.pbf up to date — skipped"
else
  time python3 scripts/preprocess_osm_for_motis.py
fi
# Platform walk lines + quay anchors. Must precede the --valhalla patch
# (which merges the overlay) and step 5 (which reads the anchors).
# The freshness test includes the filtered stops: anchors are keyed by
# stop_id, so a GTFS refresh that renumbers a quay invalidates them even
# when the OSM extract is untouched. Without that clause the stale anchor
# file survived, the renumbered quay never got snapped onto its platform,
# and it dropped out of the footpath matrix — leaving trains that call
# there visible in /stoptimes but unboardable in /plan.
if [[ $FORCE_OSM -eq 0 \
      && data/osm/station_walk_network.osm.pbf -nt data/osm/ch_pfaedle.osm.pbf \
      && data/osm/station_walk_network.osm.pbf -nt data/gtfs_filtered/stops.txt \
      && data/osm/station_walk_network.osm.pbf -nt scripts/build_station_walk_network.py ]]; then
  echo "  station_walk_network.osm.pbf up to date — skipped"
else
  time python3 scripts/build_station_walk_network.py --force-extract
fi
if [[ $FORCE_OSM -eq 0 \
      && data/osm/ch_pfaedle_walkable.osm.pbf -nt data/osm/ch_pfaedle.osm.pbf \
      && data/osm/ch_pfaedle_walkable.osm.pbf -nt data/osm/station_walk_network.osm.pbf \
      && data/osm/ch_pfaedle_walkable.osm.pbf -nt scripts/preprocess_osm_for_motis.py ]]; then
  echo "  ch_pfaedle_walkable.osm.pbf up to date — skipped"
else
  time python3 scripts/preprocess_osm_for_motis.py --valhalla
fi

# Valhalla never notices a changed PBF: use_tiles_ignore_pbf=True means it
# serves whatever tiles exist. So the staleness check belongs here, right
# after the input is rewritten — deciding it earlier (as update_map.sh used
# to) reads the walk network's timestamp from before this step regenerated
# it, and a walk-network change with unchanged OSM data would then never
# reach the tiles. Tiles are container-owned, hence the docker-side rm; the
# slow elevation and admin data are kept.
# The compose file counts as an input too: it pins the Valhalla image, and
# a version bump changes what the tiles mean without touching any data
# file. Left out, an image bump would serve the previous version's tiles
# and the upgrade would look like it had landed when it had not — the same
# silent skip that data-only guards produced for code changes.
TILES_TAR=valhalla/data/valhalla_tiles.tar
if [[ -f "$TILES_TAR" ]] \
   && { [[ data/osm/ch_pfaedle_walkable.osm.pbf -nt "$TILES_TAR" ]] \
        || [[ valhalla/docker-compose.yml -nt "$TILES_TAR" ]]; }; then
  echo "  Valhalla tiles are older than their inputs — wiping so step 4 rebuilds"
  (cd valhalla && docker compose down) >/dev/null 2>&1 || true
  # valhalla.json goes with them: it is generated by the image, and a
  # config written by an older Valhalla can silently lack keys the new one
  # needs. Everything in it comes from the compose environment, so there is
  # nothing hand-tuned to lose. Elevation and admin data are kept — they
  # are slow to fetch and version-independent.
  docker run --rm -v "$PWD/valhalla/data:/d" alpine \
    sh -c 'rm -rf /d/valhalla_tiles /d/valhalla_tiles.tar /d/file_hashes.txt /d/valhalla.json'
elif [[ ! -f "$TILES_TAR" ]]; then
  echo "  no Valhalla tiles yet — step 4 will build them"
else
  echo "  Valhalla tiles up to date — kept"
fi
fi

if want 4; then
# ── Step 4: Valhalla ────────────────────────────────────────────────
echo ""
echo "▶ Step 4 — Start Valhalla"
if [[ ! -f valhalla/data/valhalla_tiles.tar ]]; then
  echo "  no tiles yet — first run downloads SRTM elevation and builds"
  echo "  tiles (~20-40 min). Follow along: docker logs -f kora-valhalla"
fi
(cd valhalla && docker compose up -d valhalla)
printf "  waiting for Valhalla on :8002 "
VALHALLA_UP=0
for i in $(seq 1 360); do
  if curl -sf http://localhost:8002/status >/dev/null 2>&1; then VALHALLA_UP=1; break; fi
  printf "."
  sleep 10
done
echo ""
if [[ $VALHALLA_UP -eq 0 ]]; then
  echo "Valhalla did not come up within 60 min — check: docker logs kora-valhalla" >&2
  exit 1
fi
echo "  Valhalla is serving"
fi

if want 5; then
# ── Step 5: GTFS sidecar for MOTIS ──────────────────────────────────
echo ""
echo "▶ Step 5 — Preprocess GTFS for MOTIS (platform-anchored stops.txt)"
time python3 scripts/preprocess_gtfs_for_motis.py
fi

if want 6; then
# ── Step 6: footpath matrix ─────────────────────────────────────────
# Complete = CSV present with no checkpoint (the builder deletes its
# checkpoint on successful completion; a lingering checkpoint marks a
# partial run, which the builder resumes). A remotely-built CSV (see
# .claude/runbooks/matrix_build_remote.md) ships without a checkpoint
# and is therefore treated as complete. Heavy on a laptop.
echo ""
echo "▶ Step 6 — Valhalla footpath matrix"
MATRIX_CSV=motis/data/valhalla_footpath_matrix.csv
MATRIX_CKPT=motis/data/valhalla_footpath_matrix.checkpoint
if [[ $FORCE_MATRIX -eq 1 ]]; then
  rm -f "$MATRIX_CSV" "$MATRIX_CKPT"
  echo "  --force-matrix: deleted CSV + checkpoint"
fi
if [[ -f "$MATRIX_CSV" && ! -f "$MATRIX_CKPT" ]]; then
  echo "  matrix complete — skipped (--force-matrix to recompute)"
else
  # A checkpoint without a CSV is stale resume state — the builder
  # would skip those sources and leave holes in a fresh build.
  if [[ -f "$MATRIX_CKPT" && ! -f "$MATRIX_CSV" ]]; then
    rm -f "$MATRIX_CKPT"
    echo "  removed stale checkpoint (no CSV)"
  fi
  time python3 scripts/build_valhalla_footpath_matrix.py
fi
fi

if want 7; then
# ── Step 7: MOTIS import ────────────────────────────────────────────
echo ""
echo "▶ Step 7 — MOTIS import"
if [[ $FORCE_IMPORT -eq 0 && -f motis/data/tt.bin ]]; then
  echo "  index present — skipped (--force-import to re-import)"
else
  # Gate the importer on a self-consistent feed. nigiri drops stop ids it
  # cannot resolve and imports the trip anyway, so a mixed-vintage
  # data/gtfs_motis/ produces an index that looks healthy and quietly
  # loses station calls. ~1-2 min against a ~10 min import.
  echo "  checking data/gtfs_motis/ consistency"
  python3 scripts/check_gtfs_motis_consistency.py
  # `run --rm` instead of `up` so the import's exit code propagates
  # and no stopped container lingers. On native Linux the container's
  # `motis` user (uid 999) cannot write the bind-mounted ./data, so map
  # it onto the invoking user (see the compose file's KORA_UID note);
  # macOS Docker Desktop remaps ownership itself and keeps the default.
  if [[ "$(uname -s)" == "Linux" ]]; then
    export KORA_UID="$(id -u)" KORA_GID="$(id -g)"
  fi
  (cd motis && time docker compose --profile import run --rm motis-import)
fi
fi

if want 8; then
# ── Step 8: MOTIS server + smoke test ───────────────────────────────
echo ""
echo "▶ Step 8 — Start MOTIS server"
(cd motis && docker compose up -d motis)
printf "  waiting for MOTIS on :8080 "
MOTIS_UP=0
for i in $(seq 1 60); do
  if curl -sf 'http://localhost:8080/api/v1/plan?fromPlace=47.378,8.540&toPlace=47.424,8.508&arriveBy=false&numItineraries=1&directModes=WALK' >/dev/null 2>&1; then
    MOTIS_UP=1; break
  fi
  printf "."
  sleep 2
done
echo ""
if [[ $MOTIS_UP -eq 0 ]]; then
  echo "MOTIS did not answer within 2 min — check: docker logs kora-motis" >&2
  exit 1
fi

echo ""
echo "══════════════════════════════════════════"
echo "  Done. MOTIS on :8080, Valhalla on :8002."
echo "  Start the app with: npm run dev"
echo "══════════════════════════════════════════"
fi
