"""Options picking + should-split-at-gap: choose which perpendicular bar
each cluster settles on and where a stack should split into pill+connector."""
from math import atan2, cos, degrees, pi, radians, sin, sqrt

from _state import *  # noqa: F401,F403
from _state import _DIAG_BARS, _STABBED_PAIRS  # underscore names skipped by *
from stops.extent import _funicular_snap_override, _length_key, _resolve_length
from geometry import _cum_dist_m, _directional_tangent_at, _interp_at, haversine_km
from stops.pill_zoom.geom import (
    _angular_dist_mod_pi, _circular_median_mod_pi, _expand_sigma_clump,
    _extent_intersect_axis, _mean_unit_tangent, _perpendicular_sweep,
    _place_dot_on_extent, _sigma_clumps, _smoothed_tangent_at, _stop_tangent,
    _tangent_groups,
)


def _apply_option(group, option, placed_ids, record_stabbed=True):
    """Place this option's scoring + covered dots on their extents. When
    `record_stabbed` is False (e.g. trial placements during single-group
    measurement), the (osm_id, stop_id) pairs are NOT pushed to
    _STABBED_PAIRS — that's reserved for the chosen option's final pass.

    The multi-group tie-break guarantees no member is in two chosen bars,
    so apply doesn't need its own anti-overlap guard — every member it
    places is genuinely a new placement.
    """
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


def _pick_options_multi_group(per_group_options, protection_m):
    """Pick one option per (clump, options, tgroup_id) entry.

    Reject combinations that violate either of:
      • Two bars in the SAME tangent group are within `protection_m` along
        the older bar's tangent direction (would draw as near-duplicate
        bars stacked on the same axis). Different tangent groups point in
        different directions and impose no along-tangent constraint on
        each other.
      • Any member appears in more than one chosen bar's scoring + covered
        set (would steal a stop from another bar).

    Score surviving combos by sum of pairwise bar-center distances; tie-
    break by total gtfs_dist. If no combination passes validity, fall back
    to picking each entry's min-gtfs_dist option independently — the
    structural guarantees are gone in that fallback, but it produces a
    deterministic result rather than nothing.
    """
    from itertools import product
    protection = protection_m / 111000.0

    def _valid(combo):
        # Same-tangent-group along-tangent guard.
        for i in range(len(combo)):
            tgi = per_group_options[i][2]
            cxi, cyi = combo[i]["bar_center"]
            txi, tyi = combo[i]["tx"], combo[i]["ty"]
            for j in range(i + 1, len(combo)):
                if per_group_options[j][2] != tgi:
                    continue
                cxj, cyj = combo[j]["bar_center"]
                proj = abs((cxj - cxi) * txi + (cyj - cyi) * tyi)
                if proj < protection:
                    return False
        # No member double-cover across the combo.
        seen = set()
        for i, opt in enumerate(combo):
            clump = per_group_options[i][0]
            for k in opt["scoring"]:
                mid = id(clump[k])
                if mid in seen:
                    return False
                seen.add(mid)
            for k in opt["covered"]:
                mid = id(clump[k])
                if mid in seen:
                    return False
                seen.add(mid)
        return True

    best = None
    best_key = None
    for combo in product(*(opts for _, opts, _ in per_group_options)):
        if not _valid(combo):
            continue
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

    if best is None:
        # No valid combination — pick each entry's local min-gtfs_dist
        # option. This degenerate fallback can produce overlap or close
        # bars, but it always returns something.
        best = tuple(min(opts, key=lambda o: o["gtfs_dist"])
                     for _, opts, _ in per_group_options)
    return list(best)


def _pick_option_single_group(group, options, cluster,
                               platforms, raw, gtfs_centroid,
                               cos_lat=1.0):
    """Pick a tied option from a single-group cluster.

    With ≥ 1 leftover: enumerate options, run leftover-fill per option,
    pick minimum pill + 0.5 × connector length, tie-break by gtfs_dist.

    With no leftovers: the length metric is degenerate — every tied option
    produces the same pill (the bar itself) and its measured length varies
    only with sub-mm float noise along the sweep. Skip the metric and pick
    by gtfs_dist directly.

    Cluster positions are reset to raw before returning so the outer caller
    can apply the chosen option cleanly.
    """
    # Function-level import: place.py imports from this module at top
    # level, so importing at module scope would be circular.
    from stops.pill_zoom.place import _leftover_fill, _measure_pill_geometry

    # Probe leftover count using the first option. All tied options share
    # the same scoring set by construction, and the covered set is stable
    # enough that the leftover bucket is the same across tied options.
    placed_ids = set()
    _apply_option(group, options[0], placed_ids, record_stabbed=False)
    has_leftovers = any(id(p) not in placed_ids for p in platforms)
    for s in cluster:
        s["lon"], s["lat"] = raw[id(s)]

    if not has_leftovers:
        return min(options, key=lambda o: o["gtfs_dist"])

    best = None
    best_key = None
    for option in options:
        for s in cluster:
            s["lon"], s["lat"] = raw[id(s)]
        placed_ids = set()
        _apply_option(group, option, placed_ids, record_stabbed=False)
        leftovers = [p for p in platforms if id(p) not in placed_ids]
        if leftovers:
            _leftover_fill(platforms, leftovers, placed_ids, raw,
                            gtfs_centroid, cos_lat=cos_lat)
        length = _measure_pill_geometry(cluster, cos_lat=cos_lat)
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
        least one platform whose local extent tangent (averaged over
        TANGENT_WINDOW_M at the dot position) is 90° ±PERP_PLATFORM_TOL_DEG
        from the gap direction — i.e. the gap lies along a bar's perpendicular axis,
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
    # one platform whose local extent tangent (TANGENT_WINDOW_M-averaged at
    # the dot position) is 90° ±PERP_PLATFORM_TOL_DEG from the gap direction,
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
        perp_sin_tol = sin(radians(PERP_PLATFORM_TOL_DEG))

        def _has_perp_platform(pos):
            for p in pos_to_platforms.get(pos, ()):
                # Local tangent at the dot position over a TANGENT_WINDOW_M
                # window — the full-extent chord can deviate several degrees
                # from the local direction on long curved approaches (Bern HB
                # western platforms), and the bar was placed using the local
                # tangent, so the perp test must use it too.
                tan = _stop_tangent(p)
                if tan is None:
                    continue
                tx_m = tan[0] * cos_lat
                ty_m = tan[1]
                tmag_m = sqrt(tx_m * tx_m + ty_m * ty_m)
                if tmag_m <= 0:
                    continue
                # |cos(angle to gap)| ≤ sin(tol)  ⇔  perpendicular ±tol.
                if abs(tx_m * gx_m + ty_m * gy_m) / tmag_m <= perp_sin_tol:
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


# Platform-overlap penalty: a pill or connector segment with both endpoints
# within ON_PLATFORM_TOL_M of the SAME platform extent has its base factor
# (1.0 for pills, 0.5 for connectors) scaled by ON_PLATFORM_PENALTY. The
# penalty discourages routing a pill or connector along a platform extent
# when an alternative configuration reaches the same dots without overlap.
ON_PLATFORM_TOL_M = 0.5
ON_PLATFORM_PENALTY = 2.0

