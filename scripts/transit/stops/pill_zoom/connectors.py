"""Connector construction between pill/disc endpoints — curved or straight."""
from bisect import bisect_left
from collections import defaultdict
from itertools import permutations
from math import atan2, cos, degrees, pi, radians, sin, sqrt

from _state import *  # noqa: F401,F403
from _state import _curve_max_radius  # underscore names skipped by *
from geometry import flatten_coords, haversine_km, snap_to_line
from stops.pill_zoom.curves import (
    _arrival_tangent_lonlat, _build_pill_disc_curve, _build_symmetric_arc,
    _cardinal_tangents, _pill_disc_picker, _snap_to_cardinal,
    _tangent_candidates,
)
from stops.pill_zoom.polyline import (
    _dedup_polyline_xy, _lonlat_to_xy, _norm2, _polyline_length_xy,
    _remove_pill_kinks, _rotate2, _xy_to_lonlat,
)


def _curve_connector(ca, cb, group_a, group_b, cluster_cos_lat, mode,
                     anchor_a=None, anchor_b=None, mid_attach_tangents=None):
    """Post-process an MST connector from `ca` (in group_a) to `cb` (in group_b)
    into a curved (lon, lat) polyline.

    `anchor_a` / `anchor_b`: optional OUT tangent unit vectors (in cluster-xy
    space) for an anchored disc — only meaningful when the corresponding side
    is a singleton group. A None anchor on a singleton means the disc is
    unanchored and the connector is unconstrained at that end. Pills always
    derive their tangents from their own geometry (anchors on the pill side
    are ignored).

    Returns `(coords, anchor_out_a, anchor_out_b, is_fallback)`. Each
    `anchor_out_*` is the OUT tangent at that end of the final polyline in
    cluster-xy space, or None if the polyline is too short to derive one.
    The caller decides whether to use it as a new anchor. `is_fallback` is
    True only when the function hit an explicit "no valid curve" return-chord
    path after a curve construction failed; False when the picker selected a
    chosen result (curve or aligned chord) from `_build_symmetric_arc` /
    `_pill_disc_picker`, and also False for the both-unanchored-discs straight
    chord — that chord is the natural answer with no construction attempted,
    not a recovery. A 2-point polyline can be either: an intentional
    parallel-tangent chord that the picker chose as the best valid result is
    `is_fallback=False`; the natural both-unanchored straight is
    `is_fallback=False`; the return-chord path used when every candidate
    failed is `is_fallback=True`.
    """
    r_max = _curve_max_radius(mode)

    lon0 = (ca[0] + cb[0]) / 2.0
    lat0 = (ca[1] + cb[1]) / 2.0
    A_xy = _lonlat_to_xy(ca[0], ca[1], lon0, lat0, cluster_cos_lat)
    B_xy = _lonlat_to_xy(cb[0], cb[1], lon0, lat0, cluster_cos_lat)

    if len(group_a) > 1:
        if mid_attach_tangents and ca in mid_attach_tangents:
            cands_a = [(mid_attach_tangents[ca], True)]
        else:
            cands_a = _tangent_candidates(group_a, ca, lon0, lat0, cluster_cos_lat)
    elif anchor_a is not None:
        cands_a = _cardinal_tangents(anchor_a)
    else:
        cands_a = []
    if len(group_b) > 1:
        if mid_attach_tangents and cb in mid_attach_tangents:
            cands_b = [(mid_attach_tangents[cb], True)]
        else:
            cands_b = _tangent_candidates(group_b, cb, lon0, lat0, cluster_cos_lat)
    elif anchor_b is not None:
        cands_b = _cardinal_tangents(anchor_b)
    else:
        cands_b = []

    def finalize(coords, is_fallback):
        anchor_out_a = _arrival_tangent_lonlat(coords, True, cluster_cos_lat)
        anchor_out_b = _arrival_tangent_lonlat(coords, False, cluster_cos_lat)
        return coords, anchor_out_a, anchor_out_b, is_fallback

    # Both ends unconstrained (e.g. unanchored disc ↔ unanchored disc):
    # straight chord. This is the natural answer with no construction
    # attempted, not a recovery from a failed curve — is_fallback=False. The
    # cardinal snap is intentionally NOT applied here; see `_emit_connectors`
    # for the paired rule that suppresses the on-store snap for both-
    # unanchored edges, so subsequent connectors at either end see the
    # actual chord direction rather than a snapped cardinal.
    if not cands_a and not cands_b:
        return finalize([ca, cb], False)

    # Constrained one side only: asymmetric arc-then-straight with the
    # constrained side playing the pill role. Same construction whether the
    # constrained side is a real pill or an anchored disc.
    if cands_a and not cands_b:
        poly_xy = _pill_disc_picker(A_xy, cands_a, B_xy, r_max)
        if poly_xy is None:
            return finalize([ca, cb], True)
        coords = [_xy_to_lonlat(p[0], p[1], lon0, lat0, cluster_cos_lat) for p in poly_xy]
        return finalize(coords, False)
    if cands_b and not cands_a:
        poly_xy = _pill_disc_picker(B_xy, cands_b, A_xy, r_max)
        if poly_xy is None:
            return finalize([ca, cb], True)
        coords = [_xy_to_lonlat(p[0], p[1], lon0, lat0, cluster_cos_lat) for p in poly_xy]
        coords.reverse()
        return finalize(coords, False)

    # Both ends constrained: symmetric arc. Covers pill ↔ pill, pill ↔
    # anchored-disc, and anchored ↔ anchored.
    pairs = [(ta, tb, def_a, def_b)
             for ta, def_a in cands_a
             for tb, def_b in cands_b]

    results = []
    for ta, tb, def_a, def_b in pairs:
        poly = _build_symmetric_arc(A_xy, B_xy, ta, tb, r_max)
        if poly is None:
            continue
        results.append((poly, _polyline_length_xy(poly), def_a, def_b))

    if not results:
        # No valid (cardinal × cardinal) combo. Fall back to a straight chord
        # so an anchored-disc end with no working cardinals doesn't lose its
        # connector entirely. The disc's anchor stays as it was — anchors are
        # written in `_emit_connectors`, not here. This is a real fallback
        # (a curve was attempted and could not be built), so is_fallback=True
        # regardless of whether anchors were present.
        return finalize([ca, cb], True)

    # Curves outrank 2-point straight results. _build_symmetric_arc returns a
    # 2-point chord only for the parallel-forward (turn ≈ 0) case where the
    # chord happens to align with tA; visually that is indistinguishable from
    # the explicit no-curve fallback, so it must not gate a real curve via
    # the 0.75 ratio. Among curves the axial-preferred rule still holds: a
    # perpendicular combo replaces the default only when its length is ≤
    # CURVE_PERP_PREF_RATIO × the default. Multiple combos may share the
    # default tag (4 cardinals × 1 axial-pill = 4 default combos for pill ↔
    # anchored-disc; 16 for anchored ↔ anchored) — the shortest among them
    # is the baseline. A 2-point straight is only accepted when no combo
    # produced a curve at all.
    curves = [r for r in results if len(r[0]) >= 3]
    if curves:
        defaults = [r for r in curves if r[2] and r[3]]
        if defaults:
            default_combo = min(defaults, key=lambda r: r[1])
            threshold = default_combo[1] * CURVE_PERP_PREF_RATIO
            qualifying = [r for r in curves if r[1] <= threshold]
            chosen = min(qualifying, key=lambda r: r[1]) if qualifying else default_combo
        else:
            chosen = min(curves, key=lambda r: r[1])
    else:
        defaults = [r for r in results if r[2] and r[3]]
        if not defaults:
            return finalize([ca, cb], True)
        chosen = min(defaults, key=lambda r: r[1])

    coords = [_xy_to_lonlat(p[0], p[1], lon0, lat0, cluster_cos_lat) for p in chosen[0]]
    return finalize(coords, False)


# Disc-strategy comparison: anchoring vs fixed-cardinal. See `.claude/concepts/
# stops-pill-zoom.md` § Disc anchoring → Per-cluster strategy choice.
SCORE_ON_LINE_TOL_M = 3.0
SCORE_ON_LINE_FRAC = 0.5
_FIXED_CARDINAL_SEED = (0.0, 1.0)


def _emit_connectors(chosen_edges, groups, cluster_cos_lat, mode, fixed_cardinal,
                     snap_anchors=True, mid_attach_tangents=None):
    """Run the per-connector emission loop with a chosen disc-tangent strategy.

    `fixed_cardinal=False` — anchoring strategy: discs start unanchored; each
    one anchors from its first connector's arrival.
    `fixed_cardinal=True` — fixed-cardinal strategy: every disc is pre-seeded
    with the same anchor (`_FIXED_CARDINAL_SEED`) so its 4 `_cardinal_tangents`
    rotations are exactly N / E / S / W on the geographic frame for every
    disc on the map. Anchors are immutable across the run.
    `snap_anchors` — only consulted in the anchoring strategy. When True, each
    newly-derived anchor is passed through `_snap_to_cardinal` so near-cardinal
    arrivals lock to the compass grid. Set False for rail clusters: tracks
    routinely run at arbitrary angles and snapping would distort the frame.

    Edge processing order:
      1. Pill-pill edges (no anchoring effect).
      2. Pill-disc edges (each anchors its disc end from the pill's tangent).
      3. Disc-disc edges, iteratively: process every edge with at least one
         already-anchored endpoint, then refresh and repeat. A both-unanchored
         disc-disc edge can only fire as a one-shot bootstrap at the very
         start of the cluster's processing — only reachable when the cluster
         has no pill-pill or pill-disc edges (a pure-disc cluster). After
         bootstrap, every remaining edge in the MST tree must touch the
         anchored subtree, so propagation continues normally without ever
         needing another both-unanchored chord. Bootstrap picks the first
         disc-disc edge by the existing sort order (line_max desc, lex
         coords). Within each tier the intra-tier order from `chosen_edges`
         is preserved.

    Returns list of `(coords, is_fallback)`. Order follows processing order,
    not `chosen_edges` order; callers iterate without index dependence.
    """
    disc_anchors = {}
    if fixed_cardinal:
        for grp in groups:
            if len(grp) == 1:
                disc_anchors[(grp[0][0], grp[0][1])] = _FIXED_CARDINAL_SEED

    out = []

    def _process(edge):
        ca, cb, i, j = edge
        grp_a, grp_b = groups[i], groups[j]
        pos_a = (ca[0], ca[1])
        pos_b = (cb[0], cb[1])
        anchor_a = disc_anchors.get(pos_a) if len(grp_a) == 1 else None
        anchor_b = disc_anchors.get(pos_b) if len(grp_b) == 1 else None
        # Two unanchored singletons get a straight chord (see _curve_connector's
        # both-empty branch). The arrival tangents of that chord ARE the
        # chord direction, so cardinal-snapping them on store would force
        # subsequent connectors at either end onto a different frame than
        # the chord they continue — visible as a kink at the disc. Skip the
        # snap for this case; store the raw tangents.
        skip_snap = (
            len(grp_a) == 1 and anchor_a is None and
            len(grp_b) == 1 and anchor_b is None
        )
        coords, arrival_a, arrival_b, is_fallback = _curve_connector(
            ca, cb, grp_a, grp_b, cluster_cos_lat, mode,
            anchor_a=anchor_a, anchor_b=anchor_b,
            mid_attach_tangents=mid_attach_tangents)
        out.append((coords, is_fallback))
        if not fixed_cardinal:
            if skip_snap or not snap_anchors:
                store = lambda t: t
            else:
                store = _snap_to_cardinal
            if len(grp_a) == 1 and pos_a not in disc_anchors and arrival_a is not None:
                disc_anchors[pos_a] = store(arrival_a)
            if len(grp_b) == 1 and pos_b not in disc_anchors and arrival_b is not None:
                disc_anchors[pos_b] = store(arrival_b)

    # Partition by tier; intra-tier order is preserved from chosen_edges.
    pill_pill = []
    pill_disc = []
    disc_disc = []
    for edge in chosen_edges:
        _ca, _cb, i, j = edge
        da = len(groups[i]) == 1
        db = len(groups[j]) == 1
        if not (da or db):
            pill_pill.append(edge)
        elif da and db:
            disc_disc.append(edge)
        else:
            pill_disc.append(edge)

    # Tier 1: pill-pill.
    for edge in pill_pill:
        _process(edge)
    # Tier 2: pill-disc — anchors each disc end from its pill's tangent.
    for edge in pill_disc:
        _process(edge)

    # Tier 3: disc-disc, iteratively. Process every edge with at least one
    # anchored endpoint; refresh; repeat. The one-shot bootstrap fires only
    # if nothing has been processed yet (pure-disc cluster).
    remaining = list(disc_disc)
    processed_any = bool(pill_pill or pill_disc)
    while remaining:
        eligible_mask = [
            (e[0][0], e[0][1]) in disc_anchors or (e[1][0], e[1][1]) in disc_anchors
            for e in remaining
        ]
        if any(eligible_mask):
            new_remaining = []
            for edge, eligible in zip(remaining, eligible_mask):
                if eligible:
                    _process(edge)
                else:
                    new_remaining.append(edge)
            remaining = new_remaining
            processed_any = True
        elif not processed_any:
            # Bootstrap: pure-disc cluster, no anchors yet. First disc-disc
            # edge in sort order seeds the cluster's anchor frame.
            _process(remaining.pop(0))
            processed_any = True
        else:
            # Unreachable in a connected MST tree once any node has been
            # anchored. Safety break to avoid an infinite loop.
            break

    return out


def _segment_on_any_line(p1, p2, lines, tol_sq):
    """True if BOTH endpoints of segment (p1, p2) are each within sqrt(tol_sq)
    of SOME line in `lines` — not necessarily the same one. tol_sq is the
    squared tolerance in lon/lat-degree space (same convention as
    `_segment_on_platform`).
    """
    def near_any(pt):
        for ln in lines:
            if len(ln) < 2:
                continue
            s = snap_to_line(pt[0], pt[1], ln)
            if (pt[0] - s[0]) ** 2 + (pt[1] - s[1]) ** 2 <= tol_sq:
                return True
        return False
    return near_any(p1) and near_any(p2)


def _score_connectors(connectors, lines, tol_sq):
    """Sum of two per-connector counts (lower is better):
    - on-line: connectors with >SCORE_ON_LINE_FRAC of their polyline length
      running within SCORE_ON_LINE_TOL_M of any transit line serving the
      cluster (the lines don't have to be the same along the run).
    - fallback-straight: connectors emitted as an explicit "no valid curve"
      chord (is_fallback=True). An intentional parallel-tangent chord chosen
      by the picker has is_fallback=False and does NOT count here.
    """
    on_line = 0
    straight = 0
    for coords, is_fallback in connectors:
        if is_fallback:
            straight += 1
        total = 0.0
        on_ln = 0.0
        for k in range(len(coords) - 1):
            p1, p2 = coords[k], coords[k + 1]
            seg = haversine_km(p1[0], p1[1], p2[0], p2[1])
            total += seg
            if _segment_on_any_line(p1, p2, lines, tol_sq):
                on_ln += seg
        if total > 0.0 and on_ln > SCORE_ON_LINE_FRAC * total:
            on_line += 1
    return on_line + straight


def _collect_cluster_line_polylines(cluster_stops, line_lookup):
    """Unique transit-line polylines (flattened to a single coord list) for
    every distinct osm_id appearing in the cluster. Used by the cardinal-vs-
    anchor scorer to check whether a connector runs along an actual transit
    line."""
    if not line_lookup:
        return []
    lines = []
    seen = set()
    for s in cluster_stops:
        oid = s.get("osm_id")
        if not oid or oid in seen:
            continue
        seen.add(oid)
        info = line_lookup.get(oid) or line_lookup.get(str(oid))
        if not info:
            continue
        coords = info.get("coords")
        if not coords:
            continue
        flat = flatten_coords(coords)
        if len(flat) >= 2:
            lines.append(flat)
    return lines


