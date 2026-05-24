#!/usr/bin/env python3
"""
Report all stop pill connectors longer than a given threshold (default 500m).
Usage: python3 scripts/check_long_connectors.py [min_km]
"""
import json
import math
import sys

MIN_KM = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5
PILLS_PATH = "data/transit/transit_stop_pills.geojson"


def haversine_km(lon1, lat1, lon2, lat2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


with open(PILLS_PATH) as f:
    data = json.load(f)

long_connectors = []
for feat in data["features"]:
    geom = feat["geometry"]
    props = feat.get("properties", {})
    if geom["type"] == "LineString":
        coords = geom["coordinates"]
        if len(coords) == 2:
            dist = haversine_km(coords[0][0], coords[0][1], coords[1][0], coords[1][1])
            if dist > MIN_KM:
                lines = json.loads(props.get("lines_json", "[]"))
                refs = list(dict.fromkeys(l.get("ref", "?") for l in lines))
                long_connectors.append({
                    "dist_km": round(dist, 2),
                    "name": props.get("stop_name", "?"),
                    "refs": refs,
                    "parent_station": props.get("parent_station", "?"),
                    "c0": coords[0],
                    "c1": coords[1],
                })

long_connectors.sort(key=lambda x: -x["dist_km"])
print(f"Total connectors > {MIN_KM}km: {len(long_connectors)}\n")
for c in long_connectors:
    ref_str = ",".join(c["refs"][:5])
    print(f"{c['dist_km']:5.2f} km | {c['name']:<32s} | {ref_str:<35s} | parent={c['parent_station']}")
