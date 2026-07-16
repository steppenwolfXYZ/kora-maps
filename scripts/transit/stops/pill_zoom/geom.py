"""Pill-zoom platform-geometry helpers: tangents, sigma clumps,
tangent groups, the perpendicular sweep."""
from bisect import bisect_left
from math import atan2, cos, degrees, pi, radians, sin, sqrt

from _state import *  # noqa: F401,F403
from stops.extent import TANGENT_WINDOW_M, _platform_extent
from geometry import (
    _cum_dist_m, _directional_tangent_at, _interp_at, _meters_per_deg,
    _project_meters, _slice_polyline, flatten_coords, haversine_km,
    snap_to_line,
)


def _stop_tangent(s):
    """Unit polyline tangent at the stop's snap position, averaged over a
    ±TANGENT_WINDOW_M/2 window centred on (s["lon"], s["lat"]) projected
    onto its extent. Canonicalised to the upper half-plane so opposite-
    direction polylines don't cancel. Returns None when the extent is
    degenerate or the polyline is too short to compute a tangent.
    """
    ext = s.get("extent")
    if not ext or len(ext) < 2:
        return None
    dists = _cum_dist_m(ext)
    if dists[-1] <= 0:
        return None
    t = _project_meters(s["lon"], s["lat"], ext, dists)
    tan = _smoothed_tangent_at(ext, dists, t, window_m=TANGENT_WINDOW_M)
    if tan is None:
        return None
    dx, dy = tan
    mag = sqrt(dx * dx + dy * dy)
    if mag <= 0:
        return None
    if dx < 0 or (dx == 0 and dy < 0):
        dx, dy = -dx, -dy
    return (dx / mag, dy / mag)


def _mean_unit_tangent(cluster: list):
    """Mean unit tangent across stops in the cluster, computed at each
    stop's snap position via _stop_tangent. Returns (tx, ty) or None if no
    usable stops.
    """
    ax = ay = 0.0
    n = 0
    for s in cluster:
        t = _stop_tangent(s)
        if t is None:
            continue
        ax += t[0]
        ay += t[1]
        n += 1
    if n == 0:
        return None
    mag = sqrt(ax*ax + ay*ay)
    if mag <= 0:
        return None
    return (ax / mag, ay / mag)


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


def _circular_median_mod_pi(angles, reference):
    """Median of angles on the half-circle [0, π), computed as signed offsets
    from `reference` in [-π/2, π/2). Robust to a single outlier angle.
    `None` entries are skipped; if nothing remains, returns `reference`.

    Caller ensures inputs lie within π/2 of `reference` — true for σ-clump
    members, which the tangent-group gate keeps within ~10° of each other.
    """
    offsets = []
    for a in angles:
        if a is None:
            continue
        d = (a - reference) % pi
        if d > pi / 2:
            d -= pi
        offsets.append(d)
    if not offsets:
        return reference
    offsets.sort()
    n = len(offsets)
    if n % 2 == 1:
        med = offsets[n // 2]
    else:
        med = 0.5 * (offsets[n // 2 - 1] + offsets[n // 2])
    return (reference + med) % pi


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
# Tolerance on the σ-projection scoring check. A member whose σ-range boundary
# coincides with the sweep position can drop out by float-precision noise; this
# slack keeps it in scoring. Sized larger than pure float noise so it also
# absorbs minor pfaedle-routing jitter on the polyline tangent.
SIGMA_BOUNDARY_TOL_M = 0.5
PROTECTION_RADIUS_RAIL_M = 30.0
PROTECTION_RADIUS_NONRAIL_M = 5.0


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


def _expand_sigma_clump(clump, angle_tol_rad, raw, lone_outlier_gap_m):
    """Recursively run the perpendicular sweep on a σ-clump, peeling off the
    matched members after each pass and re-σ-clumping the rest. Yields one
    (sub_clump, options) pair per discovered bar.

    Catches σ-clumps that contain two parallel sub-clusters on different
    transverse axes — both share enough σ-overlap to stay in one σ-clump,
    but no single bar can stab both. The first sweep finds one sub-cluster,
    the rerun finds the other.

    `raw` is the cluster-level raw[id(p)] → (lon, lat) snapshot of pre-
    placement positions. Used for the distinct-position gate: members
    sharing a snapped GTFS position count once. The recursion terminates
    when the next sweep finds no candidate, or fewer than two distinct-
    position members remain.

    Peel-off uses the local pick (min gtfs_dist among tied options). The
    cluster-level tie-break may later choose a different option from the
    tied set whose matched set differs; any resulting overlap (or near-
    duplicate along-tangent placement) is rejected by the tie-break's
    combination validity check, not pre-filtered here.
    """
    if len(clump) < 2:
        return
    if len({raw[id(p)] for p in clump}) < 2:
        return
    options = _perpendicular_sweep(clump, angle_tol_rad, lone_outlier_gap_m)
    if not options:
        return

    yield (clump, options)

    chosen = min(options, key=lambda o: o["gtfs_dist"])
    matched_ids = {id(clump[k]) for k in chosen["scoring"]}
    matched_ids.update(id(clump[k]) for k in chosen["covered"])
    remaining = [p for p in clump if id(p) not in matched_ids]

    for sub in _sigma_clumps(remaining):
        yield from _expand_sigma_clump(sub, angle_tol_rad, raw, lone_outlier_gap_m)


def _perpendicular_sweep(group, angle_tol_rad, lone_outlier_gap_m):
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

    # Per-member extent + cum-dist cache (used per sweep step for the
    # closest-point projection and local tangent computation).
    member_exts = []
    member_dists_list = []
    central_idx = None
    for k, p in enumerate(group):
        ext = p.get("extent")
        if ext and len(ext) >= 2:
            member_exts.append(ext)
            member_dists_list.append(_cum_dist_m(ext))
        else:
            member_exts.append(None)
            member_dists_list.append(None)
        if p is central:
            central_idx = k

    # Single pass: compute scoring (≤10°-aligned members crossing bar) AND
    # accidentally-covered members (wrong-angle, crossing within scoring-set
    # drawn span). The stab count counts BOTH — wrong-angle members on the
    # bar's drawn span are real placements and contribute. The bar's drawn
    # span is still determined by scoring members only (no extension for
    # wrong-angle members).
    gap_thresh = lone_outlier_gap_m / 111000.0
    dedup_tol = DEDUP_TOL_M / 111000.0
    sigma_tol = SIGMA_BOUNDARY_TOL_M / 111000.0
    best_count = 0
    raw_tied = []
    for arc_d in candidate_arcs:
        pos = _interp_at(central_ext, central_dists, arc_d)

        # Per-position consensus bar angle: each σ-clump member's local
        # tangent at the closest point on its own extent to `pos`,
        # TANGENT_WINDOW_M-smoothed, then circular median (mod π) across
        # all members. Curvature-aware (each member contributes its local
        # direction at the bar's location, not its overall extent chord)
        # and robust to one outlier whose pfaedle shape is rotated.
        member_angles = []
        for k in range(n):
            m_ext = member_exts[k]
            if m_ext is None:
                member_angles.append(None)
                continue
            m_dists = member_dists_list[k]
            m_arc = _project_meters(pos[0], pos[1], m_ext, m_dists)
            m_tan = _smoothed_tangent_at(m_ext, m_dists, m_arc,
                                          TANGENT_WINDOW_M)
            if m_tan is None:
                member_angles.append(None)
                continue
            member_angles.append(atan2(m_tan[1], m_tan[0]))
        ref_angle = (member_angles[central_idx]
                     if central_idx is not None
                     else None)
        if ref_angle is None:
            for a in member_angles:
                if a is not None:
                    ref_angle = a
                    break
        if ref_angle is None:
            continue
        bar_angle = _circular_median_mod_pi(member_angles, ref_angle)
        tx, ty = cos(bar_angle), sin(bar_angle)
        sigma = pos[0] * tx + pos[1] * ty
        nx, ny = -ty, tx

        # Phase 1: scoring members
        scoring = []
        for k, p in enumerate(group):
            ma = member_angles[k]
            if ma is None:
                continue
            if _angular_dist_mod_pi(ma, bar_angle) > angle_tol_rad:
                continue
            ext = p["extent"]
            ts = [v[0] * tx + v[1] * ty for v in ext]
            if min(ts) - sigma_tol <= sigma <= max(ts) + sigma_tol:
                scoring.append(k)
        if len(scoring) < 2:
            continue
        # ≥ 2 distinct platform positions among scoring members (bar's drawn
        # anchors). Wrong-angle members are not anchors and aren't counted.
        distinct_positions = {
            (round(group[k]["lon"], 6), round(group[k]["lat"], 6))
            for k in scoring
        }
        if len(distinct_positions) < 2:
            continue

        # Drawn span from scoring members
        scoring_pts = [
            _place_dot_on_extent(group[k]["extent"], tx, ty, sigma)
            for k in scoring
        ]
        scoring_n = [pt[0] * nx + pt[1] * ny for pt in scoring_pts]

        # Lone-outlier drop: any scoring member on a single-distinct-
        # position side of a ≥ lone_outlier_gap_m gap along the bar axis
        # is dropped from this candidate's scoring set. Repeats because
        # removing a dot can expose a new wide gap. Dropped members re-
        # enter the σ-clump's unplaced pool via the recursive rerun →
        # leftover-fill path (where an isolated platform belongs).
        while len(scoring) >= 2:
            order = sorted(range(len(scoring)), key=lambda i: scoring_n[i])
            sn = [scoring_n[i] for i in order]
            # Cluster successive sorted entries within dedup_tol into one
            # distinct bar-axis position.
            pos_groups = [[order[0]]]
            for j in range(1, len(order)):
                if sn[j] - sn[j - 1] <= dedup_tol:
                    pos_groups[-1].append(order[j])
                else:
                    pos_groups.append([order[j]])
            drop = None
            for gi in range(len(pos_groups) - 1):
                gap = (scoring_n[pos_groups[gi + 1][0]]
                       - scoring_n[pos_groups[gi][-1]])
                if gap < gap_thresh:
                    continue
                # gi + 1 distinct positions on the left side of this gap;
                # the remaining pos_groups on the right.
                if gi + 1 == 1:
                    drop = set(pos_groups[0])
                    break
                if len(pos_groups) - (gi + 1) == 1:
                    drop = set(pos_groups[-1])
                    break
            if drop is None:
                break
            scoring = [s for i, s in enumerate(scoring) if i not in drop]
            scoring_pts = [s for i, s in enumerate(scoring_pts)
                           if i not in drop]
            scoring_n = [s for i, s in enumerate(scoring_n) if i not in drop]

        if len(scoring) < 2:
            continue
        # Re-check distinct platform positions on the post-drop scoring set.
        distinct_positions = {
            (round(group[k]["lon"], 6), round(group[k]["lat"], 6))
            for k in scoring
        }
        if len(distinct_positions) < 2:
            continue

        n_min, n_max = min(scoring_n), max(scoring_n)

        # Phase 2: wrong-angle members whose extent crosses the bar within
        # the scoring-set drawn span. These count toward the stab total but
        # do NOT influence n_min / n_max — the bar is not extended for them.
        scoring_set = set(scoring)
        covered = []
        covered_pts = []
        for k, p in enumerate(group):
            if k in scoring_set:
                continue
            ma = member_angles[k]
            if ma is None:
                continue
            # Only wrong-angle members are eligible for covered (scoring set
            # already takes the aligned ones).
            if _angular_dist_mod_pi(ma, bar_angle) <= angle_tol_rad:
                continue
            ext = p["extent"]
            ts = [v[0] * tx + v[1] * ty for v in ext]
            if not (min(ts) - sigma_tol <= sigma <= max(ts) + sigma_tol):
                continue
            cross_pt = _extent_intersect_axis(ext, tx, ty, sigma)
            if cross_pt is None:
                continue
            n_val = cross_pt[0] * nx + cross_pt[1] * ny
            if n_min <= n_val <= n_max:
                covered.append(k)
                covered_pts.append(cross_pt)

        total = len(scoring) + len(covered)
        entry = (tx, ty, sigma, scoring, covered,
                 scoring_pts, covered_pts)
        if total > best_count:
            best_count = total
            raw_tied = [entry]
        elif total == best_count:
            raw_tied.append(entry)
    if not raw_tied:
        return None

    # Second pass: enrich each tied position with bar center + gtfs-distance.
    options = []
    for tx, ty, sigma, scoring, covered, scoring_pts, covered_pts in raw_tied:
        bar_cx = sum(pt[0] for pt in scoring_pts) / len(scoring_pts)
        bar_cy = sum(pt[1] for pt in scoring_pts) / len(scoring_pts)

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


