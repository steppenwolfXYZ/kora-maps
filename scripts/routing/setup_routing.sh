#!/usr/bin/env bash
# Local routing backend bring-up (Valhalla + MOTIS fork).
# Run from the project root: ./scripts/routing/setup_routing.sh
#
# Turns a finished map pipeline run (./scripts/rebuild_transit.sh) into a
# working local routing stack. Every step is idempotent — re-running the
# script skips whatever is already in place, so it doubles as a repair /
# refresh tool after a pipeline rebuild.
#
# Steps:
#   1  Create docker network `koramaps`                 (instant; skipped if present)
#   2  Build the MOTIS + Valhalla fork images           (baseline images once per pin, 30-90 min
#                                                        each; fork overlays ~5-10 min; skipped if
#                                                        up to date with motis/fork, valhalla/fork)
#   3  Patch OSM PBFs + admin bounds + elevation        (~minutes each; skipped if up to date;
#                                                        first elevation build downloads ~min)
#   4  Start Valhalla (first run builds tiles)          (first run ~20-40 min, later instant)
#   5  Preprocess GTFS for MOTIS (platform snap)        (~1 min; always runs, cheap)
#   6  Build the Valhalla footpath matrices             (foot: hours on a laptop; stroller:
#                                                        ~1/16 of that; each skipped when
#                                                        complete, resumes partial runs)
#   7  MOTIS import                                     (~10 min; skipped if index present)
#   8  Start MOTIS server + smoke test                  (seconds)
#
# Force flags (redo a step whose output already exists):
#
#   --force-image     rebuild the MOTIS and Valhalla fork docker images
#   --force-osm       re-patch both preprocessed OSM PBFs
#   --force-elevation regenerate the Mapterhorn elevation cells
#   --force-matrix    delete both matrix CSVs + checkpoints, recompute from scratch
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
cd "$(dirname "$0")/../.."

FORCE_IMAGE=0
FORCE_OSM=0
FORCE_ELEVATION=0
FORCE_MATRIX=0
FORCE_IMPORT=0
STEPS="1,2,3,4,5,6,7,8"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force-image)  FORCE_IMAGE=1 ;;
    --force-osm)    FORCE_OSM=1 ;;
    --force-elevation) FORCE_ELEVATION=1 ;;
    --force-matrix) FORCE_MATRIX=1 ;;
    --force-import) FORCE_IMPORT=1 ;;
    --steps)        shift; STEPS="$1" ;;
    --steps=*)      STEPS="${1#--steps=}" ;;
    -h|--help)
      sed -n '2,31p' "$0"; exit 0 ;;
    *)
      echo "unknown arg: $1" >&2
      echo "usage: $0 [--force-image] [--force-osm] [--force-elevation] [--force-matrix] [--force-import] [--steps LIST]" >&2
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
# ── Baseline images ─────────────────────────────────────────────────
# Each fork builds on a BASELINE image — toolchain + upstream at the pin +
# every dependency clone + the full unmodified compile — tagged
# koramaps/<engine>-baseline:<pin> (motis/fork/Dockerfile.baseline,
# valhalla/fork/Dockerfile.baseline). A tagged image is never garbage-
# collected; the single-file build used to keep the baseline as a cached
# layer, and BuildKit's cache GC evicted it, after which every retry
# re-cloned MOTIS's ~30 dependency repos until GitHub rate-limited the
# machine. Built once per machine and per pin; a pin bump gets a new tag.
# The pin is read from the fork Dockerfile's ARG line — the one place it
# lives — and passed to both builds.
ensure_baseline() {
  local image="$1" pin="$2" dockerfile="$3" ctx="$4" arg="$5"
  if docker image inspect "$image:$pin" >/dev/null 2>&1; then
    echo "  baseline $image:$pin present — reused"
  else
    echo "  baseline $image:$pin missing — building (once per machine and pin)"
    time docker build --build-arg "$arg=$pin" -t "$image:$pin" -f "$dockerfile" "$ctx"
  fi
}
MOTIS_PIN="$(sed -n 's/^ARG MOTIS_REF=//p' motis/fork/Dockerfile | head -1)"
if [[ -z "$MOTIS_PIN" ]]; then
  echo "cannot read MOTIS_REF from motis/fork/Dockerfile" >&2
  exit 1
fi
VALHALLA_PIN_IMG="$(sed -n 's/^ARG VALHALLA_REF=//p' valhalla/fork/Dockerfile | head -1)"
if [[ -z "$VALHALLA_PIN_IMG" ]]; then
  echo "cannot read VALHALLA_REF from valhalla/fork/Dockerfile" >&2
  exit 1
fi

# ── Step 2: MOTIS fork image ────────────────────────────────────────
echo ""
echo "▶ Step 2 — MOTIS fork image (koramaps/motis:footpath-matrix)"
# Rebuild when the fork sources are newer than the last build, not merely
# when the image is absent. "Image present" once let a pulled fork change
# sit unbuilt through a whole update_map.sh run: the import then produced
# an index without the minimum-transfer-time floor and without the 2-h
# transfer profile, and nothing said so — the index is only wrong in what
# it contains. Same shape of check step 3 applies to the OSM patch.
#
# The stamp lives outside motis/data/ on purpose: that directory is synced
# between machines with --delete, so a stamp inside it would carry the
# other machine's build time and defeat the comparison.
IMAGE_STAMP=motis/.image_build_stamp
image_stale() {
  [[ $FORCE_IMAGE -eq 1 ]] && return 0
  docker image inspect koramaps/motis:footpath-matrix >/dev/null 2>&1 || return 0
  [[ -f $IMAGE_STAMP ]] || return 0
  [[ -n "$(find motis/fork -type f -newer "$IMAGE_STAMP" -print -quit)" ]]
}
if ! image_stale; then
  echo "  image up to date with motis/fork/ — skipped (--force-image to rebuild)"
else
  ensure_baseline koramaps/motis-baseline "$MOTIS_PIN" motis/fork/Dockerfile.baseline motis/fork MOTIS_REF
  time docker build --build-arg "MOTIS_REF=$MOTIS_PIN" \
    -t koramaps/motis:footpath-matrix -f motis/fork/Dockerfile motis/fork
  touch "$IMAGE_STAMP"
  # A new binary can build a different transfer table from identical
  # inputs, but MOTIS's task hash covers only the data (timetable, osm,
  # matches, way_matches) — never the image. Without dropping this key the
  # next import reports "running tasks: []" and silently keeps the table
  # the old binary produced. Only the footpath task is invalidated; osr,
  # tt and matches are unaffected by the fork.
  rm -f motis/data/meta/osr_footpath.json
  echo "  image rebuilt — dropped meta/osr_footpath.json so step 7 redoes the transfer table"
fi

# ── Step 2b: Valhalla fork image ────────────────────────────────────
# Kora bicycle costing on pinned upstream (valhalla/fork/, see
# .claude/concepts/bicycle-costing-fork.md). Same staleness rule as the
# MOTIS image: rebuild when anything under valhalla/fork/ is newer than the
# last build, not merely when the image is absent. Costing is query-time,
# so a rebuilt image needs a container restart (step 4's `up -d` recreates
# it) and nothing else — the tiles stay. The stamp lives outside
# valhalla/data/ for the same reason as the MOTIS one: that directory is
# synced between machines with --delete.
echo ""
echo "▶ Step 2b — Valhalla fork image (koramaps/valhalla:bicycle-costing)"
VALHALLA_IMAGE_STAMP=valhalla/.image_build_stamp
valhalla_image_stale() {
  [[ $FORCE_IMAGE -eq 1 ]] && return 0
  docker image inspect koramaps/valhalla:bicycle-costing >/dev/null 2>&1 || return 0
  [[ -f $VALHALLA_IMAGE_STAMP ]] || return 0
  [[ -n "$(find valhalla/fork -type f -newer "$VALHALLA_IMAGE_STAMP" -print -quit)" ]]
}
if ! valhalla_image_stale; then
  echo "  image up to date with valhalla/fork/ — skipped (--force-image to rebuild)"
else
  ensure_baseline koramaps/valhalla-baseline "$VALHALLA_PIN_IMG" valhalla/fork/Dockerfile.baseline valhalla/fork VALHALLA_REF
  time docker build --build-arg "VALHALLA_REF=$VALHALLA_PIN_IMG" \
    -t koramaps/valhalla:bicycle-costing -f valhalla/fork/Dockerfile valhalla/fork
  touch "$VALHALLA_IMAGE_STAMP"
fi
fi

if want 3; then
# ── Step 3: preprocessed OSM PBFs ───────────────────────────────────
# foot=yes patch on access=agricultural/forestry ways so alp / forest
# roads route for pedestrians (see scripts/routing/preprocess_osm_for_motis.py),
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
      && data/osm/switzerland-motis.osm.pbf -nt scripts/routing/preprocess_osm_for_motis.py ]]; then
  echo "  switzerland-motis.osm.pbf up to date — skipped"
else
  time python3 scripts/routing/preprocess_osm_for_motis.py
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
      && data/osm/station_walk_network.osm.pbf -nt scripts/routing/build_station_walk_network.py ]]; then
  echo "  station_walk_network.osm.pbf up to date — skipped"
else
  time python3 scripts/routing/build_station_walk_network.py --force-extract
fi
if [[ $FORCE_OSM -eq 0 \
      && data/osm/ch_pfaedle_walkable.osm.pbf -nt data/osm/ch_pfaedle.osm.pbf \
      && data/osm/ch_pfaedle_walkable.osm.pbf -nt data/osm/station_walk_network.osm.pbf \
      && data/osm/ch_pfaedle_walkable.osm.pbf -nt scripts/routing/preprocess_osm_for_motis.py ]]; then
  echo "  ch_pfaedle_walkable.osm.pbf up to date — skipped"
else
  time python3 scripts/routing/preprocess_osm_for_motis.py --valhalla
fi

# Admin boundaries sidecar for Valhalla's admin DB. The routing input is
# a bbox cut, which clips the neighbours' country boundary relations into
# unclosed rings — valhalla_build_admins drops those, the admin DB covers
# only CH+FL, and every node outside its polygons defaults to
# drive-on-LEFT. That flips the bicycle costing's with-traffic turn
# exemption abroad: legal right-hand roundabout exits price like 45 s
# crossings while wrong-way counterflow pushes go free (canonical case:
# the Domodossola SP166 roundabouts, pushed against circulation).
# Fix: extract the complete level-2/4 administrative boundary relations
# from the full country PBFs (osmium tags-filter pulls the member ways
# and nodes along) and drop the merged sidecar into the container dir —
# the scripted entrypoint feeds every *.pbf in /custom_files to
# valhalla_build_admins. The parser keeps the FIRST copy of a way/node
# it sees, and "admin_bounds" sorts before "ch_pfaedle_walkable", so the
# complete geometry wins over any clipped remnant; the clipped admin
# relations themselves are dropped from the walkable PBF by the
# --valhalla preprocess so each admin polygon is built exactly once.
# Two filter passes per country because tags-filter cannot AND two tags:
# first by admin_level (small result), then by boundary=administrative.
ADMIN_BOUNDS=data/osm/admin_bounds.osm.pbf
PFAEDLE_IMAGE="${PFAEDLE_IMAGE:-carfree-pfaedle:latest}" # has osmium-tool
admin_bounds_fresh() {
  [[ $FORCE_OSM -eq 0 && -f $ADMIN_BOUNDS ]] || return 1
  local f
  for f in data/osm/*-latest.osm.pbf; do
    [[ $ADMIN_BOUNDS -nt $f ]] || return 1
  done
  return 0
}
build_admin_bounds() {
  local f cc lvl adm parts=()
  if ! compgen -G "data/osm/*-latest.osm.pbf" >/dev/null; then
    echo "no country PBFs in data/osm/ — run ./scripts/rebuild_transit.sh (step 02) first" >&2
    return 1
  fi
  for f in data/osm/*-latest.osm.pbf; do
    cc=$(basename "$f" | cut -d- -f1)
    lvl=data/osm/_tmp_admin_lvl_$cc.osm.pbf
    adm=data/osm/_tmp_admin_$cc.osm.pbf
    docker run --rm -v "$PWD:/work" -w /work "$PFAEDLE_IMAGE" \
      osmium tags-filter --overwrite -o "$lvl" "$f" r/admin_level=2 r/admin_level=4
    docker run --rm -v "$PWD:/work" -w /work "$PFAEDLE_IMAGE" \
      osmium tags-filter --overwrite -o "$adm" "$lvl" r/boundary=administrative
    rm -f "$lvl"
    parts+=("$adm")
  done
  docker run --rm -v "$PWD:/work" -w /work "$PFAEDLE_IMAGE" \
    osmium merge --overwrite -o "$ADMIN_BOUNDS" "${parts[@]}"
  rm -f "${parts[@]}"
}
if admin_bounds_fresh; then
  echo "  admin_bounds.osm.pbf up to date — skipped"
else
  echo "  building admin_bounds.osm.pbf (complete country/state boundaries)"
  time build_admin_bounds
fi
# Ship it to the container dir (container-owned, hence the docker cp) and
# retire an admin DB older than it — the scripted entrypoint rebuilds the
# DB only when the file is missing, and an admin rebuild forces the full
# tile build that bakes the corrected drive-side into the graph.
if [[ ! -f valhalla/data/admin_bounds.osm.pbf \
      || $ADMIN_BOUNDS -nt valhalla/data/admin_bounds.osm.pbf ]]; then
  docker run --rm -v "$PWD/data/osm:/src" -v "$PWD/valhalla/data:/d" alpine \
    cp /src/admin_bounds.osm.pbf /d/admin_bounds.osm.pbf
fi
if [[ -f valhalla/data/admins.sqlite \
      && valhalla/data/admin_bounds.osm.pbf -nt valhalla/data/admins.sqlite ]]; then
  echo "  admins.sqlite predates admin_bounds.osm.pbf — removing so step 4 rebuilds it"
  docker run --rm -v "$PWD/valhalla/data:/d" alpine rm -f /d/admins.sqlite
fi

# Elevation cells from Mapterhorn (mapterhorn-elevation-source.md). The
# container no longer downloads AWS terrain tiles (build_elevation=False
# in the compose) — those are void-filled SRTM, off by +200-450 m in
# gorges (Gondo), which faked profiles, ascent totals, durations and
# costing detours. The generator is a one-off: it runs when its output
# is missing, when it changed itself, or on --force-elevation; routine
# rebuilds never touch it. A regenerated set is synced into the
# container dir wholesale (never mixed with leftover AWS cells) and the
# tile-wipe below picks the new stamp up so step 4 rebuilds against it.
ELEV_SRC=data/elevation/mapterhorn_hgt
ELEV_STAMP=$ELEV_SRC/.mapterhorn_stamp
ELEV_DST_STAMP=valhalla/data/elevation_data/.mapterhorn_stamp
if [[ $FORCE_ELEVATION -eq 0 && -f $ELEV_STAMP \
      && $ELEV_STAMP -nt scripts/routing/build_valhalla_elevation.py ]]; then
  echo "  mapterhorn elevation cells up to date — skipped"
else
  time python3 scripts/routing/build_valhalla_elevation.py --force
fi
if [[ ! -f $ELEV_DST_STAMP || $ELEV_STAMP -nt $ELEV_DST_STAMP ]]; then
  echo "  installing mapterhorn elevation into valhalla/data/elevation_data"
  docker run --rm -v "$PWD/$ELEV_SRC:/src:ro" -v "$PWD/valhalla/data:/d" alpine \
    sh -c 'rm -rf /d/elevation_data && mkdir -p /d/elevation_data && cp -r /src/. /d/elevation_data/'
  # Tiles and matrix must describe the same surface: the wipe below
  # rebuilds the tiles; the matrix is rebuilt every update_map cycle on
  # the data machine, so locally only a warning is needed.
  if [[ -f motis/data/valhalla_footpath_matrix.csv ]]; then
    echo "  NOTE: footpath matrix predates the new elevation — it will be"
    echo "  rebuilt in the next update_map cycle; for a local rebuild run"
    echo "  step 6 with --force-matrix."
  fi
fi

# Valhalla never notices a changed PBF: use_tiles_ignore_pbf=True means it
# serves whatever tiles exist. So the staleness check belongs here, right
# after the input is rewritten — deciding it earlier (as update_map.sh used
# to) reads the walk network's timestamp from before this step regenerated
# it, and a walk-network change with unchanged OSM data would then never
# reach the tiles. Tiles are container-owned, hence the docker-side rm; the
# slow elevation and admin data are kept.
# The upstream Valhalla version counts as an input too: a version bump
# changes what the tiles mean without touching any data file. Left out, an
# image bump would serve the previous version's tiles and the upgrade would
# look like it had landed when it had not — the same silent skip that
# data-only guards produced for code changes. The version is the
# VALHALLA_REF pinned in the fork's Dockerfile — NOT the compose file's
# mtime, which used to stand in for it: the compose now names the fork
# image, and a fork iteration that only changes costing must leave the
# tiles alone (bicycle-costing-fork.md). The tiles remember which version
# built them in a stamp next to them; tiles without a stamp predate the
# stamp and were built with the version pinned today, so they get one
# instead of a rebuild.
TILES_TAR=valhalla/data/valhalla_tiles.tar
TILES_VERSION_STAMP=valhalla/data/.tiles_valhalla_version
VALHALLA_PIN="$(sed -n 's/^ARG VALHALLA_REF=//p' valhalla/fork/Dockerfile | head -1)"
if [[ -z "$VALHALLA_PIN" ]]; then
  echo "cannot read VALHALLA_REF from valhalla/fork/Dockerfile" >&2
  exit 1
fi
# Written through docker like the wipe below: valhalla/data/ is
# container-owned, so a host-side write fails on Linux.
stamp_tiles_version() {
  docker run --rm -v "$PWD/valhalla/data:/d" alpine \
    sh -c "echo '$VALHALLA_PIN' > /d/.tiles_valhalla_version"
}
if [[ -f "$TILES_TAR" && ! -f "$TILES_VERSION_STAMP" ]]; then
  stamp_tiles_version
  echo "  stamped existing tiles as built with Valhalla $VALHALLA_PIN"
fi
tiles_version_stale() {
  [[ -f "$TILES_VERSION_STAMP" ]] || return 1
  [[ "$(cat "$TILES_VERSION_STAMP")" != "$VALHALLA_PIN" ]]
}
if [[ -f "$TILES_TAR" ]] \
   && { [[ data/osm/ch_pfaedle_walkable.osm.pbf -nt "$TILES_TAR" ]] || tiles_version_stale \
        || [[ ! -f valhalla/data/admins.sqlite ]] \
        || [[ "$ELEV_DST_STAMP" -nt "$TILES_TAR" ]]; }; then
  if tiles_version_stale; then
    echo "  Valhalla tiles were built with $(cat "$TILES_VERSION_STAMP"), pin is $VALHALLA_PIN — wiping so step 4 rebuilds"
  elif [[ ! -f valhalla/data/admins.sqlite ]]; then
    echo "  admin DB pending rebuild — wiping tiles so step 4 bakes the fresh admin data in"
  elif [[ "$ELEV_DST_STAMP" -nt "$TILES_TAR" ]]; then
    echo "  elevation set is newer than the tiles — wiping so step 4 rebuilds against it"
  else
    echo "  Valhalla tiles are older than their inputs — wiping so step 4 rebuilds"
  fi
  (cd valhalla && docker compose down) >/dev/null 2>&1 || true
  # valhalla.json goes with them: it is generated by the image, and a
  # config written by an older Valhalla can silently lack keys the new one
  # needs. Everything in it comes from the compose environment, so there is
  # nothing hand-tuned to lose. Elevation and admin data are kept — they
  # are slow to fetch and version-independent.
  docker run --rm -v "$PWD/valhalla/data:/d" alpine \
    sh -c 'rm -rf /d/valhalla_tiles /d/valhalla_tiles.tar /d/file_hashes.txt /d/valhalla.json /d/.tiles_valhalla_version'
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
# The container no longer fetches elevation itself (build_elevation=False):
# a tile build against an empty elevation dir would silently produce
# grade-less tiles, so refuse to start one.
if [[ ! -f valhalla/data/valhalla_tiles.tar ]] \
   && ! ls valhalla/data/elevation_data/*/*.hgt >/dev/null 2>&1; then
  echo "no elevation cells in valhalla/data/elevation_data — run step 3 first" >&2
  exit 1
fi
if [[ ! -f valhalla/data/valhalla_tiles.tar ]]; then
  echo "  no tiles yet — first run builds admins + tiles (~20-40 min)."
  echo "  Follow along: docker logs -f kora-valhalla"
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
# Freshly built tiles get their version stamp (see step 3). The pin is
# re-read here because step 4 can run without step 3 (--steps 4); the
# write goes through docker because valhalla/data/ is container-owned.
if [[ -f valhalla/data/valhalla_tiles.tar && ! -f valhalla/data/.tiles_valhalla_version ]]; then
  PIN_NOW="$(sed -n 's/^ARG VALHALLA_REF=//p' valhalla/fork/Dockerfile | head -1)"
  docker run --rm -v "$PWD/valhalla/data:/d" alpine \
    sh -c "echo '$PIN_NOW' > /d/.tiles_valhalla_version"
  echo "  stamped tiles as built with Valhalla $PIN_NOW"
fi
fi

if want 5; then
# ── Step 5: GTFS sidecar for MOTIS ──────────────────────────────────
echo ""
echo "▶ Step 5 — Preprocess GTFS for MOTIS (platform-anchored stops.txt)"
time python3 scripts/routing/preprocess_gtfs_for_motis.py
fi

if want 6; then
# ── Step 6: footpath matrix ─────────────────────────────────────────
# Complete = CSV present with no checkpoint (the builder deletes its
# checkpoint on successful completion; a lingering checkpoint marks a
# partial run, which the builder resumes). A remotely-built CSV (see
# .claude/runbooks/matrix_build_remote.md) ships without a checkpoint
# and is therefore treated as complete. Heavy on a laptop.
echo ""
echo "▶ Step 6 — Valhalla footpath matrices (foot + stroller)"
# Two matrices, one per walker (routing-options.md § Stroller mode): the
# foot matrix feeds the default + full transfer tables, the stroller one
# the stroller table. Same skip / resume logic each; the import refuses
# to run without both.
build_matrix() {
  local profile="$1" csv="$2" ckpt="$3"
  if [[ $FORCE_MATRIX -eq 1 ]]; then
    rm -f "$csv" "$ckpt"
    echo "  --force-matrix: deleted $profile CSV + checkpoint"
  fi
  if [[ -f "$csv" && ! -f "$ckpt" ]]; then
    echo "  $profile matrix complete — skipped (--force-matrix to recompute)"
  else
    # A checkpoint without a CSV is stale resume state — the builder
    # would skip those sources and leave holes in a fresh build.
    if [[ -f "$ckpt" && ! -f "$csv" ]]; then
      rm -f "$ckpt"
      echo "  removed stale $profile checkpoint (no CSV)"
    fi
    time python3 scripts/routing/build_valhalla_footpath_matrix.py --profile "$profile"
  fi
}
build_matrix foot     motis/data/valhalla_footpath_matrix.csv \
                      motis/data/valhalla_footpath_matrix.checkpoint
build_matrix stroller motis/data/valhalla_footpath_matrix_stroller.csv \
                      motis/data/valhalla_footpath_matrix_stroller.checkpoint
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
  python3 scripts/routing/check_gtfs_motis_consistency.py
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
