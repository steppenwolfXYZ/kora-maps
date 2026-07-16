"""Spatial stop clustering: rail dot clusters, pill clusters, and the
same-parent-station merge. Salience/min-zoom scoring lives in salience.py."""
from collections import defaultdict
from math import floor

from _state import *  # noqa: F401,F403 — shared constants (CLUSTER_DEG, ...)
from geometry import haversine_km


# =============================================================================

def cluster_rail_stops(rail_stops: list) -> list:
    """
    Cluster (lon, lat, color, mode, width_base) tuples within CLUSTER_DEG.
    Returns list of (lon, lat, color, mode, max_width_base) cluster centroids.
    """
    grid: dict = defaultdict(list)
    for pt in rail_stops:
        lon, lat = pt[0], pt[1]
        key = (int(lon / CLUSTER_DEG), int(lat / CLUSTER_DEG))
        grid[key].append(pt)

    visited = set()
    clusters = []

    for key, pts in grid.items():
        for pt in pts:
            if id(pt) in visited:
                continue
            cx0, cy0 = pt[0], pt[1]
            group = []
            kx, ky = key
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for npt in grid.get((kx + dx, ky + dy), []):
                        if id(npt) in visited:
                            continue
                        if haversine_km(cx0, cy0, npt[0], npt[1]) < 0.3:
                            group.append(npt)
                            visited.add(id(npt))

            if not group:
                group = [pt]
                visited.add(id(pt))

            lon  = sum(p[0] for p in group) / len(group)
            lat  = sum(p[1] for p in group) / len(group)
            best = group[0]
            max_wb = max(p[4] for p in group)
            clusters.append((lon, lat, best[2], best[3], max_wb))

    return clusters


def cluster_stops_for_pills(raw_stops, radius_km, lines_of_stop=None):
    """
    Spatially cluster raw stop dicts by their lon/lat within radius_km.
    Returns list of clusters; each cluster is a list of stop dicts.

    Same-line guard (see `pill-cluster-same-line-guard.md`): when
    `lines_of_stop` is provided ({stop_id: set(osm_id)}), a candidate is
    rejected from joining a cluster whose existing members share any drawn
    line with it. Stops served by the same line are by definition different
    stations and must not be merged.
    """
    cluster_deg = radius_km / 111.0
    grid = defaultdict(list)
    for stop in raw_stops:
        key = (floor(stop["lon"] / cluster_deg), floor(stop["lat"] / cluster_deg))
        grid[key].append(stop)

    visited = set()
    clusters = []

    for key, stops_in_cell in grid.items():
        for stop in stops_in_cell:
            sid = id(stop)
            if sid in visited:
                continue
            cx0, cy0 = stop["lon"], stop["lat"]
            group = []
            group_lines: set = set()
            kx, ky = key
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for ns in grid.get((kx + dx, ky + dy), []):
                        if id(ns) in visited:
                            continue
                        if haversine_km(cx0, cy0, ns["lon"], ns["lat"]) >= radius_km:
                            continue
                        if lines_of_stop is not None and group_lines:
                            cand_lines = lines_of_stop.get(ns.get("stop_id", ""))
                            if cand_lines and not cand_lines.isdisjoint(group_lines):
                                continue
                        group.append(ns)
                        visited.add(id(ns))
                        if lines_of_stop is not None:
                            cand_lines = lines_of_stop.get(ns.get("stop_id", ""))
                            if cand_lines:
                                group_lines |= cand_lines

            if not group:
                group = [stop]
                visited.add(sid)

            clusters.append(group)

    return clusters


def merge_clusters_by_parent_station(clusters):
    """
    Merge spatially separate clusters into one super-cluster whenever they
    share any GTFS parent_station. Enforces the same-parent invariant from
    `stops-pill-zoom.md` § "Pill grouping": every stop_id under one parent
    UIC lands in a single pill cluster, regardless of what foreign parents
    happen to sit in the same spatial cluster.

    Union-find over cluster indices: two clusters are unified iff they
    share at least one parent_station. Foreign-parent stops carried into a
    spatial cluster (e.g. an aerial dot pulled into a rail cluster because
    same-line guard couldn't reject it) propagate the merge to every other
    cluster containing that foreign parent, which is intended — otherwise
    that parent's stops would be split. Clusters with no parent_station on
    any member are left as-is.

    Deterministic: cluster iteration follows the input order and per-cluster
    parents are visited in sorted order, so the output is independent of
    Python's per-process set iteration order (which depends on
    PYTHONHASHSEED and is randomised by default).
    """
    with_parents: list[tuple[list, list[str]]] = []
    no_parent: list[list] = []
    for cluster in clusters:
        parents = sorted({s.get("parent_station", "") for s in cluster
                          if s.get("parent_station", "")})
        if parents:
            with_parents.append((cluster, parents))
        else:
            no_parent.append(cluster)

    n = len(with_parents)
    uf = list(range(n))

    def find(x: int) -> int:
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            uf[ra] = rb

    seen_parent: dict[str, int] = {}
    for i, (_, parents) in enumerate(with_parents):
        for p in parents:
            j = seen_parent.get(p)
            if j is None:
                seen_parent[p] = i
            else:
                union(i, j)

    groups: dict[int, list] = defaultdict(list)
    for i, (cluster, _) in enumerate(with_parents):
        groups[find(i)].extend(cluster)

    return list(groups.values()) + no_parent


# =============================================================================
