"""Pure geometry helpers: distance, projection, polyline utilities.

Depends only on stdlib math so any pipeline step can import safely.
"""

from math import atan2, cos, degrees, radians, sin, sqrt

# Meters per degree at the equator; lon components are additionally scaled
# by cos(latitude) for equal-distance projection.
_M_PER_DEG = 111319.49


def haversine_km(lon1, lat1, lon2, lat2) -> float:
    R = 6371.0
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlam = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlam / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def polyline_length_km(coords: list) -> float:
    if len(coords) < 2:
        return 0.0
    total = 0.0
    for i in range(len(coords) - 1):
        total += haversine_km(coords[i][0], coords[i][1],
                              coords[i + 1][0], coords[i + 1][1])
    return total


def parse_time(t: str) -> int:
    """HH:MM:SS → seconds. Caller catches ValueError."""
    parts = t.strip().split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])


def line_bbox(coords):
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lons), min(lats), max(lons), max(lats)


def _bbox_overlap_fraction(b1, b2) -> float:
    ix0, iy0 = max(b1[0], b2[0]), max(b1[1], b2[1])
    ix1, iy1 = min(b1[2], b2[2]), min(b1[3], b2[3])
    if ix0 >= ix1 or iy0 >= iy1:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    a1 = (b1[2] - b1[0]) * (b1[3] - b1[1])
    a2 = (b2[2] - b2[0]) * (b2[3] - b2[1])
    smaller = min(a1, a2)
    return inter / smaller if smaller > 0 else 0.0


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


def _meters_per_deg(lat):
    """Return (mx_per_deg_lon, my_per_deg_lat) for local equirectangular
    scaling at the given latitude."""
    return 111320.0 * cos(radians(lat)), 111320.0


# ── Polyline arc-length helpers (used by extent, OSM walks, close-zoom) ──────

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


def _slice_polyline(pts, dists, t0, t1):
    """Sub-polyline of `pts` between arc positions t0 < t1 (interpolated
    endpoints, interior vertices kept). No swapping or clamping of t0/t1 —
    callers pass ordered in-range positions."""
    out = [tuple(_interp_at(pts, dists, t0))]
    for p, t in zip(pts, dists):
        if t0 < t < t1:
            out.append(tuple(p))
    out.append(tuple(_interp_at(pts, dists, t1)))
    return out


def _directional_tangent_at(polyline, dists, t, window_m=20.0):
    """Per-metre (dx, dy) tangent of `polyline` at arc-length `t`, directional
    (forward in increasing-t direction). Chord computed over a ±window_m window
    around t — so pfaedle "stub" segments at line termini that carry normal-sized
    lon/lat deltas across sub-metre arc-lengths don't blow up the per-metre rate.
    Returns None if the polyline is too short to compute a chord.
    """
    if len(polyline) < 2:
        return None
    poly_max = dists[-1]
    if poly_max <= 0:
        return None
    lo_t = max(0.0, t - window_m)
    hi_t = min(poly_max, t + window_m)
    arc = hi_t - lo_t
    if arc <= 0:
        return None
    lo = _interp_at(polyline, dists, lo_t)
    hi = _interp_at(polyline, dists, hi_t)
    return ((hi[0] - lo[0]) / arc, (hi[1] - lo[1]) / arc)


def _start_segment_tangent(polyline, dists, min_seg_m=1.0):
    """Per-metre (dx, dy) direction of the polyline's very first segment
    (poly[0] → poly[1]). Returns None if that segment is shorter than
    `min_seg_m` — the caller then falls back to a windowed average, because a
    pfaedle sub-metre stub at a line terminus would otherwise be used as the
    extension direction and point wildly wrong.

    Used as the anchor direction of the tram/bus missing-range fill (borrow
    tangent matching and OSM-walk way matching) — the fill follows the
    actual arrival angle at the polyline's starting vertex, not a chord
    averaged over a window that may cross a curve at the platform.
    """
    if len(polyline) < 2:
        return None
    seg_len = dists[1] - dists[0]
    if seg_len < min_seg_m:
        return None
    ax, ay = polyline[0]
    bx, by = polyline[1]
    return ((bx - ax) / seg_len, (by - ay) / seg_len)


def _polyline_midpoint(coords):
    """Return the (lon, lat) midpoint of a polyline by arc length."""
    if not coords:
        return (0.0, 0.0)
    if len(coords) == 1:
        return (coords[0][0], coords[0][1])
    seg_lens = []
    total = 0.0
    for i in range(len(coords) - 1):
        a, b = coords[i], coords[i + 1]
        d = haversine_km(a[0], a[1], b[0], b[1])
        seg_lens.append(d)
        total += d
    half = total / 2.0
    acc = 0.0
    for i, d in enumerate(seg_lens):
        if acc + d >= half:
            t = (half - acc) / d if d > 0 else 0.0
            a, b = coords[i], coords[i + 1]
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        acc += d
    return (coords[-1][0], coords[-1][1])


def _polyline_midpoint_and_tangent_deg(coords):
    """
    Return ((lon, lat), tangent_deg) for the polyline midpoint by arc length.

    The tangent angle is in degrees clockwise from east (MapLibre
    `text-rotate` convention with `text-rotation-alignment: map`).
    Returns 0° tangent for degenerate polylines.
    """
    if not coords or len(coords) < 2:
        if coords:
            return (coords[0][0], coords[0][1]), 0.0
        return (0.0, 0.0), 0.0
    seg_lens = []
    total = 0.0
    for i in range(len(coords) - 1):
        a, b = coords[i], coords[i + 1]
        d = haversine_km(a[0], a[1], b[0], b[1])
        seg_lens.append(d)
        total += d
    half = total / 2.0
    acc = 0.0
    seg_idx = len(coords) - 2
    t = 1.0
    for i, d in enumerate(seg_lens):
        if acc + d >= half:
            seg_idx = i
            t = (half - acc) / d if d > 0 else 0.0
            break
        acc += d
    a, b = coords[seg_idx], coords[seg_idx + 1]
    mid_lon = a[0] + (b[0] - a[0]) * t
    mid_lat = a[1] + (b[1] - a[1]) * t
    # Tangent in metric local frame, so angle is in real geography.
    cl = cos(radians(mid_lat)) or 1.0
    dx_m = (b[0] - a[0]) * 111320.0 * cl
    dy_m = (b[1] - a[1]) * 111320.0
    # MapLibre y-axis on screen is down; `text-rotate` with
    # `text-rotation-alignment: map` rotates clockwise from east in map
    # space (north up). atan2(-dy, dx) converts our north-up tangent
    # vector into that clockwise convention.
    tangent_deg = degrees(atan2(-dy_m, dx_m))
    return (mid_lon, mid_lat), tangent_deg

