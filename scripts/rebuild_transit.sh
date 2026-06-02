#!/usr/bin/env bash
# Full transit layer rebuild pipeline (pfaedle-based).
# Run from the project root: ./scripts/rebuild_transit.sh
#
# Steps:
#   1  Download GTFS                                      (~30 sec)
#   2  Download OSM (CH + LI + DE + FR + IT + AT)         (~12 GB; one-off)
#   3  Cut bbox slice from country PBFs                   (~2 min)
#   4  Preprocess GTFS (excluded agencies, foreign-terminus)  (~2 min)
#   5  Run pfaedle (Docker)                               (~5–15 min)
#   6  Emit transit_lines.geojson from pfaedle shapes     (~3 min)
#   7  Build stop dots & pills + regenerate style.json    (~10 sec)
#   8  Build all tl_*.pmtiles                             (~1 min)
#
# Use --start N to start from step N (default 3). Steps before N are skipped
# and their existing outputs reused. Steps cannot be skipped individually —
# each step's output is the next step's input.
#
# Examples:
#   ./scripts/rebuild_transit.sh                  # default: --start 3
#   ./scripts/rebuild_transit.sh --start 4        # bbox cut up-to-date, re-route only
#   ./scripts/rebuild_transit.sh --start 6        # iterate on emission + style + tiles
#   ./scripts/rebuild_transit.sh --start 2        # refresh OSM and everything after
#   ./scripts/rebuild_transit.sh --start 8        # rebuild pmtiles only

set -euo pipefail
cd "$(dirname "$0")/.."

START=3
while [[ $# -gt 0 ]]; do
  case "$1" in
    --start)     shift; START="$1" ;;
    --start=*)   START="${1#--start=}" ;;
    -h|--help)
      sed -n '2,26p' "$0"; exit 0 ;;
    *)
      echo "unknown arg: $1" >&2
      echo "usage: $0 [--start N]   (1 ≤ N ≤ 8, default 3)" >&2
      exit 2 ;;
  esac
  shift
done

if ! [[ "$START" =~ ^[1-8]$ ]]; then
  echo "--start must be between 1 and 8 (got '$START')" >&2
  exit 2
fi

echo "══════════════════════════════════════════"
echo "  Transit Rebuild Pipeline (pfaedle)"
echo "  Starting at step $START"
echo "══════════════════════════════════════════"

if [[ $START -le 1 ]]; then
  echo ""
  echo "▶ Step 1 — Download GTFS"
  time python3 scripts/transit/01_download_gtfs.py
fi

if [[ $START -le 2 ]]; then
  echo ""
  echo "▶ Step 2 — Download OSM (CH + LI + DE + FR + IT + AT)"
  time python3 scripts/transit/02_download_osm.py
fi

if [[ $START -le 3 ]]; then
  echo ""
  echo "▶ Step 3 — Cut bbox slice from country PBFs"
  time python3 scripts/transit/03_bbox_osm.py
fi

if [[ $START -le 4 ]]; then
  echo ""
  echo "▶ Step 4 — Preprocess GTFS"
  time python3 scripts/transit/04_preprocess_gtfs.py
fi

if [[ $START -le 5 ]]; then
  echo ""
  echo "▶ Step 5 — Run pfaedle"
  time python3 scripts/transit/05_run_pfaedle.py
fi

if [[ $START -le 6 ]]; then
  echo ""
  echo "▶ Step 6 — Emit transit_lines.geojson"
  time python3 scripts/transit/06_score_and_match.py
fi

if [[ $START -le 7 ]]; then
  echo ""
  echo "▶ Step 7 — Build stop dots & pills"
  time python3 scripts/transit/07_extract_stops.py

  echo ""
  echo "▶ Generate style.json"
  time python3 scripts/generate_style.py
fi

if [[ $START -le 8 ]]; then
  echo ""
  echo "▶ Step 8 — Build pmtiles"
  time bash scripts/transit/08_build_pmtiles.sh
fi

echo ""
echo "══════════════════════════════════════════"
echo "  Done. Reload the browser to see changes."
echo "══════════════════════════════════════════"
