#!/usr/bin/env bash
# Full transit layer rebuild pipeline (pfaedle-based).
# Run from the project root: ./scripts/rebuild_transit.sh
#
# Steps:
#   04a  Cut Switzerland + buffer from the Geofabrik PBFs           (~30 sec)
#   04b  Filter GTFS (excluded agencies, foreign-terminus trips)    (~2 min)
#   04c  Run pfaedle (Docker) — routes trips over OSM               (~5–15 min)
#   05   Emit transit_lines.geojson from pfaedle shapes             (~3 min)
#   07   Build stop dots & pills                                    (~10 sec)
#   gen  Generate MapLibre style JSON                               (~2 sec)
#   08   Build all tl_*.pmtiles                                     (~1 min)
#
# Skip the heavy OSM bbox extract (step 04a) when the bbox PBF is fresh:
#   ./scripts/rebuild_transit.sh --skip-osm
#
# Skip pfaedle (when shapes.txt is up-to-date):
#   ./scripts/rebuild_transit.sh --skip-pfaedle
#
# Skip OSM + pfaedle + GTFS preprocessing (iterate only on emission/render):
#   ./scripts/rebuild_transit.sh --skip-routing

set -euo pipefail
cd "$(dirname "$0")/.."

SKIP_OSM=0
SKIP_GTFS=0
SKIP_PFAEDLE=0
for arg in "$@"; do
  case "$arg" in
    --skip-osm)     SKIP_OSM=1 ;;
    --skip-gtfs)    SKIP_GTFS=1 ;;
    --skip-pfaedle) SKIP_PFAEDLE=1 ;;
    --skip-routing) SKIP_OSM=1; SKIP_GTFS=1; SKIP_PFAEDLE=1 ;;
  esac
done

echo "══════════════════════════════════════════"
echo "  Transit Rebuild Pipeline (pfaedle)"
echo "══════════════════════════════════════════"

if [[ $SKIP_OSM -eq 0 ]]; then
  echo ""
  echo "▶ Step 04a — Cut Switzerland+buffer OSM PBF"
  time python3 scripts/transit/04a_bbox_osm.py
else
  echo "(skipping 04a — using existing data/osm/ch_pfaedle.osm.pbf)"
fi

if [[ $SKIP_GTFS -eq 0 ]]; then
  echo ""
  echo "▶ Step 04b — Preprocess GTFS (agency + foreign-terminus filters)"
  time python3 scripts/transit/04b_preprocess_gtfs.py
else
  echo "(skipping 04b — using existing data/gtfs_filtered/)"
fi

if [[ $SKIP_PFAEDLE -eq 0 ]]; then
  echo ""
  echo "▶ Step 04c — Run pfaedle"
  time python3 scripts/transit/04c_run_pfaedle.py
else
  echo "(skipping 04c — using existing data/gtfs_routed/)"
fi

echo ""
echo "▶ Step 05 — Emit transit_lines.geojson"
time python3 scripts/transit/05_score_and_match.py

echo ""
echo "▶ Step 07 — Build stop dots & pills"
time python3 scripts/transit/07_extract_stops.py

echo ""
echo "▶ Generate style.json"
time python3 scripts/generate_style.py

echo ""
echo "▶ Step 08 — Build pmtiles"
time bash scripts/transit/08_build_pmtiles.sh

echo ""
echo "══════════════════════════════════════════"
echo "  Done. Reload the browser to see changes."
echo "══════════════════════════════════════════"
