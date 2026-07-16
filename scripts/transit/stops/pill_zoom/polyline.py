"""Pill-polyline utilities: lonlat/xy conversions, kink removal, adaptive
simplification, mid-attach candidates."""
from math import atan2, cos, degrees, floor, log, pi, radians, sin, sqrt

from _state import *  # noqa: F401,F403
from _state import _M_PER_DEG  # underscore names skipped by *
from geometry import haversine_km


def _lonlat_to_xy(lon, lat, lon0, lat0, cos_lat):
    """Equal-distance metric frame anchored at (lon0, lat0)."""
    return ((lon - lon0) * cos_lat * _M_PER_DEG,
            (lat - lat0) * _M_PER_DEG)


def _xy_to_lonlat(x, y, lon0, lat0, cos_lat):
    return (lon0 + x / (cos_lat * _M_PER_DEG),
            lat0 + y / _M_PER_DEG)


def _rotate2(v, ang):
    c, s = cos(ang), sin(ang)
    return (v[0] * c - v[1] * s, v[0] * s + v[1] * c)


def _norm2(v):
    m = sqrt(v[0] * v[0] + v[1] * v[1])
    if m < 1e-12:
        return None
    return (v[0] / m, v[1] / m)


def _polyline_length_xy(poly):
    total = 0.0
    for i in range(len(poly) - 1):
        dx = poly[i + 1][0] - poly[i][0]
        dy = poly[i + 1][1] - poly[i][1]
        total += sqrt(dx * dx + dy * dy)
    return total


def _remove_pill_kinks(coords, cos_lat, pill_diameter_m):
    """Post-DP cleanup: iteratively drop any interior vertex where both
    adjacent arm segments are shorter than the pill's rendered diameter
    at the current band's target zoom.

    Rationale: when an interior arm is shorter than the pill's width,
    the arm can't extend beyond the round join at the corner — it's
    buried inside the pill body — so the vertex can only produce a
    "kink" bulge, never a real corner. A real L- or T-shape platform
    has arms much longer than the pill diameter, so its corner is
    preserved. No angle threshold: a shallow bend with sub-diameter
    arms is still a bulge; a steep bend with long arms is still a
    corner.

    `pill_diameter_m` is the pill's estimated diameter in metres for
    the current cluster and band (computed from `PILL_MIN_D_PX`,
    `PILL_SLOPE_PX_PER_WB`, `PILL_BAND_ZOOM`, cluster wb, and cluster
    cos_lat). Convergence: at most one vertex dropped per pass; loop
    until stable.
    """
    if len(coords) <= 2 or pill_diameter_m is None or pill_diameter_m <= 0:
        return list(coords)
    result = list(coords)
    while len(result) >= 3:
        lon0 = result[0][0]
        lat0 = result[0][1]
        xy = [_lonlat_to_xy(p[0], p[1], lon0, lat0, cos_lat) for p in result]
        drop_i = -1
        for i in range(1, len(xy) - 1):
            dx_in = xy[i][0] - xy[i-1][0]
            dy_in = xy[i][1] - xy[i-1][1]
            dx_out = xy[i+1][0] - xy[i][0]
            dy_out = xy[i+1][1] - xy[i][1]
            arm_in = sqrt(dx_in * dx_in + dy_in * dy_in)
            arm_out = sqrt(dx_out * dx_out + dy_out * dy_out)
            if arm_in < pill_diameter_m and arm_out < pill_diameter_m:
                drop_i = i
                break
        if drop_i < 0:
            break
        result = result[:drop_i] + result[drop_i+1:]
    return result


def _current_pill_diameter_m(wb, cos_lat):
    """Pill diameter estimate in metres for a stop with width_base `wb`
    at the currently-set band's target zoom (via `_set_pill_design_band`)
    and the given cluster latitude. Formula matches the paint expression
    in `generate_style.py`: `min_d + slope × min(wb, WB_HIGH)`, then
    convert pixels to metres using the standard Web-Mercator m/px."""
    wb_clamped = min(wb, PILL_WB_HIGH)
    d_px = PILL_MIN_D_PX + PILL_SLOPE_PX_PER_WB * wb_clamped
    # Web-Mercator m/px at latitude with MapLibre's 512-px tiles.
    # Matches `apply_stop_dedup`'s `m_per_px` (EARTH_M = 40075016.7).
    mp_per_px = (40075016.7 * cos_lat) / (512.0 * (2 ** PILL_BAND_ZOOM))
    return d_px * mp_per_px


def _simplify_pill_lonlat(coords, cos_lat, tol_m=None, pill_diameter_m=None):
    """Douglas-Peucker simplification of a pill polyline followed by
    kink removal. DP drops interior vertices whose perpendicular
    deviation from the chord through kept neighbours is below `tol_m`;
    `_remove_pill_kinks` then drops any surviving vertex whose adjacent
    arm segments are both shorter than `pill_diameter_m` (the pill's
    rendered diameter at the current band's target zoom). Works in
    metric (x, y) space anchored at the first vertex so the tolerance
    is in true metres.

    `tol_m=None` reads the current band's `PILL_SIMPLIFY_TOL_M` at call
    time (band-swapped via `_set_pill_design_band`).
    `pill_diameter_m=None` disables kink removal — callers that don't
    know the pill diameter (e.g. `_tangent_candidates` when the wb
    isn't threaded through) get DP-only simplification.
    """
    if tol_m is None:
        tol_m = PILL_SIMPLIFY_TOL_M
    if len(coords) <= 2:
        return list(coords)
    lon0 = coords[0][0]
    lat0 = coords[0][1]
    xy = [_lonlat_to_xy(p[0], p[1], lon0, lat0, cos_lat) for p in coords]
    n = len(xy)
    keep = [False] * n
    keep[0] = True
    keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        if e - s < 2:
            continue
        A = xy[s]
        B = xy[e]
        dx = B[0] - A[0]
        dy = B[1] - A[1]
        L2 = dx * dx + dy * dy
        max_d = 0.0
        max_i = -1
        if L2 < 1e-12:
            for i in range(s + 1, e):
                px = xy[i][0] - A[0]
                py = xy[i][1] - A[1]
                d = sqrt(px * px + py * py)
                if d > max_d:
                    max_d = d
                    max_i = i
        else:
            inv_L = 1.0 / sqrt(L2)
            for i in range(s + 1, e):
                cross = (xy[i][0] - A[0]) * dy - (xy[i][1] - A[1]) * dx
                d = abs(cross) * inv_L
                if d > max_d:
                    max_d = d
                    max_i = i
        if max_d > tol_m and max_i >= 0:
            keep[max_i] = True
            stack.append((s, max_i))
            stack.append((max_i, e))
    simplified = [coords[i] for i in range(n) if keep[i]]
    if pill_diameter_m is None:
        return simplified
    return _remove_pill_kinks(simplified, cos_lat, pill_diameter_m)


def _dedup_polyline_xy(poly, tol_m=DEDUP_TOL_M):
    """Collapse adjacent metric-space vertices closer than `tol_m` to a
    single vertex (first-seen wins). MapLibre's line tessellation produces
    visible wobble artifacts at z18+ when adjacent polyline vertices sit
    within line-width of each other — any curve that ends up with
    micrometre-spaced or exact-duplicate samples (recovery-shrunk arc with
    tiny chosen_L, a sub-half-metre `sA` stub that straddles the include
    threshold, etc.) is cleaned up before reaching tippecanoe.
    """
    if len(poly) < 2:
        return list(poly)
    out = [poly[0]]
    tol_sq = tol_m * tol_m
    for p in poly[1:]:
        dx = p[0] - out[-1][0]
        dy = p[1] - out[-1][1]
        if dx * dx + dy * dy > tol_sq:
            out.append(p)
    return out


def _pill_mid_attach_candidates(simp, cluster_cos_lat, max_cos_bend=0.5):
    """For a simplified pill polyline (>= 3 vertices), return a list of
    (pos, outer_tangent_xy) for interior vertices where the bend angle
    is at least 60° (cos(bend) <= 0.5 with the default). Bend angle is
    the deviation from a straight continuation: 0° = straight, 90° =
    right angle, 180° = U-turn.

    `pos` is the (lon, lat) tuple of the vertex.
    `outer_tangent_xy` is the outward unit tangent in cluster-xy space
    (opposite the bisector of the interior angle, so it points away
    from the concave side of the corner).
    """
    if len(simp) < 3:
        return []
    lon0, lat0 = simp[0][0], simp[0][1]
    xy = [_lonlat_to_xy(p[0], p[1], lon0, lat0, cluster_cos_lat) for p in simp]
    out = []
    for i in range(1, len(simp) - 1):
        prev_xy, v_xy, next_xy = xy[i - 1], xy[i], xy[i + 1]
        u_in = _norm2((v_xy[0] - prev_xy[0], v_xy[1] - prev_xy[1]))
        u_out = _norm2((next_xy[0] - v_xy[0], next_xy[1] - v_xy[1]))
        if u_in is None or u_out is None:
            continue
        cos_bend = u_in[0] * u_out[0] + u_in[1] * u_out[1]
        if cos_bend > max_cos_bend:
            continue  # not bent enough
        u_v_prev = _norm2((prev_xy[0] - v_xy[0], prev_xy[1] - v_xy[1]))
        u_v_next = _norm2((next_xy[0] - v_xy[0], next_xy[1] - v_xy[1]))
        if u_v_prev is None or u_v_next is None:
            continue
        bisector = (u_v_prev[0] + u_v_next[0], u_v_prev[1] + u_v_next[1])
        outer = _norm2((-bisector[0], -bisector[1]))
        if outer is None:
            continue
        out.append((simp[i], outer))
    return out

