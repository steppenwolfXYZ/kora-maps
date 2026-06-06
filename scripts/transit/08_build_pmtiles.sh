#!/usr/bin/env bash
# Build all transit pmtiles from the current GeoJSON outputs.
# Outputs go to static/tl_*.pmtiles as referenced by static/style.json.
# Run from the project root.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DATA="$ROOT/data/transit"
STATIC="$ROOT/static"

echo "=== Building transit_lines → tl_lines.pmtiles ==="
tippecanoe -o "$STATIC/tl_lines.pmtiles" --force \
  -z14 -Z4 -d18 \
  --drop-densest-as-needed \
  --extend-zooms-if-still-dropping \
  "$DATA/transit_lines.geojson"

echo ""
echo "=== Splitting stops by mode group ==="

# Split transit_stops.geojson into per-group files
python3 - <<'PYEOF'
import json, sys
from pathlib import Path

data = json.loads(Path("data/transit/transit_stops.geojson").read_text())
groups = {
    "rail":     {"intercity", "train", "mountain"},
    "tram":     {"tram", "metro"},
    "regional": {"regional_bus", "ferry"},
    "bus":      {"bus"},
}

for grp, modes in groups.items():
    feats = [f for f in data["features"] if f["properties"].get("mode") in modes]
    out = Path(f"data/transit/transit_stops_{grp}.geojson")
    out.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    print(f"  {grp}: {len(feats):,} features → {out}")
PYEOF

echo ""
echo "=== Building tl_stops_rail.pmtiles (minzoom 5) ==="
tippecanoe -o "$STATIC/tl_stops_rail.pmtiles" --force \
  -z14 -Z5 -d18 --layer transit_stops \
  --drop-densest-as-needed \
  "$DATA/transit_stops_rail.geojson"

echo ""
echo "=== Building tl_stops_tram.pmtiles (minzoom 10) ==="
tippecanoe -o "$STATIC/tl_stops_tram.pmtiles" --force \
  -z14 -Z10 -d18 --layer transit_stops \
  --drop-densest-as-needed \
  "$DATA/transit_stops_tram.geojson"

echo ""
echo "=== Building tl_stops_regional.pmtiles (minzoom 9) ==="
tippecanoe -o "$STATIC/tl_stops_regional.pmtiles" --force \
  -z14 -Z9 -d18 --layer transit_stops \
  --drop-densest-as-needed \
  "$DATA/transit_stops_regional.geojson"

echo ""
echo "=== Building tl_stops_bus.pmtiles (minzoom 11) ==="
tippecanoe -o "$STATIC/tl_stops_bus.pmtiles" --force \
  -z14 -Z11 -d18 --layer transit_stops \
  --drop-densest-as-needed \
  "$DATA/transit_stops_bus.geojson"

echo ""
echo "=== Building tl_stop_pills.pmtiles ==="
tippecanoe -o "$STATIC/tl_stop_pills.pmtiles" --force \
  -z14 -Z11 -d18 --layer transit_stop_pills \
  --drop-densest-as-needed \
  "$DATA/transit_stop_pills.geojson"

echo ""
echo "=== Building tl_debug_platforms.pmtiles ==="
tippecanoe -o "$STATIC/tl_debug_platforms.pmtiles" --force \
  -z14 -Z5 -d18 --layer transit_debug_platforms \
  --drop-densest-as-needed \
  "$DATA/transit_debug_platforms.geojson"

echo ""
echo "=== Building tl_debug_stops.pmtiles ==="
tippecanoe -o "$STATIC/tl_debug_stops.pmtiles" --force \
  -z14 -Z5 -d18 --layer transit_debug_stops \
  --drop-densest-as-needed \
  "$DATA/transit_debug_stops.geojson"

echo ""
echo "=== Building tl_debug_bars.pmtiles ==="
tippecanoe -o "$STATIC/tl_debug_bars.pmtiles" --force \
  -z14 -Z5 -d18 --layer transit_debug_bars \
  --drop-densest-as-needed \
  "$DATA/transit_debug_bars.geojson"

echo ""
echo "=== Done! ==="
ls -lh "$STATIC"/tl_*.pmtiles
