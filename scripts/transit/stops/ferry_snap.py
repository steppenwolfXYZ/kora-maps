"""Ferry pier snap helpers: pier position on the line, oriented-bounding-box
overlap for pier splitting, and the canonical multi-line pier snap. See
stops-pill-zoom.md § "Ferry stops"."""
from math import atan2, cos, degrees, radians, sin, sqrt

from _state import *  # noqa: F401,F403 — FERRY_* constants
from geometry import _cum_dist_m, _interp_at, _project_meters, haversine_km

# =============================================================================
# Geometry helpers
# =============================================================================

def _ferry_pier_t_on_line(stop_lon, stop_lat, polyline, dists):
    """The pier's on-line arc position for a single ferry line: the
    polyline VERTEX closest to the GTFS stop coord, with endpoint pull
    (if the closer polyline endpoint is within FERRY_ENDPOINT_PULL_M of
    that vertex, use the endpoint instead). Mirrors the per-line piece of
    `_ferry_canonical_snap` — stops-pill-zoom.md § "Ferry stops" defines
    the same rule — reduced to the arc-position along one polyline that
    the close-zoom pill-arrow needs as an anchor."""
    if len(polyline) < 2 or dists[-1] <= 0:
        return 0.0
    # Closest-vertex index.
    best_i = 0
    best_d2 = float("inf")
    for i, v in enumerate(polyline):
        dx = v[0] - stop_lon
        dy = v[1] - stop_lat
        d2 = dx * dx + dy * dy
        if d2 < best_d2:
            best_d2 = d2
            best_i = i
    cv_lon, cv_lat = polyline[best_i]
    # Endpoint pull.
    for ep_i in (0, len(polyline) - 1):
        ep_lon, ep_lat = polyline[ep_i]
        d_m = haversine_km(cv_lon, cv_lat, ep_lon, ep_lat) * 1000.0
        if d_m <= FERRY_ENDPOINT_PULL_M:
            return dists[ep_i]
    return dists[best_i]


def _obb_overlap(cx1, cy1, tx1, ty1, l1_half, w1_half,
                  cx2, cy2, tx2, ty2, l2_half, w2_half):
    """SAT overlap test for two oriented rectangles in a shared metric
    frame. tangent (tx, ty) is a unit vector along the rect's long axis;
    l_half is the half-length along the tangent, w_half the half-width
    perpendicular to it. Returns True when the rectangles overlap; False
    when a separating axis is found. Used by the ferry ring-alternating
    layout to detect visual pill-arrow collisions at z18."""
    px1, py1 = -ty1, tx1
    px2, py2 = -ty2, tx2
    dx = cx2 - cx1
    dy = cy2 - cy1
    for ax, ay in ((tx1, ty1), (px1, py1), (tx2, ty2), (px2, py2)):
        r1 = (abs(l1_half * (tx1 * ax + ty1 * ay))
              + abs(w1_half * (px1 * ax + py1 * ay)))
        r2 = (abs(l2_half * (tx2 * ax + ty2 * ay))
              + abs(w2_half * (px2 * ax + py2 * ay)))
        d_proj = abs(dx * ax + dy * ay)
        if d_proj > r1 + r2 + 1e-6:
            return False
    return True


def _ferry_canonical_snap(polylines, gtfs):
    """Find a single canonical on-line position for a pier served by
    multiple ferry lines.

    For each line, take its polyline VERTEX closest to the GTFS coord
    (not the closest point on a segment). Closest-vertex matters at fan
    piers like Spiez Schiffstation: most ferry trips ride the same OSM
    ferry way out of the pier, and the way has a shared node V at the
    physical convergence. The GTFS coord typically sits a few metres
    inland on the building, so closest-segment-point slides east along
    each line's first segment and ends up at the per-line GTFS projection
    — never at V. Closest-vertex pins each line to the OSM node it
    actually shares with the others, so the medoid lands at V.

    Endpoint pull: if the closer of the polyline's two endpoints sits
    within FERRY_ENDPOINT_PULL_M of the closest-vertex pick, prefer the
    endpoint. The polyline endpoint is by construction the OSM ferry-pier
    node pfaedle routed to (the physical dock); the closest-vertex can
    otherwise land on an intermediate routing waypoint when pfaedle's
    snap node sits a bit further from the GTFS coord than a curve vertex
    on the approach (Lausanne-Ouchy line 3150).

    The canonical is the medoid (vertex with min sum of distances to all
    others). Returns (canonical_lonlat, max_distance_to_medoid_m). The
    max distance is the convergence-quality signal: small ⇒ the lines
    really do meet at one node; large ⇒ the parent_station bundles two
    physically separate berths and the caller falls back to per-line
    dots (see stops-pill-zoom.md § "Ferry stops")."""
    if not polylines:
        return gtfs, 0.0
    pier_verts = []
    for pl in polylines:
        if not pl:
            continue
        cv = min(pl, key=lambda v: (v[0] - gtfs[0]) ** 2 + (v[1] - gtfs[1]) ** 2)
        cv = (float(cv[0]), float(cv[1]))
        endpoints = (pl[0], pl[-1])
        closer_ep = min(endpoints,
                        key=lambda v: (v[0] - cv[0]) ** 2 + (v[1] - cv[1]) ** 2)
        if haversine_km(cv[0], cv[1], closer_ep[0], closer_ep[1]) * 1000.0 \
                <= FERRY_ENDPOINT_PULL_M:
            cv = (float(closer_ep[0]), float(closer_ep[1]))
        pier_verts.append(cv)
    if not pier_verts:
        return gtfs, 0.0
    if len(pier_verts) == 1:
        return pier_verts[0], 0.0
    best_idx = 0
    best_sum = float("inf")
    for i, p in enumerate(pier_verts):
        s = 0.0
        for j, q in enumerate(pier_verts):
            if i == j:
                continue
            s += haversine_km(p[0], p[1], q[0], q[1])
        if s < best_sum:
            best_sum = s
            best_idx = i
    medoid = pier_verts[best_idx]
    max_dist_m = max(
        haversine_km(v[0], v[1], medoid[0], medoid[1]) * 1000.0
        for v in pier_verts
    )
    return medoid, max_dist_m

