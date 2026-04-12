#!/usr/bin/env python3
"""
Score transit stations and build transit_stations.geojson.

Approach:
  - Geometry source: OSM stop_areas (logical stations, one point per station)
  - Scoring: spatial join to scored transit lines (transit_lines.geojson)
    - For each stop_area, find all transit lines whose geometry passes within 300m
    - Take max freq_score + best mode rank from matching lines
  - Output: data/transit/transit_stations.geojson
    Properties: name, mode_rank, freq_score, radius_base (for MapLibre circle-radius)

Run after 05_score_and_match.py.
"""

import json
from math import radians, cos, sin, sqrt, atan2, floor
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[2]
OSM_STATIONS = ROOT / "data" / "osm" / "stations.geojson"
TRANSIT_LINES = ROOT / "data" / "transit" / "transit_lines.geojson"
OUT = ROOT / "data" / "transit" / "transit_stations.geojson"

# Mode rank — determines base circle size (higher = bigger circle)
MODE_RANK = {
    "intercity":        5,
    "train":            4,
    "tram":             3,
    "metro":            3,
    "longdistance_bus": 2,
    "ferry":            2,
    "bus":              1,
    "mountain":         1,
}

# Circle radius at zoom 14 (px)
# radius_base = MODE_BASE[best_mode] + freq_score * FREQ_SCALE
MODE_RADIUS_BASE = {
    5: 9.0,   # intercity
    4: 7.0,   # regional train
    3: 5.5,   # tram / metro
    2: 4.0,   # long-distance bus, ferry
    1: 3.0,   # bus, mountain
    0: 2.5,   # unknown
}
FREQ_SCALE = 3.0   # adds up to 3px at freq_score=1


def haversine_km(lon1, lat1, lon2, lat2) -> float:
    R = 6371
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi, dlam = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlam / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


# ── Spatial grid index ────────────────────────────────────────────────────────

CELL_DEG = 0.004   # ~400m grid cells

def grid_key(lon, lat):
    return (floor(lon / CELL_DEG), floor(lat / CELL_DEG))

def grid_keys_within(lon, lat, radius_km):
    """Return all grid cell keys within radius_km of (lon, lat)."""
    deg_lat = radius_km / 111.0
    deg_lon = radius_km / (111.0 * cos(radians(lat)))
    keys = set()
    lo_x = floor((lon - deg_lon) / CELL_DEG)
    hi_x = floor((lon + deg_lon) / CELL_DEG)
    lo_y = floor((lat - deg_lat) / CELL_DEG)
    hi_y = floor((lat + deg_lat) / CELL_DEG)
    for x in range(lo_x, hi_x + 1):
        for y in range(lo_y, hi_y + 1):
            keys.add((x, y))
    return keys


def build_line_grid(lines: list, sample_every: int = 4) -> dict:
    """Build a spatial grid of transit line points for fast proximity lookup.
    Returns: grid_cell → list of (lon, lat, mode, freq_score) tuples."""
    grid: dict = defaultdict(list)
    for feat in lines:
        props = feat["properties"]
        if not props.get("gtfs_matched"):
            continue
        mode = props.get("mode", "bus")
        freq = props.get("freq_score", 0.0)
        coords = feat["geometry"]["coordinates"]
        for i, (lon, lat) in enumerate(coords):
            if i % sample_every != 0:
                continue
            key = grid_key(lon, lat)
            grid[key].append((lon, lat, mode, freq))
    return grid


def score_station(lon, lat, grid, radius_km=0.3):
    """Find best (mode_rank, freq_score) from transit lines near this station."""
    best_rank = 0
    best_freq = 0.0

    for key in grid_keys_within(lon, lat, radius_km):
        for (llon, llat, mode, freq) in grid.get(key, []):
            dist = haversine_km(lon, lat, llon, llat)
            if dist <= radius_km:
                rank = MODE_RANK.get(mode, 0)
                if rank > best_rank or (rank == best_rank and freq > best_freq):
                    best_rank = rank
                    best_freq = max(best_freq, freq)

    return best_rank, best_freq


def main():
    print("Loading transit lines...")
    lines_data = json.loads(TRANSIT_LINES.read_text())
    lines = lines_data["features"]
    print(f"  {len(lines):,} lines")

    print("Building spatial grid of line points...")
    grid = build_line_grid(lines)
    total_pts = sum(len(v) for v in grid.values())
    print(f"  {total_pts:,} indexed points in {len(grid):,} grid cells")

    print("Loading OSM stations...")
    stations_data = json.loads(OSM_STATIONS.read_text())
    all_stations = stations_data["features"]

    # Use only stop_areas + parent stations (not raw platforms/stop_positions)
    stations = [
        f for f in all_stations
        if f["properties"].get("public_transport") in ("stop_area",)
        or f["properties"].get("railway") in ("station", "halt")
    ]
    print(f"  Using {len(stations):,} of {len(all_stations):,} station features (stop_areas + stations)")

    print("Scoring stations...")
    features = []
    rank_counts = defaultdict(int)

    for feat in stations:
        geom = feat["geometry"]
        if geom["type"] != "Point":
            continue
        lon, lat = geom["coordinates"]
        name = feat["properties"].get("name", "")

        rank, freq = score_station(lon, lat, grid)
        rank_counts[rank] += 1

        # Skip if no transit service found nearby (likely an OSM error or very remote stop)
        if rank == 0 and freq == 0.0:
            continue

        base = MODE_RADIUS_BASE.get(rank, MODE_RADIUS_BASE[0])
        radius_base = round(base + freq * FREQ_SCALE, 2)

        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "name": name,
                "mode_rank": rank,
                "freq_score": round(freq, 3),
                "radius_base": radius_base,
                "osm_id": feat["properties"].get("osm_id", ""),
                "uic_ref": feat["properties"].get("uic_ref", ""),
            }
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}))

    print(f"\nStations output: {len(features):,} features → {OUT}")
    print("\nMode rank distribution:")
    for rank in sorted(rank_counts.keys(), reverse=True):
        label = {5:"intercity",4:"train",3:"tram/metro",2:"ld-bus/ferry",1:"bus/mountain",0:"unmatched"}
        print(f"  rank {rank} ({label.get(rank,'?'):<16}): {rank_counts[rank]:>5,}")


if __name__ == "__main__":
    main()
