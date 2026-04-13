#!/usr/bin/env python3
"""
Build transit_stops.geojson: stop dots for all visible lines.

Rules:
  - Every stop of every matched line gets a dot, visible from the same
    zoom level the line itself appears.
  - Rail (train): stops clustered within 300m → one dot per
    physical station. Larger circle. Visible from zoom 5.
  - All other modes: one dot per stop, snapped to the line geometry.
    Visible from the mode's minzoom.
  - Each dot carries: color, mode, mode_group (for layer filtering).

Output: data/transit/transit_stops.geojson
"""

import json
from math import radians, cos, sin, sqrt, atan2, floor
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[2]
LINES     = ROOT / "data" / "transit" / "transit_lines.geojson"
LINE_STOPS = ROOT / "data" / "transit" / "line_stops.json"
OUT       = ROOT / "data" / "transit" / "transit_stops.geojson"

RAIL_MODES = {"train"}

# Cluster radius for rail station deduplication (degrees, ~300m at CH latitudes)
CLUSTER_DEG = 0.003

# Per-mode minzoom — tells tippecanoe at which zoom to first include the feature.
# Must match the style layer minzooms exactly.
MODE_MINZOOM = {
    "train":        5,
    "tram":        10,
    "metro":        9,
    "regional_bus": 9,
    "ferry":        9,
    "bus":         11,
    "mountain":    11,
}


def haversine_km(lon1, lat1, lon2, lat2) -> float:
    R = 6371
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi, dlam = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlam / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def snap_to_line(px: float, py: float, coords: list) -> tuple:
    """Return the closest point on a polyline to (px, py)."""
    best_dist_sq = float("inf")
    best = (px, py)
    for i in range(len(coords) - 1):
        ax, ay = coords[i]
        bx, by = coords[i + 1]
        dx, dy = bx - ax, by - ay
        len_sq = dx * dx + dy * dy
        if len_sq == 0:
            cx, cy = ax, ay
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
            cx, cy = ax + t * dx, ay + t * dy
        d = (px - cx) ** 2 + (py - cy) ** 2
        if d < best_dist_sq:
            best_dist_sq = d
            best = (cx, cy)
    return best


def cluster_rail_stops(rail_stops: list) -> list:
    """
    Cluster (lon, lat, color, mode) tuples within CLUSTER_DEG into one point.
    Returns list of (lon, lat, color, mode) cluster centroids.
    """
    grid: dict = defaultdict(list)
    for lon, lat, color, mode in rail_stops:
        key = (int(lon / CLUSTER_DEG), int(lat / CLUSTER_DEG))
        grid[key].append((lon, lat, color, mode))

    # For each grid cell, also check 8 neighbours to merge across cell boundaries
    visited = set()
    clusters = []

    for key, pts in grid.items():
        for pt in pts:
            if id(pt) in visited:
                continue
            # Collect all points in this and adjacent cells within threshold
            cx0, cy0 = pt[0], pt[1]
            group = []
            kx, ky = key
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for npt in grid.get((kx + dx, ky + dy), []):
                        dist = haversine_km(cx0, cy0, npt[0], npt[1])
                        if dist < 0.3:   # 300m
                            group.append(npt)
                            visited.add(id(npt))

            if not group:
                group = [pt]
                visited.add(id(pt))

            lon = sum(p[0] for p in group) / len(group)
            lat = sum(p[1] for p in group) / len(group)
            best = group[0]
            clusters.append((lon, lat, best[2], best[3]))

    return clusters


def main():
    print("Loading lines...")
    lines_data = json.loads(LINES.read_text())
    line_lookup = {}
    # Collect straight-line mountain features that carry embedded gtfs_stops
    gtfs_stop_features = []
    for feat in lines_data["features"]:
        p = feat["properties"]
        oid = str(p.get("osm_id", ""))
        if oid:
            line_lookup[oid] = {
                "color":  p["color"],
                "mode":   p["mode"],
                "coords": feat["geometry"]["coordinates"],
            }
        if p.get("gtfs_stops"):
            gtfs_stop_features.append(feat)
    print(f"  {len(line_lookup):,} lines, {len(gtfs_stop_features):,} with embedded gtfs_stops")

    print("Loading stop coordinates...")
    line_stops = json.loads(LINE_STOPS.read_text())
    print(f"  {len(line_stops):,} lines with stops")

    print("Building stop dots...")

    rail_stops_raw = []   # collect all rail stops for deduplication
    other_features = []   # non-rail stop dots (already final)

    # First: render stops for straight-line mountain features using embedded gtfs_stops.
    # These have no osm_id so would never be matched by the line_stops loop below.
    for feat in gtfs_stop_features:
        p     = feat["properties"]
        color = p["color"]
        mode  = p["mode"]
        coords = feat["geometry"]["coordinates"]   # straight-line pts = stop pts
        minzoom = MODE_MINZOOM.get(mode, 11)
        for lon, lat in p["gtfs_stops"]:
            slon, slat = snap_to_line(lon, lat, coords)
            other_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": minzoom},
                "geometry": {"type": "Point", "coordinates": [slon, slat]},
                "properties": {"color": color, "mode": mode},
            })

    for osm_id, stop_coords in line_stops.items():
        line = line_lookup.get(osm_id)
        if not line:
            continue

        color  = line["color"]
        mode   = line["mode"]
        coords = line["coords"]

        minzoom = MODE_MINZOOM.get(mode, 11)

        # Flatten MultiLineString [[...], [...]] → flat list of [lon, lat]
        geom_type = None
        if coords and isinstance(coords[0][0], list):
            # MultiLineString: coords is list of segments
            flat_coords = [pt for seg in coords for pt in seg]
            geom_type = "multi"
        else:
            flat_coords = coords
            geom_type = "single"

        if mode in RAIL_MODES:
            # Collect raw positions — will be clustered
            for lon, lat in stop_coords:
                rail_stops_raw.append((lon, lat, color, mode))
        elif mode == "ferry":
            # Ferry pier positions come from GTFS stops collected in 05_score_and_match.py.
            # Do NOT snap to line: each route relation gets all piers in its bbox, including
            # piers not on that specific sub-route. Snapping those to the sailing track puts
            # them mid-lake. GTFS pier coordinates are already at the correct shore position.
            for lon, lat in stop_coords:
                other_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": minzoom},
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {"color": color, "mode": mode},
                })
        else:
            # Snap each stop to the line, emit immediately
            for lon, lat in stop_coords:
                slon, slat = snap_to_line(lon, lat, flat_coords)
                other_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": minzoom},  # TOP LEVEL — tippecanoe reads this
                    "geometry": {"type": "Point", "coordinates": [slon, slat]},
                    "properties": {"color": color, "mode": mode},
                })

    print(f"  {len(rail_stops_raw):,} raw rail stop positions → clustering...")
    rail_clusters = cluster_rail_stops(rail_stops_raw)
    print(f"  → {len(rail_clusters):,} rail station clusters")

    rail_features = []
    for lon, lat, color, mode in rail_clusters:
        rail_features.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": 5},  # TOP LEVEL
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"color": color, "mode": mode},
        })

    features = rail_features + other_features
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}))

    mode_counts: dict = defaultdict(int)
    for f in features:
        mode_counts[f["properties"]["mode"]] += 1

    print(f"\n{len(features):,} stop dots → {OUT}")
    for m, c in sorted(mode_counts.items(), key=lambda x: -x[1]):
        print(f"  {m:<20} {c:>6,}")


if __name__ == "__main__":
    main()
