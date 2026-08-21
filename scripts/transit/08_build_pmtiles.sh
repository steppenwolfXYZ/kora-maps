#!/usr/bin/env bash
# Build all transit pmtiles from the current GeoJSON outputs.
# Outputs go to static/map-assets/tl_*.pmtiles as referenced by static/map-assets/style.json.
# Run from the project root.
#
# The tippecanoe builds are independent of each other and run concurrently,
# TIPPECANOE_JOBS at a time (env, default 2). Each job is memory-hungry on
# the big inputs, so size it to RAM: 2 is safe on a 16 GB laptop, 8 is
# comfortable on the data machine.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DATA="$ROOT/data/transit"
STATIC="$ROOT/static/map-assets"
LOGS="$DATA/logs"
mkdir -p "$STATIC" "$LOGS"

JOBS="${TIPPECANOE_JOBS:-2}"
[[ "$JOBS" =~ ^[0-9]+$ ]] && [[ "$JOBS" -ge 1 ]] || JOBS=2

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

# ── Job pool ──────────────────────────────────────────────────────────
# build NAME tippecanoe-args… — runs in the background, at most $JOBS at
# once, output to $LOGS/tippecanoe_NAME.log. Any failure aborts the
# script after the running jobs finish.
declare -a PIDS=() NAMES=()
FAILED=0

build() {
  local name="$1"; shift
  # Throttle by polling the job count: `wait -n` would be neater but needs
  # bash >= 4.3, and macOS ships 3.2.
  while (( $(jobs -rp | wc -l) >= JOBS )); do
    sleep 1
  done
  echo "  → $name"
  ( tippecanoe "$@" > "$LOGS/tippecanoe_$name.log" 2>&1 \
      && echo "  ✓ $name" \
      || { echo "  ✗ $name (see $LOGS/tippecanoe_$name.log)"; exit 1; } ) &
  PIDS+=($!); NAMES+=("$name")
}

echo ""
echo "=== Building pmtiles ($JOBS concurrent tippecanoe jobs) ==="

# tl_lines — maxzoom z16: line/stop-dot alignment only matters up to
# z16.99 because the close-zoom pill-arrow design (z17+) no longer
# overlays dots on lines. So the tile-tessellation drift that motivated
# native z18 tiles is only a problem below z17, and z16 native tiles are
# enough. Cuts the file from ~500 MB to ~130 MB.
build lines -o "$STATIC/tl_lines.pmtiles" --force \
  -z16 -Z4 -d18 \
  --layer transit_lines \
  --drop-densest-as-needed \
  --extend-zooms-if-still-dropping \
  "$DATA/transit_lines_extended.geojson"

# Stops — maxzoom z18: upscaling z14 tiles at z18+ views drifts point
# coords enough that the parent stop circle no longer aligns with the
# natively-tiled line and pill indicator. Native z18 tiles keep all three
# sources in agreement. Per-group minzooms: rail 5, tram 10, regional 9,
# bus 11.
build stops_rail -o "$STATIC/tl_stops_rail.pmtiles" --force \
  -z18 -Z5 -d18 --layer transit_stops \
  --drop-densest-as-needed \
  "$DATA/transit_stops_rail.geojson"

build stops_tram -o "$STATIC/tl_stops_tram.pmtiles" --force \
  -z18 -Z10 -d18 --layer transit_stops \
  --drop-densest-as-needed \
  "$DATA/transit_stops_tram.geojson"

build stops_regional -o "$STATIC/tl_stops_regional.pmtiles" --force \
  -z18 -Z9 -d18 --layer transit_stops \
  --drop-densest-as-needed \
  "$DATA/transit_stops_regional.geojson"

build stops_bus -o "$STATIC/tl_stops_bus.pmtiles" --force \
  -z18 -Z11 -d18 --layer transit_stops \
  --drop-densest-as-needed \
  "$DATA/transit_stops_bus.geojson"

# tl_stop_pills — maxzoom z18 so the high-zoom viewing range renders
# natively instead of upscaling z14 tiles 16×. Densely-sampled curved
# connectors hit a MapLibre line-tessellation artifact (visible wobble at
# z18+) when the native tile is upscaled — see pill-rendering concept
# § Connector curving. The remaining transit layers stay at -z14 because
# they are straight-segment polylines or points where upscaling is fine.
build stop_pills -o "$STATIC/tl_stop_pills.pmtiles" --force \
  -z18 -Z11 -d18 --layer transit_stop_pills \
  --drop-densest-as-needed \
  "$DATA/transit_stop_pills.geojson"

# tl_close_zoom — minzoom z15 so features exist a couple of zoom levels
# below the z17 activation point. maxzoom z18 like the other high-zoom
# transit bundles; overzoomed to z22 in the client, which needs the full
# vertex density — hence --no-line-simplification (pill arcs otherwise
# get faceted on the non-maxzoom tiles that MapLibre shows at z17.x).
build close_zoom -o "$STATIC/tl_close_zoom.pmtiles" --force \
  -z18 -Z15 -d18 --layer transit_close_zoom \
  --no-line-simplification \
  --drop-densest-as-needed \
  "$DATA/transit_close_zoom.geojson"

# Debug overlay pmtiles: gated on presence of the source geojson. Step 07's
# stops/debug_overlay.py either emits these files (when debug.debug_overlay
# is true) or unlinks any stale copies. See stops/debug_overlay.py for the
# full delete-checklist.
for dbg in platforms stops bars; do
  if [ -f "$DATA/transit_debug_$dbg.geojson" ]; then
    build "debug_$dbg" -o "$STATIC/tl_debug_$dbg.pmtiles" --force \
      -z14 -Z5 -d18 --layer "transit_debug_$dbg" \
      --drop-densest-as-needed \
      "$DATA/transit_debug_$dbg.geojson"
  fi
done

# Drain the pool; any job failure fails the step.
for pid in "${PIDS[@]}"; do
  wait "$pid" || FAILED=1
done
if [[ $FAILED -ne 0 ]]; then
  echo "tippecanoe failed — see $LOGS/tippecanoe_*.log" >&2
  exit 1
fi

echo ""
echo "=== Done! ==="
ls -lh "$STATIC"/tl_*.pmtiles
