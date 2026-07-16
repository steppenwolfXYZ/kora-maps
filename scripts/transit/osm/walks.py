"""OSM way index + rail / street walk helpers.

Used by `extent/fill.py` at train, tram, and bus terminals: when a stop's
platform extent needs more range than the trip polyline provides, the walk
follows a matching OSM way (rail track for trains, street or tram track
for buses / trams) until it hits `target_length_m` metres, a junction it
can't cross, or the end of the network.

See:
  .claude/concepts/implemented/stop-extent-osm-walk.md
"""
import json
from collections import defaultdict
from math import cos, radians, sqrt

from common import load_transit_cfg
from geometry import (
    _cum_dist_m,
    _directional_tangent_at,
    _interp_at,
    _project_meters,
)

_PILL_CFG = load_transit_cfg().get("pill_rendering", {}) or {}

OSM_MATCH_RADIUS_M              = float(_PILL_CFG.get("osm_match_radius_m", 5.0))
OSM_MATCH_MAX_TANGENT_DIFF_DEG  = float(_PILL_CFG.get("osm_match_max_tangent_diff_deg", 15.0))
OSM_FALLBACK_MAX_STRAIGHT_M     = float(_PILL_CFG.get("osm_fallback_max_straight_m", 50.0))
TERMINAL_SNAP_TOLERANCE_M       = 20.0
ROAD_MATCH_RADIUS_M             = float(_PILL_CFG.get("road_match_radius_m", 5.0))
ROAD_MATCH_MAX_TANGENT_DIFF_DEG = float(_PILL_CFG.get("road_match_max_tangent_diff_deg", 45.0))


class _RailIndex:
    """Spatial grid + adjacency over OSM way LineStrings (rail, tram, or
    street networks from step 03). Used by `_osm_rail_walk` to extend
    train-line polylines at terminal stops along the actual rail track, and
    by `_osm_street_walk` for the tram/bus stop-extent fill.

    `way_props` holds each way's retained tags (highway / railway / name —
    they feed the street walk's same-street rule; empty for rail).
    `endpoint_to_ways` is the endpoint-only adjacency the rail walk uses.
    `node_to_ways` (populated when the loader is called with
    `index_all_nodes=True`) additionally maps every vertex coordinate that
    is some way's endpoint to all ways passing through it — the street
    walk's junction continuation needs it because a street way frequently
    T-s into the *middle* of the way it continues onto.
    """

    def __init__(self, cell_size_deg: float = 0.001):
        self.ways: list = []
        self.way_dists: list = []
        self.way_props: list = []
        self.cells: dict = defaultdict(list)
        self.endpoint_to_ways: dict = defaultdict(list)
        self.node_to_ways: dict = defaultdict(list)
        self.cell_size = cell_size_deg

    def query_radius(self, lon: float, lat: float, radius_m: float):
        """Way indices whose bbox grid cell could contain points within
        radius_m of (lon, lat). Conservative — caller does the precise
        distance check."""
        deg = radius_m / 111000.0
        cs = self.cell_size
        cx_lo = int((lon - deg) / cs)
        cx_hi = int((lon + deg) / cs)
        cy_lo = int((lat - deg) / cs)
        cy_hi = int((lat + deg) / cs)
        seen: set = set()
        for cx in range(cx_lo, cx_hi + 1):
            for cy in range(cy_lo, cy_hi + 1):
                for w_idx in self.cells.get((cx, cy), ()):
                    seen.add(w_idx)
        return seen


def _load_way_index(path, label, index_all_nodes=False):
    """Load OSM ways from a FeatureCollection GeoJSON into a _RailIndex.
    Returns None if the file is missing — the caller's walk tier is then
    disabled (rail falls back to the capped-straight path; tram/bus append
    nothing)."""
    if not path.exists():
        print(f"  WARNING: {path.name} not found — {label} walk disabled")
        return None
    data = json.loads(path.read_text())
    idx = _RailIndex()
    n_skip = 0
    for feat in data.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            continue
        raw = geom.get("coordinates") or []
        # Drop z components if present; collapse consecutive duplicates so
        # cumulative distances strictly increase.
        coords = []
        for c in raw:
            t = (c[0], c[1])
            if not coords or coords[-1] != t:
                coords.append(t)
        if len(coords) < 2:
            n_skip += 1
            continue
        dists = _cum_dist_m(coords)
        if dists[-1] <= 0:
            n_skip += 1
            continue
        w_idx = len(idx.ways)
        idx.ways.append(coords)
        idx.way_dists.append(dists)
        idx.way_props.append(feat.get("properties") or {})
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        cs = idx.cell_size
        cx_lo = int(min(xs) / cs)
        cx_hi = int(max(xs) / cs)
        cy_lo = int(min(ys) / cs)
        cy_hi = int(max(ys) / cs)
        for cx in range(cx_lo, cx_hi + 1):
            for cy in range(cy_lo, cy_hi + 1):
                idx.cells[(cx, cy)].append(w_idx)
        idx.endpoint_to_ways[coords[0]].append((w_idx, 0))
        idx.endpoint_to_ways[coords[-1]].append((w_idx, len(coords) - 1))
    if index_all_nodes:
        # Second pass: adjacency for every vertex that is some way's
        # endpoint — junction continuation must find a crossing way even
        # when the walked way ends mid-way of the continuation.
        endpoints = set(idx.endpoint_to_ways.keys())
        for w_idx, coords in enumerate(idx.ways):
            for v_idx, c in enumerate(coords):
                if c in endpoints:
                    idx.node_to_ways[c].append((w_idx, v_idx))
    print(f"  Loaded {len(idx.ways):,} {label} ways from {path.name} "
          f"({n_skip:,} skipped)")
    return idx


def _osm_rail_find_best_match(rail_idx, p_lon, p_lat,
                                walk_dx_per_m, walk_dy_per_m,
                                radius_m, max_tangent_diff_deg):
    """Pick the OSM rail way under (p_lon, p_lat) whose tangent at its
    projection of P best matches the walk direction. Returns
    (way_idx, t_on_way, walk_forward) or None.

    Proximity gate: projection distance ≤ radius_m. Tangent gate: angle
    between way tangent and walk direction ≤ max_tangent_diff_deg (mod π).
    Among candidates passing both gates, smallest distance wins; tangent
    quality breaks ties.
    """
    candidates = rail_idx.query_radius(p_lon, p_lat, radius_m)
    if not candidates:
        return None

    cos_lat = cos(radians(p_lat))
    walk_ex = walk_dx_per_m * cos_lat
    walk_ey = walk_dy_per_m
    walk_mag = sqrt(walk_ex * walk_ex + walk_ey * walk_ey)
    if walk_mag <= 0:
        return None
    cos_tol = cos(radians(max_tangent_diff_deg))
    radius_sq_m = radius_m * radius_m

    best = None  # (sort_key, way_idx, t_on_way, walk_forward)
    for w_idx in candidates:
        coords = rail_idx.ways[w_idx]
        dists = rail_idx.way_dists[w_idx]
        t_proj = _project_meters(p_lon, p_lat, coords, dists)
        proj_lon, proj_lat = _interp_at(coords, dists, t_proj)
        dx_m = (proj_lon - p_lon) * cos_lat * 111000.0
        dy_m = (proj_lat - p_lat) * 111000.0
        d_sq_m = dx_m * dx_m + dy_m * dy_m
        if d_sq_m > radius_sq_m:
            continue
        way_tan = _directional_tangent_at(coords, dists, t_proj, window_m=5.0)
        if way_tan is None:
            continue
        wdx, wdy = way_tan
        way_ex = wdx * cos_lat
        way_ey = wdy
        way_mag = sqrt(way_ex * way_ex + way_ey * way_ey)
        if way_mag <= 0:
            continue
        cos_a = (way_ex * walk_ex + way_ey * walk_ey) / (way_mag * walk_mag)
        if abs(cos_a) < cos_tol:
            continue
        key = (sqrt(d_sq_m), -abs(cos_a))
        if best is None or key < best[0]:
            best = (key, w_idx, t_proj, cos_a > 0)

    if best is None:
        return None
    _, w_idx, t_proj, walk_forward = best
    return (w_idx, t_proj, walk_forward)


def _osm_rail_find_continuation(rail_idx, exit_node, exit_dir,
                                  excl_way_idx, max_tangent_diff_deg):
    """At a way endpoint shared between ways, pick the continuation way whose
    outgoing direction (from `exit_node` into that way) best matches the
    incoming `exit_dir`. Returns (way_idx, start_t, forward) or None.

    `exit_node` is the (lon, lat) tuple of the shared endpoint; matched against
    `rail_idx.endpoint_to_ways` keyed on exact coords.
    """
    candidates = rail_idx.endpoint_to_ways.get(exit_node, ())
    if not candidates:
        return None

    cos_lat = cos(radians(exit_node[1]))
    ex = exit_dir[0] * cos_lat
    ey = exit_dir[1]
    e_mag = sqrt(ex * ex + ey * ey)
    if e_mag <= 0:
        return None
    cos_tol = cos(radians(max_tangent_diff_deg))

    best = None  # (cos_a, way_idx, vert_idx, forward)
    for w_idx, vert_idx in candidates:
        if w_idx == excl_way_idx:
            continue
        coords = rail_idx.ways[w_idx]
        if len(coords) < 2:
            continue
        if vert_idx == 0:
            other = coords[1]
            forward = True
        else:
            other = coords[vert_idx - 1]
            forward = False
        out_dx = other[0] - exit_node[0]
        out_dy = other[1] - exit_node[1]
        ox = out_dx * cos_lat
        oy = out_dy
        o_mag = sqrt(ox * ox + oy * oy)
        if o_mag <= 0:
            continue
        cos_a = (ex * ox + ey * oy) / (e_mag * o_mag)
        if cos_a < cos_tol:
            # Reject reversed or sharply turning continuations.
            continue
        if best is None or cos_a > best[0]:
            best = (cos_a, w_idx, vert_idx, forward)

    if best is None:
        return None
    _, w_idx, vert_idx, _ = best
    dists = rail_idx.way_dists[w_idx]
    start_t = 0.0 if vert_idx == 0 else dists[-1]
    forward = (vert_idx == 0)
    return (w_idx, start_t, forward)


def _walk_along_way(coords, dists, t_start, forward, max_len_m):
    """Walk one way from arc-length t_start in direction `forward` for up to
    max_len_m metres. Returns (seg_coords, exit_pt, exit_dir, used_m, hit_end).

    seg_coords starts at (interpolated) t_start and ends at (interpolated)
    t_end. exit_dir is the last segment's (dx, dy) direction (in raw lon/lat
    units) — the direction the walk was travelling at the exit, used by
    `_osm_rail_find_continuation` to pick the next way.
    """
    way_max = dists[-1]
    if forward:
        t_end = min(way_max, t_start + max_len_m)
        used = t_end - t_start
        seg = [_interp_at(coords, dists, t_start)]
        for i, d in enumerate(dists):
            if t_start < d < t_end:
                seg.append((coords[i][0], coords[i][1]))
        last = _interp_at(coords, dists, t_end)
        if seg[-1] != last:
            seg.append(last)
        hit_end = (t_end >= way_max) and (used + 1e-6 < max_len_m)
    else:
        t_end = max(0.0, t_start - max_len_m)
        used = t_start - t_end
        seg = [_interp_at(coords, dists, t_start)]
        for i in range(len(coords) - 1, -1, -1):
            if t_end < dists[i] < t_start:
                seg.append((coords[i][0], coords[i][1]))
        last = _interp_at(coords, dists, t_end)
        if seg[-1] != last:
            seg.append(last)
        hit_end = (t_end <= 0.0) and (used + 1e-6 < max_len_m)
    exit_pt = (seg[-1][0], seg[-1][1])
    if len(seg) >= 2:
        exit_dir = (seg[-1][0] - seg[-2][0], seg[-1][1] - seg[-2][1])
    else:
        exit_dir = (0.0, 0.0)
    return (seg, exit_pt, exit_dir, used, hit_end)


def _osm_rail_walk(rail_idx, p_lon, p_lat,
                    walk_dx_per_m, walk_dy_per_m, target_length_m):
    """Walk an OSM rail way (with junction continuation) from a point P in
    the given walk direction for `target_length_m` metres.

    `walk_dx_per_m`, `walk_dy_per_m`: per-metre tangent components in
    (lon, lat) units pointing in the desired walk direction (the missing
    side at a terminal stop).

    Returns (status, coords):
      'walk'     — coords is the extension polyline starting at (p_lon, p_lat)
                   (translated so the first vertex equals P exactly) and
                   reaching `target_length_m` of OSM-rail geometry.
      'ran_out'  — coords is a partial walk (way chain ended early); caller
                   applies Fallback B (end-of-platform anchoring).
      'no_match' — coords is None; caller applies Fallback A (capped straight).
    """
    if rail_idx is None:
        return ("no_match", None)

    start = _osm_rail_find_best_match(
        rail_idx, p_lon, p_lat,
        walk_dx_per_m, walk_dy_per_m,
        OSM_MATCH_RADIUS_M, OSM_MATCH_MAX_TANGENT_DIFF_DEG)
    if start is None:
        return ("no_match", None)
    way_idx, t_proj, walk_forward = start

    out_coords: list = []
    remaining = target_length_m
    visited: set = set()
    ran_out = False
    while remaining > 1e-6:
        if way_idx in visited:
            ran_out = True
            break
        visited.add(way_idx)
        coords = rail_idx.ways[way_idx]
        dists = rail_idx.way_dists[way_idx]
        seg, exit_pt, exit_dir, used, hit_end = _walk_along_way(
            coords, dists, t_proj, walk_forward, remaining)
        if not out_coords:
            out_coords.extend(seg)
        else:
            # First seg vertex coincides with the previous exit point.
            out_coords.extend(seg[1:])
        remaining -= used
        if remaining <= 1e-6:
            break
        if not hit_end:
            # Defensive: walked less than max_len but didn't hit the end —
            # treat as ran_out so we don't loop forever.
            ran_out = True
            break
        cont = _osm_rail_find_continuation(
            rail_idx, exit_pt, exit_dir, way_idx,
            OSM_MATCH_MAX_TANGENT_DIFF_DEG)
        if cont is None:
            ran_out = True
            break
        way_idx, t_proj, walk_forward = cont

    if not out_coords:
        return ("no_match", None)

    # Translate so first vertex sits exactly at P (projection-distance shift,
    # bounded by OSM_MATCH_RADIUS_M).
    ox, oy = out_coords[0]
    shift_x = p_lon - ox
    shift_y = p_lat - oy
    translated = [(x + shift_x, y + shift_y) for x, y in out_coords]
    return ("ran_out" if ran_out else "walk", translated)


# ── Street / tram walk (stop-extent-osm-walk.md § "Walk tier for tram / bus") ─

# Road-class ranks for the same-street junction rule: the walk continues
# onto a way only when its class is the same or adjacent in this ordering
# ("similarly sized"), or when the way carries the same street name. Tram /
# light_rail rank together — the tram network has no class hierarchy.
_HIGHWAY_RANK = {
    "motorway":      0,
    "trunk":         1,
    "primary":       2,
    "secondary":     3,
    "tertiary":      4,
    "bus_guideway":  4,
    "unclassified":  5,
    "residential":   5,
    "living_street": 6,
    "service":       6,
}


def _way_rank(props):
    """Road-class rank of a way, or None when the way carries no usable
    class. `_link` variants rank as their base class."""
    hw = (props or {}).get("highway") or ""
    if hw.endswith("_link"):
        hw = hw[:-5]
    if hw in _HIGHWAY_RANK:
        return _HIGHWAY_RANK[hw]
    rw = (props or {}).get("railway") or ""
    if rw in ("tram", "light_rail"):
        return 0
    return None


def _same_street_ok(cur_props, cand_props):
    """Same-street rule for the walk's junction continuation: same or
    adjacent road-class rank ("similarly sized"), or same non-empty street
    name (a class change under an unchanged name is still the same street).
    A mere name change never breaks continuation on its own."""
    name = (cur_props or {}).get("name") or ""
    if name and name == ((cand_props or {}).get("name") or ""):
        return True
    ra = _way_rank(cur_props)
    rb = _way_rank(cand_props)
    if ra is None or rb is None:
        return False
    return abs(ra - rb) <= 1


def _osm_street_find_continuation(way_index, exit_node, exit_dir,
                                    excl_way_idx, cur_props):
    """At the end of a walked street/tram way, pick the continuation among
    all ways sharing the exit node (any-vertex adjacency — the walked way
    may T into the middle of the continuation). Gates: same-street rule
    against the walked way's props, and outgoing direction within
    ROAD_MATCH_MAX_TANGENT_DIFF_DEG of the incoming direction (directional
    — the walk never doubles back). Best direction match wins. Returns
    (way_idx, start_t, forward) or None."""
    candidates = way_index.node_to_ways.get(exit_node, ())
    if not candidates:
        return None

    cos_lat = cos(radians(exit_node[1]))
    ex = exit_dir[0] * cos_lat
    ey = exit_dir[1]
    e_mag = sqrt(ex * ex + ey * ey)
    if e_mag <= 0:
        return None
    cos_tol = cos(radians(ROAD_MATCH_MAX_TANGENT_DIFF_DEG))

    best = None  # (cos_a, way_idx, vert_idx, forward)
    for w_idx, vert_idx in candidates:
        if w_idx == excl_way_idx:
            continue
        if not _same_street_ok(cur_props, way_index.way_props[w_idx]):
            continue
        coords = way_index.ways[w_idx]
        for nb, forward in ((vert_idx + 1, True), (vert_idx - 1, False)):
            if nb < 0 or nb >= len(coords):
                continue
            other = coords[nb]
            ox = (other[0] - exit_node[0]) * cos_lat
            oy = other[1] - exit_node[1]
            o_mag = sqrt(ox * ox + oy * oy)
            if o_mag <= 0:
                continue
            cos_a = (ex * ox + ey * oy) / (e_mag * o_mag)
            if cos_a < cos_tol:
                continue
            if best is None or cos_a > best[0]:
                best = (cos_a, w_idx, vert_idx, forward)

    if best is None:
        return None
    _, w_idx, vert_idx, forward = best
    return (w_idx, way_index.way_dists[w_idx][vert_idx], forward)


def _osm_street_walk(way_index, p_lon, p_lat,
                      walk_dx_per_m, walk_dy_per_m, target_length_m):
    """Walk an OSM street / tram way (with same-street junction
    continuation) from a point P in the given walk direction for
    `target_length_m` metres. Mirrors `_osm_rail_walk`; differs in the
    gates (ROAD_MATCH_*), the junction rule (same-street via any-vertex
    adjacency instead of best-tangent endpoint continuation), and the
    caller's handling of the statuses (tram/bus keep a partial 'ran_out'
    walk and never fall back to a straight line — see
    stop-extent-osm-walk.md).

    Returns (status, coords):
      'walk'     — coords is the extension polyline starting at (p_lon,
                   p_lat) (translated so the first vertex equals P exactly)
                   reaching `target_length_m` of OSM geometry.
      'ran_out'  — coords is the partial walk (network ended, or the
                   same-street rule stopped the continuation).
      'no_match' — coords is None; no way satisfied both gates at P.
    """
    if way_index is None:
        return ("no_match", None)

    start = _osm_rail_find_best_match(
        way_index, p_lon, p_lat,
        walk_dx_per_m, walk_dy_per_m,
        ROAD_MATCH_RADIUS_M, ROAD_MATCH_MAX_TANGENT_DIFF_DEG)
    if start is None:
        return ("no_match", None)
    way_idx, t_proj, walk_forward = start

    out_coords: list = []
    remaining = target_length_m
    visited: set = set()
    ran_out = False
    while remaining > 1e-6:
        if way_idx in visited:
            ran_out = True
            break
        visited.add(way_idx)
        coords = way_index.ways[way_idx]
        dists = way_index.way_dists[way_idx]
        seg, exit_pt, exit_dir, used, hit_end = _walk_along_way(
            coords, dists, t_proj, walk_forward, remaining)
        if not out_coords:
            out_coords.extend(seg)
        else:
            out_coords.extend(seg[1:])
        remaining -= used
        if remaining <= 1e-6:
            break
        if not hit_end:
            ran_out = True
            break
        cont = _osm_street_find_continuation(
            way_index, exit_pt, exit_dir, way_idx,
            way_index.way_props[way_idx])
        if cont is None:
            ran_out = True
            break
        way_idx, t_proj, walk_forward = cont

    if len(out_coords) < 2:
        return ("no_match", None)

    # Translate so first vertex sits exactly at P (projection-distance
    # shift, bounded by ROAD_MATCH_RADIUS_M) — the walked geometry also
    # extends the rendered line polyline, which must join without a jog.
    ox, oy = out_coords[0]
    shift_x = p_lon - ox
    shift_y = p_lat - oy
    translated = [(x + shift_x, y + shift_y) for x, y in out_coords]
    return ("ran_out" if ran_out else "walk", translated)
