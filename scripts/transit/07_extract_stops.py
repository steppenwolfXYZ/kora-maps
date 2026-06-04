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
import yaml
from math import radians, cos, sin, sqrt, atan2, degrees, floor
from pathlib import Path
from collections import defaultdict

ROOT       = Path(__file__).resolve().parents[2]

_transit_cfg = yaml.safe_load((ROOT / "scripts" / "transit" / "config.yaml").read_text())
SNAP_GATE_DISABLED = _transit_cfg.get("debug", {}).get("disable_snap_gate", False)

LINES      = ROOT / "data" / "transit" / "transit_lines.geojson"
LINE_STOPS = ROOT / "data" / "transit" / "line_stops.json"
GTFS_STOPS   = ROOT / "data" / "gtfs_routed" / "stops.txt"
# pfaedle rewrites stops.txt to a canonical schema and drops `original_stop_id`,
# so the SLOID lookup reads from the pre-pfaedle filtered feed where the
# column is still intact.
GTFS_STOPS_PRE_PFAEDLE = ROOT / "data" / "gtfs_filtered" / "stops.txt"
ATLAS_CSV    = ROOT / "data" / "atlas" / "actual-date-world-traffic-point.csv"
OUT_DOTS     = ROOT / "data" / "transit" / "transit_stops.geojson"
OUT_PILLS    = ROOT / "data" / "transit" / "transit_stop_pills.geojson"
OUT_STOP_ATTRS_DIAG = ROOT / "data" / "transit" / "stop_attributes_sources.json"
OUT_DEBUG_PLATFORMS = ROOT / "data" / "transit" / "transit_debug_platforms.geojson"
OUT_DEBUG_STOPS     = ROOT / "data" / "transit" / "transit_debug_stops.geojson"

# Per-mode platform-length defaults and sanity ranges from config.
PILL_CFG = _transit_cfg.get("pill_rendering", {})

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
    """Return {stop_id: {"name": stop_name, "parent": parent_station}}.

    The official OTD GTFS feed prefixes parent_station values with `Parent`
    (e.g. `Parent8507000`); the prefix is stripped here so downstream
    clustering and comparisons are format-agnostic.
    """
    meta = {}
    if not GTFS_STOPS.exists():
        return meta
    with open(GTFS_STOPS, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sid = row["stop_id"]
            parent = row.get("parent_station", "").removeprefix("Parent")
            entry = {"name": row.get("stop_name", ""), "parent": parent}
            meta[sid] = entry
            base = sid.split(":")[0]
            if base not in meta:
                meta[base] = entry
    return meta


def load_stop_sloid() -> dict:
    """Return {stop_id: sloid} from the pre-pfaedle filtered stops.txt
    (`original_stop_id` column, dropped by pfaedle in `gtfs_routed`).
    """
    out = {}
    if not GTFS_STOPS_PRE_PFAEDLE.exists():
        return out
    with open(GTFS_STOPS_PRE_PFAEDLE, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sloid = (row.get("original_stop_id") or "").strip()
            if sloid:
                out[row["stop_id"]] = sloid
    return out


def load_atlas_attributes() -> dict:
    """Return {sloid: {"length": float|None, "compass_direction": float|None}}.

    Reads only the BOARDING_PLATFORM rows from atlas v2 traffic-point CSV.
    Empty / unparseable numeric fields become None.
    """
    out = {}
    if not ATLAS_CSV.exists():
        print(f"WARNING: atlas CSV not found at {ATLAS_CSV} — attributes will be empty")
        return out

    def _f(v):
        v = (v or "").strip()
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    with open(ATLAS_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            if row.get("trafficPointElementType") != "BOARDING_PLATFORM":
                continue
            sloid = row.get("sloid", "").strip()
            if not sloid:
                continue
            out[sloid] = {
                "length": _f(row.get("length")),
                "compass_direction": _f(row.get("compassDirection")),
            }
    return out


def write_stop_attributes_diag(line_stops: dict) -> dict:
    """Build the per-stop attribute lookup + diagnostic for every stop_id that
    appears in any drawn line. Emits stop_attributes_sources.json and returns
    the per-stop dict for downstream consumers (debug overlay, dot placement).
    """
    stop_sloid = load_stop_sloid()
    atlas = load_atlas_attributes()
    print(f"  {len(stop_sloid):,} GTFS stops with SLOID, "
          f"{len(atlas):,} atlas BOARDING_PLATFORM rows")

    used_stop_ids: set = set()
    for ls_entry in line_stops.values():
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        for trip in triplets:
            if len(trip) >= 3 and trip[2]:
                used_stop_ids.add(trip[2])

    out: dict = {}
    n_match = 0
    for sid in used_stop_ids:
        sloid = stop_sloid.get(sid)
        atlas_row = atlas.get(sloid) if sloid else None
        if atlas_row is not None:
            out[sid] = {
                "status": "atlas_match",
                "sloid": sloid,
                "length": atlas_row["length"],
                "compass_direction": atlas_row["compass_direction"],
            }
            n_match += 1
        else:
            out[sid] = {
                "status": "no_atlas_match",
                "sloid": sloid,
            }

    OUT_STOP_ATTRS_DIAG.write_text(json.dumps(out, ensure_ascii=False))
    print(f"  Stop attributes: {n_match:,}/{len(out):,} stops matched atlas "
          f"→ {OUT_STOP_ATTRS_DIAG}")
    return out


# =============================================================================
# Platform-extent computation (pill-rendering concept)
# =============================================================================

def _cum_dist_m(coords):
    """Cumulative distance in metres from start of polyline to each vertex."""
    out = [0.0]
    for i in range(1, len(coords)):
        out.append(out[-1] + haversine_km(
            coords[i-1][0], coords[i-1][1], coords[i][0], coords[i][1]) * 1000.0)
    return out


def _project_meters(px, py, coords, dists):
    """Closest point on polyline to (px, py); returns cumulative distance from
    polyline start in metres."""
    best_sq = float("inf")
    best_t = 0.0
    for i in range(len(coords) - 1):
        ax, ay = coords[i]
        bx, by = coords[i+1]
        dx, dy = bx - ax, by - ay
        seg_sq_lonlat = dx*dx + dy*dy
        if seg_sq_lonlat == 0:
            tt = 0.0
        else:
            tt = max(0.0, min(1.0, ((px-ax)*dx + (py-ay)*dy) / seg_sq_lonlat))
        cx, cy = ax + tt*dx, ay + tt*dy
        d = (px-cx)**2 + (py-cy)**2
        if d < best_sq:
            best_sq = d
            seg_len_m = dists[i+1] - dists[i]
            best_t = dists[i] + tt * seg_len_m
    return best_t


def _interp_at(coords, dists, t):
    """Interpolate polyline at cumulative distance t (metres). Clamps to ends."""
    if t <= 0:
        return coords[0][0], coords[0][1]
    if t >= dists[-1]:
        return coords[-1][0], coords[-1][1]
    for i in range(len(dists) - 1):
        if dists[i] <= t <= dists[i+1]:
            seg = dists[i+1] - dists[i]
            if seg == 0:
                return coords[i][0], coords[i][1]
            f = (t - dists[i]) / seg
            ax, ay = coords[i]
            bx, by = coords[i+1]
            return ax + f * (bx - ax), ay + f * (by - ay)
    return coords[-1][0], coords[-1][1]


def _slice_polyline(coords, dists, t_start, t_end):
    """Return the polyline vertex sequence between cumulative distances
    t_start and t_end (metres), with interpolated endpoints."""
    if t_start > t_end:
        t_start, t_end = t_end, t_start
    t_start = max(0.0, t_start)
    t_end = min(dists[-1], t_end)
    pts = [_interp_at(coords, dists, t_start)]
    for i, d in enumerate(dists):
        if t_start < d < t_end:
            pts.append((coords[i][0], coords[i][1]))
    pts.append(_interp_at(coords, dists, t_end))
    return pts


def _resolve_length(mode: str, atlas_length, cfg: dict):
    """Pick the platform length to use for a given mode and atlas value.

    Atlas value is used when it lies within the per-mode sanity range;
    otherwise the per-mode default is returned. Returns None for modes
    not in the rendering scope (ferry, mountain).
    """
    if mode not in cfg.get("default_length_m", {}):
        return None
    smin = cfg["sanity_min_m"][mode]
    smax = cfg["sanity_max_m"][mode]
    if atlas_length is not None and smin <= atlas_length <= smax:
        return atlas_length
    return cfg["default_length_m"][mode]


def _platform_extent(stop_lon, stop_lat, polyline, mode, atlas_length, cfg):
    """Return the (lon, lat) sequence tracing the platform's allowed range
    along its polyline, or None for out-of-scope modes / degenerate geometry.

    Anchoring (per pill-rendering concept):
      • train, metro  — GTFS coord is platform CENTRE → range = ±L/2.
      • tram, bus     — GTFS coord is FRONT of stop  → range = [coord - L, coord].
    """
    if len(polyline) < 2:
        return None
    L = _resolve_length(mode, atlas_length, cfg)
    if L is None:
        return None
    dists = _cum_dist_m(polyline)
    if dists[-1] <= 0:
        return None
    t = _project_meters(stop_lon, stop_lat, polyline, dists)
    if mode in ("train", "metro"):
        t_start, t_end = t - L / 2.0, t + L / 2.0
    else:
        t_start, t_end = t - L, t
    return _slice_polyline(polyline, dists, t_start, t_end)


def _mean_unit_tangent(cluster: list):
    """Mean unit tangent across all extents in the cluster, with direction
    canonicalised (positive x, then positive y) so opposite-direction polylines
    don't cancel. Returns (tx, ty) or None if no usable extents.
    """
    ax = ay = 0.0
    n = 0
    for s in cluster:
        ext = s.get("extent")
        if not ext or len(ext) < 2:
            continue
        dx = ext[-1][0] - ext[0][0]
        dy = ext[-1][1] - ext[0][1]
        mag = sqrt(dx*dx + dy*dy)
        if mag <= 0:
            continue
        if dx < 0 or (dx == 0 and dy < 0):
            dx, dy = -dx, -dy
        ax += dx / mag
        ay += dy / mag
        n += 1
    if n == 0:
        return None
    mag = sqrt(ax*ax + ay*ay)
    if mag <= 0:
        return None
    return (ax / mag, ay / mag)


def _closest_to_axis_line(polyline, cx, cy, ax, ay):
    """Closest point on `polyline` to the line through (cx, cy) with unit
    direction (ax, ay). Distance from (px, py) to that line equals
    |(px-cx)*(-ay) + (py-cy)*ax| since (ax, ay) is unit.

    Per segment: signed distance is linear in segment-parameter t. If the
    signs flip across a segment, the zero crossing is the closest point; else
    the closer endpoint of any segment wins overall.
    """
    def signed_d(x, y):
        return (x - cx) * (-ay) + (y - cy) * ax

    best_abs = float("inf")
    best_pt = (polyline[0][0], polyline[0][1])
    for i in range(len(polyline) - 1):
        x1, y1 = polyline[i]
        x2, y2 = polyline[i + 1]
        d1 = signed_d(x1, y1)
        d2 = signed_d(x2, y2)
        if d1 == 0 and d2 == 0:
            # segment lies on axis line — any point is closest
            return (x1, y1)
        if d1 * d2 < 0:
            # signs differ → zero crossing is at t = d1 / (d1 - d2)
            t = d1 / (d1 - d2)
            return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
        # otherwise track closest endpoint seen so far
        if abs(d1) < best_abs:
            best_abs = abs(d1)
            best_pt = (x1, y1)
        if abs(d2) < best_abs:
            best_abs = abs(d2)
            best_pt = (x2, y2)
    return best_pt


def shift_sub_pills_toward_target(merged_cluster: list, sub_clusters: list) -> None:
    """Stage 2 of the dot-placement pipeline (pill-rendering concept).

    Translate each sub-pill along its mean tangent toward the common target —
    the mean of all merged-cluster dot positions projected onto that
    sub-cluster's tangent. The shift is uniform across every dot in the sub
    (so the perpendicular extent — i.e. pill length and angle — is preserved)
    and bounded by `min(free_range)` across the sub's dots. When any dot is
    already at the range end on the side it would need to move toward, the
    shift collapses to zero — correct, the pill is already as close to the
    target as it can be without lengthening.

    For Zürich HB the merged cluster's sub-pills (parallel platforms in
    different sub-clusters) all shift toward a common along-track coordinate;
    if every dot has enough free range, the sub-pills converge onto the same
    perpendicular axis and merge visually into one big bar. For Eigerplatz
    the per-sub shifts collapse to zero (each sub's dots already clamp to the
    range end nearest the neighbour), so the two sub-pills stay where they
    are and the connector stays at its natural geographic length.
    """
    if len(sub_clusters) < 2:
        return
    # Snapshot initial positions so all sub-clusters compute targets from the
    # same starting state — otherwise a sub processed earlier moves the merged
    # cluster's mean and the next sub aims at a different target.
    snapshot = {id(s): (s["lon"], s["lat"]) for s in merged_cluster}

    pending_shifts = []  # (sub, dx, dy)
    for sub in sub_clusters:
        if len(sub) < 1:
            continue
        tangent = _mean_unit_tangent(sub)
        if tangent is None:
            continue
        tx, ty = tangent
        # Target tangent-coord = mean projection of the WHOLE merged cluster's
        # dot positions (from the snapshot) onto this sub's tangent.
        t_target = sum(snapshot[id(s)][0] * tx + snapshot[id(s)][1] * ty
                        for s in merged_cluster) / len(merged_cluster)
        t_current = sum(snapshot[id(s)][0] * tx + snapshot[id(s)][1] * ty
                         for s in sub) / len(sub)
        delta = t_target - t_current
        if abs(delta) < 1e-12:
            continue
        sign = 1.0 if delta > 0 else -1.0

        # Per-dot free shift in the chosen direction.
        free_shifts = []
        for s in sub:
            ext = s.get("extent")
            if not ext or len(ext) < 2:
                continue
            t_dot = snapshot[id(s)][0] * tx + snapshot[id(s)][1] * ty
            r_proj = [p[0] * tx + p[1] * ty for p in ext]
            r_min = min(r_proj)
            r_max = max(r_proj)
            if sign > 0:
                free = r_max - t_dot
            else:
                free = t_dot - r_min
            free_shifts.append(max(0.0, free))
        if not free_shifts:
            continue
        max_safe = min(free_shifts)
        actual = sign * min(max_safe, abs(delta))
        if abs(actual) < 1e-12:
            continue
        pending_shifts.append((sub, actual * tx, actual * ty))

    # Apply all shifts after every target has been computed.
    for sub, dx, dy in pending_shifts:
        for s in sub:
            ext = s.get("extent")
            if not ext or len(ext) < 2:
                continue
            new_x = s["lon"] + dx
            new_y = s["lat"] + dy
            s["lon"], s["lat"] = snap_to_line(new_x, new_y, ext)


def _spatial_subclusters(cluster: list, radius_km: float) -> list:
    """Split a cluster into connected components by spatial proximity.

    Two stops belong to the same component when they are within `radius_km`
    of each other. Used after `merge_clusters_by_parent_station` to recover
    the physically distinct platform groups inside a parent_station-merged
    cluster (e.g. Eigerplatz Nord vs Süd), so axis projection can run per
    sub-cluster instead of across the merged whole.
    """
    n = len(cluster)
    if n <= 1:
        return [list(cluster)]
    visited = [False] * n
    subs = []
    for i in range(n):
        if visited[i]:
            continue
        queue = [i]
        comp = []
        visited[i] = True
        while queue:
            k = queue.pop(0)
            comp.append(cluster[k])
            kx, ky = cluster[k]["lon"], cluster[k]["lat"]
            for j in range(n):
                if visited[j]:
                    continue
                if haversine_km(kx, ky,
                                cluster[j]["lon"], cluster[j]["lat"]) <= radius_km:
                    queue.append(j)
                    visited[j] = True
        subs.append(comp)
    return subs


def coordinate_dots_in_cluster(cluster: list) -> None:
    """Place each stop's dot at the point on its allowed-range polyline closest
    to the station axis line — the line perpendicular to the cluster's mean
    polyline tangent, positioned at the mean of the ranges' tangent-projected
    midpoints.

    For parallel-line clusters where every range covers a common interval,
    the axis lands inside all of them and every dot ends up on the axis line —
    a clean perpendicular bar. For clusters where opposite-direction stops'
    ranges don't overlap, each direction's dots clamp to the end of their
    range closest to the axis, producing two aligned sub-bars; the existing
    pill-split-on-NN-gap mechanism downstream emits two pills + a connector.

    Single-stop clusters are left untouched. Range midpoints are preferred
    over stop positions as the axis anchor because the stops' GTFS coords
    sit at the platform front (tram/bus) or centre (rail), which biases a
    stop-centroid toward the front and away from the range that the dot can
    actually reach.
    """
    if len(cluster) < 2:
        return
    tangent = _mean_unit_tangent(cluster)
    if tangent is None:
        return
    tx, ty = tangent
    ax, ay = -ty, tx  # axis direction perpendicular to mean tangent

    # Target tangent-coord = mean projection of each range's midpoint.
    # Range midpoint approximated by the average of its first and last vertices
    # (good enough for the short, near-straight extents this concept produces).
    t_targets = []
    for s in cluster:
        ext = s.get("extent")
        if not ext or len(ext) < 2:
            continue
        mx = (ext[0][0] + ext[-1][0]) / 2
        my = (ext[0][1] + ext[-1][1]) / 2
        t_targets.append(mx * tx + my * ty)
    if not t_targets:
        return
    t_target = sum(t_targets) / len(t_targets)

    # Centroid (just for the axis-orthogonal coordinate of the axis line).
    cx = sum(s["lon"] for s in cluster) / len(cluster)
    cy = sum(s["lat"] for s in cluster) / len(cluster)
    t_centroid = cx * tx + cy * ty
    shift = t_target - t_centroid
    ox, oy = cx + shift * tx, cy + shift * ty

    for s in cluster:
        ext = s.get("extent")
        if not ext or len(ext) < 2:
            continue
        s["lon"], s["lat"] = _closest_to_axis_line(ext, ox, oy, ax, ay)


def write_debug_platforms(line_stops: dict, line_lookup: dict,
                           stop_attrs: dict) -> None:
    """Emit transit_debug_platforms.geojson — one LineString per stop tracing
    the platform's full allowed range along the line's polyline. Debug-only
    overlay; replaces the previous black-dot debug feature.
    """
    cfg = PILL_CFG
    if not cfg.get("default_length_m"):
        print("  No pill_rendering config — debug platforms skipped.")
        return
    feats = []
    for osm_id, ls_entry in line_stops.items():
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        line = line_lookup.get(osm_id)
        if not line:
            continue
        mode = line["mode"]
        if mode not in cfg["default_length_m"]:
            continue
        polyline = flatten_coords(line["coords"])
        if len(polyline) < 2:
            continue
        for trip in triplets:
            if len(trip) < 3:
                continue
            stop_lon, stop_lat, stop_id = trip[0], trip[1], trip[2]
            atlas_length = (stop_attrs.get(stop_id, {}) or {}).get("length")
            extent = _platform_extent(stop_lon, stop_lat, polyline,
                                       mode, atlas_length, cfg)
            if extent is None or len(extent) < 2:
                continue
            feats.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": MODE_MINZOOM.get(mode, 11)},
                "geometry": {"type": "LineString",
                             "coordinates": [list(p) for p in extent]},
                "properties": {"mode": mode, "stop_id": stop_id},
            })
    OUT_DEBUG_PLATFORMS.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": feats,
    }, ensure_ascii=False))
    print(f"  Debug platforms: {len(feats):,} features → {OUT_DEBUG_PLATFORMS}")


def write_debug_stops(line_stops: dict, line_lookup: dict,
                       stop_attrs: dict, stop_meta: dict) -> None:
    """Emit transit_debug_stops.geojson — one Point per (line, stop) pair,
    1:1 with the debug platform lines. The point sits at the GTFS coord
    snapped onto that line's polyline (the same snap-to-line used by the
    pipeline's dot placement), so every debug line has a matching dot and
    every dot has a matching line.

    The popup data is keyed on stop_id and lists every line visiting that
    stop (with origin / destination), regardless of which line's snap this
    particular dot was rendered from.
    """
    cfg = PILL_CFG

    # First pass: per stop_id, build the (deduped) list of lines visiting it
    # plus the stop name. This populates the popup for every dot rendered
    # at this stop, regardless of which line's snap produced the dot.
    by_stop: dict = {}
    for osm_id, ls_entry in line_stops.items():
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        line = line_lookup.get(osm_id)
        if not line or not triplets:
            continue
        mode = line["mode"]
        if mode not in cfg.get("default_length_m", {}):
            continue
        first_trip = triplets[0]
        last_trip = triplets[-1]
        origin_sid = first_trip[2] if len(first_trip) >= 3 else ""
        dest_sid = last_trip[2] if len(last_trip) >= 3 else ""
        origin_name = (stop_meta.get(origin_sid, {}).get("name") or "?")
        dest_name = (stop_meta.get(dest_sid, {}).get("name") or "?")
        line_info = {
            "ref":         line.get("ref", ""),
            "mode":        mode,
            "color":       line.get("color", "#888888"),
            "origin":      origin_name,
            "destination": dest_name,
        }
        for trip in triplets:
            if len(trip) < 3:
                continue
            sid = trip[2]
            if not sid:
                continue
            entry = by_stop.get(sid)
            if entry is None:
                entry = {
                    "name": stop_meta.get(sid, {}).get("name", ""),
                    "visits": [],
                }
                by_stop[sid] = entry
            entry["visits"].append(line_info)

    per_stop_lines_json: dict = {}
    per_stop_name: dict = {}
    for sid, data in by_stop.items():
        seen = set()
        unique = []
        for v in data["visits"]:
            key = (v["ref"], v["origin"], v["destination"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(v)
        per_stop_lines_json[sid] = json.dumps(unique, ensure_ascii=False)
        per_stop_name[sid] = data["name"]

    # Second pass: one dot per (line, stop) at the snapped position on that
    # line's polyline. 1:1 with debug platform lines (same filtering).
    feats = []
    for osm_id, ls_entry in line_stops.items():
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        line = line_lookup.get(osm_id)
        if not line or not triplets:
            continue
        mode = line["mode"]
        if mode not in cfg.get("default_length_m", {}):
            continue
        polyline = flatten_coords(line["coords"])
        if len(polyline) < 2:
            continue
        for trip in triplets:
            if len(trip) < 3:
                continue
            lon, lat, sid = trip[0], trip[1], trip[2]
            if not sid:
                continue
            slon, slat = snap_to_line(lon, lat, polyline)
            attrs = stop_attrs.get(sid) or {}
            atlas_len = attrs.get("length") if isinstance(attrs, dict) else None
            feats.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": MODE_MINZOOM.get(mode, 11)},
                "geometry": {"type": "Point", "coordinates": [slon, slat]},
                "properties": {
                    "stop_id":          sid,
                    "stop_name":        per_stop_name.get(sid, ""),
                    "mode":             mode,
                    "platform_length":  atlas_len,
                    "lines_json":       per_stop_lines_json.get(sid, "[]"),
                },
            })
    OUT_DEBUG_STOPS.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": feats,
    }, ensure_ascii=False))
    print(f"  Debug stops: {len(feats):,} features → {OUT_DEBUG_STOPS}")


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


def cluster_lines(cluster_stops, line_lookup):
    """
    Return a sorted list of {ref, color, mode} dicts for all distinct lines
    serving any stop in the cluster.  Sorted by mode rank then ref.
    """
    seen = {}
    for s in cluster_stops:
        oid = s.get("osm_id", "")
        if oid and oid not in seen:
            info = line_lookup.get(oid, {})
            if info:
                seen[oid] = {
                    "ref":      info.get("gtfs_ref") or info.get("ref", ""),
                    "color":    info.get("color", "#888888"),
                    "mode":     info.get("mode", ""),
                    "name":     info.get("name", ""),
                }
    return sorted(seen.values(), key=lambda x: (MODE_RANK.get(x["mode"], 99), x.get("gtfs_ref") or x["ref"]))


def make_pill_features(cluster_stops, minzoom, lines_json=""):
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
        "lines_json":     lines_json,
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

    def make_endpoint(pos):
        return {
            "type": "Feature",
            "tippecanoe": {"minzoom": minzoom},
            "geometry": {"type": "Point", "coordinates": list(pos)},
            "properties": {**stop_props, "feature_type": "endpoint"},
        }

    # Pill for each group with ≥2 positions; single-point groups get an endpoint circle
    feats = []
    for grp in groups:
        if len(grp) >= 2:
            feats.append(make_feat(grp, "pill"))
        else:
            feats.append(make_endpoint(grp[0]))

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
                "ref":        p.get("ref", ""),
                "name":       p.get("name", ""),
            }
        if p.get("gtfs_stops"):
            gtfs_stop_features.append(feat)
    print(f"  {len(line_lookup):,} lines, {len(gtfs_stop_features):,} with embedded gtfs_stops")

    print("Loading stop coordinates and metadata...")
    line_stops = json.loads(LINE_STOPS.read_text())
    stop_meta  = load_stop_meta()
    print(f"  {len(line_stops):,} lines with stops, {len(stop_meta):,} GTFS stop entries")

    print("Loading atlas platform attributes...")
    stop_attrs = write_stop_attributes_diag(line_stops)

    print("Emitting debug platform extents...")
    write_debug_platforms(line_stops, line_lookup, stop_attrs)

    print("Emitting debug stop dots...")
    write_debug_stops(line_stops, line_lookup, stop_attrs, stop_meta)

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
    for osm_id, ls_entry in line_stops.items():
        if isinstance(ls_entry, dict):
            stop_coords = ls_entry.get("stops", [])
            if ls_entry.get("gtfs_ref"):
                line_lookup.setdefault(osm_id, {})["gtfs_ref"] = ls_entry["gtfs_ref"]
        else:
            stop_coords = ls_entry
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
                snap_d = haversine_km(lon, lat, slon, slat)
                if not SNAP_GATE_DISABLED and snap_d > 0.300:
                    continue  # stop too far from this line's pfaedle geometry
                atlas_len = (stop_attrs.get(sid, {}) or {}).get("length")
                extent = _platform_extent(lon, lat, flat, mode, atlas_len, PILL_CFG)
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
                    "extent":         extent,
                })

        elif mode == "ferry":
            line_lines_json = json.dumps([{"ref": line.get("gtfs_ref") or line.get("ref", ""), "color": color, "mode": mode, "name": line.get("name", "")}])
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
                        "lines_json":     line_lines_json,
                    },
                })

        elif mode in PILL_MODES:
            for entry in stop_coords:
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
                stop_name  = meta.get("name", "")
                parent_sta = meta.get("parent", "")
                cx, cy = snap_to_line(lon, lat, flat)
                gtfs_snap_d = haversine_km(lon, lat, cx, cy)
                if not SNAP_GATE_DISABLED and gtfs_snap_d > 0.150:
                    continue  # stop too far from this line's pfaedle geometry
                atlas_len = (stop_attrs.get(sid, {}) or {}).get("length")
                extent = _platform_extent(lon, lat, flat, mode, atlas_len, PILL_CFG)
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
                    "extent":         extent,
                })

        else:
            line_lines_json = json.dumps([{"ref": line.get("gtfs_ref") or line.get("ref", ""), "color": color, "mode": mode, "name": line.get("name", "")}])
            for entry in stop_coords:
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
                slon, slat = snap_to_line(lon, lat, flat)
                if not SNAP_GATE_DISABLED and haversine_km(lon, lat, slon, slat) > 0.150:
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
                        "lines_json":     line_lines_json,
                    },
                })

    # --- Rail dots + pills (unified pass) ---
    print(f"  {len(rail_pill_raw):,} raw rail stop positions → clustering...")
    rail_pill_clusters = cluster_stops_for_pills(rail_pill_raw, PILL_CLUSTER_RAIL_KM)
    rail_pill_clusters = merge_clusters_by_parent_station(rail_pill_clusters)
    print(f"  → {len(rail_pill_clusters):,} rail station clusters")
    # Sub-cluster within each parent_station-merged cluster, run axis
    # projection per sub-cluster (stage 1), then translate each sub-pill
    # toward the common target along its mean tangent (stage 2). The merged
    # cluster stays intact for the downstream NN-path + split-on-gap +
    # connector logic.
    for c in rail_pill_clusters:
        subs = _spatial_subclusters(c, PILL_CLUSTER_RAIL_KM)
        for sub in subs:
            coordinate_dots_in_cluster(sub)
        shift_sub_pills_toward_target(c, subs)

    rail_features = []
    pill_features_rail = []
    for cluster in rail_pill_clusters:
        stop_count = count_unique_lines(cluster)
        mz = pill_minzoom("train", stop_count)

        color, mode, max_wb, dom_stop = dominant_line(cluster)
        lon = sum(s["lon"] for s in cluster) / len(cluster)
        lat = sum(s["lat"] for s in cluster) / len(cluster)
        lines_json_str = json.dumps(cluster_lines(cluster, line_lookup))
        centroid_props = {
            "color":          color,
            "mode":           mode,
            "width_base":     max_wb,
            "stop_id":        dom_stop.get("stop_id", ""),
            "stop_name":      dom_stop.get("stop_name", ""),
            "parent_station": dom_stop.get("parent_station", ""),
            "lines_json":     lines_json_str,
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
                        "lines_json":     lines_json_str,
                    },
                })
            pill_features_rail.extend(make_pill_features(cluster, mz, lines_json_str))

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
    # Same two-stage placement as rail (see pill-rendering concept).
    for c in nonrail_clusters:
        subs = _spatial_subclusters(c, PILL_CLUSTER_NONRAIL_KM)
        for sub in subs:
            coordinate_dots_in_cluster(sub)
        shift_sub_pills_toward_target(c, subs)
    nonrail_pill_count = 0
    nonrail_dot_features = []
    for cluster in nonrail_clusters:
        stop_count  = count_unique_lines(cluster)
        color, dom_mode, max_wb, dom_stop = dominant_line(cluster)
        mz = pill_minzoom(dom_mode, stop_count)

        lon_c        = sum(s["lon"] for s in cluster) / len(cluster)
        lat_c        = sum(s["lat"] for s in cluster) / len(cluster)
        mode_minzoom = min(MODE_MINZOOM.get(s["mode"], 11) for s in cluster)
        lines_json_str = json.dumps(cluster_lines(cluster, line_lookup))
        centroid_props = {
            "color":          color,
            "mode":           dom_mode,
            "width_base":     max_wb,
            "stop_id":        dom_stop.get("stop_id", ""),
            "stop_name":      dom_stop.get("stop_name", ""),
            "parent_station": dom_stop.get("parent_station", ""),
            "lines_json":     lines_json_str,
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
                        "lines_json":     lines_json_str,
                    },
                })
            feats = make_pill_features(cluster, mz, lines_json_str)
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
