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
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force-image)  FORCE_IMAGE=1 ;;
    --force-osm)    FORCE_OSM=1 ;;
    --force-matrix) FORCE_MATRIX=1 ;;
    --force-import) FORCE_IMPORT=1 ;;
    -h|--help)
      sed -n '2,31p' "$0"; exit 0 ;;
    *)
      echo "unknown arg: $1" >&2
      echo "usage: $0 [--force-image] [--force-osm] [--force-matrix] [--force-import]" >&2
      exit 2 ;;
  esac
  shift
done

echo "══════════════════════════════════════════"
echo "  Routing Backend Setup (Valhalla + MOTIS)"
echo "══════════════════════════════════════════"

# ── Prerequisite checks ─────────────────────────────────────────────
if ! docker info >/dev/null 2>&1; then
  echo "docker is not running — start Docker and retry" >&2
  exit 1
fi
for f in data/gtfs_routed/stops.txt \
         data/osm/ch_pfaedle.osm.pbf \
         data/osm/switzerland-latest.osm.pbf; do
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

# ── Step 1: docker network ──────────────────────────────────────────
echo ""
echo "▶ Step 1 — Docker network 'koramaps'"
if docker network inspect koramaps >/dev/null 2>&1; then
  echo "  already exists — skipped"
else
  docker network create koramaps
fi

# ── Step 2: MOTIS fork image ────────────────────────────────────────
echo ""
echo "▶ Step 2 — MOTIS fork image (koramaps/motis:footpath-matrix)"
if [[ $FORCE_IMAGE -eq 0 ]] && docker image inspect koramaps/motis:footpath-matrix >/dev/null 2>&1; then
  echo "  image present — skipped (--force-image to rebuild)"
else
  time docker build -t koramaps/motis:footpath-matrix -f motis/fork/Dockerfile motis/fork
fi

# ── Step 3: preprocessed OSM PBFs ───────────────────────────────────
# foot=yes patch on access=agricultural/forestry ways so alp / forest
# roads route for pedestrians (see scripts/preprocess_osm_for_motis.py).
echo ""
echo "▶ Step 3 — Patch OSM PBFs"
if [[ $FORCE_OSM -eq 0 && data/osm/switzerland-motis.osm.pbf -nt data/osm/switzerland-latest.osm.pbf ]]; then
  echo "  switzerland-motis.osm.pbf up to date — skipped"
else
  time python3 scripts/preprocess_osm_for_motis.py
fi
if [[ $FORCE_OSM -eq 0 && data/osm/ch_pfaedle_walkable.osm.pbf -nt data/osm/ch_pfaedle.osm.pbf ]]; then
  echo "  ch_pfaedle_walkable.osm.pbf up to date — skipped"
else
  time python3 scripts/preprocess_osm_for_motis.py --valhalla
fi

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

# ── Step 5: GTFS sidecar for MOTIS ──────────────────────────────────
echo ""
echo "▶ Step 5 — Preprocess GTFS for MOTIS (platform-snapped stops.txt)"
time python3 scripts/preprocess_gtfs_for_motis.py

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

# ── Step 7: MOTIS import ────────────────────────────────────────────
echo ""
echo "▶ Step 7 — MOTIS import"
if [[ $FORCE_IMPORT -eq 0 && -f motis/data/tt.bin ]]; then
  echo "  index present — skipped (--force-import to re-import)"
else
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
