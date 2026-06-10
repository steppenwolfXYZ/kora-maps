#!/usr/bin/env python3
"""
Build transit stop GeoJSON files:

  transit_stops.geojson      — Point features (circle dots, low-zoom)
  transit_stop_pills.geojson — LineString features (pill/capsule shapes, high-zoom)

Stop dot rules:
  - Every stop of every matched line gets a dot, visible from the same
    zoom level the line itself appears.
  - Rail (train): stops clustered within 300m → one dot per physical station.
  - All other modes: one dot per stop, snapped to the line geometry.
  - Every dot carries: color, mode, width_base (for data-driven circle radius).

Pill rules:
  - Pills appear when a cluster has ≥2 distinct OSM line IDs (osm_id).
  - Pill-appear zoom is determined by line count and dominant mode.
  - Ferry and mountain modes: no pills.
  - Pill geometry is derived from dot positions using a nearest-neighbor path:
      → Build a greedy nearest-neighbor path through ALL dot positions
        in the cluster. This ensures every dot is at a vertex of the pill.
      → If the path has a large gap between two groups (> gap threshold),
        split there and emit two pills + a thin connector.
      → Pills prefer cross-track orientation naturally: for parallel-track
        stops the NN path connects the nearby dots directly.
  - Cross-mode clustering: tram + bus at same location → one pill in tram color.
  - Color = dominant line at stop (by mode hierarchy, then width_base).
  - Width encoded as width_base → style applies ×2 multiplier.
"""

import csv
import json
import yaml
from itertools import permutations
from math import radians, cos, sin, sqrt, atan2, acos, degrees, floor, pi
from pathlib import Path
from collections import defaultdict

ROOT       = Path(__file__).resolve().parents[2]

_transit_cfg = yaml.safe_load((ROOT / "scripts" / "transit" / "config.yaml").read_text())

LINES      = ROOT / "data" / "transit" / "transit_lines.geojson"
LINE_STOPS = ROOT / "data" / "transit" / "line_stops.json"
GTFS_STOPS   = ROOT / "data" / "gtfs_routed" / "stops.txt"
# pfaedle rewrites stops.txt to a canonical schema and drops `original_stop_id`,
# so the SLOID lookup reads from the pre-pfaedle filtered feed where the
# column is still intact.
GTFS_STOPS_PRE_PFAEDLE = ROOT / "data" / "gtfs_filtered" / "stops.txt"
ATLAS_CSV    = ROOT / "data" / "atlas" / "actual-date-world-traffic-point.csv"
OUT_DOTS     = ROOT / "data" / "transit" / "transit_stops.geojson"
OUT_PILLS    = ROOT / "data" / "transit" / "transit_stop_pills.geojson"
OUT_STOP_ATTRS_DIAG = ROOT / "data" / "transit" / "stop_attributes_sources.json"
OUT_DEBUG_PLATFORMS = ROOT / "data" / "transit" / "transit_debug_platforms.geojson"
OUT_DEBUG_STOPS     = ROOT / "data" / "transit" / "transit_debug_stops.geojson"
OUT_DEBUG_BARS      = ROOT / "data" / "transit" / "transit_debug_bars.geojson"

# Diagnostic state populated by coordinate_dots_global_stab:
# - _DIAG_BARS: list of (endpoint1, endpoint2) tuples for each max-stab bar.
# - _STABBED_PAIRS: set of (osm_id, stop_id) for (line, stop) records placed
#   on a bar. Read by write_debug_stops to mark stabbed dots as filled.
_DIAG_BARS = []
_STABBED_PAIRS = set()

# Per-mode platform-length defaults and sanity ranges from config.
PILL_CFG = _transit_cfg.get("pill_rendering", {})

RAIL_MODES = {"train"}
# Modes that get pills; ferry and mountain are excluded
PILL_MODES = {"train", "tram", "metro", "bus", "regional_bus"}

# Cluster radius for rail station dot deduplication (degrees ≈ 300m at CH lat)
CLUSTER_DEG = 0.003

# Hierarchy for dominant-line selection at mixed-mode clusters (lower = higher priority)
MODE_RANK = {
    "train":        0,
    "metro":        1,
    "tram":         2,
    "bus":          3,
    "mountain":     4,
    "ferry":        5,
    "regional_bus": 6,
}

# Per-mode minzoom for stop dots (must match style layer minzooms)
MODE_MINZOOM = {
    "train":        5,
    "tram":        10,
    "metro":        9,
    "regional_bus": 9,
    "ferry":        9,
    "bus":         11,
    "mountain":    11,
}

# Spatial clustering radius for pill grouping
PILL_CLUSTER_RAIL_KM    = 0.300   # rail: 300 m (same as dot deduplication)
PILL_CLUSTER_NONRAIL_KM = 0.050   # all other modes combined: 50 m

# Absolute-metre gap thresholds for splitting the NN path into separate
# pills + connectors. Not scaled by width_base — `wb` controls disc/pill
# width, not gap length.
PILL_GAP_STRAIGHT_M = 50   # gap threshold when the NN-path continues dead
                           # straight into the gap on either side (gap is
                           # an in-line pill continuation).
PILL_GAP_ANGLED_M = 8      # gap threshold otherwise (gap is an angled /
                           # T-junction connector).

# Bar-axis gap above which a single-distinct-position scoring member on one
# side of the bar is dropped (kicked to leftover-fill). Distinct from
# PILL_GAP_STRAIGHT_M, which is the post-placement pill split-vs-connector
# threshold and stays at 50 m for every mode. Rail and metro keep the
# legacy 50 m radius; bus/tram/regional_bus drop sooner because their
# platforms are physically shorter and a 20 m off-axis member is already
# clearly a separate bay.
LONE_OUTLIER_GAP_RAIL_METRO_M = 50
LONE_OUTLIER_GAP_BUS_TRAM_M = 20

# Parallel-stub drop (rail clusters only). After leftover-fill, a placed
# leftover whose distance to its nearest non-coincident other placed dot
# is < this gap AND whose gap direction is within PARALLEL_STUB_TOL_DEG of
# either the leftover's or the neighbor's extent tangent is treated as a
# spurious "sub-platform" stop: a small subset of trips departs from one
# end of a long shared platform, gets its own stop_id, and would otherwise
# render as a short connector running along the line. The stop is dropped
# from rendering (its position is snapped to the absorbing dot so
# _dedup_stop_positions collapses it later, preserving its line in the
# popup), and leftover-fill is re-run on the remaining leftovers.
#
# The 100 m gap covers typical sub-platform distances (Bern ~17 m,
# Fribourg ~58 m measured). The parallel check is what protects against
# false positives at multi-track stations: dots on parallel adjacent
# platforms separate perpendicular to the line direction, so their gap
# vector is perpendicular to the extent tangent and fails the parallel
# test even when the dots are < 100 m apart.
PARALLEL_STUB_GAP_M = 100.0
PARALLEL_STUB_TOL_DEG = 15.0

PERP_PLATFORM_TOL_DEG = float(PILL_CFG.get("perp_platform_tol_deg", 2.0))

# Connector curving (see pill-rendering concept § Connector curving).
# CURVE_PERP_PREF_RATIO: a perpendicular tangent at a pill tip replaces the
# default axial tangent only if its connector length is ≤ this fraction of
# the axial-tangent connector length. CURVE_MAX_RADIUS_M_BY_MODE: per-mode
# arc radius. Rail / metro use a larger radius (30 m) than tram / bus / regional
# bus (20 m) so the curve scales with the physically larger rail pills.
CURVE_PERP_PREF_RATIO = 0.75
CURVE_MAX_RADIUS_M_BY_MODE = {
    "train":        30.0,
    "metro":        30.0,
    "tram":         20.0,
    "bus":          20.0,
    "regional_bus": 20.0,
}
CURVE_MAX_RADIUS_M_DEFAULT = 20.0
# Adaptive arc sampling: chord pitch is derived from the arc radius so the
# chord-to-arc sagitta stays near `CURVE_TARGET_SAGITTA_M` regardless of
# radius — tight 5 m arcs get ~1.4 m chords, 30 m arcs get ~3.5 m chords,
# wide pill-pill arcs get coarser chords still. Hard-capped at
# `CURVE_MAX_SAMPLES` to keep PMTile vertex counts bounded.
CURVE_TARGET_SAGITTA_M = 0.05
CURVE_MAX_SAMPLES = 64
# Below this arc radius the construction degenerates: 12 samples on a
# sub-metre circle land within line-width of each other, and MapLibre's
# line tessellation produces visible wobble where the round-join bulges
# overlap. Below the floor the caller falls back to the straight 2-point
# connector.
CURVE_MIN_RADIUS_M = 5.0
# Minimum inter-vertex spacing for a curved connector polyline. Catches the
# pathological recovery-shrunk arcs (sub-millimetre chords clustering all 13
# samples at a point) but stays close to the stop dedup so genuine curves
# keep their shape. The remaining MapLibre wobble at z18+ is a render-side
# issue (line-join bulge interaction with casing+fill), addressed by the
# style's `line-join` choice, not by collapsing samples further.
CURVE_DEDUP_TOL_M = 0.5
# Douglas-Peucker tolerance for pill polylines. A pill represents the line
# through several platform dots; when the dot placement intends a straight
# line on a perpendicular bar but float precision or an off-bar leftover
# leaves a dot a few centimetres off-axis, the resulting pill has a
# visible micro-kink at high zoom that reads as "wobble". Vertices whose
# perpendicular deviation from the chord through their neighbours is below
# this tolerance are dropped; genuine curved pills (real curved tracks)
# deviate well above 0.1 m and are preserved.
PILL_SIMPLIFY_TOL_M = 0.1


def _curve_max_radius(mode: str) -> float:
    return CURVE_MAX_RADIUS_M_BY_MODE.get(mode, CURVE_MAX_RADIUS_M_DEFAULT)


def _arc_chord_samples(radius: float, arc_length: float) -> int:
    """Number of chord segments to sample an arc at. Chord pitch is picked
    so the chord sagitta stays near `CURVE_TARGET_SAGITTA_M` regardless of
    radius (`chord ≈ sqrt(8·r·sagitta)`). Capped at `CURVE_MAX_SAMPLES`.
    Minimum 2 — single-chord arcs would render as straight lines.
    """
    chord = sqrt(8.0 * radius * CURVE_TARGET_SAGITTA_M)
    return max(2, min(CURVE_MAX_SAMPLES,
                      int(arc_length / chord + 0.999)))
# Meters per degree at equator; lon component is additionally scaled by
# cos(latitude) for equal-distance projection.
_M_PER_DEG = 111319.49


# =============================================================================
# GTFS stop metadata
# =============================================================================

def load_stop_meta() -> dict:
    """Return {stop_id: {"name": stop_name, "parent": parent_station,
    "platform_code": platform_code}}.

    The official OTD GTFS feed prefixes parent_station values with `Parent`
    (e.g. `Parent8507000`); the prefix is stripped here so downstream
    clustering and comparisons are format-agnostic. `platform_code` is the
    raw GTFS field (empty string when the feed omits it).
    """
    meta = {}
    if not GTFS_STOPS.exists():
        return meta
    with open(GTFS_STOPS, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sid = row["stop_id"]
            parent = row.get("parent_station", "").removeprefix("Parent")
            entry = {
                "name": row.get("stop_name", ""),
                "parent": parent,
                "platform_code": (row.get("platform_code") or "").strip(),
            }
            meta[sid] = entry
            base = sid.split(":")[0]
            if base not in meta:
                meta[base] = entry
    return meta


def load_stop_sloid() -> dict:
    """Return {stop_id: sloid} from the pre-pfaedle filtered stops.txt
    (`original_stop_id` column, dropped by pfaedle in `gtfs_routed`).
    """
    out = {}
    if not GTFS_STOPS_PRE_PFAEDLE.exists():
        return out
    with open(GTFS_STOPS_PRE_PFAEDLE, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sloid = (row.get("original_stop_id") or "").strip()
            if sloid:
                out[row["stop_id"]] = sloid
    return out


def load_atlas_attributes() -> dict:
    """Return {sloid: {"length": float|None, "compass_direction": float|None}}.

    Reads only the BOARDING_PLATFORM rows from atlas v2 traffic-point CSV.
    Empty / unparseable numeric fields become None.
    """
    out = {}
    if not ATLAS_CSV.exists():
        print(f"WARNING: atlas CSV not found at {ATLAS_CSV} — attributes will be empty")
        return out

    def _f(v):
        v = (v or "").strip()
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    with open(ATLAS_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            if row.get("trafficPointElementType") != "BOARDING_PLATFORM":
                continue
            sloid = row.get("sloid", "").strip()
            if not sloid:
                continue
            out[sloid] = {
                "length": _f(row.get("length")),
                "compass_direction": _f(row.get("compassDirection")),
            }
    return out


def write_stop_attributes_diag(line_stops: dict) -> dict:
    """Build the per-stop attribute lookup + diagnostic for every stop_id that
    appears in any drawn line. Emits stop_attributes_sources.json and returns
    the per-stop dict for downstream consumers (debug overlay, dot placement).
    """
    stop_sloid = load_stop_sloid()
    atlas = load_atlas_attributes()
    print(f"  {len(stop_sloid):,} GTFS stops with SLOID, "
          f"{len(atlas):,} atlas BOARDING_PLATFORM rows")

    used_stop_ids: set = set()
    for ls_entry in line_stops.values():
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        for trip in triplets:
            if len(trip) >= 3 and trip[2]:
                used_stop_ids.add(trip[2])

    out: dict = {}
    n_match = 0
    for sid in used_stop_ids:
        sloid = stop_sloid.get(sid)
        atlas_row = atlas.get(sloid) if sloid else None
        if atlas_row is not None:
            out[sid] = {
                "status": "atlas_match",
                "sloid": sloid,
                "length": atlas_row["length"],
                "compass_direction": atlas_row["compass_direction"],
            }
            n_match += 1
        else:
            out[sid] = {
                "status": "no_atlas_match",
                "sloid": sloid,
            }

    OUT_STOP_ATTRS_DIAG.write_text(json.dumps(out, ensure_ascii=False))
    print(f"  Stop attributes: {n_match:,}/{len(out):,} stops matched atlas "
          f"→ {OUT_STOP_ATTRS_DIAG}")
    return out


TERMINUS_DEDUP_RADIUS_M = 10.0
ARRIVAL_DROP_MODES = {"tram", "bus", "regional_bus"}


def compute_terminus_skip_oids(line_stops: dict,
                                line_lookup: dict | None = None,
                                stop_meta: dict | None = None,
                                radius_m: float = TERMINUS_DEDUP_RADIUS_M):
    """Return `(skip_first_oids, skip_last_oids)`.

    `skip_first_oids` — osm_ids whose FIRST entry (departure terminus) should
    be omitted because another line arrives at the same stop_id within
    `radius_m`. Keeps the arrival side as the visible dot+extent (its extent
    is the non-degenerate one for non-rail) and lets the popup-aggregation
    pass surface both directions.

    `skip_last_oids` — tram / bus / regional_bus osm_ids whose LAST entry
    (arrival terminus) is dropped because either (1) the rule above did NOT
    pair it with any departure (layover ~100 m from the real terminus that
    the same line never visits), OR (2) its stop_id has no `platform_code`
    AND some other feature in the same sibling group (ref, agency_id, mode)
    visits the same UIC at a stop_id WITH a `platform_code` — the
    platform-coded entry is the real platform, the bare-numeric layover is
    redundant. `line_lookup` is required to apply rule 1; `stop_meta` is
    additionally required for rule 2.
    """
    arrivals_by_sid: dict = {}
    departures: list = []
    arrivals_meta: list = []  # (osm_id, sid, lon, lat) for arrival-side rule
    for oid, entry in line_stops.items():
        triplets = entry.get("stops", []) if isinstance(entry, dict) else entry
        if not triplets or len(triplets) < 2:
            continue
        first = triplets[0]
        last = triplets[-1]
        if len(first) >= 3 and first[2]:
            departures.append((str(oid), first[2], first[0], first[1]))
        if len(last) >= 3 and last[2]:
            arrivals_by_sid.setdefault(last[2], []).append(
                (str(oid), last[0], last[1]))
            arrivals_meta.append((str(oid), last[2], last[0], last[1]))

    skip_first: set = set()
    departures_by_sid: dict = {}
    for oid_dep, sid, lon_d, lat_d in departures:
        departures_by_sid.setdefault(sid, []).append((oid_dep, lon_d, lat_d))
        for oid_arr, lon_a, lat_a in arrivals_by_sid.get(sid, []):
            if oid_arr == oid_dep:
                continue
            if haversine_km(lon_d, lat_d, lon_a, lat_a) * 1000.0 <= radius_m:
                skip_first.add(oid_dep)
                break

    skip_last: set = set()
    if line_lookup is None:
        return skip_first, skip_last

    # Sibling-group index: (ref, agency_id, mode) -> set of UICs visited at a
    # stop_id with a non-empty platform_code. Used by rule 2.
    sibling_platform_uics: dict = {}
    if stop_meta is not None:
        for oid, entry in line_stops.items():
            info = line_lookup.get(str(oid)) or line_lookup.get(oid)
            if not info or info.get("mode") not in ARRIVAL_DROP_MODES:
                continue
            key = (info.get("ref", ""), info.get("agency_id", ""),
                   info.get("mode", ""))
            triplets = entry.get("stops", []) if isinstance(entry, dict) else entry
            uics = sibling_platform_uics.setdefault(key, set())
            for trip in triplets:
                if len(trip) < 3:
                    continue
                sid = trip[2]
                if not sid:
                    continue
                meta = stop_meta.get(sid) or stop_meta.get(sid.split(":")[0])
                if not meta or not meta.get("platform_code"):
                    continue
                uics.add(sid.split(":")[0])

    for oid_arr, sid, lon_a, lat_a in arrivals_meta:
        info = line_lookup.get(oid_arr) or line_lookup.get(str(oid_arr))
        if not info or info.get("mode") not in ARRIVAL_DROP_MODES:
            continue

        # Rule 1: unpaired arrival.
        paired = False
        for oid_dep, lon_d, lat_d in departures_by_sid.get(sid, []):
            if oid_dep == oid_arr:
                continue
            if haversine_km(lon_d, lat_d, lon_a, lat_a) * 1000.0 <= radius_m:
                paired = True
                break
        if not paired:
            skip_last.add(oid_arr)
            continue

        # Rule 2: layover shadowed by same-line real-platform sibling.
        if stop_meta is None:
            continue
        meta = stop_meta.get(sid) or stop_meta.get(sid.split(":")[0])
        if meta and meta.get("platform_code"):
            continue
        key = (info.get("ref", ""), info.get("agency_id", ""),
               info.get("mode", ""))
        uic = sid.split(":")[0]
        if uic in sibling_platform_uics.get(key, set()):
            skip_last.add(oid_arr)

    return skip_first, skip_last


# =============================================================================
# Platform-extent computation (pill-rendering concept)
# =============================================================================

def _cum_dist_m(coords):
    """Cumulative distance in metres from start of polyline to each vertex."""
    out = [0.0]
    for i in range(1, len(coords)):
        out.append(out[-1] + haversine_km(
            coords[i-1][0], coords[i-1][1], coords[i][0], coords[i][1]) * 1000.0)
    return out


def _project_meters(px, py, coords, dists):
    """Closest point on polyline to (px, py); returns cumulative distance from
    polyline start in metres."""
    best_sq = float("inf")
    best_t = 0.0
    for i in range(len(coords) - 1):
        ax, ay = coords[i]
        bx, by = coords[i+1]
        dx, dy = bx - ax, by - ay
        seg_sq_lonlat = dx*dx + dy*dy
        if seg_sq_lonlat == 0:
            tt = 0.0
        else:
            tt = max(0.0, min(1.0, ((px-ax)*dx + (py-ay)*dy) / seg_sq_lonlat))
        cx, cy = ax + tt*dx, ay + tt*dy
        d = (px-cx)**2 + (py-cy)**2
        if d < best_sq:
            best_sq = d
            seg_len_m = dists[i+1] - dists[i]
            best_t = dists[i] + tt * seg_len_m
    return best_t


def _interp_at(coords, dists, t):
    """Interpolate polyline at cumulative distance t (metres). Clamps to ends."""
    if t <= 0:
        return coords[0][0], coords[0][1]
    if t >= dists[-1]:
        return coords[-1][0], coords[-1][1]
    for i in range(len(dists) - 1):
        if dists[i] <= t <= dists[i+1]:
            seg = dists[i+1] - dists[i]
            if seg == 0:
                return coords[i][0], coords[i][1]
            f = (t - dists[i]) / seg
            ax, ay = coords[i]
            bx, by = coords[i+1]
            return ax + f * (bx - ax), ay + f * (by - ay)
    return coords[-1][0], coords[-1][1]


def _slice_polyline(coords, dists, t_start, t_end):
    """Return the polyline vertex sequence between cumulative distances
    t_start and t_end (metres), with interpolated endpoints."""
    if t_start > t_end:
        t_start, t_end = t_end, t_start
    t_start = max(0.0, t_start)
    t_end = min(dists[-1], t_end)
    pts = [_interp_at(coords, dists, t_start)]
    for i, d in enumerate(dists):
        if t_start < d < t_end:
            pts.append((coords[i][0], coords[i][1]))
    pts.append(_interp_at(coords, dists, t_end))
    return pts


def _directional_tangent_at(polyline, dists, t, window_m=20.0):
    """Per-metre (dx, dy) tangent of `polyline` at arc-length `t`, directional
    (forward in increasing-t direction). Chord computed over a ±window_m window
    around t — so pfaedle "stub" segments at line termini that carry normal-sized
    lon/lat deltas across sub-metre arc-lengths don't blow up the per-metre rate.
    Returns None if the polyline is too short to compute a chord.
    """
    if len(polyline) < 2:
        return None
    poly_max = dists[-1]
    if poly_max <= 0:
        return None
    lo_t = max(0.0, t - window_m)
    hi_t = min(poly_max, t + window_m)
    arc = hi_t - lo_t
    if arc <= 0:
        return None
    lo = _interp_at(polyline, dists, lo_t)
    hi = _interp_at(polyline, dists, hi_t)
    return ((hi[0] - lo[0]) / arc, (hi[1] - lo[1]) / arc)


# Missing-range fill (tram/bus/regional_bus): sibling-borrow gates.
SIBLING_PROXIMITY_M = 2.0
SIBLING_ANGLE_TOL_RAD = radians(15.0)


def _borrow_backward_segment(p_lon, p_lat, target_lon, target_lat,
                              my_dx, my_dy, t_on_self, L,
                              siblings, self_oid):
    """Try to borrow the missing `L - t_on_self` metres of backward extent from
    a sibling line's polyline. Returns a list of (lon, lat) in backward→forward
    order ending at (target_lon, target_lat), translated so the join with the
    on-polyline portion is exact. Returns None if no sibling qualifies.

    Gates per concept (pill-rendering, missing-range fill):
      • ~2 m proximity at the snapped GTFS coord p (rejects parallel-street siblings)
      • ~15° tangent agreement at the sibling's nearest point (rejects diverging
        siblings — e.g. tram turning loops at termini)
      • aligned vs reversed direction: handled via the tangent dot product, with
        the sibling walk reversed to keep our backward direction consistent.

    Circular lines are their own sibling (self_oid == sib_oid): the projection
    starts from the polyline's far end so a loop's "return to start" geometry
    fills its own first stop's backward extent.
    """
    if L <= t_on_self:
        return None
    fill_m = L - t_on_self

    cos_lat = cos(radians(p_lat))
    my_ex, my_ey = my_dx * cos_lat, my_dy
    my_mag = sqrt(my_ex * my_ex + my_ey * my_ey)
    if my_mag == 0:
        return None
    cos_tol = cos(SIBLING_ANGLE_TOL_RAD)

    for sib_oid, sib_poly in siblings:
        if len(sib_poly) < 2:
            continue
        sib_dists = _cum_dist_m(sib_poly)
        sib_total = sib_dists[-1]
        if sib_total <= 0:
            continue

        if sib_oid == self_oid:
            q_t = sib_total
            q_lon, q_lat = sib_poly[-1]
        else:
            q_t = _project_meters(p_lon, p_lat, sib_poly, sib_dists)
            q_lon, q_lat = _interp_at(sib_poly, sib_dists, q_t)

        if haversine_km(p_lon, p_lat, q_lon, q_lat) * 1000.0 > SIBLING_PROXIMITY_M:
            continue

        sib_tan = _directional_tangent_at(sib_poly, sib_dists, q_t)
        if sib_tan is None:
            continue
        sib_dx, sib_dy = sib_tan
        sib_ex, sib_ey = sib_dx * cos_lat, sib_dy
        sib_mag = sqrt(sib_ex * sib_ex + sib_ey * sib_ey)
        if sib_mag == 0:
            continue

        cos_ang = (my_ex * sib_ex + my_ey * sib_ey) / (my_mag * sib_mag)
        if abs(cos_ang) < cos_tol:
            continue
        aligned = cos_ang > 0

        if aligned:
            walk_end_t = q_t - t_on_self
            walk_start_t = walk_end_t - fill_m
            if walk_start_t < 0:
                continue
            seg = list(_slice_polyline(sib_poly, sib_dists, walk_start_t, walk_end_t))
        else:
            walk_start_t = q_t + t_on_self
            walk_end_t = walk_start_t + fill_m
            if walk_end_t > sib_total:
                continue
            seg = list(_slice_polyline(sib_poly, sib_dists, walk_start_t, walk_end_t))
            seg.reverse()

        if len(seg) < 2:
            continue

        end_lon, end_lat = seg[-1]
        dlon = target_lon - end_lon
        dlat = target_lat - end_lat
        return [(x + dlon, y + dlat) for x, y in seg]

    return None


def _resolve_length(mode: str, atlas_length, cfg: dict):
    """Pick the platform length to use for a given mode and atlas value.

    Atlas value is used when it lies within the per-mode sanity range;
    otherwise the per-mode default is returned. Returns None for modes
    not in the rendering scope (ferry, mountain).
    """
    if mode not in cfg.get("default_length_m", {}):
        return None
    smin = cfg["sanity_min_m"][mode]
    smax = cfg["sanity_max_m"][mode]
    if atlas_length is not None and smin <= atlas_length <= smax:
        return atlas_length
    return cfg["default_length_m"][mode]


def _platform_extent(stop_lon, stop_lat, polyline, mode, atlas_length, cfg,
                      osm_id=None, siblings=None):
    """Return the (lon, lat) sequence tracing the platform's allowed range
    along its polyline, or None for out-of-scope modes / degenerate geometry.

    Anchoring (per pill-rendering concept):
      • train, metro  — GTFS coord (snapped to polyline) is platform CENTRE
                        → range = ±L/2.
      • tram, bus     — GTFS coord is FRONT of stop → range = [coord - L, coord].

    Missing-range fill differs by mode:
      • rail (train, metro): straight-line tangent-direction extrapolation only.
      • tram / bus / regional_bus: sibling-borrow first (passes through `siblings`
        as a list of (osm_id, polyline) tuples in the same `(ref, agency_id, mode)`
        group), straight-line tangent extrapolation as fallback.
    """
    if len(polyline) < 2:
        return None
    L = _resolve_length(mode, atlas_length, cfg)
    if L is None:
        return None
    dists = _cum_dist_m(polyline)
    poly_max = dists[-1]
    if poly_max <= 0:
        return None
    t = _project_meters(stop_lon, stop_lat, polyline, dists)

    if mode not in ("train", "metro"):
        # Tram / bus / regional_bus: backward-anchored range [t-L, t].
        if t >= L:
            # Polyline supports the full backward range — slice and return.
            return list(_slice_polyline(polyline, dists, t - L, t))

        # On-polyline portion: polyline start to snapped point (length t).
        on_slice = list(_slice_polyline(polyline, dists, 0.0, t))
        if len(on_slice) >= 2 and on_slice[0] == on_slice[-1]:
            on_slice = [on_slice[0]]

        tan = _directional_tangent_at(polyline, dists, t)
        if tan is None:
            return on_slice  # no usable tangent → can't fill
        dx_per_m, dy_per_m = tan

        p = _interp_at(polyline, dists, t)
        target = on_slice[0] if on_slice else (polyline[0][0], polyline[0][1])

        if siblings:
            borrowed = _borrow_backward_segment(
                p[0], p[1], target[0], target[1],
                dx_per_m, dy_per_m, t, L, siblings, osm_id)
            if borrowed is not None:
                if len(on_slice) <= 1:
                    return borrowed
                return borrowed[:-1] + on_slice

        # Straight-line tangent extrapolation backward.
        missing_m = L - t
        extrap = (target[0] - dx_per_m * missing_m,
                  target[1] - dy_per_m * missing_m)
        if not on_slice:
            return [extrap, (p[0], p[1])]
        return [extrap] + on_slice

    half_L = L / 2.0
    t_start_ideal = t - half_L
    t_end_ideal = t + half_L

    on_start = max(0.0, t_start_ideal)
    on_end = min(poly_max, t_end_ideal)
    slice_pts = list(_slice_polyline(polyline, dists, on_start, on_end))

    tan = _directional_tangent_at(polyline, dists, t)
    if tan is None:
        return slice_pts
    dx_per_m, dy_per_m = tan

    pts = []
    if t_start_ideal < 0 and slice_pts:
        # Extrapolate from polyline[0] backwards (against forward tangent)
        missing_m = -t_start_ideal
        wx = slice_pts[0][0] - dx_per_m * missing_m
        wy = slice_pts[0][1] - dy_per_m * missing_m
        pts.append((wx, wy))
    pts.extend(slice_pts)
    if t_end_ideal > poly_max and slice_pts:
        # Extrapolate from polyline[-1] forward (with forward tangent)
        missing_m = t_end_ideal - poly_max
        ex = slice_pts[-1][0] + dx_per_m * missing_m
        ey = slice_pts[-1][1] + dy_per_m * missing_m
        pts.append((ex, ey))
    return pts


# Window over which the per-stop polyline tangent is averaged. Sized to
# stay inside the platform extent (per-mode default ≤ 35 m for non-rail,
# 100 m for rail) so the averaged direction reflects what's happening at
# the dot, not the chord of the whole extent. For 30 m bus/tram extents,
# 40 m smoothing would spill past the extent into adjacent polyline and
# pull the angle off — see Eigerplatz, where it puts C and D into
# different tangent groups despite both running on the same OSM way.
TANGENT_WINDOW_M = 10.0


def _stop_tangent(s):
    """Unit polyline tangent at the stop's snap position, averaged over a
    ±TANGENT_WINDOW_M/2 window centred on (s["lon"], s["lat"]) projected
    onto its extent. Canonicalised to the upper half-plane so opposite-
    direction polylines don't cancel. Returns None when the extent is
    degenerate or the polyline is too short to compute a tangent.
    """
    ext = s.get("extent")
    if not ext or len(ext) < 2:
        return None
    dists = _cum_dist_m(ext)
    if dists[-1] <= 0:
        return None
    t = _project_meters(s["lon"], s["lat"], ext, dists)
    tan = _smoothed_tangent_at(ext, dists, t, window_m=TANGENT_WINDOW_M)
    if tan is None:
        return None
    dx, dy = tan
    mag = sqrt(dx * dx + dy * dy)
    if mag <= 0:
        return None
    if dx < 0 or (dx == 0 and dy < 0):
        dx, dy = -dx, -dy
    return (dx / mag, dy / mag)


def _mean_unit_tangent(cluster: list):
    """Mean unit tangent across stops in the cluster, computed at each
    stop's snap position via _stop_tangent. Returns (tx, ty) or None if no
    usable stops.
    """
    ax = ay = 0.0
    n = 0
    for s in cluster:
        t = _stop_tangent(s)
        if t is None:
            continue
        ax += t[0]
        ay += t[1]
        n += 1
    if n == 0:
        return None
    mag = sqrt(ax*ax + ay*ay)
    if mag <= 0:
        return None
    return (ax / mag, ay / mag)


def _extent_intersect_axis(ext, tx, ty, sigma):
    """Return the point on `ext` polyline whose tangent-coordinate equals
    `sigma`, i.e. where x*tx + y*ty == sigma. None if no segment crosses.
    For monotone-in-t polylines (typical short station extents) the first
    crossing is the only one.
    """
    prev_d = None
    for i, (x, y) in enumerate(ext):
        d = x * tx + y * ty - sigma
        if d == 0.0:
            return (x, y)
        if prev_d is not None and prev_d * d < 0:
            px, py = ext[i - 1]
            t = prev_d / (prev_d - d)
            return (px + t * (x - px), py + t * (y - py))
        prev_d = d
    return None


def _place_dot_on_extent(ext, tx, ty, sigma):
    """Best point on `ext` for a bar at axial position σ. Uses the σ-line
    intersection when it exists; otherwise snaps to the polyline endpoint
    closest to σ in the tangent direction (so an asymmetric polyline whose
    end is just past σ still places its dot at the polyline tip — visually
    right next to the bar — rather than missing the bar entirely).
    """
    pt = _extent_intersect_axis(ext, tx, ty, sigma)
    if pt is not None:
        return pt
    t_first = ext[0][0] * tx + ext[0][1] * ty
    t_last = ext[-1][0] * tx + ext[-1][1] * ty
    if abs(t_first - sigma) <= abs(t_last - sigma):
        return (ext[0][0], ext[0][1])
    return (ext[-1][0], ext[-1][1])


def _smoothed_tangent_at(polyline, dists, t, window_m=40.0):
    """Unit polyline tangent at arc-length `t`, averaged over a ±window_m/2
    window. Returns (tx, ty) or None if the polyline is too short. Smoothing
    reduces sensitivity to small pfaedle-routing kinks.
    """
    if not polyline or len(polyline) < 2:
        return None
    poly_max = dists[-1]
    if poly_max <= 0:
        return None
    half = window_m / 2.0
    t_lo = max(0.0, t - half)
    t_hi = min(poly_max, t + half)
    if t_hi - t_lo < 1e-9:
        # Window collapsed (extremely short polyline) — fall back to the
        # nearest segment's direction.
        i = 0 if t <= 0 else len(polyline) - 2
        dx = polyline[i + 1][0] - polyline[i][0]
        dy = polyline[i + 1][1] - polyline[i][1]
    else:
        lo = _interp_at(polyline, dists, t_lo)
        hi = _interp_at(polyline, dists, t_hi)
        dx = hi[0] - lo[0]
        dy = hi[1] - lo[1]
    mag = sqrt(dx * dx + dy * dy)
    if mag <= 0:
        return None
    return dx / mag, dy / mag


def _angular_dist_mod_pi(a1, a2):
    """Smallest angular distance between two angles on the half-circle
    [0, π) (0 and π are the same orientation)."""
    d = abs(a1 - a2) % pi
    return min(d, pi - d)


def _circular_median_mod_pi(angles, reference):
    """Median of angles on the half-circle [0, π), computed as signed offsets
    from `reference` in [-π/2, π/2). Robust to a single outlier angle.
    `None` entries are skipped; if nothing remains, returns `reference`.

    Caller ensures inputs lie within π/2 of `reference` — true for σ-clump
    members, which the tangent-group gate keeps within ~10° of each other.
    """
    offsets = []
    for a in angles:
        if a is None:
            continue
        d = (a - reference) % pi
        if d > pi / 2:
            d -= pi
        offsets.append(d)
    if not offsets:
        return reference
    offsets.sort()
    n = len(offsets)
    if n % 2 == 1:
        med = offsets[n // 2]
    else:
        med = 0.5 * (offsets[n // 2 - 1] + offsets[n // 2])
    return (reference + med) % pi


def _tangent_groups(platforms, max_angle_rad):
    """Group platforms by extent tangent direction. Union-find with the
    given angular tolerance (mod π) — two platforms are in the same group
    if their tangents are within `max_angle_rad`. Transitive closure means
    curved-but-coherent sets stay together. Returns list of groups."""
    data = []
    for p in platforms:
        t = _stop_tangent(p)
        if t is None:
            continue
        data.append((atan2(t[1], t[0]), p))
    n = len(data)
    if n == 0:
        return []
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _angular_dist_mod_pi(data[i][0], data[j][0]) <= max_angle_rad:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj

    groups = {}
    for i in range(n):
        r = find(i)
        groups.setdefault(r, []).append(data[i][1])
    return list(groups.values())


SWEEP_STEP_M = 10.0
CENTRAL_INNER_FRACTION = 0.7
SIGMA_CLUMP_SLACK_M = 5.0
# Tolerance on the σ-projection scoring check. A member whose σ-range boundary
# coincides with the sweep position can drop out by float-precision noise; this
# slack keeps it in scoring. Sized larger than pure float noise so it also
# absorbs minor pfaedle-routing jitter on the polyline tangent.
SIGMA_BOUNDARY_TOL_M = 0.5
PROTECTION_RADIUS_RAIL_M = 30.0
PROTECTION_RADIUS_NONRAIL_M = 5.0


def _sigma_clumps(group, slack_m=SIGMA_CLUMP_SLACK_M):
    """Split a tangent group into σ-clumps along the group's mean tangent.

    The perpendicular sweep walks only the central member's extent, so a
    tangent group spread across hundreds of metres of the same line — common
    at large stations where multiple stop bays sit along one street — gets
    only one bar near whichever sub-cluster contains the 2-D centroid; the
    far-away sub-cluster is unreachable. Splitting by σ-interval overlap
    along the group's mean tangent yields one sweep per clump.

    Two members belong to the same clump iff their σ-intervals overlap
    within `slack_m`. The slack absorbs the small mismatch between the
    group's mean tangent (used here) and each member's own tangent (used in
    `_perpendicular_sweep`'s sigma calc): 10° angular tolerance can shift a
    30 m extent's σ endpoints by a couple of metres.
    """
    if len(group) < 2:
        return [list(group)]
    mean_tan = _mean_unit_tangent(group)
    if mean_tan is None:
        return [list(group)]
    tx, ty = mean_tan

    intervals = []
    for p in group:
        ext = p.get("extent")
        if not ext or len(ext) < 2:
            continue
        sigmas = [v[0] * tx + v[1] * ty for v in ext]
        intervals.append((min(sigmas), max(sigmas), p))
    if not intervals:
        return []

    # Inside coordinate_dots_global_stab the cluster runs in scaled coords
    # (lon × cos_lat), so 1° on either axis ≈ 111000 m.
    slack = slack_m / 111000.0

    intervals.sort(key=lambda iv: iv[0])
    clumps = []
    current = [intervals[0][2]]
    current_hi = intervals[0][1]
    for lo, hi, p in intervals[1:]:
        if lo <= current_hi + slack:
            current.append(p)
            if hi > current_hi:
                current_hi = hi
        else:
            clumps.append(current)
            current = [p]
            current_hi = hi
    clumps.append(current)
    return clumps


def _expand_sigma_clump(clump, angle_tol_rad, raw, lone_outlier_gap_m):
    """Recursively run the perpendicular sweep on a σ-clump, peeling off the
    matched members after each pass and re-σ-clumping the rest. Yields one
    (sub_clump, options) pair per discovered bar.

    Catches σ-clumps that contain two parallel sub-clusters on different
    transverse axes — both share enough σ-overlap to stay in one σ-clump,
    but no single bar can stab both. The first sweep finds one sub-cluster,
    the rerun finds the other.

    `raw` is the cluster-level raw[id(p)] → (lon, lat) snapshot of pre-
    placement positions. Used for the distinct-position gate: members
    sharing a snapped GTFS position count once. The recursion terminates
    when the next sweep finds no candidate, or fewer than two distinct-
    position members remain.

    Peel-off uses the local pick (min gtfs_dist among tied options). The
    cluster-level tie-break may later choose a different option from the
    tied set whose matched set differs; any resulting overlap (or near-
    duplicate along-tangent placement) is rejected by the tie-break's
    combination validity check, not pre-filtered here.
    """
    if len(clump) < 2:
        return
    if len({raw[id(p)] for p in clump}) < 2:
        return
    options = _perpendicular_sweep(clump, angle_tol_rad, lone_outlier_gap_m)
    if not options:
        return

    yield (clump, options)

    chosen = min(options, key=lambda o: o["gtfs_dist"])
    matched_ids = {id(clump[k]) for k in chosen["scoring"]}
    matched_ids.update(id(clump[k]) for k in chosen["covered"])
    remaining = [p for p in clump if id(p) not in matched_ids]

    for sub in _sigma_clumps(remaining):
        yield from _expand_sigma_clump(sub, angle_tol_rad, raw, lone_outlier_gap_m)


def _perpendicular_sweep(group, angle_tol_rad, lone_outlier_gap_m):
    """For a tangent group, find every perpendicular bar tied at the max
    scoring-stab count by sweeping along the central member's platform
    extent at SWEEP_STEP_M resolution.

    The sweep walks the central member's extent (the same per-stop polyline
    drawn as the debug overlay) — not the full line polyline. This keeps
    the sweep bounded to the platform region and intrinsically fast.

    The central member is picked from the inner CENTRAL_INNER_FRACTION of
    the group (closest to the group centroid) — outer members are excluded
    from central-member selection so an off-to-the-side member can't drag
    the sweep away. Excluded members still count for stab scoring.

    Returns a non-empty list of option dicts, or None when no sweep position
    scoring-stabs ≥ 2 members. Each option carries the bar geometry, the
    scoring/covered member sets, the bar's perpendicular center (used by the
    multi-group inter-bar-distance tie-break), and the gtfs-distance score
    (used as final tie-break — sum of placed-dot to GTFS-snap distance in
    scaled-coord units).
    """
    n = len(group)
    if n < 2:
        return None

    cx = sum(p["lon"] for p in group) / n
    cy = sum(p["lat"] for p in group) / n

    # Inner-fraction subset for central-member selection.
    n_inner = max(1, n - int((1.0 - CENTRAL_INNER_FRACTION) * n))
    inner_sorted = sorted(
        group,
        key=lambda p: (p["lon"] - cx) ** 2 + (p["lat"] - cy) ** 2,
    )[:n_inner]
    central = inner_sorted[0]
    central_ext = central.get("extent")
    if not central_ext or len(central_ext) < 2:
        return None
    central_dists = _cum_dist_m(central_ext)
    ext_max = central_dists[-1]
    if ext_max <= 0:
        return None

    # Dense sweep at SWEEP_STEP_M along the central extent, plus the
    # arc-length projections of every group member's extent endpoints. The
    # 10 m grid alone can miss the optimal sigma by up to ±5 m; the
    # endpoint projections are exactly the sub-metre-precise positions
    # where a member transitions from stabbed to not-stabbed (or vice
    # versa), so adding them snaps the candidate set to the transitions.
    n_steps = max(2, int(ext_max / SWEEP_STEP_M) + 1)
    candidate_arcs_set = {i * ext_max / (n_steps - 1) for i in range(n_steps)}
    for p in group:
        ext = p["extent"]
        if not ext or len(ext) < 2:
            continue
        for endpoint in (ext[0], ext[-1]):
            candidate_arcs_set.add(
                _project_meters(endpoint[0], endpoint[1],
                                central_ext, central_dists))
    candidate_arcs = sorted(candidate_arcs_set)

    # Per-member extent + cum-dist cache (used per sweep step for the
    # closest-point projection and local tangent computation).
    member_exts = []
    member_dists_list = []
    central_idx = None
    for k, p in enumerate(group):
        ext = p.get("extent")
        if ext and len(ext) >= 2:
            member_exts.append(ext)
            member_dists_list.append(_cum_dist_m(ext))
        else:
            member_exts.append(None)
            member_dists_list.append(None)
        if p is central:
            central_idx = k

    # Single pass: compute scoring (≤10°-aligned members crossing bar) AND
    # accidentally-covered members (wrong-angle, crossing within scoring-set
    # drawn span). The stab count counts BOTH — wrong-angle members on the
    # bar's drawn span are real placements and contribute. The bar's drawn
    # span is still determined by scoring members only (no extension for
    # wrong-angle members).
    gap_thresh = lone_outlier_gap_m / 111000.0
    dedup_tol = DEDUP_TOL_M / 111000.0
    sigma_tol = SIGMA_BOUNDARY_TOL_M / 111000.0
    best_count = 0
    raw_tied = []
    for arc_d in candidate_arcs:
        pos = _interp_at(central_ext, central_dists, arc_d)

        # Per-position consensus bar angle: each σ-clump member's local
        # tangent at the closest point on its own extent to `pos`,
        # TANGENT_WINDOW_M-smoothed, then circular median (mod π) across
        # all members. Curvature-aware (each member contributes its local
        # direction at the bar's location, not its overall extent chord)
        # and robust to one outlier whose pfaedle shape is rotated.
        member_angles = []
        for k in range(n):
            m_ext = member_exts[k]
            if m_ext is None:
                member_angles.append(None)
                continue
            m_dists = member_dists_list[k]
            m_arc = _project_meters(pos[0], pos[1], m_ext, m_dists)
            m_tan = _smoothed_tangent_at(m_ext, m_dists, m_arc,
                                          TANGENT_WINDOW_M)
            if m_tan is None:
                member_angles.append(None)
                continue
            member_angles.append(atan2(m_tan[1], m_tan[0]))
        ref_angle = (member_angles[central_idx]
                     if central_idx is not None
                     else None)
        if ref_angle is None:
            for a in member_angles:
                if a is not None:
                    ref_angle = a
                    break
        if ref_angle is None:
            continue
        bar_angle = _circular_median_mod_pi(member_angles, ref_angle)
        tx, ty = cos(bar_angle), sin(bar_angle)
        sigma = pos[0] * tx + pos[1] * ty
        nx, ny = -ty, tx

        # Phase 1: scoring members
        scoring = []
        for k, p in enumerate(group):
            ma = member_angles[k]
            if ma is None:
                continue
            if _angular_dist_mod_pi(ma, bar_angle) > angle_tol_rad:
                continue
            ext = p["extent"]
            ts = [v[0] * tx + v[1] * ty for v in ext]
            if min(ts) - sigma_tol <= sigma <= max(ts) + sigma_tol:
                scoring.append(k)
        if len(scoring) < 2:
            continue
        # ≥ 2 distinct platform positions among scoring members (bar's drawn
        # anchors). Wrong-angle members are not anchors and aren't counted.
        distinct_positions = {
            (round(group[k]["lon"], 6), round(group[k]["lat"], 6))
            for k in scoring
        }
        if len(distinct_positions) < 2:
            continue

        # Drawn span from scoring members
        scoring_pts = [
            _place_dot_on_extent(group[k]["extent"], tx, ty, sigma)
            for k in scoring
        ]
        scoring_n = [pt[0] * nx + pt[1] * ny for pt in scoring_pts]

        # Lone-outlier drop: any scoring member on a single-distinct-
        # position side of a ≥ lone_outlier_gap_m gap along the bar axis
        # is dropped from this candidate's scoring set. Repeats because
        # removing a dot can expose a new wide gap. Dropped members re-
        # enter the σ-clump's unplaced pool via the recursive rerun →
        # leftover-fill path (where an isolated platform belongs).
        while len(scoring) >= 2:
            order = sorted(range(len(scoring)), key=lambda i: scoring_n[i])
            sn = [scoring_n[i] for i in order]
            # Cluster successive sorted entries within dedup_tol into one
            # distinct bar-axis position.
            pos_groups = [[order[0]]]
            for j in range(1, len(order)):
                if sn[j] - sn[j - 1] <= dedup_tol:
                    pos_groups[-1].append(order[j])
                else:
                    pos_groups.append([order[j]])
            drop = None
            for gi in range(len(pos_groups) - 1):
                gap = (scoring_n[pos_groups[gi + 1][0]]
                       - scoring_n[pos_groups[gi][-1]])
                if gap < gap_thresh:
                    continue
                # gi + 1 distinct positions on the left side of this gap;
                # the remaining pos_groups on the right.
                if gi + 1 == 1:
                    drop = set(pos_groups[0])
                    break
                if len(pos_groups) - (gi + 1) == 1:
                    drop = set(pos_groups[-1])
                    break
            if drop is None:
                break
            scoring = [s for i, s in enumerate(scoring) if i not in drop]
            scoring_pts = [s for i, s in enumerate(scoring_pts)
                           if i not in drop]
            scoring_n = [s for i, s in enumerate(scoring_n) if i not in drop]

        if len(scoring) < 2:
            continue
        # Re-check distinct platform positions on the post-drop scoring set.
        distinct_positions = {
            (round(group[k]["lon"], 6), round(group[k]["lat"], 6))
            for k in scoring
        }
        if len(distinct_positions) < 2:
            continue

        n_min, n_max = min(scoring_n), max(scoring_n)

        # Phase 2: wrong-angle members whose extent crosses the bar within
        # the scoring-set drawn span. These count toward the stab total but
        # do NOT influence n_min / n_max — the bar is not extended for them.
        scoring_set = set(scoring)
        covered = []
        covered_pts = []
        for k, p in enumerate(group):
            if k in scoring_set:
                continue
            ma = member_angles[k]
            if ma is None:
                continue
            # Only wrong-angle members are eligible for covered (scoring set
            # already takes the aligned ones).
            if _angular_dist_mod_pi(ma, bar_angle) <= angle_tol_rad:
                continue
            ext = p["extent"]
            ts = [v[0] * tx + v[1] * ty for v in ext]
            if not (min(ts) - sigma_tol <= sigma <= max(ts) + sigma_tol):
                continue
            cross_pt = _extent_intersect_axis(ext, tx, ty, sigma)
            if cross_pt is None:
                continue
            n_val = cross_pt[0] * nx + cross_pt[1] * ny
            if n_min <= n_val <= n_max:
                covered.append(k)
                covered_pts.append(cross_pt)

        total = len(scoring) + len(covered)
        entry = (tx, ty, sigma, scoring, covered,
                 scoring_pts, covered_pts)
        if total > best_count:
            best_count = total
            raw_tied = [entry]
        elif total == best_count:
            raw_tied.append(entry)
    if not raw_tied:
        return None

    # Second pass: enrich each tied position with bar center + gtfs-distance.
    options = []
    for tx, ty, sigma, scoring, covered, scoring_pts, covered_pts in raw_tied:
        bar_cx = sum(pt[0] for pt in scoring_pts) / len(scoring_pts)
        bar_cy = sum(pt[1] for pt in scoring_pts) / len(scoring_pts)

        gtfs_dist = 0.0
        for k, pt in zip(scoring, scoring_pts):
            p = group[k]
            gtfs_dist += sqrt((pt[0] - p["lon"]) ** 2
                              + (pt[1] - p["lat"]) ** 2)
        for k, pt in zip(covered, covered_pts):
            p = group[k]
            gtfs_dist += sqrt((pt[0] - p["lon"]) ** 2
                              + (pt[1] - p["lat"]) ** 2)

        options.append({
            "tx": tx, "ty": ty, "sigma": sigma,
            "scoring": scoring,
            "covered": covered,
            "bar_center": (bar_cx, bar_cy),
            "gtfs_dist": gtfs_dist,
        })

    return options


def _apply_option(group, option, placed_ids, record_stabbed=True):
    """Place this option's scoring + covered dots on their extents. When
    `record_stabbed` is False (e.g. trial placements during single-group
    measurement), the (osm_id, stop_id) pairs are NOT pushed to
    _STABBED_PAIRS — that's reserved for the chosen option's final pass.

    The multi-group tie-break guarantees no member is in two chosen bars,
    so apply doesn't need its own anti-overlap guard — every member it
    places is genuinely a new placement.
    """
    tx, ty, sigma = option["tx"], option["ty"], option["sigma"]
    for k in option["scoring"]:
        p = group[k]
        pt = _place_dot_on_extent(p["extent"], tx, ty, sigma)
        p["lon"], p["lat"] = pt
        placed_ids.add(id(p))
        if record_stabbed:
            _STABBED_PAIRS.add((str(p.get("osm_id", "")),
                                str(p.get("stop_id", ""))))
    for k in option["covered"]:
        p = group[k]
        pt = _extent_intersect_axis(p["extent"], tx, ty, sigma)
        if pt is None:
            continue
        p["lon"], p["lat"] = pt
        placed_ids.add(id(p))
        if record_stabbed:
            _STABBED_PAIRS.add((str(p.get("osm_id", "")),
                                str(p.get("stop_id", ""))))


def _record_diag_bar(group, option):
    """Append this option's perpendicular debug-bar geometry to _DIAG_BARS."""
    tx, ty, sigma = option["tx"], option["ty"], option["sigma"]
    nx, ny = -ty, tx
    n_values = [group[k]["lon"] * nx + group[k]["lat"] * ny
                for k in option["scoring"]]
    if len(n_values) < 2:
        return
    n_min, n_max = min(n_values), max(n_values)
    margin = (n_max - n_min) * 0.05 + 1e-6
    n_min -= margin
    n_max += margin
    ep1 = (sigma * tx + n_min * nx, sigma * ty + n_min * ny)
    ep2 = (sigma * tx + n_max * nx, sigma * ty + n_max * ny)
    _DIAG_BARS.append((ep1, ep2))


def _pick_options_multi_group(per_group_options, protection_m):
    """Pick one option per (clump, options, tgroup_id) entry.

    Reject combinations that violate either of:
      • Two bars in the SAME tangent group are within `protection_m` along
        the older bar's tangent direction (would draw as near-duplicate
        bars stacked on the same axis). Different tangent groups point in
        different directions and impose no along-tangent constraint on
        each other.
      • Any member appears in more than one chosen bar's scoring + covered
        set (would steal a stop from another bar).

    Score surviving combos by sum of pairwise bar-center distances; tie-
    break by total gtfs_dist. If no combination passes validity, fall back
    to picking each entry's min-gtfs_dist option independently — the
    structural guarantees are gone in that fallback, but it produces a
    deterministic result rather than nothing.
    """
    from itertools import product
    protection = protection_m / 111000.0

    def _valid(combo):
        # Same-tangent-group along-tangent guard.
        for i in range(len(combo)):
            tgi = per_group_options[i][2]
            cxi, cyi = combo[i]["bar_center"]
            txi, tyi = combo[i]["tx"], combo[i]["ty"]
            for j in range(i + 1, len(combo)):
                if per_group_options[j][2] != tgi:
                    continue
                cxj, cyj = combo[j]["bar_center"]
                proj = abs((cxj - cxi) * txi + (cyj - cyi) * tyi)
                if proj < protection:
                    return False
        # No member double-cover across the combo.
        seen = set()
        for i, opt in enumerate(combo):
            clump = per_group_options[i][0]
            for k in opt["scoring"]:
                mid = id(clump[k])
                if mid in seen:
                    return False
                seen.add(mid)
            for k in opt["covered"]:
                mid = id(clump[k])
                if mid in seen:
                    return False
                seen.add(mid)
        return True

    best = None
    best_key = None
    for combo in product(*(opts for _, opts, _ in per_group_options)):
        if not _valid(combo):
            continue
        total_dist = 0.0
        m = len(combo)
        for i in range(m):
            bx, by = combo[i]["bar_center"]
            for j in range(i + 1, m):
                jx, jy = combo[j]["bar_center"]
                total_dist += sqrt((bx - jx) ** 2 + (by - jy) ** 2)
        gtfs_total = sum(o["gtfs_dist"] for o in combo)
        key = (total_dist, gtfs_total)
        if best_key is None or key < best_key:
            best_key = key
            best = combo

    if best is None:
        # No valid combination — pick each entry's local min-gtfs_dist
        # option. This degenerate fallback can produce overlap or close
        # bars, but it always returns something.
        best = tuple(min(opts, key=lambda o: o["gtfs_dist"])
                     for _, opts, _ in per_group_options)
    return list(best)


def _pick_option_single_group(group, options, cluster,
                               platforms, raw, gtfs_centroid,
                               cos_lat=1.0):
    """Pick a tied option from a single-group cluster.

    With ≥ 1 leftover: enumerate options, run leftover-fill per option,
    pick minimum pill + 0.5 × connector length, tie-break by gtfs_dist.

    With no leftovers: the length metric is degenerate — every tied option
    produces the same pill (the bar itself) and its measured length varies
    only with sub-mm float noise along the sweep. Skip the metric and pick
    by gtfs_dist directly.

    Cluster positions are reset to raw before returning so the outer caller
    can apply the chosen option cleanly.
    """
    # Probe leftover count using the first option. All tied options share
    # the same scoring set by construction, and the covered set is stable
    # enough that the leftover bucket is the same across tied options.
    placed_ids = set()
    _apply_option(group, options[0], placed_ids, record_stabbed=False)
    has_leftovers = any(id(p) not in placed_ids for p in platforms)
    for s in cluster:
        s["lon"], s["lat"] = raw[id(s)]

    if not has_leftovers:
        return min(options, key=lambda o: o["gtfs_dist"])

    best = None
    best_key = None
    for option in options:
        for s in cluster:
            s["lon"], s["lat"] = raw[id(s)]
        placed_ids = set()
        _apply_option(group, option, placed_ids, record_stabbed=False)
        leftovers = [p for p in platforms if id(p) not in placed_ids]
        if leftovers:
            _leftover_fill(platforms, leftovers, placed_ids, raw,
                            gtfs_centroid, cos_lat=cos_lat)
        length = _measure_pill_geometry(cluster, cos_lat=cos_lat)
        key = (length, option["gtfs_dist"])
        if best_key is None or key < best_key:
            best_key = key
            best = option
    for s in cluster:
        s["lon"], s["lat"] = raw[id(s)]
    return best


def _should_split_at_gap(path, k, gap_len_km, pos_to_platforms=None,
                          cos_lat=1.0):
    """Decide whether the NN-path segment path[k]→path[k+1] is a split
    (separates two pills + connector) or a regular in-pill segment.

    Two absolute-metre thresholds: PILL_GAP_STRAIGHT_M when the gap is a
    dead-straight in-line continuation of the surrounding pill, and
    PILL_GAP_ANGLED_M for angled / T-junction connectors. The straight
    threshold applies when either of these holds:
      • From each gap-adjacent dot, the NN-path continues dead straight in
        line with the gap direction for at least the gap length (no angle
        tolerance; any bend at all breaks the walk).
      • OR (perpendicular-platforms rule) both gap-adjacent dots have at
        least one platform whose extent tangent is 90° ±PERP_PLATFORM_TOL_DEG
        from the gap direction — i.e. the gap lies along a bar's perpendicular axis,
        so the bar continues through the gap even though the surrounding
        NN-path is too sparse to prove it via the walk. Only one platform
        per stacked dot needs to satisfy the angle test.
    Otherwise the angled threshold applies.

    cos_lat scales lon deltas to metric-equivalent space for the
    perpendicular-platforms check. Perpendicularity (unlike colinearity)
    is not preserved under non-uniform axis scaling, so the angle math
    must be done in metric. Pass cos(mean_lat) when the input is true
    (lon, lat); pass 1.0 when lon has already been pre-scaled. The
    dead-straight walk uses colinearity only and is scale-invariant.
    """
    straight_threshold_km = PILL_GAP_STRAIGHT_M / 1000.0
    angled_threshold_km = PILL_GAP_ANGLED_M / 1000.0
    if gap_len_km <= angled_threshold_km:
        return False
    if gap_len_km > straight_threshold_km:
        return True

    gap_dx = path[k + 1][0] - path[k][0]
    gap_dy = path[k + 1][1] - path[k][1]
    gnorm = sqrt(gap_dx * gap_dx + gap_dy * gap_dy)
    if gnorm <= 0:
        return False
    gx = gap_dx / gnorm
    gy = gap_dy / gnorm

    # Perpendicular-platforms rule: if both gap-adjacent dots have at least
    # one platform whose extent tangent is 90° ±PERP_PLATFORM_TOL_DEG from
    # the gap direction, the gap lies along a bar's perpendicular axis. Treat as in-line.
    # The angle math is done in metric-equivalent space (lon × cos_lat) —
    # perpendicularity is not preserved under raw lon/lat scaling for
    # non-axis-aligned tracks (Zurich/Bern HB, etc.).
    if pos_to_platforms is not None:
        gap_dx_m = gap_dx * cos_lat
        gap_dy_m = gap_dy
        gnorm_m = sqrt(gap_dx_m * gap_dx_m + gap_dy_m * gap_dy_m)
        if gnorm_m <= 0:
            return False
        gx_m = gap_dx_m / gnorm_m
        gy_m = gap_dy_m / gnorm_m
        perp_sin_tol = sin(radians(PERP_PLATFORM_TOL_DEG))

        def _has_perp_platform(pos):
            for p in pos_to_platforms.get(pos, ()):
                ext = p.get("extent")
                if not ext or len(ext) < 2:
                    continue
                dx_m = (ext[-1][0] - ext[0][0]) * cos_lat
                dy_m = ext[-1][1] - ext[0][1]
                snorm_m = sqrt(dx_m * dx_m + dy_m * dy_m)
                if snorm_m <= 0:
                    continue
                # |cos(angle to gap)| ≤ sin(tol)  ⇔  perpendicular ±tol.
                if abs(dx_m * gx_m + dy_m * gy_m) / snorm_m <= perp_sin_tol:
                    return True
            return False

        if (_has_perp_platform(path[k])
                and _has_perp_platform(path[k + 1])):
            return False

    # "Dead straight" walk — only floating-point noise is tolerated. A
    # segment whose cross product with the gap direction (= sin of the
    # angle) is above ~1e-6 is treated as bent and breaks the walk.
    sin_eps = 1e-6

    def _is_aligned(seg_dx, seg_dy, snorm):
        # Must point the same way as the gap (positive dot product) AND
        # be colinear (cross product ≈ 0).
        if seg_dx * gx + seg_dy * gy < 0:
            return False
        return abs(seg_dx * gy - seg_dy * gx) / snorm <= sin_eps

    # Walk away from the gap on the left side: segments path[i-1]→path[i]
    # for i = k, k-1, ..., 1. Direction is path[i] - path[i-1], which
    # should match gap_dir for a straight continuation.
    back_len_km = 0.0
    for i in range(k, 0, -1):
        ax, ay = path[i - 1]
        bx, by = path[i]
        seg_dx, seg_dy = bx - ax, by - ay
        snorm = sqrt(seg_dx * seg_dx + seg_dy * seg_dy)
        if snorm <= 0:
            continue
        if not _is_aligned(seg_dx, seg_dy, snorm):
            break
        back_len_km += haversine_km(ax, ay, bx, by)
        if back_len_km >= gap_len_km:
            return False

    # Walk away from the gap on the right side: segments path[i]→path[i+1]
    # for i = k+1, k+2, ..., len(path)-2. Direction is path[i+1] - path[i].
    forward_len_km = 0.0
    for i in range(k + 1, len(path) - 1):
        ax, ay = path[i]
        bx, by = path[i + 1]
        seg_dx, seg_dy = bx - ax, by - ay
        snorm = sqrt(seg_dx * seg_dx + seg_dy * seg_dy)
        if snorm <= 0:
            continue
        if not _is_aligned(seg_dx, seg_dy, snorm):
            break
        forward_len_km += haversine_km(ax, ay, bx, by)
        if forward_len_km >= gap_len_km:
            return False

    return True


# Platform-overlap penalty: a pill or connector segment with both endpoints
# within ON_PLATFORM_TOL_M of the SAME platform extent has its base factor
# (1.0 for pills, 0.5 for connectors) scaled by ON_PLATFORM_PENALTY. The
# penalty discourages routing a pill or connector along a platform extent
# when an alternative configuration reaches the same dots without overlap.
ON_PLATFORM_TOL_M = 0.5
ON_PLATFORM_PENALTY = 2.0


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

    pos_to_platforms = {}
    for s in cluster_stops:
        pos_to_platforms.setdefault((s["lon"], s["lat"]), []).append(s)

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
    # so make_pill_features's connector geometry is mirrored). Retain the
    # actual (p1, p2) chosen per edge so the on-platform check sees the same
    # segment that would be drawn.
    n_g = len(groups)
    mst_edges = []
    for i in range(n_g):
        for j in range(i + 1, n_g):
            best_d = float("inf")
            best_pair = None
            for p1 in groups[i]:
                for p2 in groups[j]:
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


def _find_parallel_stub_drop(cluster: list, placed_leftovers: list):
    """Scan just-placed leftovers in a rail (train) cluster for one whose
    nearest non-coincident placed dot is within PARALLEL_STUB_GAP_M AND
    whose gap direction is parallel (within PARALLEL_STUB_TOL_DEG) to
    either the leftover's own extent tangent OR the neighbor's. Returns
    (stop_to_drop, absorbing_position) for the first match, or None.

    Coincident neighbors (within DEDUP_TOL_M) are skipped because they
    represent the same physical dot — a duplicate record collapsed by
    _dedup_stop_positions later — and offer no useful gap. The neighbor-
    tangent fallback handles degenerate-extent leftovers: at Bern, the
    sub-platform stop_ids land with no extent at all (pfaedle didn't shape
    them or the extent collapsed), so their own tangent is None but the
    main pill bar dot they sit next to has a usable extent along the
    line direction. Coordinates are in the scaled (cos_lat) cluster space;
    distances convert via _M_PER_DEG.
    """
    tol_cos = cos(radians(PARALLEL_STUB_TOL_DEG))
    gap_threshold_units = PARALLEL_STUB_GAP_M / _M_PER_DEG
    gap_threshold_sq = gap_threshold_units * gap_threshold_units
    coincident_units = DEDUP_TOL_M / _M_PER_DEG
    coincident_sq = coincident_units * coincident_units

    for p in placed_leftovers:
        nearest = None  # (d_sq, lon, lat, stop_dict)
        for q in cluster:
            if q is p:
                continue
            dx = q["lon"] - p["lon"]
            dy = q["lat"] - p["lat"]
            d_sq = dx * dx + dy * dy
            if d_sq <= coincident_sq:
                continue
            if nearest is None or d_sq < nearest[0]:
                nearest = (d_sq, q["lon"], q["lat"], q)
        if nearest is None:
            continue
        d_sq, q_lon, q_lat, q = nearest
        if d_sq >= gap_threshold_sq:
            continue

        # Prefer p's own tangent; fall back to the neighbor's so degenerate-
        # extent leftovers (no tangent of their own) can still be checked.
        tan = _stop_tangent(p) or _stop_tangent(q)
        if tan is None:
            continue

        gx = q_lon - p["lon"]
        gy = q_lat - p["lat"]
        gmag = sqrt(gx * gx + gy * gy)
        cos_a = (gx * tan[0] + gy * tan[1]) / gmag
        if abs(cos_a) < tol_cos:
            continue
        return p, (q_lon, q_lat)
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

        # Pick one option per group — see pill-rendering.md "Tie-breaking
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


def write_debug_platforms(line_stops: dict, line_lookup: dict,
                           stop_attrs: dict, skip_first_oids: set,
                           skip_last_oids: set,
                           sibling_groups: dict, oid_sibling_key: dict) -> None:
    """Emit transit_debug_platforms.geojson — one LineString per stop tracing
    the platform's full allowed range along the line's polyline. Debug-only
    overlay; replaces the previous black-dot debug feature.
    """
    cfg = PILL_CFG
    if not cfg.get("default_length_m"):
        print("  No pill_rendering config — debug platforms skipped.")
        return
    feats = []
    for osm_id, ls_entry in line_stops.items():
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        line = line_lookup.get(osm_id)
        if not line:
            continue
        mode = line["mode"]
        if mode not in cfg["default_length_m"]:
            continue
        polyline = flatten_coords(line["coords"])
        if len(polyline) < 2:
            continue
        skip_first_here = str(osm_id) in skip_first_oids
        skip_last_here = str(osm_id) in skip_last_oids
        last_idx = len(triplets) - 1
        sib_key = oid_sibling_key.get(str(osm_id))
        siblings = sibling_groups.get(sib_key, []) if sib_key else []
        for idx, trip in enumerate(triplets):
            if idx == 0 and skip_first_here:
                continue
            if idx == last_idx and skip_last_here:
                continue
            if len(trip) < 3:
                continue
            stop_lon, stop_lat, stop_id = trip[0], trip[1], trip[2]
            atlas_length = (stop_attrs.get(stop_id, {}) or {}).get("length")
            extent = _platform_extent(stop_lon, stop_lat, polyline,
                                       mode, atlas_length, cfg,
                                       osm_id=str(osm_id), siblings=siblings)
            if extent is None or len(extent) < 2:
                continue
            feats.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": MODE_MINZOOM.get(mode, 11)},
                "geometry": {"type": "LineString",
                             "coordinates": [list(p) for p in extent]},
                "properties": {"mode": mode, "stop_id": stop_id},
            })
    OUT_DEBUG_PLATFORMS.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": feats,
    }, ensure_ascii=False))
    print(f"  Debug platforms: {len(feats):,} features → {OUT_DEBUG_PLATFORMS}")


def write_debug_stops(line_stops: dict, line_lookup: dict,
                       stop_attrs: dict, stop_meta: dict,
                       skip_first_oids: set, skip_last_oids: set) -> None:
    """Emit transit_debug_stops.geojson — one Point per (line, stop) pair,
    1:1 with the debug platform lines. The point sits at the GTFS coord
    snapped onto that line's polyline (the same snap-to-line used by the
    pipeline's dot placement), so every debug line has a matching dot and
    every dot has a matching line.

    The popup data is keyed on stop_id and lists every line visiting that
    stop (with origin / destination), regardless of which line's snap this
    particular dot was rendered from.
    """
    cfg = PILL_CFG

    # First pass: per stop_id, build the (deduped) list of lines visiting it
    # plus the stop name. This populates the popup for every dot rendered
    # at this stop, regardless of which line's snap produced the dot.
    by_stop: dict = {}
    for osm_id, ls_entry in line_stops.items():
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        line = line_lookup.get(osm_id)
        if not line or not triplets:
            continue
        mode = line["mode"]
        if mode not in cfg.get("default_length_m", {}):
            continue
        first_trip = triplets[0]
        last_trip = triplets[-1]
        origin_sid = first_trip[2] if len(first_trip) >= 3 else ""
        dest_sid = last_trip[2] if len(last_trip) >= 3 else ""
        origin_name = (stop_meta.get(origin_sid, {}).get("name") or "?")
        dest_name = (stop_meta.get(dest_sid, {}).get("name") or "?")
        line_info = {
            "ref":         line.get("ref", ""),
            "mode":        mode,
            "color":       line.get("color", "#888888"),
            "origin":      origin_name,
            "destination": dest_name,
            "osm_id":      str(osm_id),
        }
        for trip in triplets:
            if len(trip) < 3:
                continue
            sid = trip[2]
            if not sid:
                continue
            entry = by_stop.get(sid)
            if entry is None:
                entry = {
                    "name": stop_meta.get(sid, {}).get("name", ""),
                    "visits": [],
                }
                by_stop[sid] = entry
            entry["visits"].append(line_info)

    per_stop_lines_json: dict = {}
    per_stop_name: dict = {}
    for sid, data in by_stop.items():
        by_key: dict = {}
        order = []
        for v in data["visits"]:
            key = (v["ref"], v["origin"], v["destination"])
            if key not in by_key:
                entry = {
                    "ref":         v["ref"],
                    "mode":        v["mode"],
                    "color":       v["color"],
                    "origin":      v["origin"],
                    "destination": v["destination"],
                    "osm_ids":     [v["osm_id"]],
                }
                by_key[key] = entry
                order.append(key)
            else:
                osm_ids = by_key[key]["osm_ids"]
                if v["osm_id"] not in osm_ids:
                    osm_ids.append(v["osm_id"])
        unique = [by_key[k] for k in order]
        per_stop_lines_json[sid] = json.dumps(unique, ensure_ascii=False)
        per_stop_name[sid] = data["name"]

    # Second pass: one dot per (line, stop) at the snapped position on that
    # line's polyline. 1:1 with debug platform lines (same filtering).
    feats = []
    for osm_id, ls_entry in line_stops.items():
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        line = line_lookup.get(osm_id)
        if not line or not triplets:
            continue
        mode = line["mode"]
        if mode not in cfg.get("default_length_m", {}):
            continue
        polyline = flatten_coords(line["coords"])
        if len(polyline) < 2:
            continue
        skip_first_here = str(osm_id) in skip_first_oids
        skip_last_here = str(osm_id) in skip_last_oids
        last_idx = len(triplets) - 1
        for idx, trip in enumerate(triplets):
            if idx == 0 and skip_first_here:
                continue
            if idx == last_idx and skip_last_here:
                continue
            if len(trip) < 3:
                continue
            lon, lat, sid = trip[0], trip[1], trip[2]
            if not sid:
                continue
            dot_lon, dot_lat = snap_to_line(lon, lat, polyline)
            attrs = stop_attrs.get(sid) or {}
            atlas_len = attrs.get("length") if isinstance(attrs, dict) else None
            stabbed = (str(osm_id), str(sid)) in _STABBED_PAIRS
            feats.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": MODE_MINZOOM.get(mode, 11)},
                "geometry": {"type": "Point", "coordinates": [dot_lon, dot_lat]},
                "properties": {
                    "stop_id":          sid,
                    "stop_name":        per_stop_name.get(sid, ""),
                    "mode":             mode,
                    "platform_length":  atlas_len,
                    "lines_json":       per_stop_lines_json.get(sid, "[]"),
                    "stabbed":          stabbed,
                    "current_osm_id":   str(osm_id),
                },
            })
    OUT_DEBUG_STOPS.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": feats,
    }, ensure_ascii=False))
    stabbed_count = sum(1 for f in feats if f["properties"]["stabbed"])
    print(f"  Debug stops: {len(feats):,} features ({stabbed_count:,} stabbed) "
          f"→ {OUT_DEBUG_STOPS}")


def write_debug_bars() -> None:
    """Emit transit_debug_bars.geojson — one LineString per max-stab bar
    found during cluster processing. Each line spans the perpendicular
    extent of its stabbed dots (plus a small visual margin), so on the map
    the line draws exactly where the bar "is" in 2D.
    """
    feats = []
    for ep1, ep2 in _DIAG_BARS:
        feats.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": 5},
            "geometry": {"type": "LineString",
                         "coordinates": [list(ep1), list(ep2)]},
            "properties": {},
        })
    OUT_DEBUG_BARS.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": feats,
    }, ensure_ascii=False))
    print(f"  Debug bars: {len(feats):,} features → {OUT_DEBUG_BARS}")


# =============================================================================
# Geometry helpers
# =============================================================================

def haversine_km(lon1, lat1, lon2, lat2) -> float:
    R = 6371.0
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlam = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlam / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def snap_to_line(px, py, coords):
    """Return the closest point on a polyline to (px, py)."""
    best_dist_sq = float("inf")
    best = (px, py)
    for i in range(len(coords) - 1):
        ax, ay = coords[i]
        bx, by = coords[i + 1]
        dx, dy = bx - ax, by - ay
        len_sq = dx * dx + dy * dy
        if len_sq == 0:
            cx, cy = ax, ay
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
            cx, cy = ax + t * dx, ay + t * dy
        d = (px - cx) ** 2 + (py - cy) ** 2
        if d < best_dist_sq:
            best_dist_sq = d
            best = (cx, cy)
    return best


def flatten_coords(coords):
    """Flatten MultiLineString [[...], [...]] or return LineString coords as-is."""
    if coords and isinstance(coords[0][0], list):
        return [pt for seg in coords for pt in seg]
    return coords


# =============================================================================
# Pill geometry — nearest-neighbor path through dot positions
# =============================================================================

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

def count_unique_lines(cluster_stops):
    """
    Count distinct OSM line IDs in a cluster.
    Each direction of a tram/bus line has its own osm_id, so both directions
    of a bidirectional line count as 2 — correctly triggering a pill.
    """
    return len(set(s.get("osm_id", str(id(s))) for s in cluster_stops))


def pill_minzoom(mode, stop_count):
    """
    Return the zoom level at which pills appear for a stop cluster,
    or None if the cluster should not get a pill (single line).
    """
    if mode == "train":
        if stop_count >= 5:
            return 11
        if stop_count >= 2:
            return 13
        return None
    else:
        if stop_count >= 10:
            return 12
        if stop_count >= 5:
            return 13
        if stop_count >= 2:
            return 14
        return None


def color_luminance(hex_color: str) -> float:
    """Perceived luminance of a hex color (lower = darker)."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return 1.0
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def dominant_line(stops_in_cluster):
    """
    Return (color, mode, max_width_base, dominant_stop).
    - Mode: highest-priority type present (MODE_RANK; lower = higher priority; strict).
    - Color: darkest (lowest luminance) among stops of that type.
    - width_base: max across ALL stops, regardless of type.
    """
    best_rank = min(MODE_RANK.get(s["mode"], 99) for s in stops_in_cluster)
    dom_stops = [s for s in stops_in_cluster if MODE_RANK.get(s["mode"], 99) == best_rank]

    best_lum   = 2.0
    best_color = "#888888"
    best_stop  = dom_stops[0]
    for s in dom_stops:
        lum = color_luminance(s["color"])
        if lum < best_lum:
            best_lum   = lum
            best_color = s["color"]
            best_stop  = s

    max_wb = max(s["width_base"] for s in stops_in_cluster)
    return best_color, best_stop["mode"], max_wb, best_stop


def cluster_lines(cluster_stops, line_lookup):
    """
    Return a sorted list of {ref, color, mode} dicts for all distinct lines
    serving any stop in the cluster.  Sorted by mode rank then ref.
    """
    seen = {}
    for s in cluster_stops:
        oid = s.get("osm_id", "")
        if oid and oid not in seen:
            info = line_lookup.get(oid, {})
            if info:
                seen[oid] = {
                    "ref":      info.get("gtfs_ref") or info.get("ref", ""),
                    "color":    info.get("color", "#888888"),
                    "mode":     info.get("mode", ""),
                    "name":     info.get("name", ""),
                }
    return sorted(seen.values(), key=lambda x: (MODE_RANK.get(x["mode"], 99), x.get("gtfs_ref") or x["ref"]))


# =============================================================================
# Connector curving — symmetric-arc geometry applied to MST connectors after
# pill placement. See `.claude/concepts/pill-rendering.md` § Connector curving.
# =============================================================================

def _lonlat_to_xy(lon, lat, lon0, lat0, cos_lat):
    """Equal-distance metric frame anchored at (lon0, lat0)."""
    return ((lon - lon0) * cos_lat * _M_PER_DEG,
            (lat - lat0) * _M_PER_DEG)


def _xy_to_lonlat(x, y, lon0, lat0, cos_lat):
    return (lon0 + x / (cos_lat * _M_PER_DEG),
            lat0 + y / _M_PER_DEG)


def _rotate2(v, ang):
    c, s = cos(ang), sin(ang)
    return (v[0] * c - v[1] * s, v[0] * s + v[1] * c)


def _norm2(v):
    m = sqrt(v[0] * v[0] + v[1] * v[1])
    if m < 1e-12:
        return None
    return (v[0] / m, v[1] / m)


def _polyline_length_xy(poly):
    total = 0.0
    for i in range(len(poly) - 1):
        dx = poly[i + 1][0] - poly[i][0]
        dy = poly[i + 1][1] - poly[i][1]
        total += sqrt(dx * dx + dy * dy)
    return total


def _simplify_pill_lonlat(coords, cos_lat, tol_m=PILL_SIMPLIFY_TOL_M):
    """Iterative Douglas-Peucker simplification on a pill polyline. An
    interior vertex is kept only if its perpendicular deviation from the
    chord through the nearest retained neighbours exceeds `tol_m`. Works in
    metric (x, y) space anchored at the first vertex so the tolerance is in
    true metres. Bar-placed dots fall onto a single line in design, so
    sub-line-width deviations are float / leftover-dot noise; genuine
    bent pills deviate well above `tol_m` and survive.
    """
    if len(coords) <= 2:
        return list(coords)
    lon0 = coords[0][0]
    lat0 = coords[0][1]
    xy = [_lonlat_to_xy(p[0], p[1], lon0, lat0, cos_lat) for p in coords]
    n = len(xy)
    keep = [False] * n
    keep[0] = True
    keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        if e - s < 2:
            continue
        A = xy[s]
        B = xy[e]
        dx = B[0] - A[0]
        dy = B[1] - A[1]
        L2 = dx * dx + dy * dy
        max_d = 0.0
        max_i = -1
        if L2 < 1e-12:
            for i in range(s + 1, e):
                px = xy[i][0] - A[0]
                py = xy[i][1] - A[1]
                d = sqrt(px * px + py * py)
                if d > max_d:
                    max_d = d
                    max_i = i
        else:
            inv_L = 1.0 / sqrt(L2)
            for i in range(s + 1, e):
                cross = (xy[i][0] - A[0]) * dy - (xy[i][1] - A[1]) * dx
                d = abs(cross) * inv_L
                if d > max_d:
                    max_d = d
                    max_i = i
        if max_d > tol_m and max_i >= 0:
            keep[max_i] = True
            stack.append((s, max_i))
            stack.append((max_i, e))
    return [coords[i] for i in range(n) if keep[i]]


def _dedup_polyline_xy(poly, tol_m=DEDUP_TOL_M):
    """Collapse adjacent metric-space vertices closer than `tol_m` to a
    single vertex (first-seen wins). MapLibre's line tessellation produces
    visible wobble artifacts at z18+ when adjacent polyline vertices sit
    within line-width of each other — any curve that ends up with
    micrometre-spaced or exact-duplicate samples (recovery-shrunk arc with
    tiny chosen_L, a sub-half-metre `sA` stub that straddles the include
    threshold, etc.) is cleaned up before reaching tippecanoe.
    """
    if len(poly) < 2:
        return list(poly)
    out = [poly[0]]
    tol_sq = tol_m * tol_m
    for p in poly[1:]:
        dx = p[0] - out[-1][0]
        dy = p[1] - out[-1][1]
        if dx * dx + dy * dy > tol_sq:
            out.append(p)
    return out


def _tangent_candidates(group, endpoint, other_endpoint, lon0, lat0, cos_lat):
    """Candidate outward-pointing unit tangents at `endpoint` within `group`.

    Returns a list of (tangent, is_default) tuples in metric (x, y) space:
    - Singleton group (disc): [] — tangent is unconstrained, derived from
      symmetry by the caller.
    - Pill tip: [(axial, True), (perp_left, False), (perp_right, False)].
    - Pill interior: [(perp_toward_other, True)] — the only sensible choice.
    """
    if len(group) <= 1:
        return []

    # Locate endpoint within group. Positions flow through unchanged from
    # nearest_neighbor_path / split, so float-equality matches.
    idx = None
    for k, p in enumerate(group):
        if p[0] == endpoint[0] and p[1] == endpoint[1]:
            idx = k
            break
    if idx is None:
        return []

    xy = [_lonlat_to_xy(p[0], p[1], lon0, lat0, cos_lat) for p in group]

    if idx == 0 or idx == len(group) - 1:
        neighbor = 1 if idx == 0 else len(group) - 2
        axial_raw = (xy[idx][0] - xy[neighbor][0], xy[idx][1] - xy[neighbor][1])
        axial = _norm2(axial_raw)
        if axial is None:
            return []
        return [
            (axial, True),
            (_rotate2(axial, pi / 2), False),
            (_rotate2(axial, -pi / 2), False),
        ]

    # Interior: average of incoming/outgoing segment directions, then
    # perpendicular on the side facing the other connector endpoint.
    prev_dir = _norm2((xy[idx][0] - xy[idx - 1][0], xy[idx][1] - xy[idx - 1][1]))
    next_dir = _norm2((xy[idx + 1][0] - xy[idx][0], xy[idx + 1][1] - xy[idx][1]))
    if prev_dir is None and next_dir is None:
        return []
    if prev_dir is None:
        avg = next_dir
    elif next_dir is None:
        avg = prev_dir
    else:
        avg = _norm2(((prev_dir[0] + next_dir[0]) / 2, (prev_dir[1] + next_dir[1]) / 2))
        if avg is None:
            avg = next_dir
    perp_a = _rotate2(avg, pi / 2)
    perp_b = _rotate2(avg, -pi / 2)
    other_xy = _lonlat_to_xy(other_endpoint[0], other_endpoint[1], lon0, lat0, cos_lat)
    towards = (other_xy[0] - xy[idx][0], other_xy[1] - xy[idx][1])
    dot_a = perp_a[0] * towards[0] + perp_a[1] * towards[1]
    dot_b = perp_b[0] * towards[0] + perp_b[1] * towards[1]
    return [((perp_a if dot_a >= dot_b else perp_b), True)]


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
        # connector candidate and must surface to the picker so a much shorter
        # straight line can win against a wildly wider axial-axial arc on the
        # 0.7-ratio rule. Only emit when the chord actually aligns with tA —
        # otherwise a "straight line" between A and B has hard kinks at both
        # ends and the combo is geometrically inconsistent.
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
        # Disc on the line of tA — bending does not help; caller falls
        # back to a 2-point straight connector.
        return None

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

    if best is None or best[0] < 1e-6:
        return None
    _, delta = best

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


def _curve_connector(ca, cb, group_a, group_b, cluster_cos_lat, mode,
                     anchor_a=None, anchor_b=None):
    """Post-process an MST connector from `ca` (in group_a) to `cb` (in group_b)
    into a curved (lon, lat) polyline.

    `anchor_a` / `anchor_b`: optional OUT tangent unit vectors (in cluster-xy
    space) for an anchored disc — only meaningful when the corresponding side
    is a singleton group. A None anchor on a singleton means the disc is
    unanchored and the connector is unconstrained at that end. Pills always
    derive their tangents from their own geometry (anchors on the pill side
    are ignored).

    Returns `(coords, anchor_out_a, anchor_out_b)`. Each `anchor_out_*` is
    the OUT tangent at that end of the final polyline in cluster-xy space, or
    None if the polyline is too short to derive one. The caller decides
    whether to use it as a new anchor.
    """
    r_max = _curve_max_radius(mode)

    lon0 = (ca[0] + cb[0]) / 2.0
    lat0 = (ca[1] + cb[1]) / 2.0
    A_xy = _lonlat_to_xy(ca[0], ca[1], lon0, lat0, cluster_cos_lat)
    B_xy = _lonlat_to_xy(cb[0], cb[1], lon0, lat0, cluster_cos_lat)

    if len(group_a) > 1:
        cands_a = _tangent_candidates(group_a, ca, cb, lon0, lat0, cluster_cos_lat)
    elif anchor_a is not None:
        cands_a = _cardinal_tangents(anchor_a)
    else:
        cands_a = []
    if len(group_b) > 1:
        cands_b = _tangent_candidates(group_b, cb, ca, lon0, lat0, cluster_cos_lat)
    elif anchor_b is not None:
        cands_b = _cardinal_tangents(anchor_b)
    else:
        cands_b = []

    def finalize(coords):
        anchor_out_a = _arrival_tangent_lonlat(coords, True, cluster_cos_lat)
        anchor_out_b = _arrival_tangent_lonlat(coords, False, cluster_cos_lat)
        return coords, anchor_out_a, anchor_out_b

    # Both ends unconstrained (e.g. unanchored disc ↔ unanchored disc): straight.
    if not cands_a and not cands_b:
        return finalize([ca, cb])

    # Constrained one side only: asymmetric arc-then-straight with the
    # constrained side playing the pill role. Same construction whether the
    # constrained side is a real pill or an anchored disc.
    if cands_a and not cands_b:
        poly_xy = _pill_disc_picker(A_xy, cands_a, B_xy, r_max)
        if poly_xy is None:
            return finalize([ca, cb])
        coords = [_xy_to_lonlat(p[0], p[1], lon0, lat0, cluster_cos_lat) for p in poly_xy]
        return finalize(coords)
    if cands_b and not cands_a:
        poly_xy = _pill_disc_picker(B_xy, cands_b, A_xy, r_max)
        if poly_xy is None:
            return finalize([ca, cb])
        coords = [_xy_to_lonlat(p[0], p[1], lon0, lat0, cluster_cos_lat) for p in poly_xy]
        coords.reverse()
        return finalize(coords)

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
        # No valid (cardinal × cardinal) combo: fall back to the unconstrained
        # logic so an anchored-disc end with no working cardinals doesn't lose
        # its connector entirely. The disc's anchor stays as it was.
        if anchor_a is not None or anchor_b is not None:
            return _curve_connector(ca, cb, group_a, group_b,
                                    cluster_cos_lat, mode,
                                    anchor_a=None, anchor_b=None)
        return finalize([ca, cb])

    # Axial-preferred rule: the default tangent combination is the baseline
    # (axial at every pill tip; any cardinal at an anchored disc, all of which
    # are tagged default). A perpendicular at a pill tip replaces the baseline
    # only when its combination length is ≤ CURVE_PERP_PREF_RATIO × the
    # baseline length. Multiple combos may share the default tag (4 cardinals
    # × 1 axial-pill = 4 default combos for pill ↔ anchored-disc; 16 for
    # anchored ↔ anchored) — the shortest among them is the baseline.
    defaults = [r for r in results if r[2] and r[3]]
    if not defaults:
        if anchor_a is not None or anchor_b is not None:
            return _curve_connector(ca, cb, group_a, group_b,
                                    cluster_cos_lat, mode,
                                    anchor_a=None, anchor_b=None)
        return finalize([ca, cb])
    default_combo = min(defaults, key=lambda r: r[1])
    threshold = default_combo[1] * CURVE_PERP_PREF_RATIO
    qualifying = [r for r in results if r[1] <= threshold]
    chosen = min(qualifying, key=lambda r: r[1]) if qualifying else default_combo

    coords = [_xy_to_lonlat(p[0], p[1], lon0, lat0, cluster_cos_lat) for p in chosen[0]]
    return finalize(coords)


def make_pill_features(cluster_stops, minzoom, lines_json=""):
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
    4. MST connectors join the resulting groups at their nearest dot pair.
    """
    color, mode, max_wb, dom_stop = dominant_line(cluster_stops)
    positions = _dedup_stop_positions(cluster_stops)
    n = len(positions)

    if n < 2:
        return []

    path = nearest_neighbor_path(positions)

    stop_props = {
        "color":          color,
        "mode":           mode,
        "width_base":     max_wb,
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

    # Find every gap that splits the NN-path into separate pills.
    # _should_split_at_gap applies the per-shape threshold (PILL_GAP_STRAIGHT_M
    # for dead-straight in-line continuations or gaps along a bar's
    # perpendicular axis; PILL_GAP_ANGLED_M for angled / T-junction
    # connectors). Absolute metres — no width_base scaling.
    pos_to_platforms = {}
    for s in cluster_stops:
        pos_to_platforms.setdefault((s["lon"], s["lat"]), []).append(s)
    mean_lat = sum(p[1] for p in positions) / len(positions)
    cluster_cos_lat = cos(radians(mean_lat))
    split_indices = [
        k for k in range(len(path) - 1)
        if _should_split_at_gap(
            path, k,
            haversine_km(path[k][0], path[k][1],
                         path[k + 1][0], path[k + 1][1]),
            pos_to_platforms,
            cos_lat=cluster_cos_lat)
    ]

    if not split_indices:
        return [make_feat(_simplify_pill_lonlat(path, cluster_cos_lat), "pill")]

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
    for grp in groups:
        if len(grp) >= 2:
            feats.append(make_feat(_simplify_pill_lonlat(grp, cluster_cos_lat), "pill"))
        else:
            feats.append(make_endpoint(grp[0]))

    # MST connectors (Kruskal's) — produces tree topology so branches are shorter than
    # a forced chain when groups fan out from a hub rather than lying in a sequence.
    n_g = len(groups)
    mst_edges = []   # (dist, ca, cb) for all candidate edges, sorted
    for i in range(n_g):
        for j in range(i + 1, n_g):
            best_d = float("inf")
            ca, cb = groups[i][0], groups[j][0]
            for p1 in groups[i]:
                for p2 in groups[j]:
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

    disc_anchors = {}  # (lon, lat) → cluster-xy OUT tangent unit vector

    for ca, cb, i, j in chosen_edges:
        grp_a, grp_b = groups[i], groups[j]
        pos_a = (ca[0], ca[1])
        pos_b = (cb[0], cb[1])
        anchor_a = disc_anchors.get(pos_a) if len(grp_a) == 1 else None
        anchor_b = disc_anchors.get(pos_b) if len(grp_b) == 1 else None
        curve_coords, arrival_a, arrival_b = _curve_connector(
            ca, cb, grp_a, grp_b, cluster_cos_lat, mode,
            anchor_a=anchor_a, anchor_b=anchor_b)
        feats.append(make_feat(curve_coords, "connector"))
        if len(grp_a) == 1 and pos_a not in disc_anchors and arrival_a is not None:
            disc_anchors[pos_a] = _snap_to_cardinal(arrival_a)
        if len(grp_b) == 1 and pos_b not in disc_anchors and arrival_b is not None:
            disc_anchors[pos_b] = _snap_to_cardinal(arrival_b)

    return feats


# =============================================================================
# Clustering
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


def cluster_stops_for_pills(raw_stops, radius_km):
    """
    Spatially cluster raw stop dicts by their lon/lat within radius_km.
    Returns list of clusters; each cluster is a list of stop dicts.
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
            kx, ky = key
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for ns in grid.get((kx + dx, ky + dy), []):
                        if id(ns) in visited:
                            continue
                        if haversine_km(cx0, cy0, ns["lon"], ns["lat"]) < radius_km:
                            group.append(ns)
                            visited.add(id(ns))

            if not group:
                group = [stop]
                visited.add(sid)

            clusters.append(group)

    return clusters


def merge_clusters_by_parent_station(clusters):
    """
    Merge spatially separate clusters that share the same parent_station into
    one super-cluster so make_pill_features can connect them with pills and connectors.
    Clusters with no parent_station are left as-is.
    """
    by_parent = defaultdict(list)
    no_parent = []
    for cluster in clusters:
        parents = [s.get("parent_station", "") for s in cluster if s.get("parent_station", "")]
        if parents:
            dominant = max(set(parents), key=parents.count)
            by_parent[dominant].extend(cluster)
        else:
            no_parent.append(cluster)
    return list(by_parent.values()) + no_parent


# =============================================================================
# Main
# =============================================================================

def main():
    print("Loading lines...")
    lines_data = json.loads(LINES.read_text())
    line_lookup = {}
    gtfs_stop_features = []
    for feat in lines_data["features"]:
        p   = feat["properties"]
        oid = str(p.get("osm_id", ""))
        if oid:
            line_lookup[oid] = {
                "color":      p["color"],
                "mode":       p["mode"],
                "width_base": p.get("width_base", 3.0),
                "coords":     feat["geometry"]["coordinates"],
                "ref":        p.get("ref", ""),
                "name":       p.get("name", ""),
                "agency_id":  p.get("agency_id", ""),
            }
        if p.get("gtfs_stops"):
            gtfs_stop_features.append(feat)
    print(f"  {len(line_lookup):,} lines, {len(gtfs_stop_features):,} with embedded gtfs_stops")

    # Sibling index for the missing-range fill rule (tram/bus/regional_bus):
    # {(ref, agency_id, mode) → [(osm_id, flat_polyline)]}. The two-metre
    # proximity gate inside _borrow_backward_segment does the real filtering;
    # this index just bounds the search to same-line variants.
    sibling_groups: dict = defaultdict(list)
    oid_sibling_key: dict = {}
    for oid_s, info in line_lookup.items():
        key = (info.get("ref", ""), info.get("agency_id", ""), info.get("mode", ""))
        flat_poly = flatten_coords(info["coords"])
        if len(flat_poly) >= 2:
            sibling_groups[key].append((oid_s, flat_poly))
            oid_sibling_key[oid_s] = key

    print("Loading stop coordinates and metadata...")
    line_stops = json.loads(LINE_STOPS.read_text())
    stop_meta  = load_stop_meta()
    print(f"  {len(line_stops):,} lines with stops, {len(stop_meta):,} GTFS stop entries")

    print("Loading atlas platform attributes...")
    stop_attrs = write_stop_attributes_diag(line_stops)

    skip_first_oids, skip_last_oids = compute_terminus_skip_oids(
        line_stops, line_lookup, stop_meta)
    print(f"  Terminus dedup: {len(skip_first_oids):,} departure-side entries "
          f"will be omitted from rendering (popup retains both directions)")
    print(f"  Arrival drop (tram/bus/regional_bus): {len(skip_last_oids):,} "
          f"unpaired or layover-shadowed arrival entries omitted from pill construction")

    print("Emitting debug platform extents...")
    write_debug_platforms(line_stops, line_lookup, stop_attrs,
                          skip_first_oids, skip_last_oids,
                          sibling_groups, oid_sibling_key)

    print("Building stop dots and pill candidates...")

    rail_pill_raw     = []   # dicts for rail pill clustering (also used for dots)
    all_nonrail_pills = []   # ALL non-rail pill modes combined (tram+bus+metro+regional_bus)
    other_features    = []   # dot features for non-rail, ferry, mountain

    # --- Mountain / straight-line features with embedded gtfs_stops ---
    for feat in gtfs_stop_features:
        p       = feat["properties"]
        color   = p["color"]
        mode    = p["mode"]
        wb      = p.get("width_base", 3.0)
        coords  = feat["geometry"]["coordinates"]
        minzoom = MODE_MINZOOM.get(mode, 11)
        for lon, lat in p["gtfs_stops"]:
            slon, slat = snap_to_line(lon, lat, coords)
            other_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": minzoom},
                "geometry": {"type": "Point", "coordinates": [slon, slat]},
                "properties": {"color": color, "mode": mode, "width_base": wb},
            })
        # Mountain/ferry via gtfs_stops: no pills

    # --- Per-line stops ---
    for osm_id, ls_entry in line_stops.items():
        if isinstance(ls_entry, dict):
            stop_coords = ls_entry.get("stops", [])
            if ls_entry.get("gtfs_ref"):
                line_lookup.setdefault(osm_id, {})["gtfs_ref"] = ls_entry["gtfs_ref"]
        else:
            stop_coords = ls_entry
        line = line_lookup.get(osm_id)
        if not line:
            continue

        color      = line["color"]
        mode       = line["mode"]
        width_base = line["width_base"]
        coords     = line["coords"]
        minzoom    = MODE_MINZOOM.get(mode, 11)
        flat       = flatten_coords(coords)

        skip_first_here = str(osm_id) in skip_first_oids
        skip_last_here = str(osm_id) in skip_last_oids
        last_idx = len(stop_coords) - 1
        sib_key = oid_sibling_key.get(str(osm_id))
        siblings = sibling_groups.get(sib_key, []) if sib_key else []

        if mode in RAIL_MODES:
            for idx, entry in enumerate(stop_coords):
                if idx == 0 and skip_first_here:
                    continue
                if idx == last_idx and skip_last_here:
                    continue
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
                stop_name  = meta.get("name", "")
                parent_sta = meta.get("parent", "")
                slon, slat = snap_to_line(lon, lat, flat)
                atlas_len = (stop_attrs.get(sid, {}) or {}).get("length")
                extent = _platform_extent(lon, lat, flat, mode, atlas_len, PILL_CFG,
                                          osm_id=str(osm_id), siblings=siblings)
                rail_pill_raw.append({
                    "lon":            slon,
                    "lat":            slat,
                    "osm_id":         osm_id,
                    "mode":           mode,
                    "color":          color,
                    "width_base":     width_base,
                    "stop_id":        sid,
                    "stop_name":      stop_name,
                    "parent_station": parent_sta,
                    "extent":         extent,
                })

        elif mode == "ferry":
            line_lines_json = json.dumps([{"ref": line.get("gtfs_ref") or line.get("ref", ""), "color": color, "mode": mode, "name": line.get("name", "")}])
            for idx, entry in enumerate(stop_coords):
                if idx == 0 and skip_first_here:
                    continue
                if idx == last_idx and skip_last_here:
                    continue
                lon, lat = entry[0], entry[1]
                sid      = entry[2] if len(entry) > 2 else ""
                meta     = stop_meta.get(sid, {})
                other_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": minzoom},
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {
                        "color":          color,
                        "mode":           mode,
                        "width_base":     width_base,
                        "stop_id":        sid,
                        "stop_name":      meta.get("name", ""),
                        "parent_station": meta.get("parent", ""),
                        "lines_json":     line_lines_json,
                    },
                })

        elif mode in PILL_MODES:
            for idx, entry in enumerate(stop_coords):
                if idx == 0 and skip_first_here:
                    continue
                if idx == last_idx and skip_last_here:
                    continue
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
                stop_name  = meta.get("name", "")
                parent_sta = meta.get("parent", "")
                cx, cy = snap_to_line(lon, lat, flat)
                atlas_len = (stop_attrs.get(sid, {}) or {}).get("length")
                extent = _platform_extent(lon, lat, flat, mode, atlas_len, PILL_CFG,
                                          osm_id=str(osm_id), siblings=siblings)
                # Dots are generated post-cluster (like rail) to avoid duplicates at low zoom
                all_nonrail_pills.append({
                    "lon":            cx,
                    "lat":            cy,
                    "osm_id":         osm_id,
                    "mode":           mode,
                    "color":          color,
                    "width_base":     width_base,
                    "stop_id":        sid,
                    "stop_name":      stop_name,
                    "parent_station": parent_sta,
                    "extent":         extent,
                })

        else:
            line_lines_json = json.dumps([{"ref": line.get("gtfs_ref") or line.get("ref", ""), "color": color, "mode": mode, "name": line.get("name", "")}])
            for idx, entry in enumerate(stop_coords):
                if idx == 0 and skip_first_here:
                    continue
                if idx == last_idx and skip_last_here:
                    continue
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
                slon, slat = snap_to_line(lon, lat, flat)
                other_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": minzoom},
                    "geometry": {"type": "Point", "coordinates": [slon, slat]},
                    "properties": {
                        "color":          color,
                        "mode":           mode,
                        "width_base":     width_base,
                        "stop_id":        sid,
                        "stop_name":      meta.get("name", ""),
                        "parent_station": meta.get("parent", ""),
                        "lines_json":     line_lines_json,
                    },
                })

    # --- Rail dots + pills (unified pass) ---
    print(f"  {len(rail_pill_raw):,} raw rail stop positions → clustering...")
    rail_pill_clusters = cluster_stops_for_pills(rail_pill_raw, PILL_CLUSTER_RAIL_KM)
    rail_pill_clusters = merge_clusters_by_parent_station(rail_pill_clusters)
    print(f"  → {len(rail_pill_clusters):,} rail station clusters")
    # Place dots via tangent grouping + perpendicular sweep along the central
    # member's platform extent (per-group). Stabbed dots get placed on the
    # perpendicular bar; leftovers run through the old algorithm.
    print(f"  Placing rail dots across {len(rail_pill_clusters):,} clusters...")
    for c in rail_pill_clusters:
        coordinate_dots_global_stab(c, PROTECTION_RADIUS_RAIL_M,
                                    LONE_OUTLIER_GAP_RAIL_METRO_M)
    print("  → rail dot placement done")

    rail_features = []
    pill_features_rail = []
    for cluster in rail_pill_clusters:
        stop_count = count_unique_lines(cluster)
        mz = pill_minzoom("train", stop_count)

        color, mode, max_wb, dom_stop = dominant_line(cluster)
        lon = sum(s["lon"] for s in cluster) / len(cluster)
        lat = sum(s["lat"] for s in cluster) / len(cluster)
        lines_json_str = json.dumps(cluster_lines(cluster, line_lookup))
        centroid_props = {
            "color":          color,
            "mode":           mode,
            "width_base":     max_wb,
            "stop_id":        dom_stop.get("stop_id", ""),
            "stop_name":      dom_stop.get("stop_name", ""),
            "parent_station": dom_stop.get("parent_station", ""),
            "lines_json":     lines_json_str,
        }

        if mz is None:
            # Single-line station: one cluster dot at all zooms.
            rail_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": 5},
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": centroid_props,
            })
        else:
            feats = make_pill_features(cluster, mz, lines_json_str)
            if feats:
                # Multi-line station with a real pill: cluster dot at low
                # zoom, pill takes over at mz. The per-platform dots that
                # used to render alongside the pill caused visible wobble
                # along the pill casing — `make_pill_features` now emits
                # endpoint Points for singletons inside the cluster, and
                # the pill itself stands in for every other platform.
                rail_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": 5, "maxzoom": mz - 1},
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": centroid_props,
                })
                pill_features_rail.extend(feats)
            else:
                # Multi-line cluster whose pill collapsed (all positions
                # deduped to one point) — no pill is emitted, so the
                # cluster dot stays visible at all zooms.
                rail_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": 5},
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": centroid_props,
                })

    rail_pill_count = len(pill_features_rail)
    print(f"  → {rail_pill_count} rail pill/connector features "
          f"from {len(rail_pill_clusters):,} clusters")

    # ==========================================================================
    # Pill generation (non-rail)
    # ==========================================================================

    pill_features = list(pill_features_rail)

    # --- Non-rail pills (all modes combined → dominant wins) ---
    print(f"  {len(all_nonrail_pills):,} non-rail pill candidates "
          f"(tram+metro+bus+regional combined) → clustering...")
    nonrail_clusters = cluster_stops_for_pills(all_nonrail_pills, PILL_CLUSTER_NONRAIL_KM)
    nonrail_clusters = merge_clusters_by_parent_station(nonrail_clusters)
    # Same global stabbing placement as rail.
    print(f"  Placing non-rail dots across {len(nonrail_clusters):,} clusters...")
    for c in nonrail_clusters:
        _, dom_mode, _, _ = dominant_line(c)
        lone_outlier_m = (LONE_OUTLIER_GAP_RAIL_METRO_M
                          if dom_mode == "metro"
                          else LONE_OUTLIER_GAP_BUS_TRAM_M)
        coordinate_dots_global_stab(c, PROTECTION_RADIUS_NONRAIL_M,
                                    lone_outlier_m)
    print("  → non-rail dot placement done")

    # Emit debug overlays now that all clusters have been processed and
    # _STABBED_PAIRS / _DIAG_BARS are populated.
    print("Emitting debug stop dots...")
    write_debug_stops(line_stops, line_lookup, stop_attrs, stop_meta,
                       skip_first_oids, skip_last_oids)
    print("Emitting debug max-stab bars...")
    write_debug_bars()

    nonrail_pill_count = 0
    nonrail_dot_features = []
    for cluster in nonrail_clusters:
        stop_count  = count_unique_lines(cluster)
        color, dom_mode, max_wb, dom_stop = dominant_line(cluster)
        mz = pill_minzoom(dom_mode, stop_count)

        lon_c        = sum(s["lon"] for s in cluster) / len(cluster)
        lat_c        = sum(s["lat"] for s in cluster) / len(cluster)
        mode_minzoom = min(MODE_MINZOOM.get(s["mode"], 11) for s in cluster)
        lines_json_str = json.dumps(cluster_lines(cluster, line_lookup))
        centroid_props = {
            "color":          color,
            "mode":           dom_mode,
            "width_base":     max_wb,
            "stop_id":        dom_stop.get("stop_id", ""),
            "stop_name":      dom_stop.get("stop_name", ""),
            "parent_station": dom_stop.get("parent_station", ""),
            "lines_json":     lines_json_str,
        }

        if mz is None:
            # Single-line stop: one cluster dot at all zooms.
            nonrail_dot_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": mode_minzoom},
                "geometry": {"type": "Point", "coordinates": [lon_c, lat_c]},
                "properties": centroid_props,
            })
        else:
            feats = make_pill_features(cluster, mz, lines_json_str)
            if feats:
                # Multi-line stop with a real pill: cluster dot at low
                # zoom, pill from `mz` up. See the matching rail-side
                # comment above.
                nonrail_dot_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": mode_minzoom, "maxzoom": mz - 1},
                    "geometry": {"type": "Point", "coordinates": [lon_c, lat_c]},
                    "properties": centroid_props,
                })
                pill_features.extend(feats)
                nonrail_pill_count += len(feats)
            else:
                # Pill collapsed — cluster dot stays at all zooms in place
                # of the missing pill.
                nonrail_dot_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": mode_minzoom},
                    "geometry": {"type": "Point", "coordinates": [lon_c, lat_c]},
                    "properties": centroid_props,
                })

    print(f"  → {nonrail_pill_count} non-rail pill/connector features "
          f"from {len(nonrail_clusters):,} clusters")

    # ==========================================================================
    # Write outputs
    # ==========================================================================

    dot_features = rail_features + other_features + nonrail_dot_features
    OUT_DOTS.parent.mkdir(parents=True, exist_ok=True)
    OUT_DOTS.write_text(json.dumps({"type": "FeatureCollection", "features": dot_features}))
    OUT_PILLS.write_text(json.dumps({"type": "FeatureCollection", "features": pill_features}))

    # Summary
    mode_counts: dict = defaultdict(int)
    for f in dot_features:
        mode_counts[f["properties"]["mode"]] += 1
    print(f"\n{len(dot_features):,} stop dots → {OUT_DOTS}")
    for m, c in sorted(mode_counts.items(), key=lambda x: -x[1]):
        print(f"  {m:<20} {c:>6,}")

    pill_type_counts: dict = defaultdict(int)
    for f in pill_features:
        pill_type_counts[f["properties"].get("feature_type", "?")] += 1
    print(f"\n{len(pill_features):,} pill features → {OUT_PILLS}")
    for t, c in sorted(pill_type_counts.items(), key=lambda x: -x[1]):
        print(f"  {t:<20} {c:>6,}")


if __name__ == "__main__":
    main()
