"""Symmetric-arc curve construction between pill discs / mid-attach points."""
from math import acos, atan2, cos, degrees, pi, radians, sin, sqrt

from _state import *  # noqa: F401,F403
from _state import _arc_chord_samples  # underscore names skipped by *
from _state import _M_PER_DEG  # underscore names skipped by *
from geometry import haversine_km
from stops.pill_zoom.polyline import (
    _current_pill_diameter_m, _dedup_polyline_xy,
    _lonlat_to_xy, _norm2, _pill_mid_attach_candidates, _polyline_length_xy,
    _remove_pill_kinks, _rotate2, _simplify_pill_lonlat, _xy_to_lonlat,
)

# Per-cluster pill diameter stash, set by make.py (`build_stop_pills`) before
# the connector construction that reaches `_tangent_candidates`. Lives here
# because make.py imports this module (setting it in make's own globals would
# leave this module's reads unresolved).
_CURRENT_CLUSTER_PILL_DIAMETER_M = None


def _tangent_candidates(group, endpoint, lon0, lat0, cos_lat):
    """Candidate outward-pointing unit tangents at `endpoint` within `group`.

    Connectors attach only at pill endpoints, so `endpoint` is always the
    first or last dot of a pill group, never an interior dot.

    Returns a list of (tangent, is_default) tuples in metric (x, y) space:
    - Singleton group (disc): [] — tangent is unconstrained, derived from
      symmetry by the caller.
    - Pill tip: [(axial, True), (perp_left, False), (perp_right, False)].
    """
    if len(group) <= 1:
        return []

    # Compute OUT from the simplified polyline (what the renderer draws), not
    # the raw NN-path group. The path can zig-zag through pill vertices (e.g.
    # disc → middle → south → north at Bethlehem Kirche), which makes the raw
    # next-vertex point into the pill body instead of away from it.
    simplified = _simplify_pill_lonlat(group, cos_lat,
                                       pill_diameter_m=_CURRENT_CLUSTER_PILL_DIAMETER_M)
    if len(simplified) < 2:
        return []

    if endpoint[0] == simplified[0][0] and endpoint[1] == simplified[0][1]:
        idx, neighbor = 0, 1
    else:
        idx, neighbor = len(simplified) - 1, len(simplified) - 2

    xy_e = _lonlat_to_xy(simplified[idx][0], simplified[idx][1], lon0, lat0, cos_lat)
    xy_n = _lonlat_to_xy(simplified[neighbor][0], simplified[neighbor][1], lon0, lat0, cos_lat)
    axial = _norm2((xy_e[0] - xy_n[0], xy_e[1] - xy_n[1]))
    if axial is None:
        return []
    return [
        (axial, True),
        (_rotate2(axial, pi / 2), False),
        (_rotate2(axial, -pi / 2), False),
    ]


# Within this tolerance of a geographic cardinal (N / E / S / W in cluster-xy
# space), a newly-derived disc anchor is snapped to the cardinal — lines that
# happen to run almost cardinally anchor an exactly compass-aligned frame; a
# diagonal tram through the station keeps its actual direction.
DISC_ANCHOR_CARDINAL_SNAP_DEG = 10.0


def _cardinal_tangents(t):
    """4 cardinal OUT tangents for an anchored disc with anchor direction `t`.
    All 4 are tagged as default (is_default=True) since no cardinal is
    preferred over the others — the picker picks shortest among them.
    """
    return [
        (t, True),
        ((-t[1], t[0]), True),
        ((-t[0], -t[1]), True),
        ((t[1], -t[0]), True),
    ]


def _arrival_tangent_lonlat(coords, at_start, cos_lat):
    """OUT tangent at one end of a (lon, lat) polyline, as a unit vector in
    cluster-xy space (with cos_lat scaling). `at_start=True` returns the
    tangent at coords[0] pointing away from coords[1]; `at_start=False`
    returns the tangent at coords[-1] pointing away from coords[-2]. Direction
    is invariant across origin shifts, so this is usable across per-connector
    xy frames so long as the cluster's cos_lat is constant.
    """
    if len(coords) < 2:
        return None
    if at_start:
        p_to, p_from = coords[0], coords[1]
    else:
        p_to, p_from = coords[-1], coords[-2]
    dx = (p_to[0] - p_from[0]) * cos_lat * _M_PER_DEG
    dy = (p_to[1] - p_from[1]) * _M_PER_DEG
    return _norm2((dx, dy))


def _snap_to_cardinal(t, tol_deg=DISC_ANCHOR_CARDINAL_SNAP_DEG):
    """If `t` is within `tol_deg` of a geographic cardinal (N / E / S / W),
    snap to that cardinal as an exact unit vector. Otherwise return `t`
    unchanged. `t` is a unit vector in cluster-xy space; cardinals are
    `(0, 1)`, `(1, 0)`, `(0, -1)`, `(-1, 0)`.
    """
    if t is None:
        return None
    ang = atan2(t[1], t[0])
    ang_q = round(ang / (pi / 2)) * (pi / 2)
    if abs(ang - ang_q) <= radians(tol_deg):
        return (cos(ang_q), sin(ang_q))
    return t


def _build_symmetric_arc(A, B, tA, tB, r_max):
    """Build a symmetric arc connector between A and B in metric (x, y) space.

    tA, tB are unit tangents pointing OUT of each pill. Returns the polyline
    `[A, A', interior arc samples, B', B]` (collapsing degenerate-length
    stubs), or None if no valid construction exists.
    """
    neg_tB = (-tB[0], -tB[1])
    cross = tA[0] * neg_tB[1] - tA[1] * neg_tB[0]
    dot = tA[0] * neg_tB[0] + tA[1] * neg_tB[1]
    turn = atan2(cross, dot)  # signed angle from tA to -tB, in (-π, π]

    if abs(turn) < 1e-6:
        # Parallel forward tangents (tA aligns with -tB): the only
        # tangent-consistent connector is a straight line in direction tA. This
        # is the "both tips face each other" case — the symmetric-arc
        # construction has no work to do, but the combo is still a legitimate
        # connector candidate and must surface to the picker as a 2-point
        # result so that, when no combo produces a real curve, the picker has
        # a last-resort straight to fall back on. Real curves at other tangent
        # combos outrank this chord in the picker. Only emit when the chord
        # actually aligns with tA — otherwise a "straight line" between A and
        # B has hard kinks at both ends and the combo is geometrically
        # inconsistent.
        BAx = B[0] - A[0]
        BAy = B[1] - A[1]
        BA_len = sqrt(BAx * BAx + BAy * BAy)
        if BA_len < 1e-9:
            return None
        cos_BA_tA = (BAx * tA[0] + BAy * tA[1]) / BA_len
        if cos_BA_tA > 0.999:  # within ~2.5° of tA direction
            return [A, B]
        return None
    if abs(abs(turn) - pi) < 1e-6:
        # Anti-parallel tangents — would require a U-turn semicircle, not
        # handled by the symmetric-arc construction.
        return None

    half = turn / 2.0
    theta = abs(half)
    chord_dir = _rotate2(tA, half)
    # |chord| at which arc radius equals r_max.
    L_target = 2.0 * r_max * sin(theta)

    # Linear system in (sA, sB) for any given L:
    #   sB*tB - sA*tA = L*chord_dir - (B - A)
    # Solved via 2D Cramer's rule. det = tAy*tBx - tAx*tBy (= -(tA × tB)).
    det = tA[1] * tB[0] - tA[0] * tB[1]
    if abs(det) < 1e-9:
        return None

    def stubs(L):
        qx = L * chord_dir[0] - (B[0] - A[0])
        qy = L * chord_dir[1] - (B[1] - A[1])
        # sB*tB - sA*tA = (qx, qy)
        # [[-tAx, tBx], [-tAy, tBy]] [sA, sB]^T = [qx, qy]^T
        sA = (qx * tB[1] - qy * tB[0]) / det
        sB = (qx * tA[1] - qy * tA[0]) / det
        return sA, sB

    # Pick the largest L for which both stubs stay non-negative — that gives
    # the widest symmetric arc the (tA, tB) geometry admits. sA(L), sB(L)
    # are linear in L, so the valid range is a single interval [L_lo, L_hi].
    # The per-mode `r_max` (via L_target) is only a soft fallback for the
    # rare case where neither stub has a slope that drives it back to 0 (no
    # natural upper bound).
    if L_target <= 1e-9:
        return None
    sA_at_target, sB_at_target = stubs(L_target)
    sA0, sB0 = stubs(0.0)
    dsA = (sA_at_target - sA0) / L_target
    dsB = (sB_at_target - sB0) / L_target

    L_lo = 0.0
    L_hi = float("inf")
    for s0, ds in ((sA0, dsA), (sB0, dsB)):
        if abs(ds) < 1e-12:
            if s0 < -1e-6:
                return None  # constant negative stub
            continue
        L_zero = -s0 / ds
        if ds > 0:
            if s0 < -1e-6:
                # Stub starts negative and grows — needs L ≥ L_zero.
                L_lo = max(L_lo, L_zero)
        else:
            if s0 < -1e-6:
                # Stub starts negative and shrinks further.
                return None
            # Stub starts ≥ 0 and shrinks — needs L ≤ L_zero (= 0 when s0 = 0).
            L_hi = min(L_hi, L_zero)
    if L_lo > L_hi + 1e-6:
        return None

    if L_hi == float("inf"):
        # Unbounded above — both stubs grow with L without ever shrinking
        # to 0. Fall back to the per-mode r_max so the curve doesn't
        # extend its stubs forever.
        chosen_L = max(L_lo, L_target)
    else:
        chosen_L = L_hi

    sA, sB = stubs(chosen_L)
    sA = max(0.0, sA)
    sB = max(0.0, sB)

    radius = chosen_L / (2.0 * sin(theta)) if theta > 1e-9 else 0.0
    if radius < CURVE_MIN_RADIUS_M:
        # Sub-floor radius would land all 13 arc samples inside line-width of
        # each other → MapLibre wobble. Drop the curve entirely; the caller
        # will emit a straight 2-point connector instead.
        return None

    A_prime = (A[0] + sA * tA[0], A[1] + sA * tA[1])
    B_prime = (B[0] + sB * tB[0], B[1] + sB * tB[1])

    # Arc center on the perpendicular to tA at A', on the side the curve bends toward.
    perp_to_C = _rotate2(tA, pi / 2 if half > 0 else -pi / 2)
    C = (A_prime[0] + radius * perp_to_C[0], A_prime[1] + radius * perp_to_C[1])

    angle_A = atan2(A_prime[1] - C[1], A_prime[0] - C[0])
    angle_B = atan2(B_prime[1] - C[1], B_prime[0] - C[0])
    delta = angle_B - angle_A
    if half > 0:
        while delta < -1e-9:
            delta += 2 * pi
    else:
        while delta > 1e-9:
            delta -= 2 * pi

    arc_length = radius * abs(delta)
    n_samples = _arc_chord_samples(radius, arc_length)
    samples = []
    for k in range(n_samples + 1):
        t = k / n_samples
        a = angle_A + t * delta
        samples.append((C[0] + radius * cos(a), C[1] + radius * sin(a)))

    # Compose final polyline, dropping stubs whose length sits within the
    # dedup tolerance so a near-zero `sA` doesn't add an `A_prime` vertex
    # within micrometres of `A` (same for `B`).
    poly = [A]
    if sA > CURVE_DEDUP_TOL_M:
        poly.append(samples[0])
    poly.extend(samples[1:-1])
    if sB > CURVE_DEDUP_TOL_M:
        poly.append(samples[-1])
    poly.append(B)
    poly = _dedup_polyline_xy(poly, tol_m=CURVE_DEDUP_TOL_M)
    if len(poly) < 3:
        return None
    return poly


def _build_pill_disc_curve(A, tA, B, r_max):
    """Pill-to-disc connector geometry in metric (x, y) space. The curve
    begins at the pill tip A tangent to tA (no pill-side stub) and bends
    toward B until the forward tangent points at B; from that tangent
    point a straight segment connects to B.

    Radius is the per-mode `r_max` when the disc lies outside the curve
    circle that radius would draw; otherwise the radius is shrunk to fit,
    floored at `CURVE_MIN_RADIUS_M`. Returns the polyline
    `[A, …arc samples…, P, B]` (P collapses out when coincident with B).
    Returns None when the disc lies on the line of tA or the fitted radius
    falls below the floor.
    """
    BA = (B[0] - A[0], B[1] - A[1])
    BA_sq = BA[0] * BA[0] + BA[1] * BA[1]
    if BA_sq < 1e-12:
        return None  # disc coincident with pill tip

    cross = tA[0] * BA[1] - tA[1] * BA[0]
    if abs(cross) < 1e-9:
        # Disc exactly on the line of tA: the chord IS the tangent-continuous
        # connector. Emit it directly so the picker sees a valid candidate at
        # the right length instead of dropping axial and falling through to a
        # swooping perpendicular arc.
        return [A, B]

    # Arc center on the side of tA that contains B. Bend chirality matches.
    if cross > 0:
        perp_to_C = (-tA[1], tA[0])
        ccw = True
    else:
        perp_to_C = (tA[1], -tA[0])
        ccw = False

    # The disc-outside-circle condition |CB| > r reduces to r < BA² / (2h),
    # where h = |cross| is the perpendicular distance from B to tA's line
    # (tA is unit). Shrink r_max to fit when the disc is too close, floored
    # at CURVE_MIN_RADIUS_M so sub-floor radii fall back to straight.
    h = abs(cross)
    r_fit_max = BA_sq / (2.0 * h)
    r = min(r_max, r_fit_max - 1e-6)
    if r < CURVE_MIN_RADIUS_M:
        return None

    C = (A[0] + r * perp_to_C[0], A[1] + r * perp_to_C[1])
    CB = (B[0] - C[0], B[1] - C[1])
    d = sqrt(CB[0] * CB[0] + CB[1] * CB[1])

    # Two tangent points on the circle from B; pick the one we reach with
    # the shorter forward sweep in the chirality direction whose tangent at
    # P points toward B (not away around the long side).
    theta_CB = atan2(CB[1], CB[0])
    phi = acos(max(-1.0, min(1.0, r / d)))
    theta_A = atan2(A[1] - C[1], A[0] - C[0])

    best = None
    for theta_p in (theta_CB + phi, theta_CB - phi):
        Px = C[0] + r * cos(theta_p)
        Py = C[1] + r * sin(theta_p)
        if ccw:
            tan_dir = (-sin(theta_p), cos(theta_p))
        else:
            tan_dir = (sin(theta_p), -cos(theta_p))
        if tan_dir[0] * (B[0] - Px) + tan_dir[1] * (B[1] - Py) < 0:
            continue
        delta = theta_p - theta_A
        if ccw:
            while delta < -1e-9:
                delta += 2 * pi
        else:
            while delta > 1e-9:
                delta -= 2 * pi
        sweep_mag = abs(delta)
        if best is None or sweep_mag < best[0]:
            best = (sweep_mag, delta)

    if best is None:
        return None
    if best[0] < 1e-6:
        # Sweep is essentially zero — tA is already aligned with the chord A→B
        # to within float precision. The chord IS the tangent-continuous answer;
        # emit it directly so the picker sees a valid candidate instead of
        # falling through to a perpendicular arc.
        return [A, B]
    _, delta = best

    # Sub-degree sweep: tA is essentially aligned with the chord A→B, so the
    # straight chord is the right answer. Emitting it here avoids the arc
    # samples collapsing under dedup and triggering the degenerate-curve
    # rejection below. The tangent error at A stays under ~1.5° (invisible).
    if abs(delta) < radians(1.5):
        return [A, B]

    arc_length = r * abs(delta)
    n_samples = _arc_chord_samples(r, arc_length)
    samples = []
    for k in range(n_samples + 1):
        t = k / n_samples
        a = theta_A + t * delta
        samples.append((C[0] + r * cos(a), C[1] + r * sin(a)))

    # samples[0] == A by construction; build polyline as A + interior + P + B
    # (collapse P when it coincides with B), then dedup adjacent vertices
    # within line-width to avoid MapLibre wobble where a small sweep packs
    # the arc samples into a sub-metre region.
    P = samples[-1]
    poly = [A] + samples[1:]
    if (P[0] - B[0]) * (P[0] - B[0]) + (P[1] - B[1]) * (P[1] - B[1]) > CURVE_DEDUP_TOL_M * CURVE_DEDUP_TOL_M:
        poly.append(B)
    poly = _dedup_polyline_xy(poly, tol_m=CURVE_DEDUP_TOL_M)
    if len(poly) < 3:
        return None
    return poly


def _pill_disc_picker(pill_xy, pill_cands, disc_xy, r_max):
    """Pick the best (tangent, polyline) for a pill-to-disc connector.

    Tangent ranking: the axial-preferred rule applies when both axial and
    perpendicular candidates produce a valid curve — a perpendicular wins
    over the axial default only when its length is ≤ CURVE_PERP_PREF_RATIO ×
    the default length. When the default tangent itself produces no valid
    curve (typical when the disc is closer to the pill than r_max forces
    the curve circle out toward), the shortest valid perpendicular is used
    — the asymmetric pill-disc construction cannot produce the L-shape
    detours that the strict default-or-straight rule guards against in the
    pill-pill case. Returns None only when no tangent admits any valid
    curve (disc on the pill's axis line, etc.), in which case the caller
    falls back to a straight 2-point connector.
    """
    results = []
    for ta, is_default in pill_cands:
        poly = _build_pill_disc_curve(pill_xy, ta, disc_xy, r_max)
        if poly is None:
            continue
        results.append((poly, _polyline_length_xy(poly), is_default))
    if not results:
        return None
    default = next((r for r in results if r[2]), None)
    if default is not None:
        threshold = default[1] * CURVE_PERP_PREF_RATIO
        qualifying = [r for r in results if r[1] <= threshold]
        chosen = min(qualifying, key=lambda r: r[1]) if qualifying else default
    else:
        chosen = min(results, key=lambda r: r[1])
    return chosen[0]


