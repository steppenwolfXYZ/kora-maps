#!/usr/bin/env bash
# Bring the dev Mac's routing stack in line after a fetch_build.sh pull.
# Run from the project root: ./scripts/post_sync.sh
#
# fetch_build.sh copies files; it cannot restart anything on this side and
# it does not know whether what it delivered still matches. This script
# closes that gap: it inspects what arrived, does the minimum needed to
# make it serve correctly, and tells you what it decided and why.
#
# What it checks, in order:
#
#   1  Sidecar consistency — every stop_id in data/gtfs_motis/stop_times.txt
#      resolves in its stops.txt. It routinely will not right after a sync:
#      that directory is a hardlink farm over data/gtfs_routed/, and rsync
#      mirrors the feed by writing new inodes, so the links still point at
#      the feed this machine held before. Rebuilding the farm is the repair,
#      and it is the reason this script exists — MOTIS would otherwise
#      import the mismatch happily and silently drop the unresolvable stops,
#      which costs you whole station calls.
#
#   2  Index freshness — whether motis/data/ was built from the feed that
#      is now on disk. rsync -a preserves mtimes, so the data machine's
#      "index newer than feed" relationship survives the trip and a plain
#      timestamp comparison is meaningful here. A synced index is normally
#      current and is left alone: re-importing would rebuild the very
#      thing that was just copied over.
#
#   3  Restart — always, and in the right order. Both services mmap their
#      data, so a process that was running during the sync still holds the
#      pre-sync mapping. Valhalla comes up first: the MOTIS fork exits at
#      startup while Valhalla is unreachable.
#
#   4  Smoke test — one real plan query end to end.
#
# Flags:
#   --force-import   re-import even when the index looks current (use after
#                    changing the fork's import path)
#   --no-import      never import, whatever the check says
#   --skip-check     skip the consistency scan (~1-2 min over ~2.8 GB)
#   -n, --dry-run    report the decisions, change nothing

set -euo pipefail
cd "$(dirname "$0")/.."

FORCE_IMPORT=0
NO_IMPORT=0
SKIP_CHECK=0
DRY_RUN=0
while [ $# -gt 0 ]; do
	case "$1" in
		--force-import) FORCE_IMPORT=1 ;;
		--no-import)    NO_IMPORT=1 ;;
		--skip-check)   SKIP_CHECK=1 ;;
		-n|--dry-run)   DRY_RUN=1 ;;
		-h|--help)      sed -n '2,38p' "$0"; exit 0 ;;
		*) echo "unknown arg: $1" >&2
		   echo "usage: $0 [--force-import] [--no-import] [--skip-check] [--dry-run]" >&2
		   exit 2 ;;
	esac
	shift
done

banner() { printf '\n\033[1m── %s\033[0m\n' "$*"; }
note()   { printf '   %s\n' "$*"; }
warn()   { printf '\033[33m   %s\033[0m\n' "$*" >&2; }
run()    { if [ "$DRY_RUN" -eq 1 ]; then printf '\033[2m   would run: %s\033[0m\n' "$*"; else "$@"; fi; }

# ── Preflight ────────────────────────────────────────────────────────
banner "Preflight"
if ! docker info >/dev/null 2>&1; then
	echo "docker is not running — start Docker Desktop and retry" >&2
	exit 1
fi
for f in motis/data/tt.bin motis/data/valhalla_footpath_matrix.csv \
         valhalla/data/valhalla_tiles.tar data/gtfs_motis/stops.txt; do
	if [ ! -s "$f" ]; then
		warn "missing or empty: $f"
		warn "the fetch looks incomplete — re-run ./scripts/fetch_build.sh"
		exit 1
	fi
done
# The feed version is the single most useful thing to see up front: it is
# what every "why is this train missing" question turns out to be about.
if [ -f data/gtfs/feed_info.txt ]; then
	note "GTFS feed:    $(awk -F, 'NR==2 {gsub(/"/,"",$NF); print $NF}' data/gtfs/feed_info.txt)"
fi
note "MOTIS index:  $(date -r motis/data/tt.bin '+%Y-%m-%d %H:%M')"
note "Valhalla:     $(date -r valhalla/data/valhalla_tiles.tar '+%Y-%m-%d %H:%M')"
note "Map assets:   $(date -r static/map-assets/style.json '+%Y-%m-%d %H:%M' 2>/dev/null || echo 'style.json missing')"

# ── 1. Sidecar consistency ───────────────────────────────────────────
banner "1 — GTFS sidecar consistency"
# data/gtfs_motis/ is a hardlink farm over data/gtfs_routed/ plus its own
# platform-anchored stops.txt. rsync mirrors the routed feed by writing NEW
# inodes, so after a sync the sidecar's links still point at this machine's
# PREVIOUS feed — the sync can never refresh it, and that old feed stays on
# disk, held alive by those links. Only preprocess_gtfs_for_motis.py rebuilds
# the farm, which is why an inconsistent sidecar is repaired here instead of
# merely reported. Its inputs all arrive with the sync (quay_anchors.json,
# platform_ways.geojson, stop_identity.json), so the result matches what the
# data machine produced.
#
# The repair cannot mask a stale feed: it makes the sidecar agree with
# whatever data/gtfs_routed/ holds, and step 2 judges importing from that
# same feed's timestamp. A stale feed therefore yields "no import", and the
# synced index keeps serving.
CONSISTENT=1
if [ "$SKIP_CHECK" -eq 1 ]; then
	note "skipped (--skip-check)"
elif python3 scripts/routing/check_gtfs_motis_consistency.py; then
	:
else
	echo ""
	warn "sidecar out of step with data/gtfs_routed/ — rebuilding the hardlinks"
	run python3 scripts/routing/preprocess_gtfs_for_motis.py
	if [ "$DRY_RUN" -eq 0 ]; then
		if python3 scripts/routing/check_gtfs_motis_consistency.py; then
			note "sidecar repaired"
		else
			CONSISTENT=0
			echo ""
			warn "still inconsistent, so data/gtfs_routed/ is itself mixed."
			warn "The synced index is unaffected — this only blocks a local"
			warn "re-import. Re-sync the feed whole from the data machine:"
			warn "    ./scripts/fetch_build.sh --only routed"
		fi
	fi
fi

# ── 2. Index freshness ───────────────────────────────────────────────
banner "2 — MOTIS index"
NEED_IMPORT=0
# Compare against the importer's real inputs: shapes.txt stands in for the
# routed feed (pfaedle's last write) and the matrix for the transfer table.
# NOT the sidecar's stops.txt — step 1 may have just regenerated it, and its
# fresh mtime would then demand an import of a feed the synced index already
# describes.
for input in data/gtfs_routed/shapes.txt \
             motis/data/valhalla_footpath_matrix.csv; do
	if [ -f "$input" ] && [ "$input" -nt motis/data/tt.bin ]; then
		note "newer than the index: $input"
		NEED_IMPORT=1
	fi
done
if [ "$FORCE_IMPORT" -eq 1 ]; then
	note "--force-import given"
	NEED_IMPORT=1
fi
if [ "$NO_IMPORT" -eq 1 ] && [ "$NEED_IMPORT" -eq 1 ]; then
	warn "import needed but suppressed by --no-import"
	NEED_IMPORT=0
fi
if [ "$NEED_IMPORT" -eq 1 ] && [ "$CONSISTENT" -eq 0 ]; then
	echo "" >&2
	echo "refusing to import: data/gtfs_motis/ is not self-consistent (see above)." >&2
	echo "Re-sync the routed feed first, or pass --no-import to restart on the" >&2
	echo "index that is already here." >&2
	exit 1
fi
if [ "$NEED_IMPORT" -eq 0 ]; then
	note "index is current — no import (the synced one is the data machine's own)"
fi

# ── 3. Stop, import, start ───────────────────────────────────────────
# Stop MOTIS before importing as well as before restarting: the importer
# rewrites the same files the server has mapped.
banner "3 — Restart routing stack"
run docker compose -f motis/docker-compose.yml stop motis
run docker compose -f valhalla/docker-compose.yml stop valhalla

if [ "$NEED_IMPORT" -eq 1 ]; then
	banner "3a — MOTIS import (~10 min)"
	# The importer needs Valhalla for nothing, but the fork's server does —
	# so import first, bring the pair up afterwards in the right order.
	if [ "$(uname -s)" = "Linux" ]; then
		export KORA_UID="$(id -u)" KORA_GID="$(id -g)"
	fi
	run docker compose -f motis/docker-compose.yml --profile import run --rm motis-import
fi

run docker compose -f valhalla/docker-compose.yml up -d valhalla
if [ "$DRY_RUN" -eq 0 ]; then
	printf '   waiting for Valhalla on :8002 '
	VALHALLA_UP=0
	for _ in $(seq 1 60); do
		if curl -sf -X POST 'http://localhost:8002/route' \
			-H 'Content-Type: application/json' \
			-d '{"costing":"pedestrian","locations":[{"lat":47.378,"lon":8.540},{"lat":47.380,"lon":8.542}]}' \
			>/dev/null 2>&1; then
			VALHALLA_UP=1; break
		fi
		printf '.'; sleep 2
	done
	echo ""
	if [ "$VALHALLA_UP" -eq 0 ]; then
		warn "Valhalla did not answer — docker logs kora-valhalla --tail 50"
		warn "MOTIS will not serve without it, so stopping here."
		exit 1
	fi
	note "Valhalla up"
fi

run docker compose -f motis/docker-compose.yml up -d motis

# ── 4. Smoke test ────────────────────────────────────────────────────
banner "4 — Smoke test"
if [ "$DRY_RUN" -eq 1 ]; then
	note "would query MOTIS on :8080"
	echo ""
	note "(dry run — nothing was changed)"
	exit 0
fi
printf '   waiting for MOTIS on :8080 '
MOTIS_UP=0
for _ in $(seq 1 90); do
	if curl -sf 'http://localhost:8080/api/v1/plan?fromPlace=47.378,8.540&toPlace=47.424,8.508&arriveBy=false&numItineraries=1&directModes=WALK' \
		>/dev/null 2>&1; then
		MOTIS_UP=1; break
	fi
	printf '.'; sleep 2
done
echo ""
if [ "$MOTIS_UP" -eq 0 ]; then
	warn "MOTIS did not answer — docker logs kora-motis --tail 50"
	exit 1
fi
# A station-to-station query exercises what a walk-only smoke test cannot:
# the footpath matrix rows that make a quay boardable at all.
ITINS=$(curl -s 'http://localhost:8080/api/v1/plan?fromPlace=ch_Parentch:1:sloid:7000&toPlace=ch_Parentch:1:sloid:9000&arriveBy=false&numItineraries=3&useRoutedTransfers=true' \
	| python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("itineraries") or []))' 2>/dev/null || echo 0)
if [ "$ITINS" -eq 0 ]; then
	warn "MOTIS answers, but Bern → Chur returned no transit itinerary."
	warn "Check the footpath matrix and the import log before trusting results."
	exit 1
fi
note "MOTIS up — Bern → Chur returned $ITINS itineraries"

banner "Done"
note "The dev server picks up new map assets on a browser reload."
