"""Backward-borrow tiers of the tram/bus missing-range fill: sibling
borrow, non-sibling borrow, and the _AllLinesIndex spatial grid backing
the non-sibling tier. See stop-extent-osm-walk.md."""
from collections import defaultdict
from math import acos, cos, degrees, radians, sqrt

from geometry import (
    _cum_dist_m, _directional_tangent_at, _interp_at,
    _project_meters, _slice_polyline, haversine_km,
)

# Missing-range fill (tram/bus/regional_bus): borrow gates.
SIBLING_PROXIMITY_M = 1.0
# Non-sibling tier admission gate. 45° (was 15°) because the own anchor
# tangent can be rotated toward an imminent turn (Herrliberg Bhf West: the
# 974 departs into its loop, skewing the tangent 34° off the road, which
# rejected the perfect 972 donor ending at the stop). Ranking is by best
# angle first, so worse-angle candidates admitted by the looser gate are
# only used when nothing better fits; perpendicular crossings (~90°,
# Sevgein) stay rejected.
SIBLING_ANGLE_TOL_RAD = radians(45.0)
SIBLING_MAX_TURN_DEG = 120.0           # both tiers: reject candidates whose
                                        # local turn at q_t exceeds this — the
                                        # aligned/reversed cos_ang sign becomes
                                        # unstable at hairpins.


def _local_turn_angle_deg(polyline, dists, t, window_m=2.0):
    """Turn angle in degrees between the polyline's backward and forward
    one-sided tangents at arc-length t. 0 = straight-through, 180 = full
    U-turn. Returns None when either side of the window is too short for
    a stable estimate.
    """
    poly_max = dists[-1]
    if poly_max <= 0:
        return None
    back_lo = max(0.0, t - window_m)
    fwd_hi = min(poly_max, t + window_m)
    back_arc = t - back_lo
    fwd_arc = fwd_hi - t
    if back_arc < window_m * 0.5 or fwd_arc < window_m * 0.5:
        return None
    p_back = _interp_at(polyline, dists, back_lo)
    p_mid = _interp_at(polyline, dists, t)
    p_fwd = _interp_at(polyline, dists, fwd_hi)
    b_dx = p_mid[0] - p_back[0]
    b_dy = p_mid[1] - p_back[1]
    f_dx = p_fwd[0] - p_mid[0]
    f_dy = p_fwd[1] - p_mid[1]
    b_mag = sqrt(b_dx * b_dx + b_dy * b_dy)
    f_mag = sqrt(f_dx * f_dx + f_dy * f_dy)
    if b_mag == 0 or f_mag == 0:
        return None
    cos_ang = (b_dx * f_dx + b_dy * f_dy) / (b_mag * f_mag)
    cos_ang = max(-1.0, min(1.0, cos_ang))
    return degrees(acos(cos_ang))


def _projections_at(anchor_lon, anchor_lat, cand_poly, cand_dists,
                     collect_m=SIBLING_PROXIMITY_M, merge_arc_m=20.0):
    """Return every `q_t` at which `cand_poly` passes within `collect_m` of
    the anchor — one entry per distinct pass (local minimum of distance),
    merged when consecutive qualifying segments lie within `merge_arc_m` of
    arc-length. A polyline that visits the anchor several times (a loop
    departing, transiting mid-route, and returning — Bad Zurzach's bus 4 is
    the canonical case) exposes a distinct tangent at each pass; each must
    enter the borrow ranking separately, because the pass whose walk fits is
    often not the globally nearest one.

    Falls back to the single global-nearest projection when no pass is
    within `collect_m` — the caller's proximity gate then rejects it, which
    keeps calling code free of a special empty case.
    """
    passes = []  # [q_t_of_best_point, best_dist_m, t_of_last_qualifying_seg]
    for i in range(len(cand_poly) - 1):
        ax, ay = cand_poly[i]
        bx, by = cand_poly[i + 1]
        dx, dy = bx - ax, by - ay
        seg_sq = dx * dx + dy * dy
        if seg_sq == 0:
            tt = 0.0
        else:
            tt = max(0.0, min(1.0, ((anchor_lon - ax) * dx
                                     + (anchor_lat - ay) * dy) / seg_sq))
        cx, cy = ax + tt * dx, ay + tt * dy
        dist_m = haversine_km(anchor_lon, anchor_lat, cx, cy) * 1000.0
        if dist_m > collect_m:
            continue
        t_here = cand_dists[i] + tt * (cand_dists[i + 1] - cand_dists[i])
        if passes and t_here - passes[-1][2] < merge_arc_m:
            if dist_m < passes[-1][1]:
                passes[-1][0] = t_here
                passes[-1][1] = dist_m
            passes[-1][2] = t_here
        else:
            passes.append([t_here, dist_m, t_here])
    if not passes:
        return [_project_meters(anchor_lon, anchor_lat, cand_poly, cand_dists)]
    return [p[0] for p in passes]


def _borrow_backward_segment(anchor_lon, anchor_lat,
                              anchor_dx, anchor_dy, t_on_self, L,
                              siblings, self_oid):
    """Try to borrow the missing `L - t_on_self` metres of backward extent from
    a same-line sibling's polyline. The search anchor is the on-polyline
    extent's backward endpoint — i.e. `poly[0]`, the far end reached after
    consuming the on-polyline portion — not the snap of the stop. This way,
    when the stop sits mid-polyline (t > 0), we still look for candidates at
    the point where the extension actually begins.

    Returns the borrowed sequence (lon, lat) in backward→forward order, in
    the donor polyline's true coordinates — never translated onto the
    anchor, so the fill always lies exactly on a drawn line (a sub-gate jog
    at the join is acceptable; a parallel offset next to the line is not).
    Returns None if nothing qualifies.

    Gates (concept § Missing-range fill, step 1):
      • SIBLING_PROXIMITY_M proximity between the anchor and the sibling's
        nearest-point projection of the anchor (rejects parallel-street
        variants).
      • Local turn at the sibling's nearest point ≤ SIBLING_MAX_TURN_DEG
        (rejects hairpins where the aligned/reversed sign of cos_ang is
        numerically unstable).
      • NO hard tangent-agreement gate. Same-line siblings are the same route
        by definition, so a big local angle at the shared point is a real
        turn (Ardez Bröl-style corner terminal), not a wrong-alignment
        signal. Candidates are ranked by tangent match instead: highest
        `|cos(angle)|` wins, ties broken by shorter proximity.

    Aligned vs reversed direction still comes from the sign of cos_ang; the
    sibling walk is reversed for opposite-direction siblings so we always
    move backward relative to our line.

    Circular lines are their own sibling (self_oid == sib_oid): multi-pass
    projection surfaces the polyline's far end (and any mid-loop transits)
    as passes, so a loop's "return to start" geometry fills its own first
    stop's backward extent. The pass at the loop's own start is harmless —
    its tangent is identical to ours (aligned), so its backward walk runs
    off the polyline start and is skipped.
    """
    if L <= t_on_self:
        return None
    fill_m = L - t_on_self

    cos_lat = cos(radians(anchor_lat))
    my_ex, my_ey = anchor_dx * cos_lat, anchor_dy
    my_mag = sqrt(my_ex * my_ex + my_ey * my_ey)
    if my_mag == 0:
        return None

    ranked = []
    for sib_oid, sib_poly in siblings:
        if len(sib_poly) < 2:
            continue
        sib_dists = _cum_dist_m(sib_poly)
        sib_total = sib_dists[-1]
        if sib_total <= 0:
            continue

        for q_t in _projections_at(anchor_lon, anchor_lat,
                                     sib_poly, sib_dists):
            q_lon, q_lat = _interp_at(sib_poly, sib_dists, q_t)

            prox_m = haversine_km(anchor_lon, anchor_lat,
                                    q_lon, q_lat) * 1000.0
            if prox_m > SIBLING_PROXIMITY_M:
                continue

            turn = _local_turn_angle_deg(sib_poly, sib_dists, q_t,
                                          window_m=2.0)
            if turn is not None and turn > SIBLING_MAX_TURN_DEG:
                continue

            sib_tan = _directional_tangent_at(sib_poly, sib_dists, q_t,
                                                window_m=2.0)
            if sib_tan is None:
                continue
            sib_dx, sib_dy = sib_tan
            sib_ex, sib_ey = sib_dx * cos_lat, sib_dy
            sib_mag = sqrt(sib_ex * sib_ex + sib_ey * sib_ey)
            if sib_mag == 0:
                continue

            cos_ang = (my_ex * sib_ex + my_ey * sib_ey) / (my_mag * sib_mag)
            aligned = cos_ang > 0
            ranked.append((abs(cos_ang), prox_m, sib_oid, sib_poly,
                            sib_dists, aligned, q_t))

    # Best tangent match wins; ties broken by shorter proximity.
    ranked.sort(key=lambda x: (-x[0], x[1]))

    for (_abs_cos, _prox_m, _sib_oid, sib_poly, sib_dists,
         aligned, q_t) in ranked:
        sib_total = sib_dists[-1]
        # Anchor sits at the on-polyline extent's far end (poly[0]), so the
        # walk on the sibling starts at q_t itself — no t_on_self shift.
        if aligned:
            walk_end_t = q_t
            walk_start_t = q_t - fill_m
            if walk_start_t < 0:
                continue
            seg = list(_slice_polyline(sib_poly, sib_dists,
                                        walk_start_t, walk_end_t))
        else:
            walk_start_t = q_t
            walk_end_t = q_t + fill_m
            if walk_end_t > sib_total:
                continue
            seg = list(_slice_polyline(sib_poly, sib_dists,
                                        walk_start_t, walk_end_t))
            seg.reverse()

        if len(seg) < 2:
            continue

        # The segment keeps the donor's true coordinates — never translate
        # it onto the anchor. Where the donor sits slightly off the anchor
        # (within the proximity gate), the extent has a small jog at the
        # join instead of running parallel next to the drawn line.
        return seg

    return None


# -----------------------------------------------------------------------------
# Non-sibling backward borrow (pill-rendering concept § Missing-range fill,
# step 2). Widens the sibling-borrow candidate set from same-line variants to
# any drawn line polyline within SIBLING_PROXIMITY_M of the anchor, kept
# honest by the SIBLING_ANGLE_TOL_RAD tangent gate. The index is built once
# by pipeline_setup and passed down as a parameter; None disables the tier.
# -----------------------------------------------------------------------------

class _AllLinesIndex:
    """Spatial grid over every drawn line polyline. Given (lon, lat) and a
    radius in metres, returns candidate (osm_id, sib_key, polyline, dists)
    tuples whose polyline touches a cell within radius of the point. Callers
    do their own precise proximity + tangent gating on the returned set."""

    _CELL_DEG = 0.0005

    def __init__(self):
        self._cells = defaultdict(set)
        self._lookup = {}

    def add(self, oid, sib_key, poly, dists):
        self._lookup[str(oid)] = (sib_key, poly, dists)
        cs = self._CELL_DEG
        for i in range(len(poly) - 1):
            ax, ay = poly[i]
            bx, by = poly[i + 1]
            lo_x = int(min(ax, bx) / cs); hi_x = int(max(ax, bx) / cs)
            lo_y = int(min(ay, by) / cs); hi_y = int(max(ay, by) / cs)
            for cx in range(lo_x, hi_x + 1):
                for cy in range(lo_y, hi_y + 1):
                    self._cells[(cx, cy)].add(str(oid))

    def query(self, lon, lat, radius_m):
        deg = radius_m / 111000.0 * 1.5
        cs = self._CELL_DEG
        lo_x = int((lon - deg) / cs); hi_x = int((lon + deg) / cs)
        lo_y = int((lat - deg) / cs); hi_y = int((lat + deg) / cs)
        seen = set()
        for cx in range(lo_x, hi_x + 1):
            for cy in range(lo_y, hi_y + 1):
                seen |= self._cells.get((cx, cy), set())
        out = []
        for oid in seen:
            entry = self._lookup.get(oid)
            if entry is not None:
                out.append((oid,) + entry)
        return out

    def own_sib_key(self, oid):
        entry = self._lookup.get(str(oid))
        return entry[0] if entry else None


def _borrow_backward_nonsibling(anchor_lon, anchor_lat,
                                 anchor_dx, anchor_dy, t_on_self, L,
                                 self_oid, self_sib_key, all_lines_index):
    """Non-sibling backward borrow: widen the missing-range fill from
    same-(ref, agency_id, mode) variants to any drawn line polyline within
    SIBLING_PROXIMITY_M of the anchor (the on-polyline extent's far end,
    `poly[0]`), keeping the SIBLING_ANGLE_TOL_RAD tangent gate. Multiple
    qualifying candidates are
    ranked by tangent match to (anchor_dx, anchor_dy) — highest
    `|cos(angle)|` wins, with shorter proximity as tie-break. The picked
    line is walked from its projection of the anchor by the missing
    arc-length; the segment keeps the donor's true coordinates (never
    translated onto the anchor — see _borrow_backward_segment). Returns
    None when the tier is disabled, when no candidate qualifies, or when
    every ranked candidate's polyline runs out before fill_m.
    """
    if all_lines_index is None or L <= t_on_self:
        return None
    fill_m = L - t_on_self

    cos_lat = cos(radians(anchor_lat))
    my_ex, my_ey = anchor_dx * cos_lat, anchor_dy
    my_mag = sqrt(my_ex * my_ex + my_ey * my_ey)
    if my_mag == 0:
        return None
    cos_tol = cos(SIBLING_ANGLE_TOL_RAD)

    ranked = []
    for (cand_oid, cand_key, cand_poly, cand_dists) in all_lines_index.query(
            anchor_lon, anchor_lat, SIBLING_PROXIMITY_M):
        if cand_oid == str(self_oid):
            continue
        if cand_key == self_sib_key:
            continue
        cand_total = cand_dists[-1]
        if cand_total <= 0:
            continue

        for q_t in _projections_at(anchor_lon, anchor_lat,
                                    cand_poly, cand_dists):
            q_lon, q_lat = _interp_at(cand_poly, cand_dists, q_t)
            prox_m = haversine_km(anchor_lon, anchor_lat,
                                    q_lon, q_lat) * 1000.0
            if prox_m > SIBLING_PROXIMITY_M:
                continue

            turn = _local_turn_angle_deg(cand_poly, cand_dists, q_t,
                                          window_m=2.0)
            if turn is not None and turn > SIBLING_MAX_TURN_DEG:
                continue

            cand_tan = _directional_tangent_at(cand_poly, cand_dists, q_t,
                                                window_m=2.0)
            if cand_tan is None:
                continue
            cand_dx, cand_dy = cand_tan
            cand_ex, cand_ey = cand_dx * cos_lat, cand_dy
            cand_mag = sqrt(cand_ex * cand_ex + cand_ey * cand_ey)
            if cand_mag == 0:
                continue

            cos_ang = (my_ex * cand_ex + my_ey * cand_ey) / (my_mag * cand_mag)
            abs_cos = abs(cos_ang)
            if abs_cos < cos_tol:
                continue
            aligned = cos_ang > 0
            ranked.append((abs_cos, prox_m, cand_oid, cand_poly, cand_dists,
                            aligned, q_t))

    ranked.sort(key=lambda x: (-x[0], x[1]))

    for (_abs_cos, _prox_m, _cand_oid, cand_poly, cand_dists,
         aligned, q_t) in ranked:
        cand_total = cand_dists[-1]
        # Anchor is at poly[0], so the walk starts at q_t itself.
        if aligned:
            walk_end_t = q_t
            walk_start_t = q_t - fill_m
            if walk_start_t < 0:
                continue
            seg = list(_slice_polyline(cand_poly, cand_dists,
                                        walk_start_t, walk_end_t))
        else:
            walk_start_t = q_t
            walk_end_t = q_t + fill_m
            if walk_end_t > cand_total:
                continue
            seg = list(_slice_polyline(cand_poly, cand_dists,
                                        walk_start_t, walk_end_t))
            seg.reverse()

        if len(seg) < 2:
            continue

        # Donor coordinates are kept as-is — see _borrow_backward_segment.
        return seg

    return None

