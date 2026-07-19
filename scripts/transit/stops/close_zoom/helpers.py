import json
from bisect import bisect_left
from collections import defaultdict
from math import atan2, cos, degrees, pi, radians, sin, sqrt

from _state import *  # noqa: F401,F403
from stops.extent import _platform_extent
from geometry import (
    _cum_dist_m, _directional_tangent_at, _interp_at, _meters_per_deg,
    _project_meters, _slice_polyline, flatten_coords, haversine_km,
)
from stops.close_zoom.constants import *  # noqa: F401,F403


def _variant_priority(v):
    """Sort value for variant representative selection and pill-arrow
    stacking: f_weighted (trips/h) for tram / bus / regional_bus,
    speed_kmh for rail-like modes."""
    if v.get("mode") in CLOSE_ZOOM_FREQ_PRIORITY_MODES:
        return v.get("f_weighted") or 0.0
    return v.get("speed_kmh") or 0.0


def _rail_direction_order(clusters):
    """Reorder a priority-sorted rail cluster list into the direction-outward
    stack described in stops-close-zoom.md § Rail: fastest cluster's
    tangent defines the "forward" direction, forward clusters queue
    fastest→slowest from the forward end, reverse clusters queue slowest→
    fastest from the backward end. Each cluster is stamped with
    `dir_forward` for the pill-arrow build to flip T on reverse pill-arrows. Called
    on both the sector-merge pool inside `_build_group_recs` and the
    same-curb merge pool for rail records."""
    if not clusters:
        return clusters
    fwd_ref = clusters[0]["tangent"]
    forwards, reverses = [], []
    for c in clusters:
        dot = (fwd_ref[0] * c["tangent"][0]
               + fwd_ref[1] * c["tangent"][1])
        if dot >= 0.0:
            c["dir_forward"] = True
            forwards.append(c)
        else:
            c["dir_forward"] = False
            reverses.append(c)
    return forwards + list(reversed(reverses))


def _local_offset_to_lonlat(cx, cy, dx_m, dy_m, cos_lat_cached=None):
    if cos_lat_cached is None:
        cos_lat_cached = cos(radians(cy))
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * cos_lat_cached
    if m_per_deg_lon <= 0.0:
        m_per_deg_lon = 111320.0
    return (cx + dx_m / m_per_deg_lon, cy + dy_m / m_per_deg_lat)


def _point_at_extrap(polyline, dists, t):
    """Point at arc position t (metres), extrapolating straight beyond the
    polyline ends using the end tangents. Returns (lon, lat)."""
    poly_max = dists[-1]
    if 0.0 <= t <= poly_max:
        return _interp_at(polyline, dists, t)
    if t < 0.0:
        tan = _directional_tangent_at(polyline, dists, 0.0)
        if tan is None:
            return (polyline[0][0], polyline[0][1])
        return (polyline[0][0] + tan[0] * t, polyline[0][1] + tan[1] * t)
    tan = _directional_tangent_at(polyline, dists, poly_max)
    if tan is None:
        return (polyline[-1][0], polyline[-1][1])
    ex = t - poly_max
    return (polyline[-1][0] + tan[0] * ex, polyline[-1][1] + tan[1] * ex)


def _unit_tangent_metric(polyline, dists, t, cos_lat):
    """Unit tangent at arc position t (clamped to the polyline range) as a
    metric (east, north) vector. None for degenerate geometry."""
    t = min(max(t, 0.0), dists[-1])
    tan = _directional_tangent_at(polyline, dists, t, window_m=20.0)
    if tan is None:
        return None
    dx = tan[0] * 111320.0 * cos_lat
    dy = tan[1] * 111320.0
    n = sqrt(dx * dx + dy * dy)
    if n <= 0.0:
        return None
    return (dx / n, dy / n)


def _sample_ts(dists, t0, t1, max_step_m=3.0):
    """Ordered arc positions from t0 to t1 (t0 < t1): endpoints, interior
    polyline vertices, plus subdivisions so no gap exceeds max_step_m."""
    ts = [t0]
    for d in dists:
        if t0 < d < t1:
            ts.append(d)
    ts.append(t1)
    out = [ts[0]]
    for a, b in zip(ts, ts[1:]):
        n = int((b - a) // max_step_m)
        for i in range(1, n + 1):
            out.append(a + (b - a) * i / (n + 1))
        out.append(b)
    return out


def _unit_chord_metric(A, B, cos_lat):
    """Unit metric direction from point A to point B (lon/lat pairs), or
    None if degenerate."""
    dxm = (B[0] - A[0]) * 111320.0 * cos_lat
    dym = (B[1] - A[1]) * 111320.0
    nrm = sqrt(dxm * dxm + dym * dym)
    if nrm < 1e-9:
        return None
    return (dxm / nrm, dym / nrm)


def _orient_rail_extent(ext, target_T, cos_lat):
    """Return `ext` with its point order aligned to `target_T` — the fastest
    cluster's direction of travel — reversing when the extent's overall
    chord opposes it. Rail-only: rail platforms are straight so a single-
    point tangent at the stop is a stable reference, and each pill-arrow's
    chevron end is picked (via the dir_forward flag downstream) relative
    to the axis's point order. If the axis was built from a borrowed slice
    running the other way — a terminal fallback from the arrival counter-
    part (RBS Bern), or a same-curb union that took its base from an
    opposite-running longer line (Bern SBB) — every chevron on the
    platform would be mirrored without this normalisation."""
    if len(ext) < 2:
        return ext
    ax = _unit_chord_metric(ext[0], ext[-1], cos_lat)
    if ax is None:
        return ext
    if ax[0] * target_T[0] + ax[1] * target_T[1] < 0.0:
        return list(reversed(ext))
    return ext


def _stop_course(extent, cos_lat, back_m, fwd_m, chord_w=10.0,
                 front_on_m=0.0):
    """Queue course for a pill-arrow stack: the stop position line `extent`
    extended DEAD STRAIGHT at both ends (rear by back_m, front by fwd_m
    metres) along the average direction (chord) of the extent's first /
    last chord_w metres. Pill-arrows whose span lies inside the extent thus
    derive their angle from the stop position line at their own segment;
    pill-arrows beyond it continue straight in the direction of the last pill-arrows
    that fit.

    NOTHING but the stop position line determines the placement — the raw
    GTFS stop coordinate is never consulted here.

    Orientation: the extent's own point order, always. For non-rail the
    extent ends at the stop position by construction (backward-anchored
    [t-L, t] slices and borrowed fills alike), so the front end is the
    stop end with no travel-direction guessing. For rail the caller has
    normalised the extent to align with the fastest cluster's tangent
    (see `_orient_rail_extent`), so the extent's forward end is the
    fastest line's destination direction and the per-pill-arrow flip against
    `dir_forward` picks the right chevron end. A previous version
    reversed non-rail orders too against the group's ±20 m travel
    tangent; that misfired at stops where the vehicle turns right after
    departing (Herrliberg Bhf West — tangent skewed ~north by the turn,
    course flipped, tip anchored at the wrong end).

    `front_on_m` marks how much of the extent's TAIL is forward-of-stop
    line geometry appended for the terminal platform stretch
    (stops-close-zoom.md § anchor): t_front then sits at the stop —
    the boundary between the stop position line proper and the appended
    forward geometry — instead of at the extent's last point, and the
    queue anchor shift (done by the caller) moves the stack onto the real
    forward geometry rather than onto a dead-straight rear extension.

    Returns (course_pts, course_dists, t_front, t_mid, t_rear) or None if
    degenerate. t_front is the stop position line's forward end in course
    arc coordinates — where the lead pill-arrow's chevron tip anchors (the
    vehicle pulled fully forward); t_mid is the line's middle — the rail
    stack center; t_rear is the extent's rear (buffer-side) end — the
    end-of-platform rail anchor (stops-close-zoom.md § anchor)."""
    pts = [tuple(p) for p in extent]
    if len(pts) < 2:
        return None
    d = _cum_dist_m(pts)
    if d[-1] <= 0.0:
        return None
    t_front = back_m + d[-1] - front_on_m
    t_mid = back_m + (d[-1] - front_on_m) / 2.0
    t_rear = back_m
    w = min(chord_w, d[-1])
    T0 = _unit_chord_metric(pts[0], _interp_at(pts, d, w), cos_lat)
    T1 = _unit_chord_metric(_interp_at(pts, d, d[-1] - w), pts[-1], cos_lat)
    if T0 is None or T1 is None:
        return None
    rear = _local_offset_to_lonlat(pts[0][0], pts[0][1],
                                   -back_m * T0[0], -back_m * T0[1], cos_lat)
    front = _local_offset_to_lonlat(pts[-1][0], pts[-1][1],
                                    fwd_m * T1[0], fwd_m * T1[1], cos_lat)
    course = [tuple(rear)] + pts + [tuple(front)]
    return course, _cum_dist_m(course), t_front, t_mid, t_rear


def _extent_overlap(extA, extB, cos_lat, lateral_threshold_m=1.0,
                     sample_step_m=2.0):
    """How much of A and B actually run close (perpendicular distance below
    lateral_threshold_m), not how far their projected footprints span.

    The earlier projection-based metric — the range on A's arc between the
    two projected positions of B's endpoints — mistook a single-point
    X-crossing for large overlap: when two extents cross at one point (a
    station-throat switch), B's endpoints project onto A on opposite sides
    of the crossing, so the arc range spans across the crossing even
    though the two never actually run together. Canonical failure: RE5 at
    Bern platform 21 fusing with SBB platform 12A-C, meeting only at a
    switch. The projection returned 50 % overlap with lateral 0.88 m at
    the crossing; both gates passed and the whole rail cluster collapsed.

    New metric: sample the SHORTER extent every sample_step_m along its
    arc; for each sample compute the perpendicular distance to the LONGER
    extent (nearest-point projection). Returns
        (close_frac, mean_lateral, ivA, ivB)
    - close_frac: fraction of shorter-extent samples whose distance is
      below lateral_threshold_m. 0 for an X-crossing; 1 for two extents
      truly running together over the shorter's whole length.
    - mean_lateral: mean perpendicular distance across close samples, or
      None if none.
    - ivA, ivB: arc-length intervals on A and B covering the LONGEST
      contiguous close range; (None, None) if no sample is close.
    Returns None only when either extent has zero length."""
    dA = _cum_dist_m(extA)
    dB = _cum_dist_m(extB)
    if dA[-1] <= 0.0 or dB[-1] <= 0.0:
        return None
    if dA[-1] <= dB[-1]:
        e_s, d_s, e_l, d_l, short_is_A = extA, dA, extB, dB, True
    else:
        e_s, d_s, e_l, d_l, short_is_A = extB, dB, extA, dA, False
    len_s = d_s[-1]
    n_samples = max(20, min(200, int(len_s / sample_step_m) + 1))
    step = len_s / (n_samples - 1)
    samples = []
    for k in range(n_samples):
        t_short = k * step
        p_s = _interp_at(e_s, d_s, t_short)
        t_long = _project_meters(p_s[0], p_s[1], e_l, d_l)
        p_l = _interp_at(e_l, d_l, t_long)
        dxm = (p_l[0] - p_s[0]) * 111320.0 * cos_lat
        dym = (p_l[1] - p_s[1]) * 111320.0
        samples.append((t_short, t_long, sqrt(dxm * dxm + dym * dym)))
    n_close = sum(1 for (_, _, lat) in samples if lat < lateral_threshold_m)
    close_frac = n_close / n_samples if n_samples else 0.0
    close_lats = [lat for (_, _, lat) in samples if lat < lateral_threshold_m]
    mean_lateral = (sum(close_lats) / len(close_lats)) if close_lats else None
    # Longest contiguous close run — arc intervals on shorter and longer.
    best_lo_s = best_hi_s = None
    best_lo_l = best_hi_l = None
    best_len = 0.0
    lo_s = lo_l = hi_l = None
    for (t_s, t_l, lat) in samples:
        if lat < lateral_threshold_m:
            if lo_s is None:
                lo_s = t_s
                lo_l = hi_l = t_l
            else:
                lo_l = min(lo_l, t_l)
                hi_l = max(hi_l, t_l)
            hi_s = t_s
        elif lo_s is not None:
            run_len = hi_s - lo_s
            if run_len > best_len:
                best_lo_s, best_hi_s = lo_s, hi_s
                best_lo_l, best_hi_l = lo_l, hi_l
                best_len = run_len
            lo_s = None
    if lo_s is not None:
        run_len = hi_s - lo_s
        if run_len > best_len:
            best_lo_s, best_hi_s = lo_s, hi_s
            best_lo_l, best_hi_l = lo_l, hi_l
    if best_lo_s is None:
        iv_short = iv_long = None
    else:
        iv_short = (best_lo_s, best_hi_s)
        iv_long = (best_lo_l, best_hi_l)
    if short_is_A:
        iv_A, iv_B = iv_short, iv_long
    else:
        iv_A, iv_B = iv_long, iv_short
    return close_frac, mean_lateral, iv_A, iv_B


def _union_extents(exts, cos_lat, chord_w=10.0):
    """Union stop position line for merged same-curb stops: the longest
    member line, extended straight along its end chords far enough to
    cover every other member's endpoints (all members run within ~2 m of
    each other, so projecting is safe)."""
    base = max(exts, key=lambda e: _cum_dist_m(e)[-1])
    d = _cum_dist_m(base)
    w = min(chord_w, d[-1])
    T0 = _unit_chord_metric(base[0], _interp_at(base, d, w), cos_lat)
    T1 = _unit_chord_metric(_interp_at(base, d, d[-1] - w), base[-1],
                            cos_lat)
    if T0 is None or T1 is None:
        return [tuple(p) for p in base]
    back = fwd = 0.0
    for e in exts:
        if e is base:
            continue
        for p in (e[0], e[-1]):
            dxm = (p[0] - base[0][0]) * 111320.0 * cos_lat
            dym = (p[1] - base[0][1]) * 111320.0
            s = dxm * T0[0] + dym * T0[1]
            if s < 0.0:
                back = max(back, -s)
            dxm = (p[0] - base[-1][0]) * 111320.0 * cos_lat
            dym = (p[1] - base[-1][1]) * 111320.0
            s = dxm * T1[0] + dym * T1[1]
            if s > 0.0:
                fwd = max(fwd, s)
    pts = [tuple(p) for p in base]
    if back > 0.0:
        pts.insert(0, tuple(_local_offset_to_lonlat(
            base[0][0], base[0][1], -back * T0[0], -back * T0[1], cos_lat)))
    if fwd > 0.0:
        pts.append(tuple(_local_offset_to_lonlat(
            base[-1][0], base[-1][1], fwd * T1[0], fwd * T1[1], cos_lat)))
    return pts


def _shorten_curb(rec, iv, min_len_m=2.0):
    """Shorten a same-curb rec's stop position line to the middle of the
    overlapping section `iv` (arc interval on the rec's own line), keeping
    the side away from the overlap so the two neighbours end up just
    touching. Records the cut point so the band loop can keep the queue
    from crossing it. No-op when the remainder would fall below
    min_len_m."""
    ext = rec["ext"]
    d = _cum_dist_m(ext)
    lo, hi = iv
    mid = (lo + hi) / 2.0
    if mid > d[-1] / 2.0:
        t0, t1 = 0.0, mid
    else:
        t0, t1 = mid, d[-1]
    if t1 - t0 < min_len_m:
        return
    rec["cut_pts"].append(tuple(_interp_at(ext, d, mid)))
    rec["ext"] = _slice_polyline(ext, d, t0, t1)


def _offset_track(polyline, dists, t_lo, t_hi, offset_m, cos_lat,
                  step_m=2.0):
    """Placement track for pill-arrows: the centerline sampled over arc
    range [t_lo, t_hi] (extrapolated past the ends) and shifted sideways
    by offset_m (positive = right of travel). Returns (pts, odists, cts)
    — the track polyline, its cumulative metre distances, and each
    sample's centerline arc position — or None if degenerate."""
    span = t_hi - t_lo
    if span <= 0.0:
        return None
    n = max(int(span / step_m) + 2, 2)
    pts, cts = [], []
    for i in range(n):
        t = t_lo + span * i / (n - 1)
        T = _unit_tangent_metric(polyline, dists, t, cos_lat)
        if T is None:
            continue
        px, py = _point_at_extrap(polyline, dists, t)
        pts.append(_local_offset_to_lonlat(
            px, py, offset_m * T[1], -offset_m * T[0], cos_lat))
        cts.append(t)
    if len(pts) < 2:
        return None
    return pts, _cum_dist_m(pts), cts


def _track_pos(x, xs, ys):
    """Piecewise-linear map of x through the monotonic table xs → ys,
    clamped at the ends. Converts centerline arc positions to track
    positions and back (pass (cts, odists) or (odists, cts))."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect_left(xs, x)
    x0, x1 = xs[i - 1], xs[i]
    if x1 <= x0:
        return ys[i]
    f = (x - x0) / (x1 - x0)
    return ys[i - 1] + f * (ys[i] - ys[i - 1])


def _build_straight_pill_arrow(origin, T, N, cos_lat, x_rear, x_neck,
                                perp_m, width_m, n_arc=16):
    """Straight pill-arrow in the local frame spanned by the unit tangent T
    (direction of travel) and right normal N at `origin` (lon, lat).

    The body spans frame positions x_rear..x_neck (metres along T) at
    lateral offset perp_m (positive = right of travel). A chevron tip of
    length width_m/2 extends beyond x_neck; a rounded cap closes x_rear.
    The whole shape shares ONE axis — no bending, so labels rotated by the
    same tangent align exactly with the body.

    Returns (ring, rear_center) — the closed polygon ring and the disc
    anchor at the body's rear centerline point — or None.
    """
    R = width_m / 2.0
    if x_neck <= x_rear:
        return None
    ox, oy = origin

    def pt(dx, dy):
        return _local_offset_to_lonlat(
            ox, oy, dx * T[0] + dy * N[0], dx * T[1] + dy * N[1], cos_lat)

    apex = pt(x_neck + R, perp_m)
    ring = [list(apex)]
    ring.append(list(pt(x_neck, perp_m - R)))
    ring.append(list(pt(x_rear, perp_m - R)))
    # Rounded back cap: half-circle from the inner edge over the back pole
    # to the outer edge.
    for i in range(1, n_arc):
        theta = pi * i / n_arc
        ring.append(list(pt(x_rear - R * sin(theta),
                            perp_m - R * cos(theta))))
    ring.append(list(pt(x_rear, perp_m + R)))
    ring.append(list(pt(x_neck, perp_m + R)))
    ring.append(list(apex))

    return ring, pt(x_rear, perp_m)


def _convex_hull_metric(pts):
    """Convex hull (Andrew monotone chain) of (x, y) metric points, in
    counter-clockwise order without the closing point."""
    pts = sorted(set(pts))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _rounded_hull_polygon(lonlat_pts, pad_m, arc_step_deg=12.0):
    """Rounded envelope around a point cloud: convex hull offset outward by
    pad_m, with circular arcs at the corners. The outline is convex
    everywhere — it never notches inward between the covered elements.
    Returns a closed [lon, lat] ring, or None for an empty/degenerate cloud.
    """
    if not lonlat_pts:
        return None
    lon0, lat0 = lonlat_pts[0]
    cl = cos(radians(lat0))
    if cl <= 0.0:
        cl = 1.0
    # To metric coords, deduped on a 0.5 m grid to keep the hull cheap.
    seen = set()
    metric = []
    for lon, lat in lonlat_pts:
        x = (lon - lon0) * 111320.0 * cl
        y = (lat - lat0) * 111320.0
        key = (round(x * 2.0), round(y * 2.0))
        if key in seen:
            continue
        seen.add(key)
        metric.append((x, y))
    hull = _convex_hull_metric(metric)
    if not hull:
        return None

    step = radians(arc_step_deg)
    out = []

    def _arc(cx, cy, a1, a2):
        """Arc points from angle a1 to a2 going counter-clockwise."""
        sweep = (a2 - a1) % (2.0 * pi)
        n = max(1, int(sweep / step))
        for j in range(n + 1):
            a = a1 + sweep * j / n
            out.append((cx + pad_m * cos(a), cy + pad_m * sin(a)))

    if len(hull) == 1:
        _arc(hull[0][0], hull[0][1], 0.0, 2.0 * pi - 1e-9)
    elif len(hull) == 2:
        # Capsule around the two points.
        (x1, y1), (x2, y2) = hull
        base = atan2(y2 - y1, x2 - x1)
        _arc(x2, y2, base - pi / 2.0, base + pi / 2.0)
        _arc(x1, y1, base + pi / 2.0, base + 3.0 * pi / 2.0)
    else:
        m_ = len(hull)

        def _norm_out(a, b):
            dx, dy = b[0] - a[0], b[1] - a[1]
            l = sqrt(dx * dx + dy * dy)
            if l <= 0.0:
                return None
            # CCW polygon → outward normal is right of the edge direction.
            return (dy / l, -dx / l)

        for i in range(m_):
            p_prev = hull[(i - 1) % m_]
            p = hull[i]
            p_next = hull[(i + 1) % m_]
            n_in = _norm_out(p_prev, p)
            n_out = _norm_out(p, p_next)
            if n_in is None or n_out is None:
                continue
            _arc(p[0], p[1], atan2(n_in[1], n_in[0]), atan2(n_out[1], n_out[0]))

    if len(out) < 3:
        return None
    ring = [list(_local_offset_to_lonlat(lon0, lat0, x, y, cl))
            for (x, y) in out]
    ring.append(ring[0])
    return ring


def _blend_colors(hex_colors) -> str:
    """Mean-RGB blend of a list of #rrggbb colors."""
    rs = gs = bs = 0
    n = 0
    for h in hex_colors:
        h = (h or "").lstrip("#")
        if len(h) != 6:
            continue
        try:
            rs += int(h[0:2], 16)
            gs += int(h[2:4], 16)
            bs += int(h[4:6], 16)
            n += 1
        except ValueError:
            continue
    if n == 0:
        return "#b340c9"
    return "#%02x%02x%02x" % (rs // n, gs // n, bs // n)


def _closest_way_distance_m(idx, lon, lat, tag_key, tag_values,
                             search_radius_m):
    """Nearest OSM way from `idx` whose properties[`tag_key`] is one of
    `tag_values`, measured as projected-point distance in metres from
    (lon, lat). Returns (min_distance_m, best_way_idx) or (None, None) if
    no such way is inside `search_radius_m`. Used by the hybrid tram
    detection to compare narrow_gauge/light_rail vs. tram proximity at a
    stop's projected position on its shape."""
    if idx is None:
        return None, None
    candidates = idx.query_radius(lon, lat, search_radius_m)
    if not candidates:
        return None, None
    cos_lat_here = cos(radians(lat))
    if cos_lat_here <= 0.0:
        cos_lat_here = 1.0
    values = set(tag_values)
    best = None
    for w_idx in candidates:
        props = idx.way_props[w_idx]
        if (props.get(tag_key) or "") not in values:
            continue
        coords = idx.ways[w_idx]
        dists = idx.way_dists[w_idx]
        t_proj = _project_meters(lon, lat, coords, dists)
        px, py = _interp_at(coords, dists, t_proj)
        dxm = (px - lon) * 111320.0 * cos_lat_here
        dym = (py - lat) * 111320.0
        d = sqrt(dxm * dxm + dym * dym)
        if best is None or d < best[0]:
            best = (d, w_idx)
    if best is None:
        return None, None
    return best


def _is_hybrid_tram_stop(shape_lon, shape_lat, rail_idx, tram_idx,
                          tolerance_m=CLOSE_ZOOM_HYBRID_TRAM_TOL_M):
    """Return True if the tram's shaped position (shape_lon, shape_lat) is
    within `tolerance_m` of a `railway=narrow_gauge` / `railway=light_rail`
    OSM way AND that way is closer than any `railway=tram` way at the same
    point. See stops-close-zoom.md § "Hybrid tram detection".

    The tram check is a tie-breaker for transitions where both tram track
    and narrow_gauge / light_rail track are nearby (Forchbahn at the
    Zürich inner-network / outer-alignment boundary): whichever way is
    closer to the projected position wins.
    """
    if rail_idx is None:
        return False
    # Search a bit wider than the tolerance so the tram tie-breaker can
    # still fire — a tram way at 3 m still beats a narrow_gauge way at
    # 1.5 m even though only the latter is inside tolerance.
    search_r = max(tolerance_m * 3.0, 5.0)
    rail_d, _ = _closest_way_distance_m(
        rail_idx, shape_lon, shape_lat, "railway",
        {"narrow_gauge", "light_rail"}, search_r)
    if rail_d is None or rail_d > tolerance_m:
        return False
    tram_d, _ = _closest_way_distance_m(
        tram_idx, shape_lon, shape_lat, "railway", {"tram"}, search_r)
    if tram_d is not None and tram_d < rail_d:
        return False
    return True
