#!/usr/bin/env python3
"""
Build transit stop GeoJSON files:

  transit_stops.geojson      — Point features (circle dots, low-zoom)
  transit_stop_pills.geojson — LineString features (pill/capsule shapes, high-zoom)

Stop dot rules:
  - Every stop of every matched line gets a dot, visible from the same
    zoom level the line itself appears.
  - Rail (train): stops clustered within 300m → one dot per physical station.
  - All other modes: one dot per stop, snapped to the line geometry.
  - Every dot carries: color, mode, width_base (for data-driven circle radius).

Pill rules:
  - Pills appear when a cluster has ≥2 distinct OSM line IDs (osm_id).
  - Pill-appear zoom is determined by line count and dominant mode.
  - Ferry and mountain modes: no pills.
  - Pill geometry is derived from dot positions using a nearest-neighbor path:
      → Build a greedy nearest-neighbor path through ALL dot positions
        in the cluster. This ensures every dot is at a vertex of the pill.
      → If the path has a large gap between two groups (> gap threshold),
        split there and emit two pills + a thin connector.
      → Pills prefer cross-track orientation naturally: for parallel-track
        stops the NN path connects the nearby dots directly.
  - Cross-mode clustering: tram + bus at same location → one pill in tram color.
  - Color = dominant line at stop (by mode hierarchy, then width_base).
  - Width encoded as width_base → style applies ×2 multiplier.
"""

import csv
import json
from math import radians, cos, sin, sqrt, atan2, degrees, floor
from pathlib import Path
from collections import defaultdict

ROOT       = Path(__file__).resolve().parents[2]
LINES      = ROOT / "data" / "transit" / "transit_lines.geojson"
LINE_STOPS = ROOT / "data" / "transit" / "line_stops.json"
GTFS_STOPS = ROOT / "data" / "gtfs" / "stops.txt"
OUT_DOTS   = ROOT / "data" / "transit" / "transit_stops.geojson"
OUT_PILLS  = ROOT / "data" / "transit" / "transit_stop_pills.geojson"

RAIL_MODES = {"train"}
# Modes that get pills; ferry and mountain are excluded
PILL_MODES = {"train", "tram", "metro", "bus", "regional_bus"}

# Cluster radius for rail station dot deduplication (degrees ≈ 300m at CH lat)
CLUSTER_DEG = 0.003

# Hierarchy for dominant-line selection at mixed-mode clusters (lower = higher priority)
MODE_RANK = {
    "train":        0,
    "metro":        1,
    "tram":         2,
    "bus":          3,
    "mountain":     4,
    "ferry":        5,
    "regional_bus": 6,
}

# Per-mode minzoom for stop dots (must match style layer minzooms)
MODE_MINZOOM = {
    "train":        5,
    "tram":        10,
    "metro":        9,
    "regional_bus": 9,
    "ferry":        9,
    "bus":         11,
    "mountain":    11,
}

# Spatial clustering radius for pill grouping
PILL_CLUSTER_RAIL_KM    = 0.300   # rail: 300 m (same as dot deduplication)
PILL_CLUSTER_NONRAIL_KM = 0.050   # all other modes combined: 50 m

# When a nearest-neighbor path segment exceeds (max_wb × this / 1000) km,
# the cluster is split into two pills + a connector at that gap.
# Tune this to separate distinct platform groups while keeping curved stops
# in a single bent pill.
PILL_GAP_SCALE = 12   # metres per unit of width_base


# =============================================================================
# GTFS stop metadata
# =============================================================================

def load_stop_meta() -> dict:
    """Return {stop_id: {"name": stop_name, "parent": parent_station}}."""
    meta = {}
    if not GTFS_STOPS.exists():
        return meta
    with open(GTFS_STOPS, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sid = row["stop_id"]
            entry = {"name": row.get("stop_name", ""), "parent": row.get("parent_station", "")}
            meta[sid] = entry
            base = sid.split(":")[0]
            if base not in meta:
                meta[base] = entry
    return meta


# =============================================================================
# Geometry helpers
# =============================================================================

def haversine_km(lon1, lat1, lon2, lat2) -> float:
    R = 6371.0
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlam = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlam / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def snap_to_line(px, py, coords):
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


def flatten_coords(coords):
    """Flatten MultiLineString [[...], [...]] or return LineString coords as-is."""
    if coords and isinstance(coords[0][0], list):
        return [pt for seg in coords for pt in seg]
    return coords


# =============================================================================
# Pill geometry — nearest-neighbor path through dot positions
# =============================================================================

def nearest_neighbor_path(positions):
    """
    Build a greedy nearest-neighbor path visiting every position exactly once.
    Starts from the position furthest from the centroid (an edge of the cluster).
    Returns the ordered list of positions.
    """
    n = len(positions)
    if n == 1:
        return list(positions)

    cx = sum(p[0] for p in positions) / n
    cy = sum(p[1] for p in positions) / n
    start = max(range(n),
                key=lambda i: haversine_km(positions[i][0], positions[i][1], cx, cy))

    visited = [False] * n
    path = [positions[start]]
    visited[start] = True

    for _ in range(n - 1):
        last = path[-1]
        best_d = float("inf")
        best_j = -1
        for j in range(n):
            if not visited[j]:
                d = haversine_km(last[0], last[1], positions[j][0], positions[j][1])
                if d < best_d:
                    best_d = d
                    best_j = j
        path.append(positions[best_j])
        visited[best_j] = True

    return path


# =============================================================================
# Pill logic
# =============================================================================

def count_unique_lines(cluster_stops):
    """
    Count distinct OSM line IDs in a cluster.
    Each direction of a tram/bus line has its own osm_id, so both directions
    of a bidirectional line count as 2 — correctly triggering a pill.
    """
    return len(set(s.get("osm_id", str(id(s))) for s in cluster_stops))


def pill_minzoom(mode, stop_count):
    """
    Return the zoom level at which pills appear for a stop cluster,
    or None if the cluster should not get a pill (single line).
    """
    if mode == "train":
        if stop_count >= 5:
            return 11
        if stop_count >= 2:
            return 13
        return None
    else:
        if stop_count >= 10:
            return 12
        if stop_count >= 5:
            return 13
        if stop_count >= 2:
            return 14
        return None


def color_luminance(hex_color: str) -> float:
    """Perceived luminance of a hex color (lower = darker)."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return 1.0
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def dominant_line(stops_in_cluster):
    """
    Return (color, mode, max_width_base, dominant_stop).
    - Mode: highest-priority type present (MODE_RANK; lower = higher priority; strict).
    - Color: darkest (lowest luminance) among stops of that type.
    - width_base: max across ALL stops, regardless of type.
    """
    best_rank = min(MODE_RANK.get(s["mode"], 99) for s in stops_in_cluster)
    dom_stops = [s for s in stops_in_cluster if MODE_RANK.get(s["mode"], 99) == best_rank]

    best_lum   = 2.0
    best_color = "#888888"
    best_stop  = dom_stops[0]
    for s in dom_stops:
        lum = color_luminance(s["color"])
        if lum < best_lum:
            best_lum   = lum
            best_color = s["color"]
            best_stop  = s

    max_wb = max(s["width_base"] for s in stops_in_cluster)
    return best_color, best_stop["mode"], max_wb, best_stop


def make_pill_features(cluster_stops, minzoom):
    """
    Build pill (and optional connector) GeoJSON features for a stop cluster.

    Algorithm:
    1. Build a nearest-neighbor path through ALL dot positions — every dot
       ends up at a vertex of the pill, so no dot is left standalone.
    2. Find the longest segment in the path (the biggest positional gap).
    3. If the gap is small (< max_wb × PILL_GAP_SCALE metres): emit as a
       single multi-point LineString. Round caps create a bent/curved capsule.
    4. If the gap is large (two distinct platform groups): split at the gap,
       emit two pills + a thin connector between the nearest endpoints.
    """
    color, mode, max_wb, dom_stop = dominant_line(cluster_stops)
    positions = list({(s["lon"], s["lat"]) for s in cluster_stops})  # deduplicate
    n = len(positions)

    if n < 2:
        return []

    path = nearest_neighbor_path(positions)

    gap_threshold_km = max_wb * PILL_GAP_SCALE / 1000.0

    stop_props = {
        "color":          color,
        "mode":           mode,
        "width_base":     max_wb,
        "stop_count":     len(cluster_stops),
        "stop_id":        dom_stop.get("stop_id", ""),
        "stop_name":      dom_stop.get("stop_name", ""),
        "parent_station": dom_stop.get("parent_station", ""),
    }

    def make_feat(coords, feature_type):
        return {
            "type": "Feature",
            "tippecanoe": {"minzoom": minzoom},
            "geometry": {"type": "LineString", "coordinates": [list(p) for p in coords]},
            "properties": {**stop_props, "feature_type": feature_type},
        }

    # Find all gaps above threshold — each is a split point between groups
    split_indices = [
        k for k in range(len(path) - 1)
        if haversine_km(path[k][0], path[k][1], path[k + 1][0], path[k + 1][1]) > gap_threshold_km
    ]

    if not split_indices:
        return [make_feat(path, "pill")]

    # Split path at every large gap → N groups
    groups = []
    prev = 0
    for idx in split_indices:
        groups.append(path[prev:idx + 1])
        prev = idx + 1
    groups.append(path[prev:])

    # Pill for each group with ≥2 positions; single-point groups rely on connector round caps
    feats = []
    for grp in groups:
        if len(grp) >= 2:
            feats.append(make_feat(grp, "pill"))

    # MST connectors (Kruskal's) — produces tree topology so branches are shorter than
    # a forced chain when groups fan out from a hub rather than lying in a sequence.
    n_g = len(groups)
    mst_edges = []   # (dist, ca, cb) for all candidate edges, sorted
    for i in range(n_g):
        for j in range(i + 1, n_g):
            best_d = float("inf")
            ca, cb = groups[i][0], groups[j][0]
            for p1 in groups[i]:
                for p2 in groups[j]:
                    d = haversine_km(p1[0], p1[1], p2[0], p2[1])
                    if d < best_d:
                        best_d, ca, cb = d, p1, p2
            mst_edges.append((best_d, ca, cb, i, j))
    mst_edges.sort()

    parent = list(range(n_g))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for best_d, ca, cb, i, j in mst_edges:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj
            feats.append(make_feat([ca, cb], "connector"))

    return feats


# =============================================================================
# Clustering
# =============================================================================

def cluster_rail_stops(rail_stops: list) -> list:
    """
    Cluster (lon, lat, color, mode, width_base) tuples within CLUSTER_DEG.
    Returns list of (lon, lat, color, mode, max_width_base) cluster centroids.
    """
    grid: dict = defaultdict(list)
    for pt in rail_stops:
        lon, lat = pt[0], pt[1]
        key = (int(lon / CLUSTER_DEG), int(lat / CLUSTER_DEG))
        grid[key].append(pt)

    visited = set()
    clusters = []

    for key, pts in grid.items():
        for pt in pts:
            if id(pt) in visited:
                continue
            cx0, cy0 = pt[0], pt[1]
            group = []
            kx, ky = key
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for npt in grid.get((kx + dx, ky + dy), []):
                        if haversine_km(cx0, cy0, npt[0], npt[1]) < 0.3:
                            group.append(npt)
                            visited.add(id(npt))

            if not group:
                group = [pt]
                visited.add(id(pt))

            lon  = sum(p[0] for p in group) / len(group)
            lat  = sum(p[1] for p in group) / len(group)
            best = group[0]
            max_wb = max(p[4] for p in group)
            clusters.append((lon, lat, best[2], best[3], max_wb))

    return clusters


def cluster_stops_for_pills(raw_stops, radius_km):
    """
    Spatially cluster raw stop dicts by their lon/lat within radius_km.
    Returns list of clusters; each cluster is a list of stop dicts.
    """
    cluster_deg = radius_km / 111.0
    grid = defaultdict(list)
    for stop in raw_stops:
        key = (floor(stop["lon"] / cluster_deg), floor(stop["lat"] / cluster_deg))
        grid[key].append(stop)

    visited = set()
    clusters = []

    for key, stops_in_cell in grid.items():
        for stop in stops_in_cell:
            sid = id(stop)
            if sid in visited:
                continue
            cx0, cy0 = stop["lon"], stop["lat"]
            group = []
            kx, ky = key
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for ns in grid.get((kx + dx, ky + dy), []):
                        if haversine_km(cx0, cy0, ns["lon"], ns["lat"]) < radius_km:
                            group.append(ns)
                            visited.add(id(ns))

            if not group:
                group = [stop]
                visited.add(sid)

            clusters.append(group)

    return clusters


def merge_clusters_by_parent_station(clusters):
    """
    Merge spatially separate clusters that share the same parent_station into
    one super-cluster so make_pill_features can connect them with pills and connectors.
    Clusters with no parent_station are left as-is.
    """
    by_parent = defaultdict(list)
    no_parent = []
    for cluster in clusters:
        parents = [s.get("parent_station", "") for s in cluster if s.get("parent_station", "")]
        if parents:
            dominant = max(set(parents), key=parents.count)
            by_parent[dominant].extend(cluster)
        else:
            no_parent.append(cluster)
    return list(by_parent.values()) + no_parent


# =============================================================================
# Main
# =============================================================================

def main():
    print("Loading lines...")
    lines_data = json.loads(LINES.read_text())
    line_lookup = {}
    gtfs_stop_features = []
    for feat in lines_data["features"]:
        p   = feat["properties"]
        oid = str(p.get("osm_id", ""))
        if oid:
            line_lookup[oid] = {
                "color":      p["color"],
                "mode":       p["mode"],
                "width_base": p.get("width_base", 3.0),
                "coords":     feat["geometry"]["coordinates"],
            }
        if p.get("gtfs_stops"):
            gtfs_stop_features.append(feat)
    print(f"  {len(line_lookup):,} lines, {len(gtfs_stop_features):,} with embedded gtfs_stops")

    print("Loading stop coordinates and metadata...")
    line_stops = json.loads(LINE_STOPS.read_text())
    stop_meta  = load_stop_meta()
    print(f"  {len(line_stops):,} lines with stops, {len(stop_meta):,} GTFS stop entries")

    print("Building stop dots and pill candidates...")

    rail_pill_raw     = []   # dicts for rail pill clustering (also used for dots)
    all_nonrail_pills = []   # ALL non-rail pill modes combined (tram+bus+metro+regional_bus)
    other_features    = []   # dot features for non-rail, ferry, mountain

    # --- Mountain / straight-line features with embedded gtfs_stops ---
    for feat in gtfs_stop_features:
        p       = feat["properties"]
        color   = p["color"]
        mode    = p["mode"]
        wb      = p.get("width_base", 3.0)
        coords  = feat["geometry"]["coordinates"]
        minzoom = MODE_MINZOOM.get(mode, 11)
        for lon, lat in p["gtfs_stops"]:
            slon, slat = snap_to_line(lon, lat, coords)
            other_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": minzoom},
                "geometry": {"type": "Point", "coordinates": [slon, slat]},
                "properties": {"color": color, "mode": mode, "width_base": wb},
            })
        # Mountain/ferry via gtfs_stops: no pills

    # --- Per-line stops ---
    for osm_id, stop_coords in line_stops.items():
        line = line_lookup.get(osm_id)
        if not line:
            continue

        color      = line["color"]
        mode       = line["mode"]
        width_base = line["width_base"]
        coords     = line["coords"]
        minzoom    = MODE_MINZOOM.get(mode, 11)
        flat       = flatten_coords(coords)

        if mode in RAIL_MODES:
            for entry in stop_coords:
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
                stop_name  = meta.get("name", "")
                parent_sta = meta.get("parent", "")
                slon, slat = snap_to_line(lon, lat, flat)
                rail_pill_raw.append({
                    "lon":            slon,
                    "lat":            slat,
                    "osm_id":         osm_id,
                    "mode":           mode,
                    "color":          color,
                    "width_base":     width_base,
                    "stop_id":        sid,
                    "stop_name":      stop_name,
                    "parent_station": parent_sta,
                })

        elif mode == "ferry":
            for entry in stop_coords:
                lon, lat = entry[0], entry[1]
                sid      = entry[2] if len(entry) > 2 else ""
                meta     = stop_meta.get(sid, {})
                other_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": minzoom},
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {
                        "color":          color,
                        "mode":           mode,
                        "width_base":     width_base,
                        "stop_id":        sid,
                        "stop_name":      meta.get("name", ""),
                        "parent_station": meta.get("parent", ""),
                    },
                })

        elif mode in PILL_MODES:
            for entry in stop_coords:
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
                stop_name  = meta.get("name", "")
                parent_sta = meta.get("parent", "")
                cx, cy     = snap_to_line(lon, lat, flat)
                if haversine_km(lon, lat, cx, cy) > 0.150:
                    continue  # stop misassigned to this line — GTFS bbox margin too generous
                # Dots are generated post-cluster (like rail) to avoid duplicates at low zoom
                all_nonrail_pills.append({
                    "lon":            cx,
                    "lat":            cy,
                    "osm_id":         osm_id,
                    "mode":           mode,
                    "color":          color,
                    "width_base":     width_base,
                    "stop_id":        sid,
                    "stop_name":      stop_name,
                    "parent_station": parent_sta,
                })

        else:
            for entry in stop_coords:
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
                slon, slat = snap_to_line(lon, lat, flat)
                if haversine_km(lon, lat, slon, slat) > 0.150:
                    continue  # stop misassigned to this line — GTFS bbox margin too generous
                other_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": minzoom},
                    "geometry": {"type": "Point", "coordinates": [slon, slat]},
                    "properties": {
                        "color":          color,
                        "mode":           mode,
                        "width_base":     width_base,
                        "stop_id":        sid,
                        "stop_name":      meta.get("name", ""),
                        "parent_station": meta.get("parent", ""),
                    },
                })

    # --- Rail dots + pills (unified pass) ---
    print(f"  {len(rail_pill_raw):,} raw rail stop positions → clustering...")
    rail_pill_clusters = cluster_stops_for_pills(rail_pill_raw, PILL_CLUSTER_RAIL_KM)
    rail_pill_clusters = merge_clusters_by_parent_station(rail_pill_clusters)
    print(f"  → {len(rail_pill_clusters):,} rail station clusters")

    rail_features = []
    pill_features_rail = []
    for cluster in rail_pill_clusters:
        stop_count = count_unique_lines(cluster)
        mz = pill_minzoom("train", stop_count)

        color, mode, max_wb, dom_stop = dominant_line(cluster)
        lon = sum(s["lon"] for s in cluster) / len(cluster)
        lat = sum(s["lat"] for s in cluster) / len(cluster)
        centroid_props = {
            "color":          color,
            "mode":           mode,
            "width_base":     max_wb,
            "stop_id":        dom_stop.get("stop_id", ""),
            "stop_name":      dom_stop.get("stop_name", ""),
            "parent_station": dom_stop.get("parent_station", ""),
        }

        if mz is None:
            # Single-line station: one cluster dot at all zooms
            rail_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": 5},
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": centroid_props,
            })
        else:
            # Multi-line station: cluster dot at low zoom, individual platform dots at pill zoom+
            rail_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": 5, "maxzoom": mz - 1},
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": centroid_props,
            })
            for s in cluster:
                rail_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": mz},
                    "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                    "properties": {
                        "color":          s["color"],
                        "mode":           s["mode"],
                        "width_base":     s["width_base"],
                        "stop_id":        s.get("stop_id", ""),
                        "stop_name":      s.get("stop_name", ""),
                        "parent_station": s.get("parent_station", ""),
                    },
                })
            pill_features_rail.extend(make_pill_features(cluster, mz))

    rail_pill_count = len(pill_features_rail)
    print(f"  → {rail_pill_count} rail pill/connector features "
          f"from {len(rail_pill_clusters):,} clusters")

    # ==========================================================================
    # Pill generation (non-rail)
    # ==========================================================================

    pill_features = list(pill_features_rail)

    # --- Non-rail pills (all modes combined → dominant wins) ---
    print(f"  {len(all_nonrail_pills):,} non-rail pill candidates "
          f"(tram+metro+bus+regional combined) → clustering...")
    nonrail_clusters = cluster_stops_for_pills(all_nonrail_pills, PILL_CLUSTER_NONRAIL_KM)
    nonrail_clusters = merge_clusters_by_parent_station(nonrail_clusters)
    nonrail_pill_count = 0
    nonrail_dot_features = []
    for cluster in nonrail_clusters:
        stop_count  = count_unique_lines(cluster)
        color, dom_mode, max_wb, dom_stop = dominant_line(cluster)
        mz = pill_minzoom(dom_mode, stop_count)

        lon_c        = sum(s["lon"] for s in cluster) / len(cluster)
        lat_c        = sum(s["lat"] for s in cluster) / len(cluster)
        mode_minzoom = min(MODE_MINZOOM.get(s["mode"], 11) for s in cluster)
        centroid_props = {
            "color":          color,
            "mode":           dom_mode,
            "width_base":     max_wb,
            "stop_id":        dom_stop.get("stop_id", ""),
            "stop_name":      dom_stop.get("stop_name", ""),
            "parent_station": dom_stop.get("parent_station", ""),
        }

        if mz is None:
            # Single-line stop: one cluster dot at all zooms
            nonrail_dot_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": mode_minzoom},
                "geometry": {"type": "Point", "coordinates": [lon_c, lat_c]},
                "properties": centroid_props,
            })
        else:
            # Multi-line stop: cluster dot at low zoom, individual platform dots at pill zoom+
            nonrail_dot_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": mode_minzoom, "maxzoom": mz - 1},
                "geometry": {"type": "Point", "coordinates": [lon_c, lat_c]},
                "properties": centroid_props,
            })
            for s in cluster:
                nonrail_dot_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": mz},
                    "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                    "properties": {
                        "color":          color,  # dominant color so pills/dots match
                        "mode":           s["mode"],
                        "width_base":     s["width_base"],
                        "stop_id":        s.get("stop_id", ""),
                        "stop_name":      s.get("stop_name", ""),
                        "parent_station": s.get("parent_station", ""),
                    },
                })
            feats = make_pill_features(cluster, mz)
            pill_features.extend(feats)
            nonrail_pill_count += len(feats)

    print(f"  → {nonrail_pill_count} non-rail pill/connector features "
          f"from {len(nonrail_clusters):,} clusters")

    # ==========================================================================
    # Write outputs
    # ==========================================================================

    dot_features = rail_features + other_features + nonrail_dot_features
    OUT_DOTS.parent.mkdir(parents=True, exist_ok=True)
    OUT_DOTS.write_text(json.dumps({"type": "FeatureCollection", "features": dot_features}))
    OUT_PILLS.write_text(json.dumps({"type": "FeatureCollection", "features": pill_features}))

    # Summary
    mode_counts: dict = defaultdict(int)
    for f in dot_features:
        mode_counts[f["properties"]["mode"]] += 1
    print(f"\n{len(dot_features):,} stop dots → {OUT_DOTS}")
    for m, c in sorted(mode_counts.items(), key=lambda x: -x[1]):
        print(f"  {m:<20} {c:>6,}")

    pill_type_counts: dict = defaultdict(int)
    for f in pill_features:
        pill_type_counts[f["properties"].get("feature_type", "?")] += 1
    print(f"\n{len(pill_features):,} pill features → {OUT_PILLS}")
    for t, c in sorted(pill_type_counts.items(), key=lambda x: -x[1]):
        print(f"  {t:<20} {c:>6,}")


if __name__ == "__main__":
    main()
