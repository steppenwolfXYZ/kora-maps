#!/usr/bin/env python3
"""
Build the final transit GeoJSON from a pfaedle-routed GTFS feed.

Pipeline:
  1. Load the filtered + pfaedle-routed GTFS feed at data/gtfs_routed/.
  2. Stream stop_times with trip-grouping (gtfs-line-grouping concept) →
     `_trip_group_export` (trip_id → (line_key, tg_id, agency_id)),
     `_trip_stops_export` (trip_id → [stop_id, …]),
     `_trip_merged_export` (trip_id → frozenset(merged_stop_id)), and
     `_trip_direction_export` (trip_id → (first_uic, last_uic)).
  3. Aggregate frequency, speed, and canonical-trip stops per
     tg_key = (line_key, agency_id, trip_group_id) — the only line identity in
     this pipeline (see trip-group-as-sole-line-identity concept).
  4. Load pfaedle shapes (shapes.txt) and per-trip shape_id from trips.txt.
  5. For each (line_key, agency_id, trip_group_id), group trips by merged
     stop set and emit one feature per kept variant. Mode comes from the
     GTFS route_type with an agency-based mountain rack override.
  6. Every variant goes through pfaedle. When pfaedle produces no shape,
     aerial route_types (5 = cable car, 6 = gondola) fall back to a straight
     line between consecutive GTFS stops; every other mode is logged as
     `pfaedle_unrouted` and not emitted.

Outputs:
  data/transit/transit_lines.geojson    one feature per distinct shape
  data/transit/line_stops.json          per-feature ordered stops
  data/transit/gtfs_unmatched.json      GTFS lines with no emitted feature
  data/transit/trip_groups.json         trip-group composition (diagnostic)
  data/transit/pfaedle_unrouted.json    trips pfaedle didn't shape

Mode categories (unchanged):
  train, tram, metro, bus, regional_bus, ferry, mountain

Long-distance coaches are dropped upstream in step 04 (agency denylist).
"""

import json
import sys
from collections import defaultdict
from math import cos, radians, sqrt, ceil, floor
from pathlib import Path

import yaml

from common import CFG_PATH, PROJECT_ROOT as ROOT
from geometry import _M_PER_DEG, haversine_km, line_bbox, polyline_length_km
from gtfs.emit import (
    best_trip_in_shape_group,
    deduplicate_mountain,
    stops_to_polyline,
    synthesise_aerial_reverse_directions,
)
from gtfs.frequency import (
    SEASONS,
    _frequencies,
    compute_freq_score,
    score_to_width_base,
    speed_to_color,
    weighted_freq,
)
from gtfs.identity import (
    _trip_dep_span_export,
    _trip_direction_export,
    _trip_group_export,
    _trip_merged_export,
    _trip_stops_export,
    _trip_weight_export,
    _trip_weight_seasonal_export,
    content_tg_id,
    stream_stop_times,
)
from gtfs.loaders import (
    GTFS,
    _BUCKET_MODE_APPROX,
    _freq_gate_exempt,
    _gate_exempt,
    _mountain_origin,
    gtfs_to_mode,
    load_agencies,
    load_calendar_dates,
    load_calendar_dates_full,
    load_frequencies,
    load_routes,
    load_shapes,
    load_stop_meta,
    load_stops,
    load_trips,
)

OUT = ROOT / "data" / "transit" / "transit_lines.geojson"
OUT_STOPS = ROOT / "data" / "transit" / "line_stops.json"
OUT_GTFS_UNMATCHED = ROOT / "data" / "transit" / "gtfs_unmatched.json"
OUT_TRIP_GROUPS = ROOT / "data" / "transit" / "trip_groups.json"
OUT_PFAEDLE_UNROUTED = ROOT / "data" / "transit" / "pfaedle_unrouted.json"
OUT_STOP_SCORES = ROOT / "data" / "transit" / "stop_size_scores.json"

_ZOOM_RULES_CACHE: dict = {}


def _zoom_rules_cfg() -> dict:
    """`zoom_level_rules` block from config.yaml. Cached. See
    .claude/concepts/zoom-level-rules.md."""
    if _ZOOM_RULES_CACHE:
        return _ZOOM_RULES_CACHE["cfg"]
    cfg = yaml.safe_load(CFG_PATH.read_text())
    sc = cfg.get("zoom_level_rules") or {}
    if not sc:
        sys.exit("config.yaml is missing zoom_level_rules section.")
    _ZOOM_RULES_CACHE["cfg"] = sc
    return sc


def _cluster_uics(uic_coords: dict, threshold_m: float) -> dict:
    """Cluster UIC nodes whose coordinates are within `threshold_m` of each
    other into one super-node. Returns {uic: super_id}. Used for the
    connectivity line-graph in the salience-ranking concept (transfer
    points whose GTFS parents differ but are physically the same).

    Implementation: grid-cell candidate search + union-find.
    """
    if not uic_coords:
        return {}
    # Grid cell sized at the threshold; any candidate must be in same or 8
    # neighbouring cells. Use latitude-corrected degree size at CH lat.
    lat0 = sum(lat for _lon, lat in uic_coords.values()) / len(uic_coords)
    cos_lat = cos(radians(lat0)) or 1e-9
    cell_lat_deg = threshold_m / _M_PER_DEG
    cell_lon_deg = cell_lat_deg / cos_lat

    grid: dict = defaultdict(list)
    for uic, (lon, lat) in uic_coords.items():
        cx = int(floor(lon / cell_lon_deg))
        cy = int(floor(lat / cell_lat_deg))
        grid[(cx, cy)].append(uic)

    uic_list = list(uic_coords.keys())
    parent: dict = {u: u for u in uic_list}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    threshold_m_sq = threshold_m * threshold_m
    for uic, (lon, lat) in uic_coords.items():
        cx = int(floor(lon / cell_lon_deg))
        cy = int(floor(lat / cell_lat_deg))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((cx + dx, cy + dy), ()):
                    if other == uic:
                        continue
                    olon, olat = uic_coords[other]
                    mdx = (olon - lon) * cos_lat * _M_PER_DEG
                    mdy = (olat - lat) * _M_PER_DEG
                    if mdx * mdx + mdy * mdy <= threshold_m_sq:
                        ru, ro = find(uic), find(other)
                        if ru != ro:
                            parent[ru] = ro

    return {u: find(u) for u in uic_list}


# ── Config loading ───────────────────────────────────────────────────────────

def load_cfg() -> dict:
    return yaml.safe_load(CFG_PATH.read_text())



# ── Service area filter ──────────────────────────────────────────────────────

_SERVICE_AREA_EXCLUDE: frozenset = frozenset({
    "8501952", "8501951", "8501950",
    "8509369", "8581990",
    "8505874", "8505861", "8505862",
    "8505599", "8505597", "8505588", "8505580", "8505590", "8505584",
    "8505578", "8505593", "8505594", "8505585", "8505589", "8505581",
    "8503420", "8503421",
})
_SERVICE_AREA_INCLUDE: frozenset = frozenset({
    "8014586", "8014587", "8014481", "8014491",
    "8774538",
    "8718444",
})


def is_in_service_area(stop_id: str) -> bool:
    sid = stop_id.split(":")[0]
    if sid in _SERVICE_AREA_INCLUDE:
        return True
    if sid in _SERVICE_AREA_EXCLUDE:
        return False
    return sid.startswith("85")


_AERIAL_ROUTE_TYPES = {"1300", "1303"}

# ── Pfaedle shape grouping ───────────────────────────────────────────────────

# Aerial GTFS route_types (extended codes 1300 = aerial lift, 1303 =
# Bern-style elevator) where OSM coverage is patchy enough that a missing
# pfaedle shape is treated as a straight-line fallback rather than a hard
# `pfaedle_unrouted` failure. Funiculars (1400) and every other mode drop
# the feature when pfaedle has no shape, same as rail / bus today.
_STRAIGHT_LINE_FALLBACK_ROUTE_TYPES = _AERIAL_ROUTE_TYPES


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    # Pipeline body is split across three files that share one namespace
    # (locals from phase N are visible to phase N+1). This keeps every
    # source file below the 1000-line cap without reshaping the monolithic
    # emission flow.
    import pathlib
    _here = pathlib.Path(__file__).parent / "gtfs"
    scope = dict(globals())
    for phase_name in ("_pipeline_grouping.py", "_pipeline_emission.py", "_pipeline_output.py"):
        with open(_here / phase_name) as _f:
            exec(compile(_f.read(), str(_here / phase_name), "exec"), scope)


if __name__ == "__main__":
    main()
