#!/usr/bin/env bash
# Pull a finished build from the data machine (Kranich) to this Mac.
#
# The mirror image of the old scripts/sync_to_mac.sh, which pushed. The
# direction flipped when the Mac became the machine that drives builds:
# the roaming machine should be the client, so that the link the transfer
# runs over is the same one you are already watching, the data machine
# needs no credentials for (and no way to reach) the Mac, and --delete
# acts on the machine you are sitting at.
#
# What did NOT change: the five groups, their filter chains, which ones
# use --delete, which ones compress, and the sentinel files that gate
# them. That logic encodes past incidents and is direction-agnostic.
#
# Groups (all run by default; --only a,b selects, --no-routed drops the
# big one):
#   assets    static/map-assets/     ~470 MB  pmtiles, style, indexes, glyphs
#   motis     motis/data/            ~6.3 GB  nigiri/OSR/shapes indexes + matrix
#   valhalla  valhalla/data/         ~1.0 GB  tile extract + admins
#   lookup    data/ (raw feed +      ~400 MB  whole GTFS feed, diagnostics,
#             derived tables)                 identity + OSM way extracts
#   routed    data/gtfs_routed/ +    ~6.2 GB  pfaedle's feed; input to
#             data/gtfs_motis/                --start 6 and to a re-import
#
# Feed directories travel WHOLE or not at all. Shipping a few tables out
# of data/gtfs/ (or the sidecar's lone stops.txt) leaves this Mac with
# directories mixing two GTFS releases. SBB renumbers quays between
# releases, so a new stops.txt over an old stop_times.txt dangles stop
# ids — and MOTIS imports that without complaint, dropping the
# unresolvable stops and silently losing whole station calls.
#
# Usage:
#   ./scripts/fetch_build.sh                      # everything
#   ./scripts/fetch_build.sh --only assets,lookup
#   ./scripts/fetch_build.sh --no-routed
#   ./scripts/fetch_build.sh --dry-run
#
# Env:
#   KRANICH_REMOTE  SSH alias of the data machine   (default: kranich)
#   KRANICH_PATH    repo path over there            (default: ~/kora-maps)
#   RSYNC_BIN       rsync to use                    (default: autodetect)
#
# Extra arguments are passed through to rsync.
set -euo pipefail

REMOTE="${KRANICH_REMOTE:-kranich}"
REMOTE_PATH="${KRANICH_PATH:-~/kora-maps}"
REMOTE_PATH="${REMOTE_PATH%/}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

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

# ── rsync flavour ────────────────────────────────────────────────────
# Under the old push, GNU rsync on the data machine drove the filter
# chain and this Mac was a passive receiver, so the bundled openrsync was
# fine. Pulling makes the Mac the client that interprets --include /
# --exclude / --delete, and openrsync's filter-rule support is not up to
# it — the failure mode is silently transferring the wrong set, not an
# error. So: require GNU rsync.
if [ -n "${RSYNC_BIN:-}" ]; then
	RSYNC="$RSYNC_BIN"
elif [ -x /opt/homebrew/bin/rsync ]; then
	RSYNC=/opt/homebrew/bin/rsync
elif [ -x /usr/local/bin/rsync ]; then
	RSYNC=/usr/local/bin/rsync
else
	RSYNC=rsync
fi
if "$RSYNC" --version 2>&1 | head -1 | grep -qi openrsync; then
	echo "error: $RSYNC is openrsync, which cannot be trusted to drive the" >&2
	echo "       filter chain as the pulling client (silent wrong-set" >&2
	echo "       transfers, not errors). Install GNU rsync:" >&2
	echo "         brew install rsync" >&2
	echo "       or point RSYNC_BIN at one." >&2
	exit 1
fi

# --partial: a dropped WiFi link resumes mid-file instead of restarting a
# 1.4 GB index. --timeout: a stalled connection fails in two minutes
# instead of hanging until someone notices. -z is per group: on for text
# payloads (JSON/CSV/GeoJSON compress 9-20x), off for pmtiles and binary
# indexes, where it only burns CPU on an already-fast link.
BASE=(-a -v --partial --human-readable --timeout=120)

# pull <label> <dest-subpath> <rsync opts…> <remote-source-subpath…>
# Remote sources are given as repo-relative paths and expanded against
# REMOTE_PATH here, so callers read like the old push() did.
pull() {
	local label="$1" dest="$2"; shift 2
	local opts=() srcs=()
	while [ $# -gt 0 ]; do
		case "$1" in
			-*) opts+=("$1") ;;
			*)  srcs+=("$REMOTE:$REMOTE_PATH/$1") ;;
		esac
		shift
	done
	banner "$label"
	mkdir -p "$ROOT/$dest"
	# Retry: the Mac is on WiFi and this leg can run for an hour. With
	# --partial each attempt resumes where the last stopped.
	local attempt
	for attempt in 1 2 3 4 5; do
		if "$RSYNC" "${BASE[@]}" ${RSYNC_ARGS[@]+"${RSYNC_ARGS[@]}"} \
			"${opts[@]}" "${srcs[@]}" "$ROOT/$dest"; then
			return 0
		fi
		if [ "$attempt" -lt 5 ]; then
			warn "$label — transfer interrupted, retry $attempt/4 in 30 s"
			sleep 30
		fi
	done
	echo "error: $label failed after 5 attempts" >&2
	return 1
}

# ── Preflight ────────────────────────────────────────────────────────
banner "Preflight"

ssh -o BatchMode=yes -o ConnectTimeout=10 "$REMOTE" true 2>/dev/null \
	|| { echo "error: cannot reach '$REMOTE' over SSH — is Tailscale up?" >&2; exit 1; }

ssh "$REMOTE" "[ -d '$REMOTE_PATH' ]" \
	|| { echo "error: $REMOTE_PATH not found on $REMOTE (set KRANICH_PATH)" >&2; exit 1; }

# A running build means artifacts are being rewritten over there —
# pfaedle mid-run, or the Valhalla tile wipe that precedes a rebuild.
# Fetching that with --delete would replace good data here with a
# half-built or empty tree. Only fetch a finished run.
if [ "$FORCE" -eq 0 ] \
	&& ssh "$REMOTE" "pgrep -f 'update_map\.sh' >/dev/null 2>&1"; then
	echo "error: a build is running on $REMOTE — its artifacts are mid-rewrite." >&2
	echo "       Wait for it to finish, or pass --force if you know better." >&2
	exit 1
fi

echo -n "free here: "
df -h "$ROOT" | tail -1 | awk '{print $4" ("$5" used)"}'
echo "remote:    $REMOTE:$REMOTE_PATH"
echo "rsync:     $RSYNC ($("$RSYNC" --version 2>&1 | head -1 | awk '{print $1, $3}'))"
echo "groups:    $SYNC_GROUPS"

# Every --delete group is gated on a sentinel that exists only once that
# group's build actually finished. Cheap insurance against mirroring an
# interrupted run. Checked on the remote now, not locally.
have() {
	if ssh "$REMOTE" "[ -s '$REMOTE_PATH/$2' ]"; then return 0; fi
	warn "$1 — $2 missing or empty on $REMOTE (build incomplete?)"
	return 1
}
rhave() { ssh "$REMOTE" "[ -f '$REMOTE_PATH/$1' ]"; }

# ── assets ───────────────────────────────────────────────────────────
# Same allowlist as deploy_map_assets.sh, so this Mac's dev server and the
# VPS serve byte-identical assets. Debug bundles stay on the data machine.
if want assets && have assets "static/map-assets/style.json"; then
	pull "map assets → static/map-assets/" "static/map-assets/" \
		--delete \
		--exclude 'tl_debug_*' \
		--include '*.json' \
		--include 'fonts/***' \
		--include 'tl_*.pmtiles' \
		--exclude '*' \
		"static/map-assets/"
fi

# ── motis ────────────────────────────────────────────────────────────
# Two transfers, because -z is worth it for exactly one file here: the
# indexes are binary and incompressible, the matrix is text and
# compresses ~8.5x. Excluding the CSV from the index transfer also keeps
# --delete from removing the local copy in the gap between the two
# (rsync never deletes excluded files without --delete-excluded).
if want motis && have motis "motis/data/tt.bin"; then
	pull "MOTIS indexes → motis/data/" "motis/data/" \
		--delete --exclude 'valhalla_footpath_matrix.csv' \
		"motis/data/"

	# The matrix always travels with the indexes. It used to be opt-in, on
	# the theory that the Mac never re-imports; in practice it re-imports
	# whenever the fork's import path changes, and a fresh tile set beside
	# a months-old matrix produces transfers the tiles cannot walk —
	# silently, since the import only counts unresolvable ids.
	if have motis "motis/data/valhalla_footpath_matrix.csv"; then
		pull "footpath matrix → motis/data/ (1.8 GB text, compressed in flight)" \
			"motis/data/" -z \
			"motis/data/valhalla_footpath_matrix.csv"
	fi
fi

# ── valhalla ─────────────────────────────────────────────────────────
# Valhalla mmaps tile_extract (valhalla_tiles.tar) when present, so the
# loose valhalla_tiles/ dir is a build artifact and redundant at serve
# time; elevation_data/ is consumed during the tile build, not after.
# --full-valhalla fetches both anyway.
if want valhalla && have valhalla "valhalla/data/valhalla_tiles.tar"; then
	VALHALLA_EXCLUDES=(--exclude '*.pbf')
	if [ "$FULL_VALHALLA" -eq 0 ]; then
		VALHALLA_EXCLUDES+=(--exclude 'valhalla_tiles/' --exclude 'elevation_data/')
	fi
	pull "Valhalla tiles → valhalla/data/" "valhalla/data/" \
		--delete "${VALHALLA_EXCLUDES[@]}" \
		"valhalla/data/"
fi

# ── lookup ───────────────────────────────────────────────────────────
# The raw GTFS feed plus the derived tables worth grepping while
# debugging. --delete is used only on data/gtfs/, which the data machine
# owns end to end; the other transfers land inside this Mac's own 40+ GB
# data/ tree and must never delete.
# Deliberately excluded: the country PBFs (12.7 GB, unreadable without
# osmium), gtfs_complete.zip (a second copy of the feed we fetch anyway),
# and the large intermediate GeoJSONs.
if want lookup; then
	# '*/' keeps the recursion alive so data/transit/diagnostics/ comes
	# along; without it the bare '*' exclude prunes every subdirectory.
	pull "diagnostics → data/transit/" "data/transit/" \
		-z --include '*/' --include '*.json' --exclude '*' \
		"data/transit/"

	# The raw feed comes over WHOLE, never table by table — see the header.
	# ~3.6 GB raw, but it overwrites in place so the disk delta is ~zero,
	# and -z gets it to ~240 MB on the wire. --delete is safe (excluded
	# files are never deleted) and necessary: a table the new release
	# dropped must not linger.
	if have lookup "data/gtfs/stop_times.txt"; then
		pull "raw GTFS feed → data/gtfs/" "data/gtfs/" \
			-z --delete --exclude 'gtfs_complete.zip' \
			"data/gtfs/"
	fi

	if rhave "data/gtfs_filtered/stop_identity.json"; then
		pull "stop identity → data/gtfs_filtered/" "data/gtfs_filtered/" -z \
			"data/gtfs_filtered/stop_identity.json"
	fi

	# NB: data/gtfs_motis/stops.txt is deliberately NOT fetched here — it
	# comes with the `routed` group instead. Everything else in that
	# directory is hardlinked from data/gtfs_routed/, so taking the one
	# independent file on its own would put the data machine's new stops
	# on top of this Mac's old stop_times.

	OSM_EXTRACTS=()
	OSM_WANTED=(rail_ways.geojson tram_ways.geojson platform_ways.geojson
	            builtup_grid_100m.json quay_anchors.json)
	# 152 MB; only worth it if you inspect bus geometry regularly.
	if [ "$STREET_WAYS" -eq 1 ]; then OSM_WANTED+=(street_ways.geojson); fi
	for f in "${OSM_WANTED[@]}"; do
		if rhave "data/osm/$f"; then OSM_EXTRACTS+=("data/osm/$f"); fi
	done
	if [ ${#OSM_EXTRACTS[@]} -gt 0 ]; then
		pull "OSM way extracts → data/osm/" "data/osm/" -z "${OSM_EXTRACTS[@]}"
	fi
fi

# ── routed ───────────────────────────────────────────────────────────
# The feed the router actually consumed: pfaedle's output, and the input
# to both `rebuild_transit.sh --start 6` and a MOTIS re-import here. The
# sidecar's stops.txt rides along, because it is only meaningful next to
# the routed feed it is hardlinked from — taking it alone is what
# produced a mixed-vintage import (see the lookup group). Both or neither.
#
# 6.2 GB, the largest group, but it overwrites in place so the disk does
# not grow. `--no-routed` skips it — after which do NOT re-import MOTIS
# here: the `motis` group already delivered finished indexes, and a local
# import would rebuild them from a stale data/gtfs_motis/.
if want routed && have routed "data/gtfs_routed/shapes.txt"; then
	pull "routed GTFS → data/gtfs_routed/ (steps 6-8 + MOTIS import input)" \
		"data/gtfs_routed/" -z --delete "data/gtfs_routed/"

	# stops.txt only: the rest of the sidecar is hardlinked from the feed
	# just fetched, and preprocess_gtfs_for_motis.py rebuilds those links
	# here anyway. Fetched after the feed so the pair is never
	# momentarily mismatched.
	if rhave "data/gtfs_motis/stops.txt"; then
		pull "MOTIS sidecar stops → data/gtfs_motis/" "data/gtfs_motis/" -z \
			"data/gtfs_motis/stops.txt"
	fi
fi

banner "Done"
if [ "$DRY_RUN" -eq 1 ]; then
	echo "(dry run — nothing was written)"
fi
