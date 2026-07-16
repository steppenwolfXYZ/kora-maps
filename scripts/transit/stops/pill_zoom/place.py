"""Dot placement: measure_pill_geometry, leftover_fill, parallel-stub drop
and the top-level coordinate_dots_global_stab driver."""
from collections import defaultdict
from itertools import permutations
from math import atan2, cos, degrees, pi, radians, sin, sqrt

from _state import *  # noqa: F401,F403
from _state import _DIAG_BARS, _M_PER_DEG, _STABBED_PAIRS  # underscore names skipped by *
from stops.extent import _funicular_snap_override, _length_key, _platform_extent, _resolve_length
from geometry import _cum_dist_m, _directional_tangent_at, _interp_at, _meters_per_deg, _project_meters, haversine_km, snap_to_line
from stops.pill_zoom.geom import (
    _angular_dist_mod_pi, _circular_median_mod_pi, _extent_intersect_axis,
    _mean_unit_tangent, _perpendicular_sweep, _place_dot_on_extent,
    _sigma_clumps, _smoothed_tangent_at, _stop_tangent, _tangent_groups,
)
from stops.pill_zoom.lines import dominant_line
from stops.pill_zoom.nn_path import nearest_neighbor_path
from stops.pill_zoom.geom import _expand_sigma_clump
from stops.pill_zoom.nn_path import _dedup_stop_positions, _pos_to_platforms
from stops.pill_zoom.options import (
    ON_PLATFORM_PENALTY, ON_PLATFORM_TOL_M,
    _apply_option, _pick_option_single_group, _pick_options_multi_group,
    _record_diag_bar, _should_split_at_gap,
)


def _segment_on_platform(p1, p2, extents, tol_sq):
    """True if both endpoints of segment (p1, p2) are within sqrt(tol_sq) of
    the same platform extent polyline. tol_sq is the squared tolerance in the
    same coordinate space as the points and extents (scaled-degree space).
    """
    for ext in extents:
        if len(ext) < 2:
            continue
        s1 = snap_to_line(p1[0], p1[1], ext)
        if (p1[0] - s1[0]) ** 2 + (p1[1] - s1[1]) ** 2 > tol_sq:
            continue
        s2 = snap_to_line(p2[0], p2[1], ext)
        if (p2[0] - s2[0]) ** 2 + (p2[1] - s2[1]) ** 2 > tol_sq:
            continue
        return True
    return False


def _measure_pill_geometry(cluster_stops, cos_lat=1.0):
    """Score a placement: total pill geometry length, with connectors counted
    at half weight, plus a platform-overlap penalty (segments running along a
    platform extent are scaled by ON_PLATFORM_PENALTY). Replicates
    make_pill_features's NN-path + per-gap split + MST connector logic without
    emitting features.

    Inside coordinate_dots_global_stab the cluster runs in equal-distance
    space (lon × cos_lat). haversine_km expects true lon/lat and applies its
    own cos(lat) on the longitude term, so feeding it scaled coords would
    double-apply the factor (cos⁴ instead of cos²) and under-weight east-west
    distance — enough to flip the option ranking on real clusters. Pass that
    same cos_lat here and the function builds a local-unscaled view before
    measuring; callers in true lon/lat space leave cos_lat at the 1.0 default.
    """
    if cos_lat != 1.0:
        cluster_stops = [
            {**s,
             "lon": s["lon"] / cos_lat,
             "extent": ([(x / cos_lat, y) for x, y in s["extent"]]
                        if s.get("extent") else s.get("extent"))}
            for s in cluster_stops
        ]

    positions = _dedup_stop_positions(cluster_stops)
    if len(positions) < 2:
        return 0.0
    path = nearest_neighbor_path(positions)

    pos_to_platforms = _pos_to_platforms(cluster_stops, positions)

    # Unique platform extents in this cluster (dedupe by object identity —
    # the same per-(line, stop) extent isn't shared, but we don't need to
    # care since the on-platform predicate stops at the first hit).
    extents = []
    seen = set()
    for s in cluster_stops:
        ext = s.get("extent")
        if not ext or len(ext) < 2:
            continue
        if id(ext) in seen:
            continue
        seen.add(id(ext))
        extents.append(ext)
    tol_sq = (ON_PLATFORM_TOL_M / 111000.0) ** 2

    def weighted(p1, p2, base_factor):
        d = haversine_km(p1[0], p1[1], p2[0], p2[1])
        if _segment_on_platform(p1, p2, extents, tol_sq):
            return d * base_factor * ON_PLATFORM_PENALTY
        return d * base_factor

    split_indices = [
        k for k in range(len(path) - 1)
        if _should_split_at_gap(
            path, k,
            haversine_km(path[k][0], path[k][1],
                         path[k + 1][0], path[k + 1][1]),
            pos_to_platforms,
            cos_lat=cos_lat)
    ]

    if not split_indices:
        # Whole path is one pill — no connectors.
        return sum(weighted(path[k], path[k + 1], 1.0)
                   for k in range(len(path) - 1))

    groups = []
    prev = 0
    for idx in split_indices:
        groups.append(path[prev:idx + 1])
        prev = idx + 1
    groups.append(path[prev:])

    # Pill segments — internal edges of each group.
    pill_total = 0.0
    for grp in groups:
        if len(grp) < 2:
            continue
        for k in range(len(grp) - 1):
            pill_total += weighted(grp[k], grp[k + 1], 1.0)

    # Connector segments — MST between groups (singletons kept as own group
    # so make_pill_features's connector geometry is mirrored). Connectors
    # attach only at pill endpoints, so the candidate set per group is the
    # two ends (one point for singletons). Retain the actual (p1, p2) chosen
    # per edge so the on-platform check sees the same segment that would
    # be drawn.
    n_g = len(groups)
    mst_edges = []
    for i in range(n_g):
        for j in range(i + 1, n_g):
            ea = [groups[i][0]] if len(groups[i]) == 1 else [groups[i][0], groups[i][-1]]
            eb = [groups[j][0]] if len(groups[j]) == 1 else [groups[j][0], groups[j][-1]]
            best_d = float("inf")
            best_pair = None
            for p1 in ea:
                for p2 in eb:
                    d = haversine_km(p1[0], p1[1], p2[0], p2[1])
                    if d < best_d:
                        best_d = d
                        best_pair = (p1, p2)
            mst_edges.append((best_d, i, j, best_pair))
    mst_edges.sort(key=lambda e: e[0])
    parent = list(range(n_g))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    connector_total = 0.0
    for _, i, j, pair in mst_edges:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj
            connector_total += weighted(pair[0], pair[1], 0.5)

    return pill_total + connector_total


# Per-call cap on placement trials in _leftover_fill. The early picks
# dominate the outcome (the first placement anchors the cluster, the
# second relative to it, and by the fourth or fifth the geometry is mostly
# fixed), so the budget is spent enumerating length-k prefixes of the
# ordering — the deepest k whose prefix count stays within budget. At 50:
# n ≤ 4 → full enumeration; n = 5–7 → first two picks; n ≥ 8 → first pick.
LEFTOVER_TRIAL_BUDGET = 50


def _snap_to_extent(p: dict, target_x: float, target_y: float) -> None:
    """Move p's lon/lat to the point on its extent polyline closest to
    (target_x, target_y). No-op if the extent is missing or degenerate
    (fewer than two distinct vertices) — a degenerate leftover keeps
    its raw snap because there is no extent to snap along.
    """
    ext = p.get("extent")
    if not ext or len(ext) < 2:
        return
    if all(pt[0] == ext[0][0] and pt[1] == ext[0][1] for pt in ext):
        return
    p["lon"], p["lat"] = snap_to_line(target_x, target_y, ext)


def _leftover_fill(cluster: list, leftovers: list, placed_ids: set,
                    raw_snapshot: dict, gtfs_centroid: tuple,
                    cos_lat: float = 1.0) -> None:
    """Place each leftover platform at the point on its own extent closest
    to the nearest already-placed dot in the cluster. The first leftover
    in a cluster with no bar dots bootstraps to the GTFS centroid instead.

    Order is decided by enumerating every length-k prefix of the leftover
    list, where k is the deepest such that the prefix count stays within
    LEFTOVER_TRIAL_BUDGET; the tail is completed in a deterministic
    fallback order (width_base desc, osm_id asc). The trial that yields
    the shortest pill + 0.5 × connector length wins.

    Degenerate-extent leftovers stay at their raw snap and do not
    participate in the ordering trial — there is no extent to snap along.
    """
    placeable = [
        p for p in leftovers
        if p.get("extent") and len(p["extent"]) >= 2
        and any(pt[0] != p["extent"][0][0] or pt[1] != p["extent"][0][1]
                for pt in p["extent"])
    ]
    if not placeable:
        return

    bar_dot_positions = [(p["lon"], p["lat"]) for p in cluster
                          if id(p) in placed_ids]

    n = len(placeable)
    det_tail_order = sorted(
        range(n),
        key=lambda i: (-placeable[i].get("width_base", 0.0),
                       str(placeable[i].get("osm_id", ""))))

    # Deepest prefix length k such that n × (n-1) × ... × (n-k+1) ≤ budget.
    k = 1
    count = n
    while k < n:
        next_count = count * (n - k)
        if next_count > LEFTOVER_TRIAL_BUDGET:
            break
        count = next_count
        k += 1

    best_length = None
    best_positions = None
    for prefix in permutations(range(n), k):
        prefix_set = set(prefix)
        order = list(prefix) + [i for i in det_tail_order
                                 if i not in prefix_set]

        for p in placeable:
            p["lon"], p["lat"] = raw_snapshot[id(p)]
        placed_so_far = list(bar_dot_positions)
        for idx in order:
            p = placeable[idx]
            if placed_so_far:
                # Nearest-already-placed: try snapping each placed dot
                # onto p's extent; keep the (extent-snap, placed) pair
                # with smallest distance. The min of "closest extent
                # point per placed dot" is the closest extent point to
                # the nearest placed dot.
                best_d_sq = float("inf")
                best_pt = None
                ext = p["extent"]
                for px, py in placed_so_far:
                    cx, cy = snap_to_line(px, py, ext)
                    d_sq = (px - cx) ** 2 + (py - cy) ** 2
                    if d_sq < best_d_sq:
                        best_d_sq = d_sq
                        best_pt = (cx, cy)
                if best_pt is not None:
                    p["lon"], p["lat"] = best_pt
            else:
                _snap_to_extent(p, gtfs_centroid[0], gtfs_centroid[1])
            placed_so_far.append((p["lon"], p["lat"]))
        length = _measure_pill_geometry(cluster, cos_lat=cos_lat)
        if best_length is None or length < best_length:
            best_length = length
            best_positions = {id(p): (p["lon"], p["lat"]) for p in placeable}

    if best_positions is not None:
        for p in placeable:
            p["lon"], p["lat"] = best_positions[id(p)]


def _platform_number(code: str) -> str:
    """Leading digit run of a GTFS platform_code. Strips any sector suffix
    so "12", "12A-C", "12D-F", "13AB" all reduce to their bare numeric
    platform identifier. Returns "" if the code is empty or starts with a
    non-digit."""
    n = 0
    while n < len(code) and code[n].isdigit():
        n += 1
    return code[:n]


def _on_same_track(p: dict, q: dict, threshold_sq: float) -> bool:
    """True when p's snap position lies within sqrt(threshold_sq) of q's
    extent polyline, or symmetrically q's snap onto p's extent. Catches
    the pfaedle-snap-error case where a stop is routed onto a different
    platform's OSM rail way: its snap position then sits on that other
    extent even though the two GTFS platform_codes disagree. Symmetric
    so a degenerate extent on one side doesn't blind the test — the
    side with a usable extent still answers. Coordinates are in the
    scaled (cos_lat) cluster space.
    """
    p_ext = p.get("extent")
    q_ext = q.get("extent")
    if q_ext and len(q_ext) >= 2:
        sx, sy = snap_to_line(p["lon"], p["lat"], q_ext)
        dx, dy = p["lon"] - sx, p["lat"] - sy
        if dx * dx + dy * dy <= threshold_sq:
            return True
    if p_ext and len(p_ext) >= 2:
        sx, sy = snap_to_line(q["lon"], q["lat"], p_ext)
        dx, dy = q["lon"] - sx, q["lat"] - sy
        if dx * dx + dy * dy <= threshold_sq:
            return True
    return False


def _find_parallel_stub_drop(cluster: list, placed_leftovers: list):
    """Scan just-placed leftovers in a rail (train) cluster for one that
    is co-located with another cluster member, by either of two tests
    (see the SAME_TRACK_PERP_M block at the top of the file for the
    full rationale):
      (a) `platform_code` numeric prefixes match — same physical platform
          per GTFS, different sectors;
      (b) the two extents geometrically coincide within SAME_TRACK_PERP_M
          — pfaedle has put both stops onto the same OSM rail way, so
          the dots overlap on the rendered map regardless of what GTFS
          says.
    Returns (stop_to_drop, absorbing_position) for the first leftover
    that finds such a partner, or None. The absorbing position is the
    matching cluster member nearest to the leftover; coincident
    neighbours (within DEDUP_TOL_M) are skipped because they represent
    the same physical dot that _dedup_stop_positions will collapse
    later. Coordinates are in the scaled (cos_lat) cluster space.

    Leftovers whose platform_code is missing or non-numeric AND whose
    extent doesn't overlap any other member's are never dropped:
    without either signal we cannot decide whether the leftover is a
    redundant sector or a genuinely separate platform, and leaving a
    possibly-redundant dot visible beats hiding a real platform.
    """
    coincident_units = DEDUP_TOL_M / _M_PER_DEG
    coincident_sq = coincident_units * coincident_units
    same_track_units = SAME_TRACK_PERP_M / _M_PER_DEG
    same_track_sq = same_track_units * same_track_units

    for p in placed_leftovers:
        p_num = _platform_number(p.get("platform_code", ""))
        best = None  # (d_sq, q_lon, q_lat)
        for q in cluster:
            if q is p:
                continue
            q_num = _platform_number(q.get("platform_code", ""))
            same_platform = bool(p_num) and bool(q_num) and p_num == q_num
            if not same_platform and not _on_same_track(p, q, same_track_sq):
                continue
            dx = q["lon"] - p["lon"]
            dy = q["lat"] - p["lat"]
            d_sq = dx * dx + dy * dy
            if d_sq <= coincident_sq:
                continue
            if best is None or d_sq < best[0]:
                best = (d_sq, q["lon"], q["lat"])
        if best is None:
            continue
        return p, (best[1], best[2])
    return None


def coordinate_dots_global_stab(cluster: list, protection_m: float,
                                  lone_outlier_gap_m: float) -> None:
    """Tangent-group + perpendicular-sweep dot placement.

    For each tangent group of platforms (extent tangents within ~10° of
    each other), pick a central member from the inner 70 % of the group
    (closest to centroid) and sweep along that member's platform extent
    at 10 m steps. At each step the bar is perpendicular to the smoothed
    extent tangent; the position maximising scoring-stab count wins.
    Scoring-stabbed platforms (≤10° aligned, extent crosses bar) get
    their dots placed on the bar and drive its drawn span. Wrong-angle
    members whose extent crosses the bar between scoring dots are also
    placed on the bar ("covered"). Everything not placed on a bar is
    handed to _leftover_fill, which snaps each leftover to the point
    on its extent closest to the nearest already-placed dot (or to the
    GTFS centroid if no bars were placed in the cluster).
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
        # Snapshot raw (scaled) positions so the leftover fill can reset
        # leftovers between permutation trials. Also used to compute the
        # GTFS centroid bootstrap target.
        raw = {id(s): (s["lon"], s["lat"]) for s in cluster}
        n_cluster = len(cluster)
        gtfs_centroid = (
            sum(raw[id(s)][0] for s in cluster) / n_cluster,
            sum(raw[id(s)][1] for s in cluster) / n_cluster,
        )

        # Tangent groups (union-find, 10° angular tolerance mod π), then
        # σ-clump each tangent group along its mean tangent so multi-clump
        # groups (opposite ends of a long station) get a sweep per clump
        # rather than one stuck near whichever clump contains the 2-D
        # centroid.
        angle_tol = radians(12.0)
        groups = _tangent_groups(platforms, angle_tol)

        # For each σ-clump of ≥ 2 members, collect every tied max-scoring-
        # stab bar position. _expand_sigma_clump recursively peels matched
        # members off after each pass and re-σ-clumps the rest, so a clump
        # with two parallel sub-clusters on different transverse axes
        # produces two bars (one per pass) instead of one.
        #
        # Each entry carries its tangent-group id so the tie-break can scope
        # the along-tangent protection check to bars within the same group
        # — bars in different tangent groups have different orientations and
        # impose no protection on each other.
        per_group_options = []  # list of (clump, [option, ...], tgroup_id)
        for tgroup_id, group in enumerate(groups):
            if len(group) < 2:
                continue
            for clump in _sigma_clumps(group):
                for sub, options in _expand_sigma_clump(
                        clump, angle_tol, raw, lone_outlier_gap_m):
                    per_group_options.append((sub, options, tgroup_id))

        # Pick one option per group — see stops-pill-zoom.md "Tie-breaking
        # among equally-stabbing sweep positions":
        #   • Multi-group: minimise sum of pairwise bar-center distances.
        #     Tie-break by total gtfs_dist.
        #   • Single-group with > 1 tied option: enumerate options, run
        #     the leftover fill per option, pick minimum pill+0.5×connector
        #     length. Tie-break by gtfs_dist.
        #   • Single-group with one option: just take it.
        chosen = []
        if len(per_group_options) >= 2:
            chosen = _pick_options_multi_group(
                per_group_options, protection_m)
        elif len(per_group_options) == 1:
            group, options, _ = per_group_options[0]
            if len(options) > 1:
                chosen = [_pick_option_single_group(
                    group, options, cluster, platforms, raw, gtfs_centroid,
                    cos_lat=cos_lat)]
            else:
                chosen = [options[0]]

        # Apply chosen options (record _STABBED_PAIRS + diag bar geometry).
        placed_ids = set()
        for (group, _, _), option in zip(per_group_options, chosen):
            _apply_option(group, option, placed_ids, record_stabbed=True)
            _record_diag_bar(group, option)

        # Leftovers: every platform NOT placed on a bar. For rail (train)
        # clusters, repeatedly run leftover-fill and check each placed
        # leftover for a short parallel "stub" connector to its nearest
        # other placed dot; drop and re-run until nothing more matches.
        leftovers = [p for p in platforms if id(p) not in placed_ids]
        if leftovers:
            _, dom_mode, _, _ = dominant_line(cluster)
            is_rail_cluster = (dom_mode == "train")
            remaining = list(leftovers)
            while remaining:
                _leftover_fill(platforms, remaining, placed_ids, raw,
                                gtfs_centroid, cos_lat=cos_lat)
                if not is_rail_cluster:
                    break
                dropped = _find_parallel_stub_drop(cluster, remaining)
                if dropped is None:
                    break
                p, absorbing_pos = dropped
                # Snap the dropped stop onto the absorbing dot; downstream
                # _dedup_stop_positions collapses it into that dot, so the
                # stop's line still surfaces in the cluster's lines_json
                # (popup) but no extra dot/connector is rendered.
                p["lon"], p["lat"] = absorbing_pos
                remaining = [x for x in remaining if x is not p]
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


