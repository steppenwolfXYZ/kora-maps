"""make_pill_features: entry point for pill/connector emission per cluster."""
from bisect import bisect_left
from collections import defaultdict
from math import atan2, cos, degrees, pi, radians, sin, sqrt

from _state import *  # noqa: F401,F403
from _state import _stop_wb  # underscore names skipped by *
from geometry import (
    _cum_dist_m, _directional_tangent_at, _interp_at, _meters_per_deg,
    _project_meters, flatten_coords, haversine_km,
)
from geometry import _polyline_midpoint_and_tangent_deg
from stops.pill_zoom.connectors import (
    SCORE_ON_LINE_TOL_M,
    _collect_cluster_line_polylines, _curve_connector, _emit_connectors,
    _score_connectors, _segment_on_any_line,
)
from stops.pill_zoom.options import _should_split_at_gap
from stops.pill_zoom import curves as _pz_curves
from stops.pill_zoom.curves import (
    _arrival_tangent_lonlat, _build_pill_disc_curve, _build_symmetric_arc,
    _cardinal_tangents, _pill_disc_picker, _snap_to_cardinal,
    _tangent_candidates,
)
from stops.pill_zoom.lines import (
    build_indicator_features, cluster_lines, color_luminance, count_unique_lines,
    dominant_line, pill_minzoom,
)
from stops.pill_zoom.nn_path import (
    _dedup_cluster_members_by_position, _dedup_stop_positions,
    _pos_to_platforms, nearest_neighbor_path,
)
from stops.pill_zoom.polyline import (
    _current_pill_diameter_m, _dedup_polyline_xy, _lonlat_to_xy, _norm2,
    _pill_mid_attach_candidates, _polyline_length_xy, _remove_pill_kinks,
    _rotate2, _simplify_pill_lonlat, _xy_to_lonlat,
)


def make_pill_features(cluster_stops, minzoom, lines_json="", line_lookup=None):
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
    4. MST connectors join the resulting groups at their nearest endpoint
       pair — only a pill's first or last dot can host a connector.
    """
    color, mode, max_wb, dom_stop = dominant_line(cluster_stops)
    positions = _dedup_stop_positions(cluster_stops)
    n = len(positions)

    stop_props = {
        "color":          color,
        "mode":           mode,
        "width_base":     _stop_wb(max_wb, mode),
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

    if n == 0:
        return []

    if n == 1:
        # Multi-platform cluster whose dots all collapsed under the current
        # band's DEDUP_TOL_M. Emit a single endpoint disc at the surviving
        # position so the station stays visible; without this, wide-band
        # tolerances (band A's 5 m) silently drop 3-5 m rail-terminal pills
        # like Basel Dreispitz at z14.
        pos = positions[0]
        feats = [make_endpoint(pos)]
        if line_lookup is not None:
            feats.extend(build_indicator_features(
                cluster_stops, pos[0], pos[1], line_lookup,
                parent_width_base=stop_props["width_base"],
                parent_mode=stop_props["mode"]))
        return feats

    path = nearest_neighbor_path(positions)

    # Find every gap that splits the NN-path into separate pills.
    # _should_split_at_gap applies the per-shape threshold (PILL_GAP_STRAIGHT_M
    # for dead-straight in-line continuations or gaps along a bar's
    # perpendicular axis; PILL_GAP_ANGLED_M for angled / T-junction
    # connectors). Absolute metres — no width_base scaling.
    pos_to_platforms = _pos_to_platforms(cluster_stops, positions)
    mean_lat = sum(p[1] for p in positions) / len(positions)
    cluster_cos_lat = cos(radians(mean_lat))
    # Pill diameter estimate in metres at the current band's target zoom,
    # for length-aware kink removal in `_simplify_pill_lonlat`. Same
    # value for every group in this cluster (they share max_wb). Also
    # stashed on the module-level context so `_tangent_candidates` (deep
    # inside `_curve_connector`) can pass it into its own simplification.
    pill_diameter_m = _current_pill_diameter_m(max_wb, cluster_cos_lat)
    _pz_curves._CURRENT_CLUSTER_PILL_DIAMETER_M = pill_diameter_m
    split_indices = [
        k for k in range(len(path) - 1)
        if _should_split_at_gap(
            path, k,
            haversine_km(path[k][0], path[k][1],
                         path[k + 1][0], path[k + 1][1]),
            pos_to_platforms,
            cos_lat=cluster_cos_lat)
    ]

    def _stops_at_positions(grp_positions):
        out = []
        for pos in grp_positions:
            out.extend(pos_to_platforms.get((pos[0], pos[1]), []))
        return out

    if not split_indices:
        simp = _simplify_pill_lonlat(path, cluster_cos_lat, pill_diameter_m=pill_diameter_m)
        (mid_lon, mid_lat), tan_deg = _polyline_midpoint_and_tangent_deg(simp)
        feats = [make_feat(simp, "pill")]
        if line_lookup is not None:
            feats.extend(build_indicator_features(
                cluster_stops, mid_lon, mid_lat, line_lookup,
                tangent_deg=tan_deg, parent_type="pill",
                parent_width_base=stop_props["width_base"],
                parent_mode=stop_props["mode"]))
        return feats

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
    group_mid_attach = []  # per group: list of (pos, outer_tangent_xy)
    mid_attach_tangents = {}  # (lon, lat) → outer_tangent_xy for _curve_connector
    for grp in groups:
        if len(grp) >= 2:
            simp = _simplify_pill_lonlat(grp, cluster_cos_lat, pill_diameter_m=pill_diameter_m)
            feats.append(make_feat(simp, "pill"))
            if line_lookup is not None:
                (mid_lon, mid_lat), tan_deg = _polyline_midpoint_and_tangent_deg(simp)
                feats.extend(build_indicator_features(
                    _stops_at_positions(grp), mid_lon, mid_lat, line_lookup,
                    tangent_deg=tan_deg, parent_type="pill",
                    parent_width_base=stop_props["width_base"],
                    parent_mode=stop_props["mode"]))
            mids = _pill_mid_attach_candidates(simp, cluster_cos_lat)
            group_mid_attach.append(mids)
            for pos, tan in mids:
                mid_attach_tangents[pos] = tan
        else:
            pos = grp[0]
            feats.append(make_endpoint(pos))
            if line_lookup is not None:
                feats.extend(build_indicator_features(
                    pos_to_platforms.get((pos[0], pos[1]), []),
                    pos[0], pos[1], line_lookup,
                    parent_width_base=stop_props["width_base"],
                    parent_mode=stop_props["mode"]))
            group_mid_attach.append([])

    # MST connectors (Kruskal's) — produces tree topology so branches are shorter than
    # a forced chain when groups fan out from a hub rather than lying in a sequence.
    # Connectors attach at pill endpoints (first / last NN-path dot) AND at
    # interior pill vertices where the bend angle is at least 60° — see
    # `_pill_mid_attach_candidates` and stops-pill-zoom.md § "Pills and
    # connectors". Mid-attach candidates are gated by the outer-side rule:
    # the direction from the vertex to the other endpoint must lie within
    # 90° of the vertex's outer normal, so the connector always exits on
    # the outer side of the corner.
    def _candidates(i):
        if len(groups[i]) == 1:
            return [groups[i][0]]
        ends = [groups[i][0], groups[i][-1]]
        return ends + [pos for pos, _ in group_mid_attach[i]]

    def _outer_side_ok(p1, p2):
        """True iff the direction from p1 toward p2 is on the outer side of
        p1's mid-attach corner. Endpoints (not in mid_attach_tangents) always
        pass."""
        tan = mid_attach_tangents.get(p1)
        if tan is None:
            return True
        dx = (p2[0] - p1[0]) * cluster_cos_lat
        dy = (p2[1] - p1[1])
        return tan[0] * dx + tan[1] * dy > 0

    n_g = len(groups)
    mst_edges = []   # (dist, ca, cb) for all candidate edges, sorted
    for i in range(n_g):
        for j in range(i + 1, n_g):
            ea = _candidates(i)
            eb = _candidates(j)
            best_d = float("inf")
            ca, cb = None, None
            for p1 in ea:
                for p2 in eb:
                    if not _outer_side_ok(p1, p2) or not _outer_side_ok(p2, p1):
                        continue
                    d = haversine_km(p1[0], p1[1], p2[0], p2[1])
                    if d < best_d:
                        best_d, ca, cb = d, p1, p2
            if ca is None:
                # No candidate pair passed the outer-side filter — fall back
                # to endpoint-only (should be rare; mid-attach filters only
                # ever remove candidates, never all of them, since endpoints
                # are always eligible).
                ea_ends = [groups[i][0]] if len(groups[i]) == 1 else [groups[i][0], groups[i][-1]]
                eb_ends = [groups[j][0]] if len(groups[j]) == 1 else [groups[j][0], groups[j][-1]]
                ca, cb = ea_ends[0], eb_ends[0]
                best_d = haversine_km(ca[0], ca[1], cb[0], cb[1])
                for p1 in ea_ends:
                    for p2 in eb_ends:
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

    # First pass: run Kruskal to pick the MST edges without curving them.
    chosen_edges = []
    for best_d, ca, cb, i, j in mst_edges:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj
            chosen_edges.append((ca, cb, i, j))

    # Overshoot rescan. For each chosen edge, compute the actual curved
    # connector length (with disc tangents free). If it exceeds
    # OVERSHOOT_FACTOR × chord length, the chord metric picked a
    # candidate whose forced arc geometry produces a large loop — rescan
    # the (i, j) group pair's candidates using curved length as the
    # metric, replacing (ca, cb) if a strictly shorter curve is found.
    # Topology is unchanged (edge still connects the same group pair).
    # See stops-pill-zoom.md § "Curved-length overshoot rescan".
    OVERSHOOT_FACTOR = 1.5

    def _connector_length_m(coords):
        tot = 0.0
        for k in range(1, len(coords)):
            tot += haversine_km(coords[k-1][0], coords[k-1][1],
                                coords[k][0],   coords[k][1]) * 1000.0
        return tot

    def _curve_length_for(p1, p2, i, j):
        coords, _, _, _ = _curve_connector(
            p1, p2, groups[i], groups[j], cluster_cos_lat, mode,
            anchor_a=None, anchor_b=None,
            mid_attach_tangents=mid_attach_tangents)
        return _connector_length_m(coords)

    rescanned = []
    for edge in chosen_edges:
        ca, cb, i, j = edge
        chord_m = haversine_km(ca[0], ca[1], cb[0], cb[1]) * 1000.0
        if chord_m == 0.0:
            rescanned.append(edge)
            continue
        curved_m = _curve_length_for(ca, cb, i, j)
        if curved_m <= OVERSHOOT_FACTOR * chord_m:
            rescanned.append(edge)
            continue
        # Overshoot — rescan (i, j) candidates by curved length.
        ea = _candidates(i)
        eb = _candidates(j)
        best_len = curved_m
        best_pair = (ca, cb)
        for p1 in ea:
            for p2 in eb:
                if not _outer_side_ok(p1, p2) or not _outer_side_ok(p2, p1):
                    continue
                if p1 == ca and p2 == cb:
                    continue
                l = _curve_length_for(p1, p2, i, j)
                if l < best_len:
                    best_len = l
                    best_pair = (p1, p2)
        rescanned.append((best_pair[0], best_pair[1], i, j))
    chosen_edges = rescanned

    # Sort chosen edges by disc-anchoring priority. Pill ↔ pill connectors
    # touch no disc state and run first in any order. Disc-incident connectors
    # follow, sorted by:
    #   - max line count at either endpoint (descending) — the more heavily
    #     served stop dictates the orientation it sees most often;
    #   - pill ↔ disc before disc ↔ disc — a pill end carries a real geometric
    #     direction, more authoritative than a chord between two free discs;
    #   - lexicographic on endpoint coords for a stable final tiebreak.
    def line_count_at(pos):
        return len(pos_to_platforms.get((pos[0], pos[1]), ()))

    def edge_sort_key(edge):
        _ca, _cb, i, j = edge
        disc_a = len(groups[i]) == 1
        disc_b = len(groups[j]) == 1
        if not (disc_a or disc_b):
            return (0, 0, 0, _ca, _cb)  # pill ↔ pill — process first
        line_max = max(line_count_at(_ca), line_count_at(_cb))
        type_key = 1 if (disc_a and disc_b) else 0  # 0 = pill-disc, 1 = disc-disc
        return (1, -line_max, type_key, _ca, _cb)

    chosen_edges.sort(key=edge_sort_key)

    # Per-cluster strategy choice: only worth doing when there's at least one
    # disc — pure pill ↔ pill clusters produce identical output under both
    # strategies. The fixed-cardinal run ignores `chosen_edges` ordering since
    # its anchors are pre-set and never change. Rail clusters skip the
    # fixed-cardinal alternative entirely and disable the cardinal snap in
    # the anchoring run — rail tracks frequently run at arbitrary angles
    # where compass alignment would distort the frame.
    any_disc = any(len(grp) == 1 for grp in groups)
    is_rail_cluster = mode in RAIL_MODES
    if any_disc and not is_rail_cluster:
        lines = _collect_cluster_line_polylines(cluster_stops, line_lookup)
        tol_sq = (SCORE_ON_LINE_TOL_M / 111000.0) ** 2
        connectors_anchor = _emit_connectors(chosen_edges, groups, cluster_cos_lat, mode,
                                             fixed_cardinal=False,
                                             mid_attach_tangents=mid_attach_tangents)
        connectors_cardinal = _emit_connectors(chosen_edges, groups, cluster_cos_lat, mode,
                                               fixed_cardinal=True,
                                               mid_attach_tangents=mid_attach_tangents)
        score_anchor = _score_connectors(connectors_anchor, lines, tol_sq)
        score_cardinal = _score_connectors(connectors_cardinal, lines, tol_sq)
        if score_cardinal < score_anchor:
            chosen_connectors = connectors_cardinal
        elif score_anchor < score_cardinal:
            chosen_connectors = connectors_anchor
        else:
            # Tie on the primary score. Three-level tie-break:
            #   1. Fewer overshooting connectors (length > 1.5 × straight-line
            #      chord) wins — penalises near-semicircle detours.
            #   2. If still tied, fewer straight-fallback connectors wins —
            #      penalises strategies that couldn't build a curve.
            #   3. If still tied, cardinal wins (default visual bias).
            def _count_overshoots(connectors):
                n = 0
                for coords, _ in connectors:
                    if len(coords) < 2:
                        continue
                    chord = haversine_km(coords[0][0], coords[0][1],
                                         coords[-1][0], coords[-1][1])
                    if chord <= 0:
                        continue
                    length = sum(haversine_km(coords[k-1][0], coords[k-1][1],
                                              coords[k][0],   coords[k][1])
                                 for k in range(1, len(coords)))
                    if length > 1.5 * chord:
                        n += 1
                return n
            def _count_fallbacks(connectors):
                return sum(1 for _, is_fb in connectors if is_fb)
            ov_a = _count_overshoots(connectors_anchor)
            ov_c = _count_overshoots(connectors_cardinal)
            if ov_a != ov_c:
                chosen_connectors = connectors_anchor if ov_a < ov_c else connectors_cardinal
            else:
                fb_a = _count_fallbacks(connectors_anchor)
                fb_c = _count_fallbacks(connectors_cardinal)
                if fb_a != fb_c:
                    chosen_connectors = connectors_anchor if fb_a < fb_c else connectors_cardinal
                else:
                    chosen_connectors = connectors_cardinal
    else:
        chosen_connectors = _emit_connectors(chosen_edges, groups, cluster_cos_lat, mode,
                                             fixed_cardinal=False,
                                             snap_anchors=not is_rail_cluster,
                                             mid_attach_tangents=mid_attach_tangents)

    for coords, _ in chosen_connectors:
        feats.append(make_feat(coords, "connector"))

    return feats


# =============================================================================
# Clustering


# =============================================================================
# Main
# =============================================================================

