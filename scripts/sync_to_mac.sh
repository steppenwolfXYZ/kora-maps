#!/usr/bin/env bash
# Push a finished update_map.sh run from this data machine to the dev Mac,
# so the Mac is current without re-running the pipeline (and without ever
# building the footpath matrix, which it cannot do in reasonable time).
#
# The Mac develops the code; this machine produces the data. Everything
# here therefore flows data-machine → Mac. Nothing in this script touches
# the repo's tracked files — only generated artifacts under static/,
# motis/data/, valhalla/data/ and data/.
#
# Groups (all but `routed` run by default):
#   assets    static/map-assets/     ~470 MB  pmtiles, style, indexes, glyphs
#   motis     motis/data/            ~4.5 GB  prebuilt nigiri/OSR/shapes indexes
#   valhalla  valhalla/data/         ~1.0 GB  tile extract + admins
#   lookup    data/ (derived only)   ~150 MB  diagnostic + identity tables
#   routed    data/gtfs_routed/      ~6.2 GB  opt-in, only to run steps 6-8 there
#
# MOTIS indexes are architecture-portable — this machine imports on amd64
# and update_map.sh already ships those same indexes to the arm64 VPS — so
# the Mac gets the finished indexes and never needs the 1.8 GB matrix CSV.
# Pass --with-matrix only when you intend to re-import MOTIS on the Mac
# (i.e. you are changing the fork's import path, not its query path).
#
# Usage:
#   ./scripts/sync_to_mac.sh                      # assets, motis, valhalla, lookup
#   ./scripts/sync_to_mac.sh --only assets,lookup
#   ./scripts/sync_to_mac.sh --with-routed        # + gtfs_routed (run steps 6-8 there)
#   ./scripts/sync_to_mac.sh --dry-run
#
# Extra arguments are passed through to rsync.
set -euo pipefail

# SSH alias from ~/.ssh/config (georgbrodbeck@MacBook-Air-von-Georg.local).
REMOTE="${MAC_REMOTE:-mac}"
REMOTE_PATH="${MAC_PATH:-/Users/georgbrodbeck/Documents/prog/newmap}"
REMOTE_PATH="${REMOTE_PATH%/}/"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

SYNC_GROUPS="assets,motis,valhalla,lookup"
WITH_MATRIX=0
STREET_WAYS=0
FULL_VALHALLA=0
DRY_RUN=0
RSYNC_ARGS=()

while [ $# -gt 0 ]; do
	case "$1" in
		--only)           SYNC_GROUPS="$2"; shift 2 ;;
		--only=*)         SYNC_GROUPS="${1#*=}"; shift ;;
		--with-routed)    SYNC_GROUPS="$SYNC_GROUPS,routed"; shift ;;
		--with-matrix)    WITH_MATRIX=1; shift ;;
		--street-ways)    STREET_WAYS=1; shift ;;
		--full-valhalla)  FULL_VALHALLA=1; shift ;;
		--dry-run|-n)     DRY_RUN=1; RSYNC_ARGS+=("$1"); shift ;;
		*)                RSYNC_ARGS+=("$1"); shift ;;
	esac
done

want() { case ",$SYNC_GROUPS," in *",$1,"*) return 0 ;; esac; return 1; }
banner() { printf '\n\033[1m── %s\033[0m\n' "$*"; }

# macOS ships openrsync, which negotiates protocol 29 with GNU rsync here.
# --partial matters on WiFi: a dropped connection resumes mid-file instead
# of restarting a 1.4 GB index from zero. -z is applied per group: on for
# text payloads (JSON/CSV/GeoJSON compress 9-20x), off for pmtiles and the
# binary indexes, where it only burns CPU on an already-fast link.
BASE=(-a -v --partial --human-readable)

push() {  # push <label> <src> <dest-subpath> [extra rsync args...]
	local label="$1" src="$2" dest="$3"; shift 3
	banner "$label"
	rsync "${BASE[@]}" "$@" ${RSYNC_ARGS[@]+"${RSYNC_ARGS[@]}"} \
		"$src" "$REMOTE:${REMOTE_PATH}${dest}"
}

# ── Preflight ────────────────────────────────────────────────────────
# The Mac's data volume runs close to full, and this pushes several GB.
banner "Preflight"
ssh "$REMOTE" "[ -d '${REMOTE_PATH}' ]" \
	|| { echo "error: ${REMOTE_PATH} not found on $REMOTE" >&2; exit 1; }
echo -n "free on Mac: "
ssh "$REMOTE" "df -h '${REMOTE_PATH}' | tail -1 | awk '{print \$4\" (\"\$5\" used)\"}'"
echo "groups: $SYNC_GROUPS"

# ── assets ───────────────────────────────────────────────────────────
# Same allowlist as deploy_map_assets.sh, so the Mac's dev server and the
# VPS serve byte-identical assets. Debug bundles stay on this machine.
if want assets; then
	push "map assets → static/map-assets/" \
		"$ROOT/static/map-assets/" "static/map-assets/" \
		--delete \
		--exclude 'tl_debug_*' \
		--include '*.json' \
		--include 'fonts/***' \
		--include 'tl_*.pmtiles' \
		--exclude '*'
fi

# ── motis ────────────────────────────────────────────────────────────
# The matrix CSV is an import-time input only. Excluding it also protects
# any copy already on the Mac from --delete (rsync never deletes excluded
# files unless --delete-excluded is given).
if want motis; then
	push "MOTIS indexes → motis/data/" \
		"$ROOT/motis/data/" "motis/data/" \
		--delete --exclude 'valhalla_footpath_matrix.csv'

	if [ "$WITH_MATRIX" -eq 1 ]; then
		push "footpath matrix → motis/data/ (1.8 GB text, compressed in flight)" \
			"$ROOT/motis/data/valhalla_footpath_matrix.csv" "motis/data/" -z
	fi
fi

# ── valhalla ─────────────────────────────────────────────────────────
# Valhalla mmaps tile_extract (valhalla_tiles.tar) when it exists, so the
# loose valhalla_tiles/ dir is a build artifact and redundant at serve
# time; elevation_data/ is consumed during the tile build, not after.
# --full-valhalla ships both anyway.
if want valhalla; then
	VALHALLA_EXCLUDES=(--exclude '*.pbf')
	if [ "$FULL_VALHALLA" -eq 0 ]; then
		VALHALLA_EXCLUDES+=(--exclude 'valhalla_tiles/' --exclude 'elevation_data/')
	fi
	push "Valhalla tiles → valhalla/data/" \
		"$ROOT/valhalla/data/" "valhalla/data/" \
		--delete "${VALHALLA_EXCLUDES[@]}"
fi

# ── lookup ───────────────────────────────────────────────────────────
# Derived tables only — the ones actually worth grepping while debugging.
# NEVER --delete here: these land inside the Mac's own 40+ GB data/ tree.
# Deliberately excluded: the country PBFs (12.7 GB, unreadable without
# osmium), stop_times.txt / trips.txt / calendar_dates.txt (pipeline fuel,
# not lookup material), and the large intermediate GeoJSONs.
if want lookup; then
	push "diagnostics → data/transit/" \
		"$ROOT/data/transit/" "data/transit/" \
		-z --include '*.json' --exclude '*'

	GTFS_TABLES=()
	for f in stops.txt routes.txt agency.txt calendar.txt frequencies.txt feed_info.txt; do
		if [ -f "$ROOT/data/gtfs/$f" ]; then GTFS_TABLES+=("$ROOT/data/gtfs/$f"); fi
	done
	push "GTFS lookup tables → data/gtfs/" \
		"${GTFS_TABLES[@]}" "data/gtfs/" -z

	push "stop identity → data/gtfs_filtered/" \
		"$ROOT/data/gtfs_filtered/stop_identity.json" "data/gtfs_filtered/" -z

	OSM_EXTRACTS=(
		"$ROOT/data/osm/rail_ways.geojson"
		"$ROOT/data/osm/tram_ways.geojson"
		"$ROOT/data/osm/platform_ways.geojson"
		"$ROOT/data/osm/builtup_grid_100m.json"
	)
	# 152 MB; only worth it if you inspect bus geometry regularly.
	if [ "$STREET_WAYS" -eq 1 ]; then
		OSM_EXTRACTS+=("$ROOT/data/osm/street_ways.geojson")
	fi
	push "OSM way extracts → data/osm/" \
		"${OSM_EXTRACTS[@]}" "data/osm/" -z
fi

# ── routed (opt-in) ──────────────────────────────────────────────────
# Only needed to run rebuild_transit.sh --start 6 on the Mac, i.e. to test
# an emission change against fresh data without re-running pfaedle there.
if want routed; then
	push "routed GTFS → data/gtfs_routed/ (steps 6-8 input)" \
		"$ROOT/data/gtfs_routed/" "data/gtfs_routed/" -z --delete
fi

banner "Done"
if [ "$DRY_RUN" -eq 1 ]; then
	echo "(dry run — nothing was written)"
fi
