"""Far-zoom stop-dot positioning: place the one far-zoom dot per station at
the dominant intersection / largest pill. See stops-far-zoom-markers.md."""
from collections import defaultdict
from math import cos, radians, sqrt

from _state import *  # noqa: F401,F403
from geometry import (
    _meters_per_deg, _polyline_midpoint, flatten_coords,
)

# =============================================================================
# Far-zoom dot positioning — see .claude/concepts/stops-far-zoom-markers.md
# =============================================================================

def _logical_line_key(oid, line_lookup):
    """Return the logical-line key `(ref, mode, agency_id)` for an osm_id.
    Direction and terminus variants of one route share this key — counting
    by it (not by osm_id) is what prevents the four parallel direction-
    variants of one bus from scoring as a four-way intersection or as a
    four-line pill. See stops-far-zoom-markers.md § 'Intersection search'."""
    info = line_lookup.get(oid) or {}
    ref = info.get("gtfs_ref") or info.get("ref") or ""
    return (ref, info.get("mode") or "", info.get("agency_id") or "")


def _key_fweighted_map(cluster, line_lookup):
    """Map each logical-line key present in the cluster to the max
    `f_weighted` (weighted trips/h) across its osm_ids — used by the
    far-zoom rule's combined-frequency scoring. Max over osm_ids of one
    logical line because direction variants can carry slightly different
    per-direction values."""
    out = {}
    for s in cluster:
        oid = str(s.get("osm_id", ""))
        if not oid:
            continue
        info = line_lookup.get(oid)
        if not info:
            continue
        key = _logical_line_key(oid, line_lookup)
        fw = info.get("f_weighted", 0.0) or 0.0
        if key not in out or fw > out[key]:
            out[key] = fw
    return out


def _snap_centre_m(cluster, mx, my):
    """Arithmetic centre of the cluster's pre-placement pfaedle snaps,
    in local metric coords. Falls back to post-placement coords for
    members that don't carry a snap (none should, but be defensive)."""
    sx = sum(s.get("snap_lon", s["lon"]) for s in cluster) / len(cluster)
    sy = sum(s.get("snap_lat", s["lat"]) for s in cluster) / len(cluster)
    return sx * mx, sy * my


def _cluster_xy_m(cluster, mx, my):
    """Per-member (x_m, y_m, osm_id) — post-placement positions used to
    match cluster members against pill / disc geometry."""
    out = []
    for s in cluster:
        oid = str(s.get("osm_id", "")) or str(id(s))
        out.append((s["lon"] * mx, s["lat"] * my, oid))
    return out


def _osm_ids_on_polyline_m(cluster_xy_m, poly_m, tol_sq):
    """Distinct osm_ids whose placed position sits within sqrt(tol_sq) of
    the polyline (any segment). When `poly_m` is a single point, this
    reduces to a proximity check around that point — used for endpoint
    discs."""
    if not poly_m:
        return set()
    oids = set()
    for x, y, oid in cluster_xy_m:
        if oid in oids:
            continue
        if len(poly_m) == 1:
            ax, ay = poly_m[0]
            if (x - ax) ** 2 + (y - ay) ** 2 <= tol_sq:
                oids.add(oid)
            continue
        for k in range(len(poly_m) - 1):
            ax, ay = poly_m[k]
            bx, by = poly_m[k + 1]
            dx, dy = bx - ax, by - ay
            len_sq = dx * dx + dy * dy
            if len_sq == 0:
                cx, cy = ax, ay
            else:
                t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / len_sq))
                cx, cy = ax + t * dx, ay + t * dy
            if (x - cx) ** 2 + (y - cy) ** 2 <= tol_sq:
                oids.add(oid)
                break
    return oids


def _segment_intersection_xy(a1, a2, b1, b2):
    """Two finite line segments in any 2-D space. Returns the single
    intersection point (x, y) when both interiors meet, else None.
    Parallel / colinear segments return None — colinear overlap doesn't
    produce a meaningful 'intersection' for the far-zoom rule (parallel
    lines on one street are handled by the stop-snap candidate path)."""
    x1, y1 = a1
    x2, y2 = a2
    x3, y3 = b1
    x4, y4 = b2
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if denom == 0:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = ((x1 - x3) * (y1 - y2) - (y1 - y3) * (x1 - x2)) / denom
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
    return None


def _polyline_crossings_m(line_a_m, line_b_m, bbox_m):
    """All pairwise segment-segment crossings between two polylines (in
    metric coords) that fall inside bbox_m=(xmin, ymin, xmax, ymax)."""
    xmin, ymin, xmax, ymax = bbox_m
    out = []
    for i in range(len(line_a_m) - 1):
        a1, a2 = line_a_m[i], line_a_m[i + 1]
        sa_xmin = a1[0] if a1[0] < a2[0] else a2[0]
        sa_xmax = a1[0] if a1[0] > a2[0] else a2[0]
        sa_ymin = a1[1] if a1[1] < a2[1] else a2[1]
        sa_ymax = a1[1] if a1[1] > a2[1] else a2[1]
        if sa_xmax < xmin or sa_xmin > xmax or sa_ymax < ymin or sa_ymin > ymax:
            continue
        for j in range(len(line_b_m) - 1):
            b1, b2 = line_b_m[j], line_b_m[j + 1]
            sb_xmin = b1[0] if b1[0] < b2[0] else b2[0]
            sb_xmax = b1[0] if b1[0] > b2[0] else b2[0]
            sb_ymin = b1[1] if b1[1] < b2[1] else b2[1]
            sb_ymax = b1[1] if b1[1] > b2[1] else b2[1]
            if sa_xmax < sb_xmin or sb_xmax < sa_xmin or \
               sa_ymax < sb_ymin or sb_ymax < sa_ymin:
                continue
            ip = _segment_intersection_xy(a1, a2, b1, b2)
            if ip is None:
                continue
            if xmin <= ip[0] <= xmax and ymin <= ip[1] <= ymax:
                out.append(ip)
    return out


def _far_zoom_intersection_search(cluster, line_lookup):
    """Far-zoom dot intersection search per
    .claude/concepts/stops-far-zoom-markers.md § 'Intersection search'.
    Returns ((lon, lat), all_lines_present) of the highest-scoring
    candidate, or None when no candidate has at least 2 distinct logical
    lines passing within tolerance. `all_lines_present` is True when every
    in-scope logical line passes within tolerance of the winning
    candidate — read by the bad-intersection gate to skip the
    centroid-distance check on full-cluster junctions.

    Candidate set: distinct pre-placement pfaedle-snapped stop positions ∪
    pairwise polyline crossings between in-scope lines. Score: sum of
    `f_weighted` (weighted trips/h) across in-scope **logical lines**
    (`(ref, mode, agency_id)` — not `osm_id`s) with at least one polyline
    passing within FAR_ZOOM_INTERSECTION_TOL_M of the candidate. Direction
    and terminus variants of one route share a logical key and contribute
    once. A candidate must have ≥2 distinct logical lines near it to
    qualify — a single line at a point is not an intersection regardless
    of its frequency. Ties: closest to the cluster snap-centre."""
    seen_oids = set()
    osm_ids = []
    for s in cluster:
        oid = str(s.get("osm_id", ""))
        if not oid or oid in seen_oids:
            continue
        seen_oids.add(oid)
        osm_ids.append(oid)
    if len(osm_ids) < 2:
        return None

    # Per-osm_id polylines, tagged with their logical-line key. We need the
    # polylines individually for proximity tests but score by distinct keys.
    lines = []  # [(polyline, logical_key)]
    for oid in osm_ids:
        info = line_lookup.get(oid)
        if not info:
            continue
        flat = flatten_coords(info.get("coords") or [])
        if len(flat) < 2:
            continue
        lines.append((flat, _logical_line_key(oid, line_lookup)))
    distinct_keys = {k for _, k in lines}
    if len(distinct_keys) < 2:
        return None

    mean_lat = sum(s.get("snap_lat", s["lat"]) for s in cluster) / len(cluster)
    mx, my = _meters_per_deg(mean_lat)
    centre_m = _snap_centre_m(cluster, mx, my)

    lines_m = [
        ([(p[0] * mx, p[1] * my) for p in flat], key)
        for flat, key in lines
    ]

    xs = [s.get("snap_lon", s["lon"]) * mx for s in cluster]
    ys = [s.get("snap_lat", s["lat"]) * my for s in cluster]
    # Pad = 1.5 × mean stop distance from the snap centre. Scales with the
    # cluster's own footprint so off-platform junctions (e.g. roundabouts
    # ~60 m from the platforms at Bern Viktoriaplatz) stay in scope without
    # over-reaching into neighbouring clusters in dense city grids.
    mean_stop_dist = sum(
        sqrt((xs[i] - centre_m[0]) ** 2 + (ys[i] - centre_m[1]) ** 2)
        for i in range(len(xs))
    ) / len(xs)
    pad = 1.5 * mean_stop_dist
    bbox_m = (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)

    candidates_m = []
    seen_keys = set()

    def add(pt_m):
        key = (round(pt_m[0], 3), round(pt_m[1], 3))
        if key in seen_keys:
            return
        seen_keys.add(key)
        candidates_m.append(pt_m)

    for s in cluster:
        add((s.get("snap_lon", s["lon"]) * mx,
             s.get("snap_lat", s["lat"]) * my))
    # Pairwise crossings only between polylines of distinct logical keys.
    # Two direction variants of one route share a key and their crossings
    # are irrelevant for the intersection rule.
    for i in range(len(lines_m)):
        for j in range(i + 1, len(lines_m)):
            if lines_m[i][1] == lines_m[j][1]:
                continue
            for ip in _polyline_crossings_m(lines_m[i][0], lines_m[j][0], bbox_m):
                add(ip)

    if not candidates_m:
        return None

    tol_sq = FAR_ZOOM_INTERSECTION_TOL_M * FAR_ZOOM_INTERSECTION_TOL_M

    def line_passes_near(line_m, p_m):
        for k in range(len(line_m) - 1):
            ax, ay = line_m[k]
            bx, by = line_m[k + 1]
            dx, dy = bx - ax, by - ay
            len_sq = dx * dx + dy * dy
            if len_sq == 0:
                cx, cy = ax, ay
            else:
                t = max(0.0, min(1.0, ((p_m[0] - ax) * dx + (p_m[1] - ay) * dy) / len_sq))
                cx, cy = ax + t * dx, ay + t * dy
            if (p_m[0] - cx) ** 2 + (p_m[1] - cy) ** 2 <= tol_sq:
                return True
        return False

    fw_by_key = _key_fweighted_map(cluster, line_lookup)
    total_keys = len(distinct_keys)

    best_score = 0.0
    best = None
    best_dist_sq = float("inf")
    best_keys_count = 0
    for cm in candidates_m:
        keys_near = set()
        for lm, key in lines_m:
            if key in keys_near:
                continue
            if line_passes_near(lm, cm):
                keys_near.add(key)
        if len(keys_near) < 2:
            continue
        score = sum(fw_by_key.get(k, 0.0) for k in keys_near)
        d_sq = (cm[0] - centre_m[0]) ** 2 + (cm[1] - centre_m[1]) ** 2
        if (score > best_score) or (score == best_score and d_sq < best_dist_sq):
            best_score = score
            best = cm
            best_dist_sq = d_sq
            best_keys_count = len(keys_near)

    if best is None:
        return None
    return (best[0] / mx, best[1] / my), best_keys_count == total_keys


def _largest_pill_or_disc_position(pill_feats, cluster, line_lookup):
    """Far-zoom position from the pill or endpoint disc with the highest
    rank, per .claude/concepts/stops-far-zoom-markers.md § 'Position rule
    by mode family' tiebreak rules:

      1. sum of `f_weighted` (weighted trips/h) across the candidate's
         logical lines desc — logical-line keys are (ref, mode, agency_id)
         so direction and terminus variants of one route contribute once.
      2. closer to cluster snap-centre.

    Pill and disc features compete in one ranking — pill geometry is not
    privileged over disc geometry. Returns (lon, lat) or None when no
    pill / endpoint feature exists.
    """
    if not pill_feats or not cluster:
        return None

    mean_lat = sum(s.get("snap_lat", s["lat"]) for s in cluster) / len(cluster)
    mx, my = _meters_per_deg(mean_lat)
    cluster_xy_m = _cluster_xy_m(cluster, mx, my)
    centre_m = _snap_centre_m(cluster, mx, my)
    tol_sq = DEDUP_TOL_M * DEDUP_TOL_M
    fw_by_key = _key_fweighted_map(cluster, line_lookup)

    best_key = None
    best_pos = None
    for feat in pill_feats:
        props = feat.get("properties") or {}
        ftype = props.get("feature_type")
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []

        if ftype == "pill":
            if len(coords) < 2:
                continue
            coords_m = [(p[0] * mx, p[1] * my) for p in coords]
            oids = _osm_ids_on_polyline_m(cluster_xy_m, coords_m, tol_sq)
            if not oids:
                continue
            pos = _polyline_midpoint(coords)
        elif ftype == "endpoint" and geom.get("type") == "Point":
            if len(coords) < 2:
                continue
            oids = _osm_ids_on_polyline_m(
                cluster_xy_m, [(coords[0] * mx, coords[1] * my)], tol_sq)
            if not oids:
                continue
            pos = (coords[0], coords[1])
        else:
            continue

        line_keys = {_logical_line_key(oid, line_lookup) for oid in oids}
        sum_fw = sum(fw_by_key.get(k, 0.0) for k in line_keys)
        d_sq = (pos[0] * mx - centre_m[0]) ** 2 + (pos[1] * my - centre_m[1]) ** 2
        # Ranking key: (-sum_fw, dist_sq). Lower tuple wins under `<`;
        # negating sum_fw sorts the highest combined frequency first.
        key = (-sum_fw, d_sq)
        if best_key is None or key < best_key:
            best_key = key
            best_pos = pos

    return best_pos


def _intersection_within_pill_spread(pos, pill_feats, cluster,
                                     all_lines_present):
    """Bad-intersection fallback gate per
    .claude/concepts/stops-far-zoom-markers.md § 'Bad-intersection gate'.
    Keeps the intersection candidate only when its distance to the cluster
    snap centre is no greater than the mean distance of pill midpoints and
    endpoint-disc positions to that same centre. Catches cases like Bern
    Breitenrain where the intersection scores at a bus-only platform
    ~80 m outside the rendered tram pill; usual intersections sit inside
    the pill spread and pass.

    When `all_lines_present` is True (every in-scope logical line passes
    within tolerance of the candidate), the gate is skipped — a junction
    that all the cluster's lines actually meet at is the correct service
    node regardless of how far it sits from the platform centroid (Bern
    Viktoriaplatz: tram 9 + bus 10 meet at a roundabout ~60 m from the
    snap centre, outside the platform-derived budget).

    Returns True when there are no pills / discs to compare against — in
    that case the intersection is the only signal available and is kept.
    """
    if all_lines_present:
        return True
    if not cluster or not pill_feats:
        return True
    mean_lat = sum(s.get("snap_lat", s["lat"]) for s in cluster) / len(cluster)
    mx, my = _meters_per_deg(mean_lat)
    centre_m = _snap_centre_m(cluster, mx, my)
    dists = []
    for feat in pill_feats:
        props = feat.get("properties") or {}
        ftype = props.get("feature_type")
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if ftype == "pill":
            if len(coords) < 2:
                continue
            ref = _polyline_midpoint(coords)
        elif ftype == "endpoint" and geom.get("type") == "Point":
            if len(coords) < 2:
                continue
            ref = (coords[0], coords[1])
        else:
            continue
        dx = ref[0] * mx - centre_m[0]
        dy = ref[1] * my - centre_m[1]
        dists.append(sqrt(dx * dx + dy * dy))
    if not dists:
        return True
    mean_dist = sum(dists) / len(dists)
    px = pos[0] * mx - centre_m[0]
    py = pos[1] * my - centre_m[1]
    return sqrt(px * px + py * py) <= mean_dist


def far_zoom_dot_position(cluster, pill_feats, line_lookup, fallback_pos,
                          rail_like):
    """Pick the far-zoom dot position per
    .claude/concepts/stops-far-zoom-markers.md § 'Position rule by mode family'.

    Rail-like (train + mountain rebucketed_rail / rack) skips the
    intersection search; every other mode runs it first. The intersection
    result is additionally gated by `_intersection_within_pill_spread`
    (§ 'Bad-intersection gate') — a result too far from the rendered pill
    geometry is discarded and the chain falls through. Falls through to
    `fallback_pos` (the existing centroid of placed positions) when
    nothing else matches."""
    if not rail_like:
        res = _far_zoom_intersection_search(cluster, line_lookup)
        if res is not None:
            pos, all_lines_present = res
            if _intersection_within_pill_spread(
                    pos, pill_feats, cluster, all_lines_present):
                return pos
    pos = _largest_pill_or_disc_position(pill_feats, cluster, line_lookup)
    if pos is not None:
        return pos
