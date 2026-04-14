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
  - Pill geometry is derived from the dot positions in the cluster, NOT route geometry:
      → Find the shortest capsule orientation (0–179°) that fits all dots
        within half_width_m of its axis. This prefers cross-track (perpendicular)
        orientation — connecting dots on parallel tracks — over along-track.
      → If no single orientation works (widely separated platform groups),
        split at the largest gap and emit two pills + a thin connector.
  - Cross-mode clustering: tram + bus at the same location → one pill in tram color.
  - Color = dominant line at that stop (by mode hierarchy, then width_base).
  - Width encoded as width_base → style applies ×2 multiplier.
"""

import csv
import json
from math import radians, cos, sin, sqrt, atan2, degrees, floor, pi
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

# Mode hierarchy for dominant stop color (lower = more important)
MODE_RANK = {"train": 0, "metro": 1, "tram": 2, "bus": 3, "regional_bus": 4}

# Spatial clustering radius for pill grouping
PILL_CLUSTER_RAIL_KM    = 0.300   # rail: 300 m (same as dot deduplication)
PILL_CLUSTER_NONRAIL_KM = 0.050   # all other modes combined: 50 m

# Half-width threshold used when searching for minimum capsule orientation.
# Physical half-width of the rendered pill ≈ width_base × this scale (meters).
# Matches rendered pill half-width at zoom ~13.
PILL_HALF_WIDTH_SCALE = 8


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
# Pill geometry — minimum enclosing capsule from dot positions
# =============================================================================

def to_local_m(positions):
    """
    Convert a list of (lon, lat) to local (x, y) in metres.
    Uses centroid as reference, applies cos(lat) correction for longitude.
    Returns (pts_m, ref_lon, ref_lat, cos_lat).
    """
    n = len(positions)
    ref_lon = sum(p[0] for p in positions) / n
    ref_lat = sum(p[1] for p in positions) / n
    cos_lat = cos(radians(ref_lat))
    pts_m = [((p[0] - ref_lon) * 111320 * cos_lat,
              (p[1] - ref_lat) * 111320)
             for p in positions]
    return pts_m, ref_lon, ref_lat, cos_lat


def from_local_m(mx, my, ref_lon, ref_lat, cos_lat):
    """Convert local metres back to (lon, lat)."""
    return (ref_lon + mx / (111320 * cos_lat),
            ref_lat + my / 111320)


def fit_capsule(pts_m, half_width_m):
    """
    Search 180 orientations (1° steps) for the shortest line-segment axis
    such that ALL points in pts_m are within half_width_m of the axis.

    Returns (min_idx, max_idx) — indices into pts_m for the pill endpoints —
    or None if no single orientation can cover all points within half_width_m.
    """
    best_span = float("inf")
    best_pair = None

    for deg in range(180):
        rad = radians(deg)
        ux, uy = cos(rad), sin(rad)     # axis unit vector
        nx, ny = -uy, ux               # normal (perpendicular) unit vector

        projs = [ux * x + uy * y for x, y in pts_m]
        perps = [abs(nx * x + ny * y) for x, y in pts_m]

        if max(perps) > half_width_m:
            continue

        span = max(projs) - min(projs)
        if span < best_span:
            best_span = span
            a = projs.index(min(projs))
            b = projs.index(max(projs))
            best_pair = (a, b)

    return best_pair


def split_and_fit(positions, pts_m, half_width_m):
    """
    When a single capsule cannot cover all dots, split them into two groups
    along the dominant direction (furthest-pair axis) at the largest gap.
    Returns (pill_coords_list, connector_coords_list).
    """
    n = len(pts_m)

    # Dominant direction: from the two furthest-apart points
    best_d = 0
    ai, bi = 0, 1
    for i in range(n):
        for j in range(i + 1, n):
            dx = pts_m[i][0] - pts_m[j][0]
            dy = pts_m[i][1] - pts_m[j][1]
            d = dx * dx + dy * dy
            if d > best_d:
                best_d = d
                ai, bi = i, j

    ddx = pts_m[bi][0] - pts_m[ai][0]
    ddy = pts_m[bi][1] - pts_m[ai][1]
    norm = sqrt(ddx * ddx + ddy * ddy) or 1.0
    ux, uy = ddx / norm, ddy / norm

    # Sort all points by projection onto dominant direction
    ordered = sorted([(ux * x + uy * y, idx) for idx, (x, y) in enumerate(pts_m)])

    # Find the largest gap between consecutive projected positions
    best_gap = 0
    split_after = 0
    for k in range(n - 1):
        gap = ordered[k + 1][0] - ordered[k][0]
        if gap > best_gap:
            best_gap = gap
            split_after = k

    g1_idxs = [idx for _, idx in ordered[:split_after + 1]]
    g2_idxs = [idx for _, idx in ordered[split_after + 1:]]

    def group_endpoints(idxs):
        """Furthest pair within a sub-group, or same point if singleton."""
        grp = [positions[i] for i in idxs]
        if len(grp) == 1:
            return [list(grp[0]), list(grp[0])]
        best = 0
        pa, pb = grp[0], grp[1]
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                d = haversine_km(grp[i][0], grp[i][1], grp[j][0], grp[j][1])
                if d > best:
                    best = d
                    pa, pb = grp[i], grp[j]
        return [list(pa), list(pb)]

    pill1 = group_endpoints(g1_idxs)
    pill2 = group_endpoints(g2_idxs)

    # Connector: nearest endpoint pair between the two pills
    ends1 = [pill1[0], pill1[-1]]
    ends2 = [pill2[0], pill2[-1]]
    best_cd = float("inf")
    ca, cb = ends1[0], ends2[0]
    for e1 in ends1:
        for e2 in ends2:
            d = haversine_km(e1[0], e1[1], e2[0], e2[1])
            if d < best_cd:
                best_cd = d
                ca, cb = e1, e2
    connector = [list(ca), list(cb)]

    return [pill1, pill2], [connector]


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


def dominant_line(stops_in_cluster):
    """
    Return (color, mode, max_width_base, dominant_stop) for the most important
    line in a stop cluster: lowest MODE_RANK wins; ties broken by width_base.
    """
    best_rank   = 999
    best_wb     = -1.0
    best_color  = "#888888"
    best_mode   = "bus"
    best_wb_out = 2.0
    best_stop   = {}
    for s in stops_in_cluster:
        rank = MODE_RANK.get(s["mode"], 99)
        wb   = s["width_base"]
        if rank < best_rank or (rank == best_rank and wb > best_wb):
            best_rank    = rank
            best_wb      = wb
            best_color   = s["color"]
            best_mode    = s["mode"]
            best_wb_out  = wb
            best_stop    = s
    return best_color, best_mode, best_wb_out, best_stop


def make_pill_features(cluster_stops, minzoom):
    """
    Build pill (and optional connector) GeoJSON features for a stop cluster.

    Pill geometry is derived entirely from dot positions:
    1. Find the shortest capsule orientation (0–179°) that fits all dots
       within the pill's physical half-width. Naturally prefers cross-track
       (shortest possible span = perpendicular to parallel tracks).
    2. If no single orientation works, split at the largest positional gap
       and emit two pills + a thin connector.
    """
    color, mode, max_wb, dom_stop = dominant_line(cluster_stops)
    positions = [(s["lon"], s["lat"]) for s in cluster_stops]
    n = len(positions)

    if n < 2:
        return []

    # Check if all dots are essentially at the same location (< 1 m apart)
    max_spread = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            d = haversine_km(
                positions[i][0], positions[i][1],
                positions[j][0], positions[j][1],
            )
            if d > max_spread:
                max_spread = d
    if max_spread < 0.001:   # < 1 m → all coincident, no pill needed
        return []

    pts_m, ref_lon, ref_lat, cos_lat = to_local_m(positions)
    half_w = max_wb * PILL_HALF_WIDTH_SCALE   # metres

    pair = fit_capsule(pts_m, half_w)

    if pair is not None:
        a, b = pair
        pill_coords_list = [[list(positions[a]), list(positions[b])]]
        connector_coords_list = []
    else:
        pill_coords_list, connector_coords_list = split_and_fit(
            positions, pts_m, half_w
        )

    stop_id    = dom_stop.get("stop_id", "")
    stop_name  = dom_stop.get("stop_name", "")
    parent_stat = dom_stop.get("parent_station", "")
    stop_count = len(cluster_stops)

    features = []
    for pill_coords in pill_coords_list:
        features.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": minzoom},
            "geometry": {"type": "LineString", "coordinates": pill_coords},
            "properties": {
                "color":          color,
                "mode":           mode,
                "width_base":     max_wb,
                "feature_type":   "pill",
                "stop_count":     stop_count,
                "stop_id":        stop_id,
                "stop_name":      stop_name,
                "parent_station": parent_stat,
            },
        })

    for connector_coords in connector_coords_list:
        features.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": minzoom},
            "geometry": {"type": "LineString", "coordinates": connector_coords},
            "properties": {
                "color":          color,
                "mode":           mode,
                "width_base":     max_wb,
                "feature_type":   "connector",
                "stop_count":     stop_count,
                "stop_id":        stop_id,
                "stop_name":      stop_name,
                "parent_station": parent_stat,
            },
        })

    return features


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

    rail_stops_raw    = []   # (lon, lat, color, mode, width_base) for dot clustering
    rail_pill_raw     = []   # dicts for rail pill clustering
    all_nonrail_pills = []   # ALL non-rail pill modes combined (tram+bus+metro+regional_bus)
    other_features    = []   # dot features for non-rail, ferry, mountain

    # --- Mountain / straight-line features with embedded gtfs_stops ---
    for feat in gtfs_stop_features:
        p      = feat["properties"]
        color  = p["color"]
        mode   = p["mode"]
        wb     = p.get("width_base", 3.0)
        coords = feat["geometry"]["coordinates"]
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
                # Dot candidate (raw GTFS position for clustering)
                rail_stops_raw.append((lon, lat, color, mode, width_base))
                # Pill candidate — use raw GTFS position (pill geometry from dot positions)
                rail_pill_raw.append({
                    "lon":            lon,
                    "lat":            lat,
                    "osm_id":         osm_id,
                    "mode":           mode,
                    "color":          color,
                    "width_base":     width_base,
                    "stop_id":        sid,
                    "stop_name":      stop_name,
                    "parent_station": parent_sta,
                })

        elif mode == "ferry":
            # Ferry: use raw GTFS coordinates (snapping puts stops mid-lake)
            for entry in stop_coords:
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
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
            # Ferry: no pills

        elif mode in PILL_MODES:
            for entry in stop_coords:
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
                stop_name  = meta.get("name", "")
                parent_sta = meta.get("parent", "")
                cx, cy     = snap_to_line(lon, lat, flat)
                # Dot
                other_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": minzoom},
                    "geometry": {"type": "Point", "coordinates": [cx, cy]},
                    "properties": {
                        "color":          color,
                        "mode":           mode,
                        "width_base":     width_base,
                        "stop_id":        sid,
                        "stop_name":      stop_name,
                        "parent_station": parent_sta,
                    },
                })
                # Pill candidate — all non-rail modes go into ONE combined pool
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
            # Unknown mode: snap dot, no pill
            for entry in stop_coords:
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
                slon, slat = snap_to_line(lon, lat, flat)
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

    # --- Rail dots ---
    print(f"  {len(rail_stops_raw):,} raw rail stop positions → clustering...")
    rail_clusters = cluster_rail_stops(rail_stops_raw)
    print(f"  → {len(rail_clusters):,} rail station clusters")

    rail_features = []
    for lon, lat, color, mode, max_wb in rail_clusters:
        rail_features.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": 5},
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "color":          color,
                "mode":           mode,
                "width_base":     max_wb,
                "stop_id":        "",
                "stop_name":      "(rail cluster)",
                "parent_station": "",
            },
        })

    # ==========================================================================
    # Pill generation
    # ==========================================================================

    pill_features = []

    # --- Rail pills ---
    print(f"  {len(rail_pill_raw):,} raw rail pill candidates → clustering...")
    rail_pill_clusters = cluster_stops_for_pills(rail_pill_raw, PILL_CLUSTER_RAIL_KM)
    rail_pill_count = 0
    for cluster in rail_pill_clusters:
        stop_count = count_unique_lines(cluster)
        mz = pill_minzoom("train", stop_count)
        if mz is None:
            continue
        feats = make_pill_features(cluster, mz)
        pill_features.extend(feats)
        rail_pill_count += len(feats)
    print(f"  → {rail_pill_count} rail pill/connector features "
          f"from {len(rail_pill_clusters):,} clusters")

    # --- Non-rail pills (all modes combined → dominant wins) ---
    print(f"  {len(all_nonrail_pills):,} non-rail pill candidates "
          f"(tram+metro+bus+regional combined) → clustering...")
    nonrail_clusters = cluster_stops_for_pills(all_nonrail_pills, PILL_CLUSTER_NONRAIL_KM)
    nonrail_pill_count = 0
    for cluster in nonrail_clusters:
        stop_count  = count_unique_lines(cluster)
        _, dom_mode, _, _ = dominant_line(cluster)
        mz = pill_minzoom(dom_mode, stop_count)
        if mz is None:
            continue
        feats = make_pill_features(cluster, mz)
        pill_features.extend(feats)
        nonrail_pill_count += len(feats)
    print(f"  → {nonrail_pill_count} non-rail pill/connector features "
          f"from {len(nonrail_clusters):,} clusters")

    # ==========================================================================
    # Write outputs
    # ==========================================================================

    dot_features = rail_features + other_features
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
