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
from math import radians, cos, sin, sqrt, atan2, degrees, floor, pi
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
OUT_DEBUG_BARS      = ROOT / "data" / "transit" / "transit_debug_bars.geojson"

# Diagnostic state populated by coordinate_dots_global_stab:
# - _DIAG_BARS: list of (endpoint1, endpoint2) tuples for each max-stab bar.
# - _STABBED_PAIRS: set of (osm_id, stop_id) for (line, stop) records placed
#   on a bar. Read by write_debug_stops to mark stabbed dots as filled.
_DIAG_BARS = []
_STABBED_PAIRS = set()

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

# Absolute-metre gap thresholds for splitting the NN path into separate
# pills + connectors. Not scaled by width_base — `wb` controls disc/pill
# width, not gap length.
PILL_GAP_STRAIGHT_M = 50   # gap threshold when the NN-path continues dead
                           # straight into the gap on either side (gap is
                           # an in-line pill continuation).
PILL_GAP_ANGLED_M = 12     # gap threshold otherwise (gap is an angled /
                           # T-junction connector).


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


TERMINUS_DEDUP_RADIUS_M = 10.0


def compute_terminus_skip_oids(line_stops: dict,
                                radius_m: float = TERMINUS_DEDUP_RADIUS_M) -> set:
    """Return the set of osm_ids whose FIRST entry (departure terminus) should
    be omitted from dot/pill/extent rendering because another line arrives at
    the same stop_id within `radius_m`. Keeps the arrival side as the visible
    dot+extent (its extent is the non-degenerate one for non-rail) and lets
    the popup-aggregation pass surface both directions.
    """
    arrivals_by_sid: dict = {}
    departures: list = []
    for oid, entry in line_stops.items():
        triplets = entry.get("stops", []) if isinstance(entry, dict) else entry
        if not triplets or len(triplets) < 2:
            continue
        first = triplets[0]
        last = triplets[-1]
        if len(first) >= 3 and first[2]:
            departures.append((str(oid), first[2], first[0], first[1]))
        if len(last) >= 3 and last[2]:
            arrivals_by_sid.setdefault(last[2], []).append(
                (str(oid), last[0], last[1]))

    skip: set = set()
    for oid_dep, sid, lon_d, lat_d in departures:
        for oid_arr, lon_a, lat_a in arrivals_by_sid.get(sid, []):
            if oid_arr == oid_dep:
                continue
            if haversine_km(lon_d, lat_d, lon_a, lat_a) * 1000.0 <= radius_m:
                skip.add(oid_dep)
                break
    return skip


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
      • train, metro  — GTFS coord (snapped to polyline) is platform CENTRE
                        → range = ±L/2.
      • tram, bus     — GTFS coord is FRONT of stop → range = [coord - L, coord].

    For rail: the snapped GTFS coord is always at the centre of the extent.
    When the polyline doesn't extend the full ±L/2 around it (e.g. a
    terminating-track polyline that ends at the buffer), the missing length
    on the clipped side is added as a straight-line extrapolation using the
    polyline's tangent at the snapped GTFS position. The on-polyline portion
    plus the extrapolated portion(s) together always total L metres, with
    the GTFS coord at the geometric centre.
    """
    if len(polyline) < 2:
        return None
    L = _resolve_length(mode, atlas_length, cfg)
    if L is None:
        return None
    dists = _cum_dist_m(polyline)
    poly_max = dists[-1]
    if poly_max <= 0:
        return None
    t = _project_meters(stop_lon, stop_lat, polyline, dists)

    if mode not in ("train", "metro"):
        t_start, t_end = t - L, t
        return _slice_polyline(polyline, dists, t_start, t_end)

    half_L = L / 2.0
    t_start_ideal = t - half_L
    t_end_ideal = t + half_L

    on_start = max(0.0, t_start_ideal)
    on_end = min(poly_max, t_end_ideal)
    slice_pts = list(_slice_polyline(polyline, dists, on_start, on_end))

    # Polyline tangent at the snapped GTFS position, expressed as a
    # per-metre (lon, lat) rate. Computed from a chord over a ±20 m window
    # around t rather than the single segment containing t, so pfaedle
    # "stub" segments (sub-metre joining segments at line termini that
    # carry normal-sized lon/lat deltas) don't blow up the per-metre ratio
    # and fling the extrapolated endpoint hundreds of km away.
    chord_lo_t = max(0.0, t - 20.0)
    chord_hi_t = min(poly_max, t + 20.0)
    chord_arc = chord_hi_t - chord_lo_t
    if chord_arc <= 0:
        return slice_pts

    lo = _interp_at(polyline, dists, chord_lo_t)
    hi = _interp_at(polyline, dists, chord_hi_t)
    dx_per_m = (hi[0] - lo[0]) / chord_arc
    dy_per_m = (hi[1] - lo[1]) / chord_arc

    pts = []
    if t_start_ideal < 0 and slice_pts:
        # Extrapolate from polyline[0] backwards (against forward tangent)
        missing_m = -t_start_ideal
        wx = slice_pts[0][0] - dx_per_m * missing_m
        wy = slice_pts[0][1] - dy_per_m * missing_m
        pts.append((wx, wy))
    pts.extend(slice_pts)
    if t_end_ideal > poly_max and slice_pts:
        # Extrapolate from polyline[-1] forward (with forward tangent)
        missing_m = t_end_ideal - poly_max
        ex = slice_pts[-1][0] + dx_per_m * missing_m
        ey = slice_pts[-1][1] + dy_per_m * missing_m
        pts.append((ex, ey))
    return pts


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


def _stop_tangent(s):
    """Unit tangent of one stop's extent (start → end), canonicalised to the
    upper half-plane so opposite directions don't cancel out. Returns
    (tx, ty) or None when the extent is degenerate.
    """
    ext = s.get("extent")
    if not ext or len(ext) < 2:
        return None
    dx = ext[-1][0] - ext[0][0]
    dy = ext[-1][1] - ext[0][1]
    mag = sqrt(dx * dx + dy * dy)
    if mag <= 0:
        return None
    if dx < 0 or (dx == 0 and dy < 0):
        dx, dy = -dx, -dy
    return (dx / mag, dy / mag)


def _extent_intersect_axis(ext, tx, ty, sigma):
    """Return the point on `ext` polyline whose tangent-coordinate equals
    `sigma`, i.e. where x*tx + y*ty == sigma. None if no segment crosses.
    For monotone-in-t polylines (typical short station extents) the first
    crossing is the only one.
    """
    prev_d = None
    for i, (x, y) in enumerate(ext):
        d = x * tx + y * ty - sigma
        if d == 0.0:
            return (x, y)
        if prev_d is not None and prev_d * d < 0:
            px, py = ext[i - 1]
            t = prev_d / (prev_d - d)
            return (px + t * (x - px), py + t * (y - py))
        prev_d = d
    return None


def _place_dot_on_extent(ext, tx, ty, sigma):
    """Best point on `ext` for a bar at axial position σ. Uses the σ-line
    intersection when it exists; otherwise snaps to the polyline endpoint
    closest to σ in the tangent direction (so an asymmetric polyline whose
    end is just past σ still places its dot at the polyline tip — visually
    right next to the bar — rather than missing the bar entirely).
    """
    pt = _extent_intersect_axis(ext, tx, ty, sigma)
    if pt is not None:
        return pt
    t_first = ext[0][0] * tx + ext[0][1] * ty
    t_last = ext[-1][0] * tx + ext[-1][1] * ty
    if abs(t_first - sigma) <= abs(t_last - sigma):
        return (ext[0][0], ext[0][1])
    return (ext[-1][0], ext[-1][1])


def _smoothed_tangent_at(polyline, dists, t, window_m=40.0):
    """Unit polyline tangent at arc-length `t`, averaged over a ±window_m/2
    window. Returns (tx, ty) or None if the polyline is too short. Smoothing
    reduces sensitivity to small pfaedle-routing kinks.
    """
    if not polyline or len(polyline) < 2:
        return None
    poly_max = dists[-1]
    if poly_max <= 0:
        return None
    half = window_m / 2.0
    t_lo = max(0.0, t - half)
    t_hi = min(poly_max, t + half)
    if t_hi - t_lo < 1e-9:
        # Window collapsed (extremely short polyline) — fall back to the
        # nearest segment's direction.
        i = 0 if t <= 0 else len(polyline) - 2
        dx = polyline[i + 1][0] - polyline[i][0]
        dy = polyline[i + 1][1] - polyline[i][1]
    else:
        lo = _interp_at(polyline, dists, t_lo)
        hi = _interp_at(polyline, dists, t_hi)
        dx = hi[0] - lo[0]
        dy = hi[1] - lo[1]
    mag = sqrt(dx * dx + dy * dy)
    if mag <= 0:
        return None
    return dx / mag, dy / mag


def _angular_dist_mod_pi(a1, a2):
    """Smallest angular distance between two angles on the half-circle
    [0, π) (0 and π are the same orientation)."""
    d = abs(a1 - a2) % pi
    return min(d, pi - d)


def _tangent_groups(platforms, max_angle_rad):
    """Group platforms by extent tangent direction. Union-find with the
    given angular tolerance (mod π) — two platforms are in the same group
    if their tangents are within `max_angle_rad`. Transitive closure means
    curved-but-coherent sets stay together. Returns list of groups."""
    data = []
    for p in platforms:
        t = _stop_tangent(p)
        if t is None:
            continue
        data.append((atan2(t[1], t[0]), p))
    n = len(data)
    if n == 0:
        return []
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _angular_dist_mod_pi(data[i][0], data[j][0]) <= max_angle_rad:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj

    groups = {}
    for i in range(n):
        r = find(i)
        groups.setdefault(r, []).append(data[i][1])
    return list(groups.values())


SWEEP_STEP_M = 10.0
CENTRAL_INNER_FRACTION = 0.7
SIGMA_CLUMP_SLACK_M = 5.0


def _sigma_clumps(group, slack_m=SIGMA_CLUMP_SLACK_M):
    """Split a tangent group into σ-clumps along the group's mean tangent.

    The perpendicular sweep walks only the central member's extent, so a
    tangent group spread across hundreds of metres of the same line — common
    at large stations where multiple stop bays sit along one street — gets
    only one bar near whichever sub-cluster contains the 2-D centroid; the
    far-away sub-cluster is unreachable. Splitting by σ-interval overlap
    along the group's mean tangent yields one sweep per clump.

    Two members belong to the same clump iff their σ-intervals overlap
    within `slack_m`. The slack absorbs the small mismatch between the
    group's mean tangent (used here) and each member's own tangent (used in
    `_perpendicular_sweep`'s sigma calc): 10° angular tolerance can shift a
    30 m extent's σ endpoints by a couple of metres.
    """
    if len(group) < 2:
        return [list(group)]
    mean_tan = _mean_unit_tangent(group)
    if mean_tan is None:
        return [list(group)]
    tx, ty = mean_tan

    intervals = []
    for p in group:
        ext = p.get("extent")
        if not ext or len(ext) < 2:
            continue
        sigmas = [v[0] * tx + v[1] * ty for v in ext]
        intervals.append((min(sigmas), max(sigmas), p))
    if not intervals:
        return []

    # Inside coordinate_dots_global_stab the cluster runs in scaled coords
    # (lon × cos_lat), so 1° on either axis ≈ 111000 m.
    slack = slack_m / 111000.0

    intervals.sort(key=lambda iv: iv[0])
    clumps = []
    current = [intervals[0][2]]
    current_hi = intervals[0][1]
    for lo, hi, p in intervals[1:]:
        if lo <= current_hi + slack:
            current.append(p)
            if hi > current_hi:
                current_hi = hi
        else:
            clumps.append(current)
            current = [p]
            current_hi = hi
    clumps.append(current)
    return clumps


def _perpendicular_sweep(group, angle_tol_rad):
    """For a tangent group, find every perpendicular bar tied at the max
    scoring-stab count by sweeping along the central member's platform
    extent at SWEEP_STEP_M resolution.

    The sweep walks the central member's extent (the same per-stop polyline
    drawn as the debug overlay) — not the full line polyline. This keeps
    the sweep bounded to the platform region and intrinsically fast.

    The central member is picked from the inner CENTRAL_INNER_FRACTION of
    the group (closest to the group centroid) — outer members are excluded
    from central-member selection so an off-to-the-side member can't drag
    the sweep away. Excluded members still count for stab scoring.

    Returns a non-empty list of option dicts, or None when no sweep position
    scoring-stabs ≥ 2 members. Each option carries the bar geometry, the
    scoring/covered member sets, the bar's perpendicular center (used by the
    multi-group inter-bar-distance tie-break), and the gtfs-distance score
    (used as final tie-break — sum of placed-dot to GTFS-snap distance in
    scaled-coord units).
    """
    n = len(group)
    if n < 2:
        return None

    cx = sum(p["lon"] for p in group) / n
    cy = sum(p["lat"] for p in group) / n

    # Inner-fraction subset for central-member selection.
    n_inner = max(1, n - int((1.0 - CENTRAL_INNER_FRACTION) * n))
    inner_sorted = sorted(
        group,
        key=lambda p: (p["lon"] - cx) ** 2 + (p["lat"] - cy) ** 2,
    )[:n_inner]
    central = inner_sorted[0]
    central_ext = central.get("extent")
    if not central_ext or len(central_ext) < 2:
        return None
    central_dists = _cum_dist_m(central_ext)
    ext_max = central_dists[-1]
    if ext_max <= 0:
        return None

    # Dense sweep at SWEEP_STEP_M along the central extent, plus the
    # arc-length projections of every group member's extent endpoints. The
    # 10 m grid alone can miss the optimal sigma by up to ±5 m; the
    # endpoint projections are exactly the sub-metre-precise positions
    # where a member transitions from stabbed to not-stabbed (or vice
    # versa), so adding them snaps the candidate set to the transitions.
    n_steps = max(2, int(ext_max / SWEEP_STEP_M) + 1)
    candidate_arcs_set = {i * ext_max / (n_steps - 1) for i in range(n_steps)}
    for p in group:
        ext = p["extent"]
        if not ext or len(ext) < 2:
            continue
        for endpoint in (ext[0], ext[-1]):
            candidate_arcs_set.add(
                _project_meters(endpoint[0], endpoint[1],
                                central_ext, central_dists))
    candidate_arcs = sorted(candidate_arcs_set)

    # Per-member tangent angle (canonical [0, π) via atan2 of stop tangent).
    member_angles = []
    for p in group:
        st = _stop_tangent(p)
        member_angles.append(atan2(st[1], st[0]) if st is not None else None)

    # First pass: find the max scoring-stab count and collect every (tx, ty,
    # sigma, scoring) tied at that count.
    best_count = 0
    raw_tied = []  # list of (tx, ty, sigma, scoring_local)
    for arc_d in candidate_arcs:
        pos = _interp_at(central_ext, central_dists, arc_d)
        tan = _smoothed_tangent_at(central_ext, central_dists, arc_d, 40.0)
        if tan is None:
            continue
        tx, ty = tan
        bar_angle = atan2(ty, tx)
        sigma = pos[0] * tx + pos[1] * ty
        scoring = []
        for k, p in enumerate(group):
            ma = member_angles[k]
            if ma is None:
                continue
            if _angular_dist_mod_pi(ma, bar_angle) > angle_tol_rad:
                continue
            ext = p["extent"]
            ts = [v[0] * tx + v[1] * ty for v in ext]
            if min(ts) <= sigma <= max(ts):
                scoring.append(k)
        if len(scoring) < 2:
            continue
        # Require ≥ 2 distinct platform positions among the scoring members:
        # multiple lines at the same snapped GTFS coord count as one platform,
        # so a bar that only stacks N lines on a single spot is rejected.
        distinct_positions = {
            (round(group[k]["lon"], 6), round(group[k]["lat"], 6))
            for k in scoring
        }
        if len(distinct_positions) < 2:
            continue
        if len(scoring) > best_count:
            best_count = len(scoring)
            raw_tied = [(tx, ty, sigma, scoring)]
        elif len(scoring) == best_count:
            raw_tied.append((tx, ty, sigma, scoring))
    if not raw_tied:
        return None

    # Second pass: enrich each tied position with covered set + bar center
    # + gtfs-distance score.
    options = []
    for tx, ty, sigma, scoring in raw_tied:
        nx, ny = -ty, tx
        scoring_pts = [
            _place_dot_on_extent(group[k]["extent"], tx, ty, sigma)
            for k in scoring
        ]
        scoring_n = [pt[0] * nx + pt[1] * ny for pt in scoring_pts]
        n_min, n_max = min(scoring_n), max(scoring_n)
        bar_cx = sum(pt[0] for pt in scoring_pts) / len(scoring_pts)
        bar_cy = sum(pt[1] for pt in scoring_pts) / len(scoring_pts)

        scoring_set = set(scoring)
        covered = []
        covered_pts = []
        for k, p in enumerate(group):
            if k in scoring_set:
                continue
            ext = p["extent"]
            ts = [v[0] * tx + v[1] * ty for v in ext]
            if not (min(ts) <= sigma <= max(ts)):
                continue
            cross_pt = _extent_intersect_axis(ext, tx, ty, sigma)
            if cross_pt is None:
                continue
            n_val = cross_pt[0] * nx + cross_pt[1] * ny
            if n_min <= n_val <= n_max:
                covered.append(k)
                covered_pts.append(cross_pt)

        gtfs_dist = 0.0
        for k, pt in zip(scoring, scoring_pts):
            p = group[k]
            gtfs_dist += sqrt((pt[0] - p["lon"]) ** 2
                              + (pt[1] - p["lat"]) ** 2)
        for k, pt in zip(covered, covered_pts):
            p = group[k]
            gtfs_dist += sqrt((pt[0] - p["lon"]) ** 2
                              + (pt[1] - p["lat"]) ** 2)

        options.append({
            "tx": tx, "ty": ty, "sigma": sigma,
            "scoring": scoring,
            "covered": covered,
            "bar_center": (bar_cx, bar_cy),
            "gtfs_dist": gtfs_dist,
        })

    return options


def _apply_option(group, option, placed_ids, record_stabbed=True):
    """Place this option's scoring + covered dots on their extents. When
    `record_stabbed` is False (e.g. trial placements during single-group
    measurement), the (osm_id, stop_id) pairs are NOT pushed to
    _STABBED_PAIRS — that's reserved for the chosen option's final pass."""
    tx, ty, sigma = option["tx"], option["ty"], option["sigma"]
    for k in option["scoring"]:
        p = group[k]
        pt = _place_dot_on_extent(p["extent"], tx, ty, sigma)
        p["lon"], p["lat"] = pt
        placed_ids.add(id(p))
        if record_stabbed:
            _STABBED_PAIRS.add((str(p.get("osm_id", "")),
                                str(p.get("stop_id", ""))))
    for k in option["covered"]:
        p = group[k]
        pt = _extent_intersect_axis(p["extent"], tx, ty, sigma)
        if pt is None:
            continue
        p["lon"], p["lat"] = pt
        placed_ids.add(id(p))
        if record_stabbed:
            _STABBED_PAIRS.add((str(p.get("osm_id", "")),
                                str(p.get("stop_id", ""))))


def _record_diag_bar(group, option):
    """Append this option's perpendicular debug-bar geometry to _DIAG_BARS."""
    tx, ty, sigma = option["tx"], option["ty"], option["sigma"]
    nx, ny = -ty, tx
    n_values = [group[k]["lon"] * nx + group[k]["lat"] * ny
                for k in option["scoring"]]
    if len(n_values) < 2:
        return
    n_min, n_max = min(n_values), max(n_values)
    margin = (n_max - n_min) * 0.05 + 1e-6
    n_min -= margin
    n_max += margin
    ep1 = (sigma * tx + n_min * nx, sigma * ty + n_min * ny)
    ep2 = (sigma * tx + n_max * nx, sigma * ty + n_max * ny)
    _DIAG_BARS.append((ep1, ep2))


def _pick_options_multi_group(per_group_options):
    """Pick one option per group minimising the sum of pairwise distances
    between groups' bar centers. Tie-break by total gtfs_dist across groups.
    """
    from itertools import product
    best = None
    best_key = None
    for combo in product(*(opts for _, opts in per_group_options)):
        total_dist = 0.0
        m = len(combo)
        for i in range(m):
            bx, by = combo[i]["bar_center"]
            for j in range(i + 1, m):
                jx, jy = combo[j]["bar_center"]
                total_dist += sqrt((bx - jx) ** 2 + (by - jy) ** 2)
        gtfs_total = sum(o["gtfs_dist"] for o in combo)
        key = (total_dist, gtfs_total)
        if best_key is None or key < best_key:
            best_key = key
            best = combo
    return list(best) if best else []


def _pick_option_single_group(group, options, cluster,
                               platforms, raw, radius_km):
    """For each tied option: place its dots, run leftover baseline (if any
    sub-cluster of ≥ 2 leftovers exists), measure pill+0.5×connector length,
    pick shortest. Tie-break by gtfs_dist. Cluster positions are reset to
    raw before returning so the outer caller can apply the chosen option
    cleanly. Runs regardless of leftover count: a single leftover still
    participates in the NN-path / MST connector, so the connector's length
    is sensitive to the chosen bar position."""
    best = None
    best_key = None
    for option in options:
        for s in cluster:
            s["lon"], s["lat"] = raw[id(s)]
        placed_ids = set()
        _apply_option(group, option, placed_ids, record_stabbed=False)
        leftovers = [p for p in platforms if id(p) not in placed_ids]
        if len(leftovers) >= 2:
            _apply_baseline_algorithm(leftovers, radius_km,
                                      anchor_set=platforms)
        length = _measure_pill_geometry(cluster)
        key = (length, option["gtfs_dist"])
        if best_key is None or key < best_key:
            best_key = key
            best = option
    for s in cluster:
        s["lon"], s["lat"] = raw[id(s)]
    return best


def _should_split_at_gap(path, k, gap_len_km, pos_to_platforms=None,
                          cos_lat=1.0):
    """Decide whether the NN-path segment path[k]→path[k+1] is a split
    (separates two pills + connector) or a regular in-pill segment.

    Two absolute-metre thresholds: PILL_GAP_STRAIGHT_M when the gap is a
    dead-straight in-line continuation of the surrounding pill, and
    PILL_GAP_ANGLED_M for angled / T-junction connectors. The straight
    threshold applies when either of these holds:
      • From each gap-adjacent dot, the NN-path continues dead straight in
        line with the gap direction for at least the gap length (no angle
        tolerance; any bend at all breaks the walk).
      • OR (perpendicular-platforms rule) both gap-adjacent dots have at
        least one platform whose extent tangent is 90° ±2° from the gap
        direction — i.e. the gap lies along a bar's perpendicular axis,
        so the bar continues through the gap even though the surrounding
        NN-path is too sparse to prove it via the walk. Only one platform
        per stacked dot needs to satisfy the angle test.
    Otherwise the angled threshold applies.

    cos_lat scales lon deltas to metric-equivalent space for the
    perpendicular-platforms check. Perpendicularity (unlike colinearity)
    is not preserved under non-uniform axis scaling, so the angle math
    must be done in metric. Pass cos(mean_lat) when the input is true
    (lon, lat); pass 1.0 when lon has already been pre-scaled. The
    dead-straight walk uses colinearity only and is scale-invariant.
    """
    straight_threshold_km = PILL_GAP_STRAIGHT_M / 1000.0
    angled_threshold_km = PILL_GAP_ANGLED_M / 1000.0
    if gap_len_km <= angled_threshold_km:
        return False
    if gap_len_km > straight_threshold_km:
        return True

    gap_dx = path[k + 1][0] - path[k][0]
    gap_dy = path[k + 1][1] - path[k][1]
    gnorm = sqrt(gap_dx * gap_dx + gap_dy * gap_dy)
    if gnorm <= 0:
        return False
    gx = gap_dx / gnorm
    gy = gap_dy / gnorm

    # Perpendicular-platforms rule: if both gap-adjacent dots have at least
    # one platform whose extent tangent is 90° ±2° from the gap direction,
    # the gap lies along a bar's perpendicular axis. Treat as in-line.
    # The angle math is done in metric-equivalent space (lon × cos_lat) —
    # perpendicularity is not preserved under raw lon/lat scaling for
    # non-axis-aligned tracks (Zurich/Bern HB, etc.).
    if pos_to_platforms is not None:
        gap_dx_m = gap_dx * cos_lat
        gap_dy_m = gap_dy
        gnorm_m = sqrt(gap_dx_m * gap_dx_m + gap_dy_m * gap_dy_m)
        if gnorm_m <= 0:
            return False
        gx_m = gap_dx_m / gnorm_m
        gy_m = gap_dy_m / gnorm_m
        perp_sin_tol = sin(radians(2.0))

        def _has_perp_platform(pos):
            for p in pos_to_platforms.get(pos, ()):
                ext = p.get("extent")
                if not ext or len(ext) < 2:
                    continue
                dx_m = (ext[-1][0] - ext[0][0]) * cos_lat
                dy_m = ext[-1][1] - ext[0][1]
                snorm_m = sqrt(dx_m * dx_m + dy_m * dy_m)
                if snorm_m <= 0:
                    continue
                # |cos(angle to gap)| ≤ sin(2°)  ⇔  perpendicular ±2°.
                if abs(dx_m * gx_m + dy_m * gy_m) / snorm_m <= perp_sin_tol:
                    return True
            return False

        if (_has_perp_platform(path[k])
                and _has_perp_platform(path[k + 1])):
            return False

    # "Dead straight" walk — only floating-point noise is tolerated. A
    # segment whose cross product with the gap direction (= sin of the
    # angle) is above ~1e-6 is treated as bent and breaks the walk.
    sin_eps = 1e-6

    def _is_aligned(seg_dx, seg_dy, snorm):
        # Must point the same way as the gap (positive dot product) AND
        # be colinear (cross product ≈ 0).
        if seg_dx * gx + seg_dy * gy < 0:
            return False
        return abs(seg_dx * gy - seg_dy * gx) / snorm <= sin_eps

    # Walk away from the gap on the left side: segments path[i-1]→path[i]
    # for i = k, k-1, ..., 1. Direction is path[i] - path[i-1], which
    # should match gap_dir for a straight continuation.
    back_len_km = 0.0
    for i in range(k, 0, -1):
        ax, ay = path[i - 1]
        bx, by = path[i]
        seg_dx, seg_dy = bx - ax, by - ay
        snorm = sqrt(seg_dx * seg_dx + seg_dy * seg_dy)
        if snorm <= 0:
            continue
        if not _is_aligned(seg_dx, seg_dy, snorm):
            break
        back_len_km += haversine_km(ax, ay, bx, by)
        if back_len_km >= gap_len_km:
            return False

    # Walk away from the gap on the right side: segments path[i]→path[i+1]
    # for i = k+1, k+2, ..., len(path)-2. Direction is path[i+1] - path[i].
    forward_len_km = 0.0
    for i in range(k + 1, len(path) - 1):
        ax, ay = path[i]
        bx, by = path[i + 1]
        seg_dx, seg_dy = bx - ax, by - ay
        snorm = sqrt(seg_dx * seg_dx + seg_dy * seg_dy)
        if snorm <= 0:
            continue
        if not _is_aligned(seg_dx, seg_dy, snorm):
            break
        forward_len_km += haversine_km(ax, ay, bx, by)
        if forward_len_km >= gap_len_km:
            return False

    return True


def _measure_pill_geometry(cluster_stops):
    """Score a placement: total pill geometry length, with connectors counted
    at half weight. Replicates make_pill_features's NN-path + per-gap split
    + MST connector logic without emitting features.
    """
    positions = list({(s["lon"], s["lat"]) for s in cluster_stops})
    if len(positions) < 2:
        return 0.0
    path = nearest_neighbor_path(positions)

    pos_to_platforms = {}
    for s in cluster_stops:
        pos_to_platforms.setdefault((s["lon"], s["lat"]), []).append(s)

    split_indices = [
        k for k in range(len(path) - 1)
        if _should_split_at_gap(
            path, k,
            haversine_km(path[k][0], path[k][1],
                         path[k + 1][0], path[k + 1][1]),
            pos_to_platforms)
    ]

    if not split_indices:
        return sum(haversine_km(path[k][0], path[k][1],
                                 path[k + 1][0], path[k + 1][1])
                   for k in range(len(path) - 1))

    groups = []
    prev = 0
    for idx in split_indices:
        groups.append(path[prev:idx + 1])
        prev = idx + 1
    groups.append(path[prev:])

    # Singleton groups contribute no pill length, but stay in `groups` so
    # the MST below mirrors make_pill_features (connector endpoints).
    pill_length = 0.0
    for grp in groups:
        if len(grp) < 2:
            continue
        for k in range(len(grp) - 1):
            pill_length += haversine_km(grp[k][0], grp[k][1],
                                         grp[k + 1][0], grp[k + 1][1])

    n_g = len(groups)
    mst_edges = []
    for i in range(n_g):
        for j in range(i + 1, n_g):
            best_d = float("inf")
            for p1 in groups[i]:
                for p2 in groups[j]:
                    d = haversine_km(p1[0], p1[1], p2[0], p2[1])
                    if d < best_d:
                        best_d = d
            mst_edges.append((best_d, i, j))
    mst_edges.sort()
    parent = list(range(n_g))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    connector_length = 0.0
    for d, i, j in mst_edges:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj
            connector_length += d
    return pill_length + 0.5 * connector_length


def _closest_to_axis_line(polyline, cx, cy, ax, ay):
    """Closest point on `polyline` to the line through (cx, cy) with unit
    direction (ax, ay). Distance from (px, py) to that line equals
    |(px-cx)*(-ay) + (py-cy)*ax| since (ax, ay) is unit. If the signs flip
    across a segment, the zero crossing is the closest point; otherwise
    the closer endpoint of any segment wins.
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
            return (x1, y1)
        if d1 * d2 < 0:
            t = d1 / (d1 - d2)
            return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
        if abs(d1) < best_abs:
            best_abs = abs(d1)
            best_pt = (x1, y1)
        if abs(d2) < best_abs:
            best_abs = abs(d2)
            best_pt = (x2, y2)
    return best_pt


def _coordinate_dots_in_subcluster(sub: list) -> None:
    """Old-algorithm stage 1: place each dot on the perpendicular axis line
    anchored at the mean of range midpoints.
    """
    if len(sub) < 2:
        return
    tangent = _mean_unit_tangent(sub)
    if tangent is None:
        return
    tx, ty = tangent
    ax, ay = -ty, tx

    t_targets = []
    for s in sub:
        ext = s.get("extent")
        if not ext or len(ext) < 2:
            continue
        mx = (ext[0][0] + ext[-1][0]) / 2
        my = (ext[0][1] + ext[-1][1]) / 2
        t_targets.append(mx * tx + my * ty)
    if not t_targets:
        return
    t_target = sum(t_targets) / len(t_targets)

    cx = sum(s["lon"] for s in sub) / len(sub)
    cy = sum(s["lat"] for s in sub) / len(sub)
    t_centroid = cx * tx + cy * ty
    shift = t_target - t_centroid
    ox, oy = cx + shift * tx, cy + shift * ty

    for s in sub:
        ext = s.get("extent")
        if not ext or len(ext) < 2:
            continue
        s["lon"], s["lat"] = _closest_to_axis_line(ext, ox, oy, ax, ay)


def _shift_subs_toward_anchor(anchor_set: list, sub_clusters: list) -> None:
    """Old-algorithm stage 2: translate each sub-pill along its own tangent
    toward the centroid of `anchor_set`, bounded by extent free range. With
    `anchor_set` == all platforms (including any already-placed bar dots),
    leftover sub-pills slide toward the bar, shortening connectors.
    """
    if not sub_clusters or not anchor_set:
        return
    snapshot = {id(s): (s["lon"], s["lat"]) for s in anchor_set}
    # Sub-cluster members must also be in the anchor set for the projection
    # snapshot lookup; build a fallback for any that aren't.
    for sub in sub_clusters:
        for s in sub:
            snapshot.setdefault(id(s), (s["lon"], s["lat"]))

    pending = []
    for sub in sub_clusters:
        if not sub:
            continue
        tangent = _mean_unit_tangent(sub)
        if tangent is None:
            continue
        tx, ty = tangent
        t_target = sum(snapshot[id(s)][0] * tx + snapshot[id(s)][1] * ty
                       for s in anchor_set) / len(anchor_set)
        t_current = sum(snapshot[id(s)][0] * tx + snapshot[id(s)][1] * ty
                        for s in sub) / len(sub)
        delta = t_target - t_current
        if abs(delta) < 1e-12:
            continue
        sign = 1.0 if delta > 0 else -1.0

        free_shifts = []
        for s in sub:
            ext = s.get("extent")
            if not ext or len(ext) < 2:
                continue
            t_dot = snapshot[id(s)][0] * tx + snapshot[id(s)][1] * ty
            r_proj = [p[0] * tx + p[1] * ty for p in ext]
            r_min = min(r_proj)
            r_max = max(r_proj)
            free = (r_max - t_dot) if sign > 0 else (t_dot - r_min)
            free_shifts.append(max(0.0, free))
        if not free_shifts:
            continue
        max_safe = min(free_shifts)
        actual = sign * min(max_safe, abs(delta))
        if abs(actual) < 1e-12:
            continue
        pending.append((sub, actual * tx, actual * ty))

    for sub, dx, dy in pending:
        for s in sub:
            ext = s.get("extent")
            if not ext or len(ext) < 2:
                continue
            new_x = s["lon"] + dx
            new_y = s["lat"] + dy
            s["lon"], s["lat"] = snap_to_line(new_x, new_y, ext)


def _spatial_subclusters(platforms: list, radius_km: float) -> list:
    """Split platforms into connected components by spatial proximity."""
    n = len(platforms)
    if n <= 1:
        return [list(platforms)]
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
            comp.append(platforms[k])
            kx, ky = platforms[k]["lon"], platforms[k]["lat"]
            for j in range(n):
                if visited[j]:
                    continue
                if haversine_km(kx, ky,
                                platforms[j]["lon"],
                                platforms[j]["lat"]) <= radius_km:
                    queue.append(j)
                    visited[j] = True
        subs.append(comp)
    return subs


def _apply_baseline_algorithm(platforms: list, radius_km: float,
                               anchor_set: list = None) -> None:
    """Old algorithm: spatial sub-cluster → axis projection per sub →
    translate each sub-pill toward `anchor_set`'s centroid. If `anchor_set`
    is None, the platforms themselves are the anchor (original behaviour).
    """
    subs = _spatial_subclusters(platforms, radius_km)
    for sub in subs:
        _coordinate_dots_in_subcluster(sub)
    _shift_subs_toward_anchor(anchor_set if anchor_set is not None else platforms, subs)


def coordinate_dots_global_stab(cluster: list, radius_km: float) -> None:
    """Tangent-group + perpendicular-sweep dot placement.

    Candidate A — for each tangent group of platforms (extent tangents
    within ~10° of each other), pick a central member from the inner 70 %
    of the group (closest to centroid) and sweep along that member's
    platform extent at 10 m steps. At each step the bar is perpendicular
    to the smoothed extent tangent; the position maximising scoring-stab
    count wins. Scoring-stabbed platforms (≤10° aligned, extent crosses
    bar) get their dots placed on the bar and drive its drawn span.
    Wrong-angle members whose extent crosses the bar between scoring dots
    are also placed on the bar ("covered"). Everything else runs through
    the old algorithm anchored to the full platform set so leftover
    sub-pills shift toward the bar.

    Candidate B (baseline) — the old algorithm on every platform.

    Whichever produces the shorter total pill geometry (pills + 0.5 ×
    connectors) wins. The temporary fallback override is still disabled —
    Candidate A is always applied at the moment for experimentation.
    """
    if len(cluster) < 2:
        return
    platforms = [s for s in cluster
                 if s.get("extent") and len(s["extent"]) >= 2]
    if len(platforms) < 2:
        return

    # --- Equal-distance scaling
    # Tangents, perpendiculars, σ-lines and dot intersections are computed in
    # 2-D Cartesian math, but raw (lon, lat) is not Cartesian: at Swiss
    # latitudes 1° lon ≈ 76 km whereas 1° lat ≈ 111 km. Without a fix,
    # "perpendicular in lon/lat" is not "perpendicular in real geography /
    # Mercator display" — diagonal tracks (Zürich HB ≈ 135° azimuth) get
    # bars ~20° off the real perpendicular. Scaling lon by cos(latitude)
    # produces a coordinate system where 1 unit lon = 1 unit lat (in
    # metres), so 2-D Cartesian perpendicular is also real perpendicular.
    # All algorithm internals run on the scaled coords; we unscale lon back
    # to real degrees before returning so the placed positions, extents,
    # and recorded debug bars are in true lon/lat.
    mean_lat = sum(s["lat"] for s in cluster) / len(cluster)
    cos_lat = cos(radians(mean_lat))
    if cos_lat <= 0:
        return

    for s in cluster:
        s["lon"] *= cos_lat
        ext = s.get("extent")
        if ext:
            s["extent"] = [(x * cos_lat, y) for x, y in ext]

    diag_bars_start = len(_DIAG_BARS)

    try:
        # Snapshot raw (scaled) positions so we can swap between candidates.
        raw = {id(s): (s["lon"], s["lat"]) for s in cluster}

        # --- Candidate B: baseline (old algorithm on all)
        _apply_baseline_algorithm(platforms, radius_km)
        baseline_length = _measure_pill_geometry(cluster)
        baseline_positions = {id(s): (s["lon"], s["lat"]) for s in cluster}

        # Reset to raw positions for Candidate A.
        for s in cluster:
            s["lon"], s["lat"] = raw[id(s)]

        # Tangent groups (union-find, 10° angular tolerance mod π), then
        # σ-clump each tangent group along its mean tangent so multi-clump
        # groups (opposite ends of a long station) get a sweep per clump
        # rather than one stuck near whichever clump contains the 2-D
        # centroid.
        angle_tol = radians(10.0)
        groups = _tangent_groups(platforms, angle_tol)

        # For each σ-clump of ≥ 2 members, collect every tied max-scoring-
        # stab bar position.
        per_group_options = []  # list of (clump, [option, ...])
        for group in groups:
            if len(group) < 2:
                continue
            for clump in _sigma_clumps(group):
                if len(clump) < 2:
                    continue
                options = _perpendicular_sweep(clump, angle_tol)
                if options:
                    per_group_options.append((clump, options))

        # Pick one option per group — see pill-rendering.md "Tie-breaking
        # among equally-stabbing sweep positions":
        #   • Multi-group: minimise sum of pairwise bar-center distances.
        #     Tie-break by total gtfs_dist.
        #   • Single-group with leftovers: enumerate options, run leftover
        #     baseline per option, pick minimum pill+0.5×connector length.
        #     Tie-break by gtfs_dist.
        #   • Single-group without leftovers: pick minimum gtfs_dist.
        chosen = []
        if len(per_group_options) >= 2:
            chosen = _pick_options_multi_group(per_group_options)
        elif len(per_group_options) == 1:
            group, options = per_group_options[0]
            if len(options) > 1:
                chosen = [_pick_option_single_group(
                    group, options, cluster, platforms, raw, radius_km)]
            else:
                chosen = [options[0]]

        # Apply chosen options (record _STABBED_PAIRS + diag bar geometry).
        placed_ids = set()
        for (group, _), option in zip(per_group_options, chosen):
            _apply_option(group, option, placed_ids, record_stabbed=True)
            _record_diag_bar(group, option)

        # Leftovers: every platform NOT placed on a bar.
        leftovers = [p for p in platforms if id(p) not in placed_ids]
        if leftovers:
            _apply_baseline_algorithm(leftovers, radius_km, anchor_set=platforms)

        candidate_a_length = _measure_pill_geometry(cluster)

        # TEMP: candidate-B fallback disabled for experiment
        # if candidate_a_length >= baseline_length:
        #     for s in cluster:
        #         s["lon"], s["lat"] = baseline_positions[id(s)]
    finally:
        # Unscale lon back to real degrees on cluster stops, extents, and
        # any debug bars added during this cluster's processing.
        for s in cluster:
            s["lon"] /= cos_lat
            ext = s.get("extent")
            if ext:
                s["extent"] = [(x / cos_lat, y) for x, y in ext]
        for i in range(diag_bars_start, len(_DIAG_BARS)):
            ep1, ep2 = _DIAG_BARS[i]
            _DIAG_BARS[i] = (
                (ep1[0] / cos_lat, ep1[1]),
                (ep2[0] / cos_lat, ep2[1]),
            )


def write_debug_platforms(line_stops: dict, line_lookup: dict,
                           stop_attrs: dict, skip_first_oids: set) -> None:
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
        skip_first_here = str(osm_id) in skip_first_oids
        for idx, trip in enumerate(triplets):
            if idx == 0 and skip_first_here:
                continue
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
                       stop_attrs: dict, stop_meta: dict,
                       skip_first_oids: set) -> None:
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
            "osm_id":      str(osm_id),
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
        by_key: dict = {}
        order = []
        for v in data["visits"]:
            key = (v["ref"], v["origin"], v["destination"])
            if key not in by_key:
                entry = {
                    "ref":         v["ref"],
                    "mode":        v["mode"],
                    "color":       v["color"],
                    "origin":      v["origin"],
                    "destination": v["destination"],
                    "osm_ids":     [v["osm_id"]],
                }
                by_key[key] = entry
                order.append(key)
            else:
                osm_ids = by_key[key]["osm_ids"]
                if v["osm_id"] not in osm_ids:
                    osm_ids.append(v["osm_id"])
        unique = [by_key[k] for k in order]
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
        skip_first_here = str(osm_id) in skip_first_oids
        for idx, trip in enumerate(triplets):
            if idx == 0 and skip_first_here:
                continue
            if len(trip) < 3:
                continue
            lon, lat, sid = trip[0], trip[1], trip[2]
            if not sid:
                continue
            dot_lon, dot_lat = snap_to_line(lon, lat, polyline)
            attrs = stop_attrs.get(sid) or {}
            atlas_len = attrs.get("length") if isinstance(attrs, dict) else None
            stabbed = (str(osm_id), str(sid)) in _STABBED_PAIRS
            feats.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": MODE_MINZOOM.get(mode, 11)},
                "geometry": {"type": "Point", "coordinates": [dot_lon, dot_lat]},
                "properties": {
                    "stop_id":          sid,
                    "stop_name":        per_stop_name.get(sid, ""),
                    "mode":             mode,
                    "platform_length":  atlas_len,
                    "lines_json":       per_stop_lines_json.get(sid, "[]"),
                    "stabbed":          stabbed,
                    "current_osm_id":   str(osm_id),
                },
            })
    OUT_DEBUG_STOPS.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": feats,
    }, ensure_ascii=False))
    stabbed_count = sum(1 for f in feats if f["properties"]["stabbed"])
    print(f"  Debug stops: {len(feats):,} features ({stabbed_count:,} stabbed) "
          f"→ {OUT_DEBUG_STOPS}")


def write_debug_bars() -> None:
    """Emit transit_debug_bars.geojson — one LineString per max-stab bar
    found during cluster processing. Each line spans the perpendicular
    extent of its stabbed dots (plus a small visual margin), so on the map
    the line draws exactly where the bar "is" in 2D.
    """
    feats = []
    for ep1, ep2 in _DIAG_BARS:
        feats.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": 5},
            "geometry": {"type": "LineString",
                         "coordinates": [list(ep1), list(ep2)]},
            "properties": {},
        })
    OUT_DEBUG_BARS.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": feats,
    }, ensure_ascii=False))
    print(f"  Debug bars: {len(feats):,} features → {OUT_DEBUG_BARS}")


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
    2. Walk each NN-path segment as a candidate gap. The effective split
       threshold for each gap depends on the local shape (see
       _should_split_at_gap): dead-straight in-line continuations get the
       generous PILL_GAP_STRAIGHT_M threshold; angled / T-junction
       connectors get the tighter PILL_GAP_ANGLED_M threshold.
    3. Gaps that exceed their threshold split the NN-path. Sub-paths of
       ≥ 2 dots emit as pills; singletons emit as endpoint Points.
    4. MST connectors join the resulting groups at their nearest dot pair.
    """
    color, mode, max_wb, dom_stop = dominant_line(cluster_stops)
    positions = list({(s["lon"], s["lat"]) for s in cluster_stops})  # deduplicate
    n = len(positions)

    if n < 2:
        return []

    path = nearest_neighbor_path(positions)

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

    def make_endpoint(pos):
        return {
            "type": "Feature",
            "tippecanoe": {"minzoom": minzoom},
            "geometry": {"type": "Point", "coordinates": list(pos)},
            "properties": {**stop_props, "feature_type": "endpoint"},
        }

    # Find every gap that splits the NN-path into separate pills.
    # _should_split_at_gap applies the per-shape threshold (PILL_GAP_STRAIGHT_M
    # for dead-straight in-line continuations or gaps along a bar's
    # perpendicular axis; PILL_GAP_ANGLED_M for angled / T-junction
    # connectors). Absolute metres — no width_base scaling.
    pos_to_platforms = {}
    for s in cluster_stops:
        pos_to_platforms.setdefault((s["lon"], s["lat"]), []).append(s)
    mean_lat = sum(p[1] for p in positions) / len(positions)
    cluster_cos_lat = cos(radians(mean_lat))
    split_indices = [
        k for k in range(len(path) - 1)
        if _should_split_at_gap(
            path, k,
            haversine_km(path[k][0], path[k][1],
                         path[k + 1][0], path[k + 1][1]),
            pos_to_platforms,
            cos_lat=cluster_cos_lat)
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

    # Singleton groups can't render as pill LineStrings, but they get an
    # endpoint circle so the connector's white casing is hidden under a
    # colored disc (drawn between connector-casing and connector-fill in
    # the style layer stack). Singletons still participate in the MST.
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
                        if id(npt) in visited:
                            continue
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
                        if id(ns) in visited:
                            continue
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

    skip_first_oids = compute_terminus_skip_oids(line_stops)
    print(f"  Terminus dedup: {len(skip_first_oids):,} departure-side entries "
          f"will be omitted from rendering (popup retains both directions)")

    print("Emitting debug platform extents...")
    write_debug_platforms(line_stops, line_lookup, stop_attrs, skip_first_oids)

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

        skip_first_here = str(osm_id) in skip_first_oids

        if mode in RAIL_MODES:
            for idx, entry in enumerate(stop_coords):
                if idx == 0 and skip_first_here:
                    continue
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
            for idx, entry in enumerate(stop_coords):
                if idx == 0 and skip_first_here:
                    continue
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
            for idx, entry in enumerate(stop_coords):
                if idx == 0 and skip_first_here:
                    continue
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
            for idx, entry in enumerate(stop_coords):
                if idx == 0 and skip_first_here:
                    continue
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
    # Place dots via tangent grouping + perpendicular sweep along the central
    # member's platform extent (per-group). Stabbed dots get placed on the
    # perpendicular bar; leftovers run through the old algorithm.
    print(f"  Placing rail dots across {len(rail_pill_clusters):,} clusters...")
    for c in rail_pill_clusters:
        coordinate_dots_global_stab(c, PILL_CLUSTER_RAIL_KM)
    print("  → rail dot placement done")

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
    # Same global stabbing placement as rail.
    print(f"  Placing non-rail dots across {len(nonrail_clusters):,} clusters...")
    for c in nonrail_clusters:
        coordinate_dots_global_stab(c, PILL_CLUSTER_NONRAIL_KM)
    print("  → non-rail dot placement done")

    # Emit debug overlays now that all clusters have been processed and
    # _STABBED_PAIRS / _DIAG_BARS are populated.
    print("Emitting debug stop dots...")
    write_debug_stops(line_stops, line_lookup, stop_attrs, stop_meta, skip_first_oids)
    print("Emitting debug max-stab bars...")
    write_debug_bars()

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
