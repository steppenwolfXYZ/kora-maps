#!/usr/bin/env bash
# Full transit layer rebuild pipeline.
# Run from the project root: ./scripts/rebuild_transit.sh
#
# Steps:
#   04  Extract OSM routes + urban landuse fractions  (~5–8 min)
#   05  Match GTFS, score frequencies, classify modes  (~3 min)
#   07  Build stop dots                                (~10 sec)
#   gen Generate MapLibre style JSON                  (~2 sec)
#   08  Build all tl_*.pmtiles                        (~1 min)
#
# Skip OSM extraction (steps 04) if routes.geojson is up-to-date:
#   ./scripts/rebuild_transit.sh --skip-osm

set -euo pipefail
cd "$(dirname "$0")/.."

SKIP_OSM=0
for arg in "$@"; do
  [[ "$arg" == "--skip-osm" ]] && SKIP_OSM=1
done

echo "══════════════════════════════════════════"
echo "  Transit Rebuild Pipeline"
echo "══════════════════════════════════════════"

if [[ $SKIP_OSM -eq 0 ]]; then
  echo ""
  echo "▶ Step 04 — Extract OSM routes + urban landuse"
  time python3 scripts/transit/04_extract_osm.py
else
  echo "(skipping OSM extraction — using existing routes.geojson)"
fi

echo ""
echo "▶ Step 05 — GTFS matching + frequency scoring"
time python3 scripts/transit/05_score_and_match.py

echo ""
echo "▶ Step 07 — Build stop dots"
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
