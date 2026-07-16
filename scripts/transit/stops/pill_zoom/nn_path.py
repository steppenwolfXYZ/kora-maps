"""Nearest-neighbor path through cluster dot positions + position/member
dedup helpers."""
from math import sqrt

from _state import *  # noqa: F401,F403
from geometry import haversine_km
from stops.pill_zoom.lines import dominant_line


def nearest_neighbor_path(positions):
    """
    Build a greedy nearest-neighbor path visiting every position exactly once.
    Starts from the position furthest from the centroid (an edge of the cluster).
    Returns the ordered list of positions.
    """
    n = len(positions)
    if n == 1:
        return list(positions)

    cx = sum(p[0] for p in positions) / n
    cy = sum(p[1] for p in positions) / n
    start = max(range(n),
                key=lambda i: haversine_km(positions[i][0], positions[i][1], cx, cy))

    visited = [False] * n
    path = [positions[start]]
    visited[start] = True

    for _ in range(n - 1):
        last = path[-1]
        best_d = float("inf")
        best_j = -1
        for j in range(n):
            if not visited[j]:
                d = haversine_km(last[0], last[1], positions[j][0], positions[j][1])
                if d < best_d:
                    best_d = d
                    best_j = j
        path.append(positions[best_j])
        visited[best_j] = True

    return path


# Two stops within DEDUP_TOL_M are treated as the same position. Catches
# float-noise twins (cos_lat round-trip in coordinate_dots_global_stab) and
# platforms snapped onto the same logical spot but emitted at slightly
# different floats (observed up to ~11 cm). Set small enough to leave real
# sub-pill geometry (3-6 m short pills) intact.
DEDUP_TOL_M = 0.5


def _dedup_stop_positions(cluster_stops):
    """Return unique (lon, lat) positions, collapsing any pair within
    DEDUP_TOL_M of each other. First-seen wins; the survivor's exact float
    is kept. Without this, near-coincident pairs emit as 2-point degenerate
    pills that MapLibre cannot render reliably (zero direction vector)."""
    tol_km = DEDUP_TOL_M / 1000.0
    unique = []
    for s in cluster_stops:
        lon, lat = s["lon"], s["lat"]
        if not any(haversine_km(lon, lat, u_lon, u_lat) < tol_km
                   for u_lon, u_lat in unique):
            unique.append((lon, lat))
    return unique


def _pos_to_platforms(cluster_stops, positions):
    """Map each survivor position from `positions` to the list of every
    cluster stop that dedup'd onto it. Keyed by survivor so downstream
    lookups (`_stops_at_positions`, perpendicular-platforms check in
    `_should_split_at_gap`) see every stop logically at that position —
    not just the first-seen stop whose exact float made it into
    `positions`. Without this redirect, stops within DEDUP_TOL_M of the
    survivor would be silently dropped from indicator color emission and
    from tangent lookups, which at band B (2.5 m) can lose real
    same-cluster platforms."""
    tol_km = DEDUP_TOL_M / 1000.0
    out = {pos: [] for pos in positions}
    for s in cluster_stops:
        lon, lat = s["lon"], s["lat"]
        for u_lon, u_lat in positions:
            if haversine_km(lon, lat, u_lon, u_lat) < tol_km:
                out[(u_lon, u_lat)].append(s)
                break
        else:
            # Positions came from _dedup_stop_positions(cluster_stops), so
            # every stop must have a survivor. Fall back to the stop's own
            # coord as a defensive slot rather than silently dropping it.
            out.setdefault((lon, lat), []).append(s)
    return out


def _dedup_cluster_members_by_position(cluster_stops):
    """Group cluster members within DEDUP_TOL_M of each other into one slot
    per unique placed position. Returns list of (lon, lat, dom_color, dom_mode,
    max_wb, dom_member) tuples — dominant_line applied per position group.
    Without this collapse, the per-member dot emission stacks features with
    different width_base on the same coordinate at single-platform multi-line
    halts (e.g. Guarda: R15 + RE4 both snap to one platform position with
    width_base 2.46 and 1.97), producing concentric-circle artifacts in the
    MapLibre circle layer."""
    tol_km = DEDUP_TOL_M / 1000.0
    groups = []
    for s in cluster_stops:
        lon, lat = s["lon"], s["lat"]
        placed = False
        for g in groups:
            if haversine_km(lon, lat, g[0]["lon"], g[0]["lat"]) < tol_km:
                g.append(s)
                placed = True
                break
        if not placed:
            groups.append([s])
    out = []
    for g in groups:
        color, mode, max_wb, dom = dominant_line(g)
        out.append((dom["lon"], dom["lat"], color, mode, max_wb, dom))
    return out


# =============================================================================
# Pill logic
# =============================================================================
