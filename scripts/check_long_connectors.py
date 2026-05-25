#!/usr/bin/env python3
"""
Report stop pill connectors exceeding mode-specific thresholds.
  trains: > 300m   all others: > 150m

Usage:
  python3 scripts/check_long_connectors.py           # summary counts per mode
  python3 scripts/check_long_connectors.py --full    # full list for all modes
  python3 scripts/check_long_connectors.py train     # full list for one mode
"""
import json
import math
import sys

PILLS_PATH = "data/transit/transit_stop_pills.geojson"
TRAIN_THRESHOLD_KM = 0.3
DEFAULT_THRESHOLD_KM = 0.15

args = sys.argv[1:]
full = "--full" in args
mode_filter = next((a for a in args if a != "--full"), None)


def haversine_km(lon1, lat1, lon2, lat2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


with open(PILLS_PATH) as f:
    data = json.load(f)

by_mode: dict[str, list] = {}
for feat in data["features"]:
    geom = feat["geometry"]
    props = feat.get("properties", {})
    if geom["type"] != "LineString":
        continue
    coords = geom["coordinates"]
    if len(coords) != 2:
        continue
    mode = props.get("mode", "unknown")
    threshold = TRAIN_THRESHOLD_KM if mode == "train" else DEFAULT_THRESHOLD_KM
    dist = haversine_km(coords[0][0], coords[0][1], coords[1][0], coords[1][1])
    if dist <= threshold:
        continue
    lines = json.loads(props.get("lines_json", "[]"))
    refs = list(dict.fromkeys(l.get("ref", "?") for l in lines))
    entry = {
        "dist_km": round(dist, 2),
        "name": props.get("stop_name", "?"),
        "refs": refs,
        "parent_station": props.get("parent_station", "?"),
        "mode": mode,
    }
    by_mode.setdefault(mode, []).append(entry)

for mode in by_mode:
    by_mode[mode].sort(key=lambda x: -x["dist_km"])

total = sum(len(v) for v in by_mode.values())
print(f"Total connectors over threshold: {total}\n")

modes_to_print = [mode_filter] if mode_filter and mode_filter in by_mode else sorted(by_mode)

for mode in modes_to_print:
    entries = by_mode.get(mode, [])
    threshold = TRAIN_THRESHOLD_KM if mode == "train" else DEFAULT_THRESHOLD_KM
    if full or mode_filter:
        print(f"── {mode} (>{threshold*1000:.0f}m): {len(entries)} ──")
        for c in entries:
            ref_str = ",".join(c["refs"][:5])
            print(f"  {c['dist_km']:5.2f} km | {c['name']:<32s} | {ref_str:<35s} | parent={c['parent_station']}")
        print()
    else:
        print(f"  {mode:<12s} (>{threshold*1000:.0f}m): {len(entries)}")
