#!/usr/bin/env bash
# Push a finished update_map.sh run from this data machine to the dev Mac,
# so the Mac is current without re-running the pipeline (and without ever
# building the footpath matrix, which it cannot do in reasonable time).
#
# The Mac develops the code; this machine produces the data. Everything
# here therefore flows data-machine → Mac. Nothing here touches tracked
# repo files — only generated artifacts.
#
# Groups (all run by default — the script's job is to leave the Mac able to
# run and debug everything, so nothing relevant is opt-in; `--no-routed`
# drops the one big group when you only want the app current):
#   assets    static/map-assets/     ~470 MB  pmtiles, style, indexes, glyphs
#   motis     motis/data/            ~4.5 GB  prebuilt nigiri/OSR/shapes indexes
#   valhalla  valhalla/data/         ~1.0 GB  tile extract + admins
#   lookup    data/ (raw feed +      ~400 MB  the whole GTFS feed, diagnostics,
#             derived tables)                 identity + OSM way extracts
#   routed    data/gtfs_routed/ +    ~6.2 GB  opt-in; needed to run steps 6-8
#             data/gtfs_motis/                or to re-import MOTIS on the Mac
#
# Feed directories travel WHOLE or not at all. Shipping a few tables out of
# data/gtfs/ (or the sidecar's lone stops.txt out of data/gtfs_motis/) left
# the Mac with directories mixing two GTFS releases. That is not merely
# incomplete: SBB renumbers quays between releases, so a new stops.txt over
# an old stop_times.txt leaves dangling stop ids, and MOTIS imports that
# without complaint — it drops the unresolvable stops, keeps the trips, and
# the affected station silently stops appearing in routing results.
#
# The `motis` group ships the finished indexes AND the 1.8 GB footpath
# matrix CSV. The matrix used to be opt-in (--with-matrix), on the theory
# that the Mac never re-imports because MOTIS indexes are architecture-
# portable. In practice the Mac re-imports whenever the fork's import path
# changes, and a Mac holding tiles from this machine next to its own months-
# old matrix produces transfers the tiles cannot walk — silently, since the
# import only counts the unresolvable ids. The matrix and the Valhalla tiles
# describe the same walking and must travel together.
#
# Usage:
#   ./scripts/sync_to_mac.sh                      # everything (all five groups)
#   ./scripts/sync_to_mac.sh --only assets,lookup
#   ./scripts/sync_to_mac.sh --no-routed          # skip the 6.2 GB routed feed
#   ./scripts/sync_to_mac.sh --dry-run
#
# Extra arguments are passed through to rsync.
set -euo pipefail

# SSH alias from ~/.ssh/config (georgbrodbeck@MacBook-Air-von-Georg.local).
REMOTE="${MAC_REMOTE:-mac}"
REMOTE_PATH="${MAC_PATH:-/Users/georgbrodbeck/Documents/prog/newmap}"
REMOTE_PATH="${REMOTE_PATH%/}/"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# NB: not `GROUPS` — that is a bash special array (the caller's group ids)
# and assigning to it silently does nothing.
SYNC_GROUPS="assets,motis,valhalla,lookup,routed"
STREET_WAYS=0
FULL_VALHALLA=0
FORCE=0
DRY_RUN=0
RSYNC_ARGS=()

while [ $# -gt 0 ]; do
	case "$1" in
		--only)           SYNC_GROUPS="$2"; shift 2 ;;
		--only=*)         SYNC_GROUPS="${1#*=}"; shift ;;
		--no-routed)      SYNC_GROUPS="${SYNC_GROUPS//,routed/}"; shift ;;
		# Accepted and ignored: both ship by default now. Kept so the old
		# habits do not fall through to rsync as unknown options.
		--with-routed)    shift ;;
		--with-matrix)    shift ;;
		--street-ways)    STREET_WAYS=1; shift ;;
		--full-valhalla)  FULL_VALHALLA=1; shift ;;
		--force)          FORCE=1; shift ;;
		--dry-run|-n)     DRY_RUN=1; RSYNC_ARGS+=("$1"); shift ;;
		*)                RSYNC_ARGS+=("$1"); shift ;;
	esac
done

want() { case ",$SYNC_GROUPS," in *",$1,"*) return 0 ;; esac; return 1; }
banner() { printf '\n\033[1m── %s\033[0m\n' "$*"; }
warn()   { printf '\033[33mskip: %s\033[0m\n' "$*" >&2; }

# macOS ships openrsync, which negotiates protocol 29 with GNU rsync here;
# -a -v --partial --delete and the include/exclude filter chain all work.
# --partial matters on WiFi: a dropped connection resumes mid-file instead
# of restarting a 1.4 GB index from zero. -z is passed per group: on for
# text payloads (JSON/CSV/GeoJSON compress 9-20x), off for pmtiles and the
# binary indexes, where it only burns CPU on an already-fast link.
BASE=(-a -v --partial --human-readable)

# push <label> <dest-subpath> <rsync opts...> <sources...>
# Sources come last, as in a normal rsync invocation, so a group can send
# an arbitrary number of them.
push() {
	local label="$1" dest="$2"; shift 2
	banner "$label"
	rsync "${BASE[@]}" ${RSYNC_ARGS[@]+"${RSYNC_ARGS[@]}"} "$@" \
		"$REMOTE:${REMOTE_PATH}${dest}"
}

# ── Preflight ────────────────────────────────────────────────────────
banner "Preflight"

# A running update_map.sh means artifacts are being rewritten underneath
# us — pfaedle mid-run, or the Valhalla tile wipe that precedes a rebuild.
# Syncing that state with --delete would replace good data on the Mac with
# a half-built or empty one. Only sync a finished run.
if [ "$FORCE" -eq 0 ] && pgrep -f "update_map\.sh" >/dev/null 2>&1; then
	echo "error: update_map.sh is running — its artifacts are mid-rewrite." >&2
	echo "       Wait for it to finish, or pass --force if you know better." >&2
	exit 1
fi

ssh "$REMOTE" "[ -d '${REMOTE_PATH}' ]" \
	|| { echo "error: ${REMOTE_PATH} not found on $REMOTE" >&2; exit 1; }
echo -n "free on Mac: "
ssh "$REMOTE" "df -h '${REMOTE_PATH}' | tail -1 | awk '{print \$4\" (\"\$5\" used)\"}'"
echo "groups: $SYNC_GROUPS"

# Every --delete group is gated on a sentinel that only exists once that
# group's build actually completed. Cheap insurance against mirroring an
# interrupted run onto the Mac.
have() {
	if [ -s "$ROOT/$2" ]; then return 0; fi
	warn "$1 — $2 missing or empty locally (build incomplete?)"
	return 1
}

# ── assets ───────────────────────────────────────────────────────────
# Same allowlist as deploy_map_assets.sh, so the Mac's dev server and the
# VPS serve byte-identical assets. Debug bundles stay on this machine.
if want assets && have assets "static/map-assets/style.json"; then
	push "map assets → static/map-assets/" "static/map-assets/" \
		--delete \
		--exclude 'tl_debug_*' \
		--include '*.json' \
		--include 'fonts/***' \
		--include 'tl_*.pmtiles' \
		--exclude '*' \
		"$ROOT/static/map-assets/"
fi

# ── motis ────────────────────────────────────────────────────────────
# Two pushes, because -z is worth it for exactly one file here: the indexes
# are binary and incompressible, the matrix is text and compresses ~8.5x.
# The CSV is therefore excluded from the index push and sent on its own.
# The exclude also keeps --delete from removing the Mac's copy in the gap
# between the two pushes (rsync never deletes excluded files unless
# --delete-excluded is given).
if want motis && have motis "motis/data/tt.bin"; then
	push "MOTIS indexes → motis/data/" "motis/data/" \
		--delete --exclude 'valhalla_footpath_matrix.csv' \
		"$ROOT/motis/data/"

	# Always ships — see the header note. Guarded by its own sentinel: a
	# missing matrix means an incomplete run, and skipping it leaves the
	# Mac's copy alone rather than aborting the whole sync.
	if have motis "motis/data/valhalla_footpath_matrix.csv"; then
		push "footpath matrix → motis/data/ (1.8 GB text, compressed in flight)" \
			"motis/data/" -z \
			"$ROOT/motis/data/valhalla_footpath_matrix.csv"
	fi
fi

# ── valhalla ─────────────────────────────────────────────────────────
# Valhalla mmaps tile_extract (valhalla_tiles.tar) when present, so the
# loose valhalla_tiles/ dir is a build artifact and redundant at serve
# time; elevation_data/ is consumed during the tile build, not after.
# --full-valhalla ships both anyway.
if want valhalla && have valhalla "valhalla/data/valhalla_tiles.tar"; then
	VALHALLA_EXCLUDES=(--exclude '*.pbf')
	if [ "$FULL_VALHALLA" -eq 0 ]; then
		VALHALLA_EXCLUDES+=(--exclude 'valhalla_tiles/' --exclude 'elevation_data/')
	fi
	push "Valhalla tiles → valhalla/data/" "valhalla/data/" \
		--delete "${VALHALLA_EXCLUDES[@]}" \
		"$ROOT/valhalla/data/"
fi

# ── lookup ───────────────────────────────────────────────────────────
# The raw GTFS feed plus the derived tables worth grepping while
# debugging. --delete is used only on data/gtfs/, which this machine owns
# end to end; the other pushes land inside the Mac's own 40+ GB data/ tree
# and must never delete.
# Deliberately excluded: the country PBFs (12.7 GB, unreadable without
# osmium), gtfs_complete.zip (a second copy of the feed we send anyway),
# and the large intermediate GeoJSONs.
if want lookup; then
	# '*/' keeps the recursion alive so data/transit/diagnostics/ comes
	# along; without it the bare '*' exclude pruned every subdirectory.
	push "diagnostics → data/transit/" "data/transit/" \
		-z --include '*/' --include '*.json' --exclude '*' \
		"$ROOT/data/transit/"

	# The raw feed goes over WHOLE, never table by table. It used to ship
	# as six small tables (stops, routes, agency, calendar, frequencies,
	# feed_info) with stop_times / trips / calendar_dates left behind as
	# "pipeline fuel". That left the Mac with a data/gtfs/ whose
	# feed_info.txt announced the new release while its big tables were
	# the previous one — a directory that lies about its vintage, which is
	# worse to debug against than one that is simply absent. It also made
	# the two questions you actually ask ("what is this trip_id?", "does
	# this service run on that date?") unanswerable, because trips.txt and
	# calendar_dates.txt were the missing ones. ~3.6 GB raw but it
	# overwrites the Mac's existing copy in place, so the disk delta is
	# ~zero, and -z gets it down to ~240 MB in flight.
	# gtfs_complete.zip is excluded: it is a second copy of what we just
	# sent. --delete is safe here (excluded files are never deleted) and
	# necessary — a table the new release dropped must not linger.
	if have lookup "data/gtfs/stop_times.txt"; then
		push "raw GTFS feed → data/gtfs/" "data/gtfs/" \
			-z --delete --exclude 'gtfs_complete.zip' \
			"$ROOT/data/gtfs/"
	fi

	if [ -f "$ROOT/data/gtfs_filtered/stop_identity.json" ]; then
		push "stop identity → data/gtfs_filtered/" "data/gtfs_filtered/" -z \
			"$ROOT/data/gtfs_filtered/stop_identity.json"
	fi

	# NB: data/gtfs_motis/stops.txt is deliberately NOT pushed here — it
	# ships with the `routed` group instead. Everything else in that
	# directory is hardlinked from data/gtfs_routed/, so sending the one
	# independent file on its own gave the Mac this machine's new stops
	# on top of its own old stop_times. See the `routed` group below.

	OSM_EXTRACTS=()
	OSM_WANTED=(rail_ways.geojson tram_ways.geojson platform_ways.geojson
	            builtup_grid_100m.json quay_anchors.json)
	# 152 MB; only worth it if you inspect bus geometry regularly.
	if [ "$STREET_WAYS" -eq 1 ]; then OSM_WANTED+=(street_ways.geojson); fi
	for f in "${OSM_WANTED[@]}"; do
		if [ -f "$ROOT/data/osm/$f" ]; then OSM_EXTRACTS+=("$ROOT/data/osm/$f"); fi
	done
	if [ ${#OSM_EXTRACTS[@]} -gt 0 ]; then
		push "OSM way extracts → data/osm/" "data/osm/" -z "${OSM_EXTRACTS[@]}"
	fi
fi

# ── routed ───────────────────────────────────────────────────────────
# The feed the router actually consumed: pfaedle's output, and the input
# to both `rebuild_transit.sh --start 6` and a MOTIS re-import on the Mac.
# The sidecar's stops.txt rides along, because it is only meaningful next
# to the routed feed it is hardlinked from — shipping it alone is what
# produced a mixed-vintage import (see the lookup group). Both or neither.
#
# 6.2 GB, the largest group, but it overwrites the Mac's copy in place so
# the disk does not grow, and -z puts far less than that on the wire.
# `--no-routed` skips it — after which do NOT re-import MOTIS on the Mac:
# the `motis` group already delivered this machine's finished indexes, and
# a local import would rebuild them from a stale data/gtfs_motis/.
# setup_routing.sh step 7 refuses the obviously-broken case, but "old yet
# internally consistent" passes by design.
if want routed && have routed "data/gtfs_routed/shapes.txt"; then
	push "routed GTFS → data/gtfs_routed/ (steps 6-8 + MOTIS import input)" \
		"data/gtfs_routed/" -z --delete "$ROOT/data/gtfs_routed/"

	# stops.txt only: the rest of the sidecar is hardlinked from the feed
	# we just sent, and preprocess_gtfs_for_motis.py rebuilds those links
	# on the Mac anyway. Sent after the feed so the pair is never
	# momentarily mismatched.
	if [ -f "$ROOT/data/gtfs_motis/stops.txt" ]; then
		push "MOTIS sidecar stops → data/gtfs_motis/" "data/gtfs_motis/" -z \
			"$ROOT/data/gtfs_motis/stops.txt"
	fi
fi

banner "Done"
if [ "$DRY_RUN" -eq 1 ]; then
	echo "(dry run — nothing was written)"
fi
