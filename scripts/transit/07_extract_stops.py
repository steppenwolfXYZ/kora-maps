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
  - Ferry: no pills, but each parent_station emits a two-dot + connector
    pattern (snap-side dot, optional GTFS-side dot, optional connector). See
    pill-rendering.md § "Ferry stops".
  - Mountain modes: no pills.
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
from bisect import bisect_left
from math import radians, cos, sin, sqrt, atan2, acos, degrees, floor, pi, log
from pathlib import Path
from collections import defaultdict

ROOT       = Path(__file__).resolve().parents[2]

_transit_cfg = yaml.safe_load((ROOT / "scripts" / "transit" / "config.yaml").read_text())

LINES      = ROOT / "data" / "transit" / "transit_lines.geojson"
LINE_STOPS = ROOT / "data" / "transit" / "line_stops.json"
STOP_SCORES = ROOT / "data" / "transit" / "stop_size_scores.json"
RAIL_WAYS_GEOJSON = ROOT / "data" / "osm" / "rail_ways.geojson"
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
OUT_CLOSE_ZOOM      = ROOT / "data" / "transit" / "transit_close_zoom.geojson"

# Diagnostic state populated by coordinate_dots_global_stab:
# - _DIAG_BARS: list of (endpoint1, endpoint2) tuples for each max-stab bar.
# - _STABBED_PAIRS: set of (osm_id, stop_id) for (line, stop) records placed
#   on a bar. Read by write_debug_stops to mark stabbed dots as filled.
_DIAG_BARS = []
_STABBED_PAIRS = set()

# Per-mode platform-length defaults and sanity ranges from config.
PILL_CFG = _transit_cfg.get("pill_rendering", {})

# Ferry stop rendering: see config.yaml `ferry_stops` and pill-rendering.md
# § "Ferry stops".
FERRY_STOPS_CFG = _transit_cfg.get("ferry_stops", {}) or {}
FERRY_DOT_WB           = float(FERRY_STOPS_CFG.get("dot_width_base", 2.5))
FERRY_CONNECTOR_WB     = float(FERRY_STOPS_CFG.get("connector_width_base", 1.0))
FERRY_COLLAPSE_M       = float(FERRY_STOPS_CFG.get("collapse_threshold_m", 15.0))
FERRY_CONVERGE_M       = float(FERRY_STOPS_CFG.get("convergence_threshold_m", 20.0))
FERRY_ENDPOINT_PULL_M  = float(FERRY_STOPS_CFG.get("endpoint_pull_threshold_m", 75.0))

# Stops carry their raw `width_base` on the tile — no per-mode floor is
# baked in. The low-zoom minimum-size floor is applied at paint time via
# the additive per-zoom `wb_floor(z)` mechanism in `generate_style.py`
# (see pill-rendering.md § "Minimum stop size at low zoom").
def _stop_wb(wb: float, mode: str) -> float:
    return wb


# Pill design bands (see pill-rendering.md § "Pills and connectors" and
# § "Tippecanoe encoding"). Each band re-runs the pill construction with
# its own `pill_gap_angled_m` and `curve_min_radius_m`; emitted features
# carry `design_band` and per-feature `tippecanoe.minzoom`/`maxzoom`.
_PILL_DESIGN_BANDS_RAW = (_transit_cfg.get("pill_design_bands") or {})
PILL_DESIGN_BANDS = {}
for _band_id in ("A", "B", "C"):
    _band_cfg = _PILL_DESIGN_BANDS_RAW.get(_band_id) or {}
    PILL_DESIGN_BANDS[_band_id] = {
        "zoom_min": int(_band_cfg.get("zoom_min", {"A": 14, "B": 15, "C": 16}[_band_id])),
        "zoom_max": _band_cfg.get("zoom_max", {"A": 14, "B": 15, "C": None}[_band_id]),
        "pill_gap_straight_m": float(_band_cfg.get("pill_gap_straight_m", {"A": 100, "B": 75, "C": 50}[_band_id])),
        "pill_gap_angled_m": float(_band_cfg.get("pill_gap_angled_m", {"A": 60, "B": 30, "C": 15}[_band_id])),
        "curve_min_radius_m": float(_band_cfg.get("curve_min_radius_m", {"A": 8, "B": 6, "C": 5}[_band_id])),
        "dedup_tol_m": float(_band_cfg.get("dedup_tol_m", {"A": 5.0, "B": 2.5, "C": 0.5}[_band_id])),
        "pill_simplify_tol_m": float(_band_cfg.get("pill_simplify_tol_m", {"A": 4.0, "B": 3.0, "C": 0.1}[_band_id])),
        "pill_min_d_px": float(_band_cfg.get("pill_min_d_px", {"A": 4.5, "B": 6.0, "C": 8.0}[_band_id])),
        "pill_slope_px_per_wb": float(_band_cfg.get("pill_slope_px_per_wb", {"A": 2.3, "B": 3.2, "C": 4.4}[_band_id])),
    }
del _PILL_DESIGN_BANDS_RAW

PILL_WB_HIGH = 5.0   # matches WB_HIGH in generate_style.py — dataset's max width_base

# Per-cluster pill diameter (in metres, at the current band's target zoom),
# set by `make_pill_features` right after it computes the cluster's max_wb
# and cos_lat. Read by `_tangent_candidates` (deep inside `_curve_connector`)
# so its internal `_simplify_pill_lonlat` call uses the same kink-aware
# simplification as the pill emission. Reset per (cluster, band) pair.
_CURRENT_CLUSTER_PILL_DIAMETER_M = None

def _set_pill_design_band(band_cfg):
    """Swap the module-level constants that pill construction consults.

    `PILL_GAP_STRAIGHT_M`, `PILL_GAP_ANGLED_M`, `CURVE_MIN_RADIUS_M`,
    `DEDUP_TOL_M`, `PILL_SIMPLIFY_TOL_M`, and the pill-sizing
    coefficients (`PILL_MIN_D_PX`, `PILL_SLOPE_PX_PER_WB`,
    `PILL_BAND_ZOOM`) are read by `_should_split_at_gap`,
    `_dedup_stop_positions`, `_simplify_pill_lonlat`, the
    connector-curving helpers, and `_remove_pill_kinks`. Setting them
    here before a `make_pill_features()` call runs that call under the
    given band's thresholds + radius.
    """
    g = globals()
    g["PILL_GAP_STRAIGHT_M"] = band_cfg["pill_gap_straight_m"]
    g["PILL_GAP_ANGLED_M"] = band_cfg["pill_gap_angled_m"]
    g["CURVE_MIN_RADIUS_M"] = band_cfg["curve_min_radius_m"]
    g["DEDUP_TOL_M"] = band_cfg["dedup_tol_m"]
    g["PILL_SIMPLIFY_TOL_M"] = band_cfg["pill_simplify_tol_m"]
    g["PILL_MIN_D_PX"] = band_cfg["pill_min_d_px"]
    g["PILL_SLOPE_PX_PER_WB"] = band_cfg["pill_slope_px_per_wb"]
    g["PILL_BAND_ZOOM"] = band_cfg["zoom_min"]

def _tag_band_features(feats, band_id, band_cfg):
    """Stamp `design_band` and per-feature tippecanoe zoom limits."""
    for f in feats:
        f["properties"]["design_band"] = band_id
        tipp = f.setdefault("tippecanoe", {})
        tipp["minzoom"] = max(int(tipp.get("minzoom", 0)), band_cfg["zoom_min"])
        if band_cfg["zoom_max"] is not None:
            tipp["maxzoom"] = band_cfg["zoom_max"]

# Far-zoom dot positioning: see config.yaml `far_zoom_marker` and
# .claude/concepts/far-zoom-stop-markers.md.
FAR_ZOOM_CFG = _transit_cfg.get("far_zoom_marker", {}) or {}
FAR_ZOOM_INTERSECTION_TOL_M      = float(FAR_ZOOM_CFG.get("intersection_tol_m", 8.0))

# Mode family for the far-zoom dot rule. RAIL_LIKE skips the intersection
# search (largest pill → largest disc → existing centroid); every other mode
# runs intersection search first. Mountain rail-like is added per-feature
# via `mountain_origin ∈ MOUNTAIN_RAIL_ORIGINS`, mirroring the rest of the
# pipeline. See far-zoom-stop-markers.md § "Position rule by mode family".
FAR_ZOOM_RAIL_LIKE_MODES = {"train"}

RAIL_MODES = {"train"}
# Modes that get pills via the non-rail pipeline. `train` uses the rail
# pipeline (RAIL_MODES). Mountain pill routing is per-feature via
# `mountain_origin` — see MOUNTAIN_PILL_ORIGINS below.
PILL_MODES = {"train", "tram", "metro", "bus", "regional_bus"}

# Mountain origins that enter the rail pill pipeline. Splits:
#   • MOUNTAIN_RAIL_ORIGINS — physical rail platforms; identical handling to
#     `train` (centred ±L/2 extent, mountain_rail length config).
#   • MOUNTAIN_EXTENT_ORIGINS — adds funicular with centred ±L/2 anchoring but
#     a smaller per-mode length config (mountain_funicular).
#   • MOUNTAIN_PILL_ORIGINS — adds aerial, which has no extent (no platform
#     geometry, zero atlas coverage). Aerial stops join the rail clustering
#     pool as fixed dots: position locked to the snapped GTFS coord, never
#     moved by the sweep / leftover-fill, but allowed to participate in the
#     NN-path / pill-split / connector logic.
MOUNTAIN_RAIL_ORIGINS = {"rebucketed_rail", "rack"}
MOUNTAIN_EXTENT_ORIGINS = MOUNTAIN_RAIL_ORIGINS | {"funicular"}
MOUNTAIN_PILL_ORIGINS = MOUNTAIN_EXTENT_ORIGINS | {"aerial"}

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

# Per-mode max speed (km/h) for the salience speed boost. Mirrors the table
# in 06_score_and_match.py. Mountain has no entry; mountain stops are
# distinguished via their own tier and tier-range, so speed isn't a
# salience signal for mountain.
MODE_MAX_SPEED = {
    "train":        100,
    "tram":          25,
    "metro":         50,
    "bus":           35,
    "regional_bus":  65,
    "ferry":         22,
}

# Color-indicator zoom (mini per-group dots inside stop dots/discs/pills).
# Appears at pill-minzoom so indicators are present from the moment pills
# appear. See `.claude/concepts/pill-zoom-stop-tweaks.md`.
INDICATOR_MIN_ZOOM = 13

# Mode → color-group key. Ferry collapses into the bus group (shared color);
# modes outside this dict produce no indicator.
MODE_TO_COLOR_GROUP = {
    "train":        "train",
    "metro":        "metro",
    "tram":         "tram",
    "bus":          "bus",
    "ferry":        "bus",
    "regional_bus": "regional_bus",
    "mountain":     "mountain",
}

# Stable iteration order for the color-group set at a location.
COLOR_GROUP_ORDER = ["train", "metro", "tram", "bus", "regional_bus", "mountain"]

# Spatial clustering radius for pill grouping
PILL_CLUSTER_RAIL_KM    = 0.300   # rail: 300 m (same as dot deduplication)
PILL_CLUSTER_NONRAIL_KM = 0.050   # all other modes combined: 50 m

# Absolute-metre gap thresholds for splitting the NN path into separate
# pills + connectors. Not scaled by width_base — `wb` controls disc/pill
# width, not gap length. Both are design-band-dependent — swapped via
# `_set_pill_design_band()` before each bake pass in `main()`. See
# pill-rendering.md § "Pills and connectors".
#   PILL_GAP_STRAIGHT_M: A=100, B=75, C=50
#   PILL_GAP_ANGLED_M:   A=60,  B=30, C=15
PILL_GAP_STRAIGHT_M = 50    # placeholder default; overwritten per band
PILL_GAP_ANGLED_M = 15      # placeholder default; overwritten per band

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
# leftover is treated as a spurious "sub-platform" stop — a small subset
# of trips appears to terminate alongside an already-placed dot and
# would otherwise render as a short connector running along the line —
# when either of these holds against another cluster member:
#   (a) their `platform_code`s share the same leading-digit run (e.g.
#       "12A-C" and "12D-F" both reduce to "12"); or
#   (b) the two extents geometrically coincide within SAME_TRACK_PERP_M
#       perpendicular — pfaedle has routed both trips onto the same OSM
#       rail way, so the dots overlap on the rendered map even though
#       GTFS gives them different platform codes.
# Standard Swiss inter-track spacing is ~4.5 m, so a 2 m perpendicular
# threshold cleanly distinguishes same-track from adjacent-track.
# The stop is dropped from rendering (its position is snapped to the
# absorbing dot so _dedup_stop_positions collapses it later, preserving
# its line in the popup), and leftover-fill is re-run on the remaining
# leftovers.
SAME_TRACK_PERP_M = 2.0

PERP_PLATFORM_TOL_DEG = float(PILL_CFG.get("perp_platform_tol_deg", 2.0))

# Rail-only missing-range fill (pill-rendering concept § "Missing-range fill
# (rail only)"): at train terminals where the pfaedle polyline ends at the
# platform-centre GTFS coord, the missing-side extent + line extension follow
# an OSM rail way under the snap point. Gates: proximity within
# OSM_MATCH_RADIUS_M and tangent within OSM_MATCH_MAX_TANGENT_DIFF_DEG of the
# polyline's last-segment tangent. Fallback A (no match) caps a straight
# extension at OSM_FALLBACK_MAX_STRAIGHT_M. Fallback B (way runs out) marks
# the stop as end-of-platform — the polyline-side absorbs the full L, no
# line extension.
OSM_MATCH_RADIUS_M             = float(PILL_CFG.get("osm_match_radius_m", 5.0))
OSM_MATCH_MAX_TANGENT_DIFF_DEG = float(PILL_CFG.get("osm_match_max_tangent_diff_deg", 15.0))
OSM_FALLBACK_MAX_STRAIGHT_M    = float(PILL_CFG.get("osm_fallback_max_straight_m", 50.0))
# How close a stop's snap must be to a polyline endpoint to count as a
# terminal eligible for OSM-walk extension. pfaedle puts polyline endpoints
# within ~metres of the snap; 20 m is comfortable headroom.
TERMINAL_SNAP_TOLERANCE_M      = 20.0

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
# connector. Design-band-dependent: A=8, B=6, C=5 (set per-band via
# `_set_pill_design_band()` before each bake pass in `main()`).
CURVE_MIN_RADIUS_M = 5.0  # placeholder default; overwritten per band
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


def load_stop_scores() -> dict:
    """Return {parent_uic: {"score": float, "tier": str}} from step 06's
    stop_size_scores.json. Empty dict if the file is missing — every dot
    then falls back to the `small_bus` tier and a `WARNING` is printed by
    the caller.
    """
    if not STOP_SCORES.exists():
        return {}
    raw = json.loads(STOP_SCORES.read_text())
    out = {}
    for uic, v in raw.items():
        if isinstance(v, dict):
            out[uic] = {
                "score": float(v.get("score", 0.0)),
                "tier": str(v.get("tier", "small_bus")),
            }
        else:
            out[uic] = {"score": float(v), "tier": "small_bus"}
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

    Direction-collapsed modes (ferry, aerial mountain, funicular mountain)
    are exempt: their opposite directions are already merged into one
    feature upstream, so any first/last pair at the same stop_id is two
    distinct variants whose pfaedle-snapped endpoints both need dots.

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
    def _is_dedup_exempt(oid):
        # Modes whose directionality is collapsed upstream (ferry +
        # aerial / funicular mountain — see .claude/rules/transit.md
        # "Mountain and ferry" § per-direction split) cannot produce
        # first/last duplicate pairs at a shared stop_id: opposite
        # directions are already merged into one feature, so any
        # first/last pair at the same stop_id belongs to two distinct
        # variants whose pfaedle-snapped pier endpoints both need their
        # own dot. Aerial additionally has the cable-car cascade reason
        # (Niederhornbahn funicular → aerial at Beatenberg, Stockhornbahn
        # lower → upper aerial at Chrindi: one bare UIC across two
        # separate aerialways) — both arguments point the same way.
        if not line_lookup:
            return False
        info = line_lookup.get(oid) or line_lookup.get(str(oid))
        if not info:
            return False
        if info.get("mode") == "ferry":
            return True
        if info.get("mode") == "mountain" and info.get("mountain_origin") in ("aerial", "funicular"):
            return True
        return False

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
        # Direction-collapsed modes (ferry + aerial / funicular mountain)
        # are exempt from terminus dedup — see _is_dedup_exempt above.
        if _is_dedup_exempt(oid_dep):
            continue
        for oid_arr, lon_a, lat_a in arrivals_by_sid.get(sid, []):
            if oid_arr == oid_dep:
                continue
            if _is_dedup_exempt(oid_arr):
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


def _start_segment_tangent(polyline, dists, min_seg_m=1.0):
    """Per-metre (dx, dy) direction of the polyline's very first segment
    (poly[0] → poly[1]). Returns None if that segment is shorter than
    `min_seg_m` — the caller then falls back to a windowed average, because a
    pfaedle sub-metre stub at a line terminus would otherwise be used as the
    extension direction and point wildly wrong.

    Used for straight-line backward extrapolation from the polyline start —
    the extension follows the actual arrival angle at the polyline's starting
    vertex, not a chord averaged over a window that may cross a curve at the
    platform.
    """
    if len(polyline) < 2:
        return None
    seg_len = dists[1] - dists[0]
    if seg_len < min_seg_m:
        return None
    ax, ay = polyline[0]
    bx, by = polyline[1]
    return ((bx - ax) / seg_len, (by - ay) / seg_len)


# =============================================================================
# OSM rail walk (pill-rendering concept § "Missing-range fill (rail only)")
# =============================================================================

class _RailIndex:
    """Spatial grid + endpoint adjacency over OSM rail-way LineStrings, loaded
    once from data/osm/rail_ways.geojson. Used by `_osm_rail_walk` to extend
    train-line polylines at terminal stops along the actual rail track.
    """

    def __init__(self, cell_size_deg: float = 0.001):
        self.ways: list = []
        self.way_dists: list = []
        self.cells: dict = defaultdict(list)
        self.endpoint_to_ways: dict = defaultdict(list)
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


def _load_rail_index(path):
    """Load OSM rail ways from a FeatureCollection GeoJSON into a _RailIndex.
    Returns None if the file is missing — terminal extension then falls back
    to the capped-straight (Fallback A) path for every stop."""
    if not path.exists():
        print(f"  WARNING: {path.name} not found — rail walk disabled, "
              f"terminal extensions fall back to capped-straight")
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
    print(f"  Loaded {len(idx.ways):,} rail ways from {path.name} "
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


def _extend_polylines_at_terminals(line_lookup, line_stops, rail_idx,
                                     pill_cfg, stop_attrs):
    """Extend train and mountain rail-like polylines at terminal stops via
    OSM rail walk (Fallback A's capped straight when no way matches).
    Modifies `line_lookup[oid]["coords"]` in place.

    Scope:
      • `mode == "train"` — full rail.
      • `mode == "mountain"` with `mountain_origin in MOUNTAIN_RAIL_ORIGINS`
        (rebucketed_rail / rack) — physical rail (narrow_gauge), present in
        `data/osm/rail_ways.geojson`. Uses `mountain_rail` length config.

    Funicular and aerial mountain origins are skipped: funicular tracks are
    `railway=funicular` (not in step 03's rail extraction), and aerial
    cable cars have no rail geometry at all.

    Returns the set of (osm_id, stop_id) pairs that hit Fallback B — the OSM
    walk matched a way but the way chain ran out before reaching L/2. These
    stops use asymmetric anchoring in `_platform_extent` (polyline side
    absorbs the full L; no polyline extension).
    """
    end_of_platform_pairs: set = set()
    if not pill_cfg.get("default_length_m"):
        return end_of_platform_pairs

    n_walk = n_straight = n_eop = 0

    for oid, info in line_lookup.items():
        mode = info.get("mode")
        mo = info.get("mountain_origin")
        if mode != "train" and not (
                mode == "mountain" and mo in MOUNTAIN_RAIL_ORIGINS):
            continue
        coords = info.get("coords")
        if not coords:
            continue
        flat = flatten_coords(coords)
        if len(flat) < 2:
            continue
        flat = [(c[0], c[1]) for c in flat]
        dists = _cum_dist_m(flat)
        poly_max = dists[-1]
        if poly_max <= 0:
            continue

        ls_entry = line_stops.get(str(oid))
        if ls_entry is None:
            ls_entry = line_stops.get(oid)
        if not ls_entry:
            continue
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        if not triplets:
            continue

        terminals = []
        first_trip = triplets[0]
        if len(first_trip) >= 3:
            terminals.append(("start", first_trip))
        if len(triplets) > 1:
            last_trip = triplets[-1]
            if len(last_trip) >= 3:
                terminals.append(("end", last_trip))

        prepend_coords = None
        append_coords = None

        for which, trip in terminals:
            stop_lon, stop_lat, sid = trip[0], trip[1], trip[2]
            t_snap = _project_meters(stop_lon, stop_lat, flat, dists)
            if which == "start":
                if t_snap > TERMINAL_SNAP_TOLERANCE_M:
                    continue
                t_endpoint = 0.0
                ep_lon, ep_lat = flat[0]
            else:
                if poly_max - t_snap > TERMINAL_SNAP_TOLERANCE_M:
                    continue
                t_endpoint = poly_max
                ep_lon, ep_lat = flat[-1]

            tan = _directional_tangent_at(flat, dists, t_endpoint, window_m=20.0)
            if tan is None:
                continue
            sign = +1.0 if which == "end" else -1.0
            # Normalise tangent to per-metre units (already per-metre in
            # _directional_tangent_at), apply sign to flip for start-end.
            walk_dx = tan[0] * sign
            walk_dy = tan[1] * sign

            atlas_len = (stop_attrs.get(sid, {}) or {}).get("length")
            L = _resolve_length(mode, atlas_len, pill_cfg, mountain_origin=mo)
            if L is None or L <= 0:
                continue
            target_m = L / 2.0

            status, walk_coords = _osm_rail_walk(
                rail_idx, ep_lon, ep_lat, walk_dx, walk_dy, target_m)

            if status == "walk":
                ext = walk_coords
                n_walk += 1
            elif status == "ran_out":
                end_of_platform_pairs.add((str(oid), sid))
                n_eop += 1
                continue
            else:
                # Fallback A: capped straight extension.
                cap_m = min(target_m, OSM_FALLBACK_MAX_STRAIGHT_M)
                ext_end_lon = ep_lon + walk_dx * cap_m
                ext_end_lat = ep_lat + walk_dy * cap_m
                ext = [(ep_lon, ep_lat), (ext_end_lon, ext_end_lat)]
                n_straight += 1

            if which == "start":
                # Extension goes from ep outward; for prepending we want it
                # to end at ep, so reverse.
                prepend_coords = list(reversed(ext))
            else:
                append_coords = ext

        if prepend_coords is None and append_coords is None:
            continue

        new_flat = []
        if prepend_coords is not None:
            new_flat.extend(prepend_coords[:-1])
        new_flat.extend(flat)
        if append_coords is not None:
            new_flat.extend(append_coords[1:])
        info["coords"] = new_flat

    print(f"  Terminal rail extension: walk={n_walk}, "
          f"straight={n_straight}, end-of-platform={n_eop}")
    return end_of_platform_pairs


# =============================================================================
# Platform extent (continued) — see pill-rendering concept § Platform extent
# =============================================================================

# Missing-range fill (tram/bus/regional_bus): borrow gates.
SIBLING_PROXIMITY_M = 1.0
# Non-sibling tier admission gate. 45° (was 15°) because the own anchor
# tangent can be rotated toward an imminent turn (Herrliberg Bhf West: the
# 974 departs into its loop, skewing the tangent 34° off the road, which
# rejected the perfect 972 donor ending at the stop). Ranking is by best
# angle first, so worse-angle candidates admitted by the looser gate are
# only used when nothing better fits; perpendicular crossings (~90°,
# Sevgein) stay rejected.
SIBLING_ANGLE_TOL_RAD = radians(45.0)
SIBLING_MAX_TURN_DEG = 120.0           # both tiers: reject candidates whose
                                        # local turn at q_t exceeds this — the
                                        # aligned/reversed cos_ang sign becomes
                                        # unstable at hairpins.


def _local_turn_angle_deg(polyline, dists, t, window_m=2.0):
    """Turn angle in degrees between the polyline's backward and forward
    one-sided tangents at arc-length t. 0 = straight-through, 180 = full
    U-turn. Returns None when either side of the window is too short for
    a stable estimate.
    """
    poly_max = dists[-1]
    if poly_max <= 0:
        return None
    back_lo = max(0.0, t - window_m)
    fwd_hi = min(poly_max, t + window_m)
    back_arc = t - back_lo
    fwd_arc = fwd_hi - t
    if back_arc < window_m * 0.5 or fwd_arc < window_m * 0.5:
        return None
    p_back = _interp_at(polyline, dists, back_lo)
    p_mid = _interp_at(polyline, dists, t)
    p_fwd = _interp_at(polyline, dists, fwd_hi)
    b_dx = p_mid[0] - p_back[0]
    b_dy = p_mid[1] - p_back[1]
    f_dx = p_fwd[0] - p_mid[0]
    f_dy = p_fwd[1] - p_mid[1]
    b_mag = sqrt(b_dx * b_dx + b_dy * b_dy)
    f_mag = sqrt(f_dx * f_dx + f_dy * f_dy)
    if b_mag == 0 or f_mag == 0:
        return None
    cos_ang = (b_dx * f_dx + b_dy * f_dy) / (b_mag * f_mag)
    cos_ang = max(-1.0, min(1.0, cos_ang))
    return degrees(acos(cos_ang))


def _projections_at(anchor_lon, anchor_lat, cand_poly, cand_dists,
                     collect_m=SIBLING_PROXIMITY_M, merge_arc_m=20.0):
    """Return every `q_t` at which `cand_poly` passes within `collect_m` of
    the anchor — one entry per distinct pass (local minimum of distance),
    merged when consecutive qualifying segments lie within `merge_arc_m` of
    arc-length. A polyline that visits the anchor several times (a loop
    departing, transiting mid-route, and returning — Bad Zurzach's bus 4 is
    the canonical case) exposes a distinct tangent at each pass; each must
    enter the borrow ranking separately, because the pass whose walk fits is
    often not the globally nearest one.

    Falls back to the single global-nearest projection when no pass is
    within `collect_m` — the caller's proximity gate then rejects it, which
    keeps calling code free of a special empty case.
    """
    passes = []  # [q_t_of_best_point, best_dist_m, t_of_last_qualifying_seg]
    for i in range(len(cand_poly) - 1):
        ax, ay = cand_poly[i]
        bx, by = cand_poly[i + 1]
        dx, dy = bx - ax, by - ay
        seg_sq = dx * dx + dy * dy
        if seg_sq == 0:
            tt = 0.0
        else:
            tt = max(0.0, min(1.0, ((anchor_lon - ax) * dx
                                     + (anchor_lat - ay) * dy) / seg_sq))
        cx, cy = ax + tt * dx, ay + tt * dy
        dist_m = haversine_km(anchor_lon, anchor_lat, cx, cy) * 1000.0
        if dist_m > collect_m:
            continue
        t_here = cand_dists[i] + tt * (cand_dists[i + 1] - cand_dists[i])
        if passes and t_here - passes[-1][2] < merge_arc_m:
            if dist_m < passes[-1][1]:
                passes[-1][0] = t_here
                passes[-1][1] = dist_m
            passes[-1][2] = t_here
        else:
            passes.append([t_here, dist_m, t_here])
    if not passes:
        return [_project_meters(anchor_lon, anchor_lat, cand_poly, cand_dists)]
    return [p[0] for p in passes]


def _borrow_backward_segment(anchor_lon, anchor_lat,
                              anchor_dx, anchor_dy, t_on_self, L,
                              siblings, self_oid):
    """Try to borrow the missing `L - t_on_self` metres of backward extent from
    a same-line sibling's polyline. The search anchor is the on-polyline
    extent's backward endpoint — i.e. `poly[0]`, the far end reached after
    consuming the on-polyline portion — not the snap of the stop. This way,
    when the stop sits mid-polyline (t > 0), we still look for candidates at
    the point where the extension actually begins.

    Returns the borrowed sequence (lon, lat) in backward→forward order, in
    the donor polyline's true coordinates — never translated onto the
    anchor, so the fill always lies exactly on a drawn line (a sub-gate jog
    at the join is acceptable; a parallel offset next to the line is not).
    Returns None if nothing qualifies.

    Gates (concept § Missing-range fill, step 1):
      • ~2 m proximity between the anchor and the sibling's nearest-point
        projection of the anchor (rejects parallel-street variants).
      • Local turn at the sibling's nearest point ≤ SIBLING_MAX_TURN_DEG
        (rejects hairpins where the aligned/reversed sign of cos_ang is
        numerically unstable).
      • NO hard tangent-agreement gate. Same-line siblings are the same route
        by definition, so a big local angle at the shared point is a real
        turn (Ardez Bröl-style corner terminal), not a wrong-alignment
        signal. Candidates are ranked by tangent match instead: highest
        `|cos(angle)|` wins, ties broken by shorter proximity.

    Aligned vs reversed direction still comes from the sign of cos_ang; the
    sibling walk is reversed for opposite-direction siblings so we always
    move backward relative to our line.

    Circular lines are their own sibling (self_oid == sib_oid): multi-pass
    projection surfaces the polyline's far end (and any mid-loop transits)
    as passes, so a loop's "return to start" geometry fills its own first
    stop's backward extent. The pass at the loop's own start is harmless —
    its tangent is identical to ours (aligned), so its backward walk runs
    off the polyline start and is skipped.
    """
    if L <= t_on_self:
        return None
    fill_m = L - t_on_self

    cos_lat = cos(radians(anchor_lat))
    my_ex, my_ey = anchor_dx * cos_lat, anchor_dy
    my_mag = sqrt(my_ex * my_ex + my_ey * my_ey)
    if my_mag == 0:
        return None

    ranked = []
    for sib_oid, sib_poly in siblings:
        if len(sib_poly) < 2:
            continue
        sib_dists = _cum_dist_m(sib_poly)
        sib_total = sib_dists[-1]
        if sib_total <= 0:
            continue

        for q_t in _projections_at(anchor_lon, anchor_lat,
                                     sib_poly, sib_dists):
            q_lon, q_lat = _interp_at(sib_poly, sib_dists, q_t)

            prox_m = haversine_km(anchor_lon, anchor_lat,
                                    q_lon, q_lat) * 1000.0
            if prox_m > SIBLING_PROXIMITY_M:
                continue

            turn = _local_turn_angle_deg(sib_poly, sib_dists, q_t,
                                          window_m=2.0)
            if turn is not None and turn > SIBLING_MAX_TURN_DEG:
                continue

            sib_tan = _directional_tangent_at(sib_poly, sib_dists, q_t,
                                                window_m=2.0)
            if sib_tan is None:
                continue
            sib_dx, sib_dy = sib_tan
            sib_ex, sib_ey = sib_dx * cos_lat, sib_dy
            sib_mag = sqrt(sib_ex * sib_ex + sib_ey * sib_ey)
            if sib_mag == 0:
                continue

            cos_ang = (my_ex * sib_ex + my_ey * sib_ey) / (my_mag * sib_mag)
            aligned = cos_ang > 0
            ranked.append((abs(cos_ang), prox_m, sib_oid, sib_poly,
                            sib_dists, aligned, q_t))

    # Best tangent match wins; ties broken by shorter proximity.
    ranked.sort(key=lambda x: (-x[0], x[1]))

    for (_abs_cos, _prox_m, _sib_oid, sib_poly, sib_dists,
         aligned, q_t) in ranked:
        sib_total = sib_dists[-1]
        # Anchor sits at the on-polyline extent's far end (poly[0]), so the
        # walk on the sibling starts at q_t itself — no t_on_self shift.
        if aligned:
            walk_end_t = q_t
            walk_start_t = q_t - fill_m
            if walk_start_t < 0:
                continue
            seg = list(_slice_polyline(sib_poly, sib_dists,
                                        walk_start_t, walk_end_t))
        else:
            walk_start_t = q_t
            walk_end_t = q_t + fill_m
            if walk_end_t > sib_total:
                continue
            seg = list(_slice_polyline(sib_poly, sib_dists,
                                        walk_start_t, walk_end_t))
            seg.reverse()

        if len(seg) < 2:
            continue

        # The segment keeps the donor's true coordinates — never translate
        # it onto the anchor. Where the donor sits slightly off the anchor
        # (within the proximity gate), the extent has a small jog at the
        # join instead of running parallel next to the drawn line.
        return seg

    return None


# -----------------------------------------------------------------------------
# Non-sibling backward borrow (pill-rendering concept § Missing-range fill,
# step 2). Widens the sibling-borrow candidate set from same-line variants to
# any drawn line polyline within ~2 m of the snapped stop coord, kept honest
# by the same ±15° tangent gate. Populated once by main() before pill / stop
# extent building runs; None disables the tier.
# -----------------------------------------------------------------------------

_ALL_LINES_INDEX = None


class _AllLinesIndex:
    """Spatial grid over every drawn line polyline. Given (lon, lat) and a
    radius in metres, returns candidate (osm_id, sib_key, polyline, dists)
    tuples whose polyline touches a cell within radius of the point. Callers
    do their own precise proximity + tangent gating on the returned set."""

    _CELL_DEG = 0.0005

    def __init__(self):
        self._cells = defaultdict(set)
        self._lookup = {}

    def add(self, oid, sib_key, poly, dists):
        self._lookup[str(oid)] = (sib_key, poly, dists)
        cs = self._CELL_DEG
        for i in range(len(poly) - 1):
            ax, ay = poly[i]
            bx, by = poly[i + 1]
            lo_x = int(min(ax, bx) / cs); hi_x = int(max(ax, bx) / cs)
            lo_y = int(min(ay, by) / cs); hi_y = int(max(ay, by) / cs)
            for cx in range(lo_x, hi_x + 1):
                for cy in range(lo_y, hi_y + 1):
                    self._cells[(cx, cy)].add(str(oid))

    def query(self, lon, lat, radius_m):
        deg = radius_m / 111000.0 * 1.5
        cs = self._CELL_DEG
        lo_x = int((lon - deg) / cs); hi_x = int((lon + deg) / cs)
        lo_y = int((lat - deg) / cs); hi_y = int((lat + deg) / cs)
        seen = set()
        for cx in range(lo_x, hi_x + 1):
            for cy in range(lo_y, hi_y + 1):
                seen |= self._cells.get((cx, cy), set())
        out = []
        for oid in seen:
            entry = self._lookup.get(oid)
            if entry is not None:
                out.append((oid,) + entry)
        return out

    def own_sib_key(self, oid):
        entry = self._lookup.get(str(oid))
        return entry[0] if entry else None


def _borrow_backward_nonsibling(anchor_lon, anchor_lat,
                                 anchor_dx, anchor_dy, t_on_self, L,
                                 self_oid, self_sib_key):
    """Non-sibling backward borrow: widen the missing-range fill from
    same-(ref, agency_id, mode) variants to any drawn line polyline within
    ~2 m of the anchor (the on-polyline extent's far end, `poly[0]`),
    keeping the ±15° tangent gate. Multiple qualifying candidates are
    ranked by tangent match to (anchor_dx, anchor_dy) — highest
    `|cos(angle)|` wins, with shorter proximity as tie-break. The picked
    line is walked from its projection of the anchor by the missing
    arc-length; the segment keeps the donor's true coordinates (never
    translated onto the anchor — see _borrow_backward_segment). Returns
    None when the tier is disabled, when no candidate qualifies, or when
    every ranked candidate's polyline runs out before fill_m.
    """
    if _ALL_LINES_INDEX is None or L <= t_on_self:
        return None
    fill_m = L - t_on_self

    cos_lat = cos(radians(anchor_lat))
    my_ex, my_ey = anchor_dx * cos_lat, anchor_dy
    my_mag = sqrt(my_ex * my_ex + my_ey * my_ey)
    if my_mag == 0:
        return None
    cos_tol = cos(SIBLING_ANGLE_TOL_RAD)

    ranked = []
    for (cand_oid, cand_key, cand_poly, cand_dists) in _ALL_LINES_INDEX.query(
            anchor_lon, anchor_lat, SIBLING_PROXIMITY_M):
        if cand_oid == str(self_oid):
            continue
        if cand_key == self_sib_key:
            continue
        cand_total = cand_dists[-1]
        if cand_total <= 0:
            continue

        for q_t in _projections_at(anchor_lon, anchor_lat,
                                    cand_poly, cand_dists):
            q_lon, q_lat = _interp_at(cand_poly, cand_dists, q_t)
            prox_m = haversine_km(anchor_lon, anchor_lat,
                                    q_lon, q_lat) * 1000.0
            if prox_m > SIBLING_PROXIMITY_M:
                continue

            turn = _local_turn_angle_deg(cand_poly, cand_dists, q_t,
                                          window_m=2.0)
            if turn is not None and turn > SIBLING_MAX_TURN_DEG:
                continue

            cand_tan = _directional_tangent_at(cand_poly, cand_dists, q_t,
                                                window_m=2.0)
            if cand_tan is None:
                continue
            cand_dx, cand_dy = cand_tan
            cand_ex, cand_ey = cand_dx * cos_lat, cand_dy
            cand_mag = sqrt(cand_ex * cand_ex + cand_ey * cand_ey)
            if cand_mag == 0:
                continue

            cos_ang = (my_ex * cand_ex + my_ey * cand_ey) / (my_mag * cand_mag)
            abs_cos = abs(cos_ang)
            if abs_cos < cos_tol:
                continue
            aligned = cos_ang > 0
            ranked.append((abs_cos, prox_m, cand_oid, cand_poly, cand_dists,
                            aligned, q_t))

    ranked.sort(key=lambda x: (-x[0], x[1]))

    for (_abs_cos, _prox_m, _cand_oid, cand_poly, cand_dists,
         aligned, q_t) in ranked:
        cand_total = cand_dists[-1]
        # Anchor is at poly[0], so the walk starts at q_t itself.
        if aligned:
            walk_end_t = q_t
            walk_start_t = q_t - fill_m
            if walk_start_t < 0:
                continue
            seg = list(_slice_polyline(cand_poly, cand_dists,
                                        walk_start_t, walk_end_t))
        else:
            walk_start_t = q_t
            walk_end_t = q_t + fill_m
            if walk_end_t > cand_total:
                continue
            seg = list(_slice_polyline(cand_poly, cand_dists,
                                        walk_start_t, walk_end_t))
            seg.reverse()

        if len(seg) < 2:
            continue

        # Donor coordinates are kept as-is — see _borrow_backward_segment.
        return seg

    return None


def _length_key(mode: str, mountain_origin):
    """Map (mode, mountain_origin) to a config key under
    pill_rendering.{default,sanity_min,sanity_max}_length_m. Returns None
    when no extent is defined for the stop (ferry; mountain aerial; any
    out-of-scope mode)."""
    if mode == "mountain":
        if mountain_origin in MOUNTAIN_RAIL_ORIGINS:
            return "mountain_rail"
        if mountain_origin == "funicular":
            return "mountain_funicular"
        return None
    return mode


def _resolve_length(mode: str, atlas_length, cfg: dict, mountain_origin=None):
    """Pick the platform length to use for a given mode and atlas value.

    Atlas value is used when it lies within the per-mode sanity range;
    otherwise the per-mode default is returned. Returns None for modes
    that don't carry a platform extent (ferry; mountain aerial).
    """
    key = _length_key(mode, mountain_origin)
    if key is None or key not in cfg.get("default_length_m", {}):
        return None
    smin = cfg["sanity_min_m"][key]
    smax = cfg["sanity_max_m"][key]
    if atlas_length is not None and smin <= atlas_length <= smax:
        return atlas_length
    return cfg["default_length_m"][key]


def _platform_extent(stop_lon, stop_lat, polyline, mode, atlas_length, cfg,
                      osm_id=None, siblings=None, end_of_platform=False,
                      mountain_origin=None):
    """Return the (lon, lat) sequence tracing the platform's allowed range
    along its polyline, or None for out-of-scope modes / degenerate geometry.

    Anchoring (per pill-rendering concept):
      • train, metro            — GTFS coord (snapped to polyline) is platform
                                  CENTRE → range = ±L/2.
      • mountain rebucketed_rail / rack / funicular — same as train/metro
                                  (centred ±L/2), but with metro-style
                                  straight-line extrapolation on the missing
                                  side (mountain polylines are not pre-extended
                                  by `_extend_polylines_at_terminals`).
      • tram, bus               — GTFS coord is FRONT of stop → range
                                  = [coord - L, coord].

    Missing-range fill differs by mode:
      • train: handled UPSTREAM by `_extend_polylines_at_terminals` — the
        polyline is pre-extended at terminal stops along the OSM rail track
        (Fallback A's capped straight when no way matches), so the
        ±L/2 slice fits within the polyline. `end_of_platform=True` flips
        the anchoring to asymmetric (Fallback B): the polyline side absorbs
        the full L and no extrapolation is performed.
      • metro, mountain rail-like / funicular: straight-line tangent-direction
        extrapolation.
      • tram / bus / regional_bus: sibling-borrow first (passes through
        `siblings` as a list of (osm_id, polyline) tuples in the same
        `(ref, agency_id, mode)` group), straight-line tangent extrapolation
        as fallback.

    Mountain aerial returns None — those stops are fixed-dot in the pill
    pipeline and have no extent.
    """
    if len(polyline) < 2:
        return None
    L = _resolve_length(mode, atlas_length, cfg, mountain_origin=mountain_origin)
    if L is None:
        return None
    dists = _cum_dist_m(polyline)
    poly_max = dists[-1]
    if poly_max <= 0:
        return None
    t = _project_meters(stop_lon, stop_lat, polyline, dists)

    is_centred_extent = (
        mode in ("train", "metro")
        or (mode == "mountain" and mountain_origin in MOUNTAIN_EXTENT_ORIGINS)
    )
    if not is_centred_extent:
        # Tram / bus / regional_bus: backward-anchored range [t-L, t].
        if t >= L:
            # Polyline supports the full backward range — slice and return.
            return list(_slice_polyline(polyline, dists, t - L, t))

        # On-polyline portion: polyline start to snapped point (length t).
        on_slice = list(_slice_polyline(polyline, dists, 0.0, t))
        if len(on_slice) >= 2 and on_slice[0] == on_slice[-1]:
            on_slice = [on_slice[0]]

        # Anchor for both the borrow search and the straight-line extension
        # is the on-polyline extent's backward endpoint (poly[0]) — the point
        # at which the extension actually needs to begin. Its direction is the
        # first non-stub segment's tangent from poly[0], falling back to the
        # ±2 m averaged tangent at t when the first segment is a sub-metre
        # pfaedle stub.
        p = _interp_at(polyline, dists, t)
        target = on_slice[0] if on_slice else (polyline[0][0], polyline[0][1])
        anchor_tan = (_start_segment_tangent(polyline, dists)
                      or _directional_tangent_at(polyline, dists, t, window_m=2.0))
        if anchor_tan is None:
            return on_slice  # no usable tangent → can't fill
        anchor_dx, anchor_dy = anchor_tan

        if siblings:
            borrowed = _borrow_backward_segment(
                target[0], target[1],
                anchor_dx, anchor_dy, t, L, siblings, osm_id)
            if borrowed is not None:
                if len(on_slice) <= 1:
                    return borrowed
                return borrowed[:-1] + on_slice

        # Non-sibling borrow (step 2 in the concept's missing-range fill).
        # Widens the candidate set to every drawn line polyline within 2 m
        # of the anchor, excluding our own line and the same-(ref, agency_id,
        # mode) siblings already tried above.
        if _ALL_LINES_INDEX is not None and osm_id is not None:
            own_key = _ALL_LINES_INDEX.own_sib_key(osm_id)
            borrowed = _borrow_backward_nonsibling(
                target[0], target[1],
                anchor_dx, anchor_dy, t, L, osm_id, own_key)
            if borrowed is not None:
                if len(on_slice) <= 1:
                    return borrowed
                return borrowed[:-1] + on_slice

        # Straight-line tangent extrapolation backward using the same anchor
        # direction. The extension follows the actual arrival angle at the
        # polyline's starting vertex, not a chord smoothed across a curve at
        # the platform.
        missing_m = L - t
        extrap = (target[0] - anchor_dx * missing_m,
                  target[1] - anchor_dy * missing_m)
        if not on_slice:
            return [extrap, (p[0], p[1])]
        return [extrap] + on_slice

    if end_of_platform:
        # Fallback B (train only): polyline side absorbs full L; snap sits
        # at one end of the range. The polyline is NOT extended in this case,
        # so the range slices whatever pfaedle geometry is available on the
        # polyline side. Pick the side with more polyline as the anchor side.
        if poly_max - t >= t:
            t_start_ideal = t
            t_end_ideal = min(poly_max, t + L)
        else:
            t_start_ideal = max(0.0, t - L)
            t_end_ideal = t
        return list(_slice_polyline(polyline, dists, t_start_ideal, t_end_ideal))

    half_L = L / 2.0
    t_start_ideal = t - half_L
    t_end_ideal = t + half_L

    on_start = max(0.0, t_start_ideal)
    on_end = min(poly_max, t_end_ideal)
    slice_pts = list(_slice_polyline(polyline, dists, on_start, on_end))

    if mode == "train" or (
            mode == "mountain" and mountain_origin in MOUNTAIN_RAIL_ORIGINS):
        # Train and mountain rail-like (rebucketed_rail / rack) extents rely
        # on the polyline being pre-extended at terminals (OSM walk or capped
        # 50 m straight) by `_extend_polylines_at_terminals`. Don't
        # re-extrapolate here — the concept caps Fallback A at
        # osm_fallback_max_straight_m, so any remaining clip on the missing
        # side must stay clipped.
        return slice_pts

    if mode == "mountain" and mountain_origin == "funicular":
        # Funicular: clip to the polyline. No straight-line extrapolation;
        # when the centred ±L/2 extent would reach a polyline endpoint, use
        # Fallback B-style asymmetric anchoring (polyline side absorbs the
        # full L) so the extent stays within the line shape. The dot's snap
        # is pinned to the same endpoint via `_funicular_snap_override`.
        if t_end_ideal > poly_max:
            t_start = max(0.0, poly_max - L)
            t_end = poly_max
        elif t_start_ideal < 0:
            t_start = 0.0
            t_end = min(poly_max, L)
        else:
            return slice_pts
        return list(_slice_polyline(polyline, dists, t_start, t_end))

    # Metro: keep the symmetric straight-line extrapolation behaviour.
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


def _funicular_snap_override(stop_lon, stop_lat, polyline, atlas_length, cfg):
    """For funicular: when the centred ±L/2 extent would reach a polyline
    endpoint, return that endpoint so the dot's snap pins there instead of
    at the GTFS-coord projection. Returns None when the extent stays inside
    the polyline (regular snap_to_line is fine) or the polyline is degenerate.
    """
    if len(polyline) < 2:
        return None
    L = _resolve_length("mountain", atlas_length, cfg,
                         mountain_origin="funicular")
    if L is None or L <= 0:
        return None
    dists = _cum_dist_m(polyline)
    poly_max = dists[-1]
    if poly_max <= 0:
        return None
    t = _project_meters(stop_lon, stop_lat, polyline, dists)
    half_L = L / 2.0
    if t + half_L >= poly_max:
        return (polyline[-1][0], polyline[-1][1])
    if t - half_L <= 0:
        return (polyline[0][0], polyline[0][1])
    return None


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
        least one platform whose local extent tangent (averaged over
        TANGENT_WINDOW_M at the dot position) is 90° ±PERP_PLATFORM_TOL_DEG
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
    # one platform whose local extent tangent (TANGENT_WINDOW_M-averaged at
    # the dot position) is 90° ±PERP_PLATFORM_TOL_DEG from the gap direction,
    # the gap lies along a bar's perpendicular axis. Treat as in-line.
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
                # Local tangent at the dot position over a TANGENT_WINDOW_M
                # window — the full-extent chord can deviate several degrees
                # from the local direction on long curved approaches (Bern HB
                # western platforms), and the bar was placed using the local
                # tangent, so the perp test must use it too.
                tan = _stop_tangent(p)
                if tan is None:
                    continue
                tx_m = tan[0] * cos_lat
                ty_m = tan[1]
                tmag_m = sqrt(tx_m * tx_m + ty_m * ty_m)
                if tmag_m <= 0:
                    continue
                # |cos(angle to gap)| ≤ sin(tol)  ⇔  perpendicular ±tol.
                if abs(tx_m * gx_m + ty_m * gy_m) / tmag_m <= perp_sin_tol:
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
                           sibling_groups: dict, oid_sibling_key: dict,
                           end_of_platform_pairs: set | None = None) -> None:
    """Emit transit_debug_platforms.geojson — one LineString per stop tracing
    the platform's full allowed range along the line's polyline. Debug-only
    overlay; replaces the previous black-dot debug feature.
    """
    cfg = PILL_CFG
    if not cfg.get("default_length_m"):
        print("  No pill_rendering config — debug platforms skipped.")
        return
    eop = end_of_platform_pairs or set()
    feats = []
    for osm_id, ls_entry in line_stops.items():
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        line = line_lookup.get(osm_id)
        if not line:
            continue
        mode = line["mode"]
        mo = line.get("mountain_origin")
        if _length_key(mode, mo) not in cfg["default_length_m"]:
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
            is_eop = (str(osm_id), stop_id) in eop
            extent = _platform_extent(stop_lon, stop_lat, polyline,
                                       mode, atlas_length, cfg,
                                       osm_id=str(osm_id), siblings=siblings,
                                       end_of_platform=is_eop,
                                       mountain_origin=mo)
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
# Close-zoom stop design (z17+): pill-arrows + yellow station backdrop
# See .claude/concepts/close-zoom-stop-design.md
# =============================================================================

# First-draft seed values. Refine after visual review.
CLOSE_ZOOM_STACK_GAP_M         = 0.8    # polygon-edge gap; 0.4 m outside-border visible after the 0.4 m centered border
CLOSE_ZOOM_LINE_GAP_M          = 2.0    # clear gap between line and pill inner edge
CLOSE_ZOOM_DIR_CLUSTER_COS     = cos(radians(45.0))  # same-direction threshold
CLOSE_ZOOM_BACKDROP_PAD_M      = 8.0    # outward padding of the station hull
CLOSE_ZOOM_CURB_LATERAL_M      = 2.0    # same-curb: max lateral gap between stop position lines
CLOSE_ZOOM_CURB_MERGE_FRAC     = 0.30   # same-curb: overlap share above which stops merge
CLOSE_ZOOM_ARC_STEP_DEG        = 12.0   # hull corner rounding granularity
# Label sizing (glyph height in metres; the style converts to px per zoom).
# Uniform within a band — destinations are pre-wrapped at build time and get
# an ellipsis beyond the band's line budget. Wrapping measures real advance
# widths from glyph_widths.json (see gen_glyph_widths.py); the flat fallback
# below only applies when that table is missing.
CLOSE_ZOOM_CHAR_W_EM           = 0.60   # fallback avg glyph width (em)

GLYPH_WIDTHS_PATH = ROOT / "scripts" / "transit" / "glyph_widths.json"
try:
    _gw_raw = json.loads(GLYPH_WIDTHS_PATH.read_text())
    GLYPH_WIDTHS = _gw_raw.get("regular") or {}
    GLYPH_WIDTH_DEFAULT = float(_gw_raw.get("default_regular",
                                            CLOSE_ZOOM_CHAR_W_EM))
    del _gw_raw
except (FileNotFoundError, ValueError):
    GLYPH_WIDTHS = {}
    GLYPH_WIDTH_DEFAULT = CLOSE_ZOOM_CHAR_W_EM

# Zoom bands: each pill is emitted once per band with band-specific sizing;
# the style gates them by display zoom (A: z17, B: z18, C: z19+). Bands B
# and C both live in the z18 tiles (z19+ overzooms them), band A in the
# z15–17 tiles.
#
# The arrow does NOT grow across bands (all 10 m long): zooming in itself
# provides the extra pixels, which the higher bands spend on destination
# text (B: one line, C: two lines) while the glyph height in metres shrinks.
# Band A has no destination (font_dest_m None) — it renders as a solid pill
# in the line color with just the centered line number, no disc.
#   length_m / width_m — pill geometry
#   font_ref_m         — line-number glyph height
#   font_dest_m        — destination glyph height (None = number-only band)
#   max_lines          — destination wrap limit before the ellipsis
#   margin_disc_m      — text-region margin on the disc side
#   margin_tip_m       — text-region margin on the chevron side (negative =
#                        the text may extend past the neck into the tip base)
#   tipp_min/tipp_max  — tippecanoe zoom range for the band's features
# The margins encode "2 px more at the disc, 3–4 px less at the arrow" at
# each band's native zoom (z18: 1 px ≈ 0.20 m, z19: 1 px ≈ 0.10 m) on top
# of the previous ~0.2 m base inset.
CLOSE_ZOOM_BANDS = {
    "A": {"length_m": 10.0, "width_m": 5.0, "font_ref_m": 2.5,
          "font_dest_m": None, "max_lines": 0,
          "margin_disc_m": 0.0, "margin_tip_m": 0.0,
          "tipp_min": 15, "tipp_max": 17},
    "B": {"length_m": 10.0, "width_m": 3.6, "font_ref_m": 1.8,
          "font_dest_m": 1.12, "max_lines": 1,
          "margin_disc_m": 0.4, "margin_tip_m": -0.5,
          "tipp_min": 18, "tipp_max": 18},
    "C": {"length_m": 10.0, "width_m": 3.6, "font_ref_m": 1.6,
          "font_dest_m": 0.84, "max_lines": 2,
          "margin_disc_m": 0.3, "margin_tip_m": -0.15,
          "tipp_min": 18, "tipp_max": 18},
    "D": {"length_m": 10.0, "width_m": 3.6, "font_ref_m": 1.6,
          "font_dest_m": 0.63, "max_lines": 3,
          "margin_disc_m": 0.15, "margin_tip_m": -0.08,
          "tipp_min": 18, "tipp_max": 18},
    "E": {"length_m": 10.0, "width_m": 3.6, "font_ref_m": 1.6,
          "font_dest_m": 0.47, "max_lines": 4,
          "margin_disc_m": 0.08, "margin_tip_m": -0.04,
          "tipp_min": 18, "tipp_max": 18},
}
# Band whose geometry feeds the backdrop hull (largest, so it covers all).
CLOSE_ZOOM_HULL_BAND = "C"

# Modes handled by the rail-style placement (centered on platform axis).
CLOSE_ZOOM_RAIL_MODES = {"train"}
CLOSE_ZOOM_RAIL_MOUNTAIN_ORIGINS = {"rack", "rebucketed_rail"}

# Modes that get a close-zoom pill-arrow at all.
CLOSE_ZOOM_PILL_MODES = {"train", "tram", "metro", "bus", "regional_bus", "ferry"}

# Modes whose variant priority (representative pick + pill stacking order)
# is frequency rather than speed. Frequency is the better proxy for "the
# canonical variant" on road modes: a rare short-turn variant terminating
# mid-route must not out-rank the through variants.
CLOSE_ZOOM_FREQ_PRIORITY_MODES = {"tram", "bus", "regional_bus"}


def _variant_priority(v):
    """Sort value for variant representative selection and pill-arrow
    stacking: f_weighted (trips/h) for tram / bus / regional_bus,
    speed_kmh for rail-like modes."""
    if v.get("mode") in CLOSE_ZOOM_FREQ_PRIORITY_MODES:
        return v.get("f_weighted") or 0.0
    return v.get("speed_kmh") or 0.0


def _local_offset_to_lonlat(cx, cy, dx_m, dy_m, cos_lat_cached=None):
    if cos_lat_cached is None:
        cos_lat_cached = cos(radians(cy))
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * cos_lat_cached
    if m_per_deg_lon <= 0.0:
        m_per_deg_lon = 111320.0
    return (cx + dx_m / m_per_deg_lon, cy + dy_m / m_per_deg_lat)


def _point_at_extrap(polyline, dists, t):
    """Point at arc position t (metres), extrapolating straight beyond the
    polyline ends using the end tangents. Returns (lon, lat)."""
    poly_max = dists[-1]
    if 0.0 <= t <= poly_max:
        return _interp_at(polyline, dists, t)
    if t < 0.0:
        tan = _directional_tangent_at(polyline, dists, 0.0)
        if tan is None:
            return (polyline[0][0], polyline[0][1])
        return (polyline[0][0] + tan[0] * t, polyline[0][1] + tan[1] * t)
    tan = _directional_tangent_at(polyline, dists, poly_max)
    if tan is None:
        return (polyline[-1][0], polyline[-1][1])
    ex = t - poly_max
    return (polyline[-1][0] + tan[0] * ex, polyline[-1][1] + tan[1] * ex)


def _unit_tangent_metric(polyline, dists, t, cos_lat):
    """Unit tangent at arc position t (clamped to the polyline range) as a
    metric (east, north) vector. None for degenerate geometry."""
    t = min(max(t, 0.0), dists[-1])
    tan = _directional_tangent_at(polyline, dists, t, window_m=20.0)
    if tan is None:
        return None
    dx = tan[0] * 111320.0 * cos_lat
    dy = tan[1] * 111320.0
    n = sqrt(dx * dx + dy * dy)
    if n <= 0.0:
        return None
    return (dx / n, dy / n)


def _sample_ts(dists, t0, t1, max_step_m=3.0):
    """Ordered arc positions from t0 to t1 (t0 < t1): endpoints, interior
    polyline vertices, plus subdivisions so no gap exceeds max_step_m."""
    ts = [t0]
    for d in dists:
        if t0 < d < t1:
            ts.append(d)
    ts.append(t1)
    out = [ts[0]]
    for a, b in zip(ts, ts[1:]):
        n = int((b - a) // max_step_m)
        for i in range(1, n + 1):
            out.append(a + (b - a) * i / (n + 1))
        out.append(b)
    return out


def _unit_chord_metric(A, B, cos_lat):
    """Unit metric direction from point A to point B (lon/lat pairs), or
    None if degenerate."""
    dxm = (B[0] - A[0]) * 111320.0 * cos_lat
    dym = (B[1] - A[1]) * 111320.0
    nrm = sqrt(dxm * dxm + dym * dym)
    if nrm < 1e-9:
        return None
    return (dxm / nrm, dym / nrm)


def _stop_course(extent, cos_lat, back_m, fwd_m, chord_w=10.0):
    """Queue course for a pill-arrow stack: the stop position line `extent`
    extended DEAD STRAIGHT at both ends (rear by back_m, front by fwd_m
    metres) along the average direction (chord) of the extent's first /
    last chord_w metres. Pills whose span lies inside the extent thus
    derive their angle from the stop position line at their own segment;
    pills beyond it continue straight in the direction of the last pills
    that fit.

    NOTHING but the stop position line determines the placement — the raw
    GTFS stop coordinate is never consulted here.

    Orientation: the extent's own point order, always. Every non-rail
    extent ends at the stop position by construction (backward-anchored
    [t-L, t] slices and borrowed fills alike), so the front end is the
    stop end with no travel-direction guessing. A previous version
    reversed the order when it opposed the group's ±20 m travel tangent;
    that misfired at stops where the vehicle turns right after departing
    (Herrliberg Bhf West — tangent skewed ~north by the turn, course
    flipped, tip anchored at the wrong end) and never produced a better
    outcome in the cases it was meant for.

    Returns (course_pts, course_dists, t_front, t_mid) or None if
    degenerate. t_front is the stop position line's forward end in course
    arc coordinates — where the lead pill's chevron tip anchors (the
    vehicle pulled fully forward); t_mid is the line's middle — the rail
    stack center."""
    pts = [tuple(p) for p in extent]
    if len(pts) < 2:
        return None
    d = _cum_dist_m(pts)
    if d[-1] <= 0.0:
        return None
    t_front = back_m + d[-1]
    t_mid = back_m + d[-1] / 2.0
    w = min(chord_w, d[-1])
    T0 = _unit_chord_metric(pts[0], _interp_at(pts, d, w), cos_lat)
    T1 = _unit_chord_metric(_interp_at(pts, d, d[-1] - w), pts[-1], cos_lat)
    if T0 is None or T1 is None:
        return None
    rear = _local_offset_to_lonlat(pts[0][0], pts[0][1],
                                   -back_m * T0[0], -back_m * T0[1], cos_lat)
    front = _local_offset_to_lonlat(pts[-1][0], pts[-1][1],
                                    fwd_m * T1[0], fwd_m * T1[1], cos_lat)
    course = [tuple(rear)] + pts + [tuple(front)]
    return course, _cum_dist_m(course), t_front, t_mid


def _slice_polyline(pts, dists, t0, t1):
    """Sub-polyline of `pts` between arc positions t0 < t1 (interpolated
    endpoints, interior vertices kept)."""
    out = [tuple(_interp_at(pts, dists, t0))]
    for p, t in zip(pts, dists):
        if t0 < t < t1:
            out.append(tuple(p))
    out.append(tuple(_interp_at(pts, dists, t1)))
    return out


def _extent_overlap(extA, extB, cos_lat):
    """Overlap metrics between two stop position lines. Returns
    (fraction, lateral_m, ivA, ivB) — the shared length as a fraction of
    the SHORTER line, the perpendicular separation sampled at the overlap
    middle, and the overlap interval on each line's own arc — or None when
    the lines don't overlap along their course."""
    dA = _cum_dist_m(extA)
    dB = _cum_dist_m(extB)
    if dA[-1] <= 0.0 or dB[-1] <= 0.0:
        return None
    tb = [_project_meters(p[0], p[1], extA, dA) for p in (extB[0], extB[-1])]
    lo_A, hi_A = max(0.0, min(tb)), min(dA[-1], max(tb))
    if hi_A <= lo_A:
        return None
    ta = [_project_meters(p[0], p[1], extB, dB) for p in (extA[0], extA[-1])]
    lo_B, hi_B = max(0.0, min(ta)), min(dB[-1], max(ta))
    if hi_B <= lo_B:
        return None
    overlap = max(hi_A - lo_A, hi_B - lo_B)
    frac = overlap / min(dA[-1], dB[-1])
    Pm = _interp_at(extA, dA, (lo_A + hi_A) / 2.0)
    Q = _interp_at(extB, dB, _project_meters(Pm[0], Pm[1], extB, dB))
    dxm = (Q[0] - Pm[0]) * 111320.0 * cos_lat
    dym = (Q[1] - Pm[1]) * 111320.0
    lateral = sqrt(dxm * dxm + dym * dym)
    return frac, lateral, (lo_A, hi_A), (lo_B, hi_B)


def _union_extents(exts, cos_lat, chord_w=10.0):
    """Union stop position line for merged same-curb stops: the longest
    member line, extended straight along its end chords far enough to
    cover every other member's endpoints (all members run within ~2 m of
    each other, so projecting is safe)."""
    base = max(exts, key=lambda e: _cum_dist_m(e)[-1])
    d = _cum_dist_m(base)
    w = min(chord_w, d[-1])
    T0 = _unit_chord_metric(base[0], _interp_at(base, d, w), cos_lat)
    T1 = _unit_chord_metric(_interp_at(base, d, d[-1] - w), base[-1],
                            cos_lat)
    if T0 is None or T1 is None:
        return [tuple(p) for p in base]
    back = fwd = 0.0
    for e in exts:
        if e is base:
            continue
        for p in (e[0], e[-1]):
            dxm = (p[0] - base[0][0]) * 111320.0 * cos_lat
            dym = (p[1] - base[0][1]) * 111320.0
            s = dxm * T0[0] + dym * T0[1]
            if s < 0.0:
                back = max(back, -s)
            dxm = (p[0] - base[-1][0]) * 111320.0 * cos_lat
            dym = (p[1] - base[-1][1]) * 111320.0
            s = dxm * T1[0] + dym * T1[1]
            if s > 0.0:
                fwd = max(fwd, s)
    pts = [tuple(p) for p in base]
    if back > 0.0:
        pts.insert(0, tuple(_local_offset_to_lonlat(
            base[0][0], base[0][1], -back * T0[0], -back * T0[1], cos_lat)))
    if fwd > 0.0:
        pts.append(tuple(_local_offset_to_lonlat(
            base[-1][0], base[-1][1], fwd * T1[0], fwd * T1[1], cos_lat)))
    return pts


def _shorten_curb(rec, iv, min_len_m=2.0):
    """Shorten a same-curb rec's stop position line to the middle of the
    overlapping section `iv` (arc interval on the rec's own line), keeping
    the side away from the overlap so the two neighbours end up just
    touching. Records the cut point so the band loop can keep the queue
    from crossing it. No-op when the remainder would fall below
    min_len_m."""
    ext = rec["ext"]
    d = _cum_dist_m(ext)
    lo, hi = iv
    mid = (lo + hi) / 2.0
    if mid > d[-1] / 2.0:
        t0, t1 = 0.0, mid
    else:
        t0, t1 = mid, d[-1]
    if t1 - t0 < min_len_m:
        return
    rec["cut_pts"].append(tuple(_interp_at(ext, d, mid)))
    rec["ext"] = _slice_polyline(ext, d, t0, t1)


def _offset_track(polyline, dists, t_lo, t_hi, offset_m, cos_lat,
                  step_m=2.0):
    """Placement track for pill-arrows: the centerline sampled over arc
    range [t_lo, t_hi] (extrapolated past the ends) and shifted sideways
    by offset_m (positive = right of travel). Returns (pts, odists, cts)
    — the track polyline, its cumulative metre distances, and each
    sample's centerline arc position — or None if degenerate."""
    span = t_hi - t_lo
    if span <= 0.0:
        return None
    n = max(int(span / step_m) + 2, 2)
    pts, cts = [], []
    for i in range(n):
        t = t_lo + span * i / (n - 1)
        T = _unit_tangent_metric(polyline, dists, t, cos_lat)
        if T is None:
            continue
        px, py = _point_at_extrap(polyline, dists, t)
        pts.append(_local_offset_to_lonlat(
            px, py, offset_m * T[1], -offset_m * T[0], cos_lat))
        cts.append(t)
    if len(pts) < 2:
        return None
    return pts, _cum_dist_m(pts), cts


def _track_pos(x, xs, ys):
    """Piecewise-linear map of x through the monotonic table xs → ys,
    clamped at the ends. Converts centerline arc positions to track
    positions and back (pass (cts, odists) or (odists, cts))."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect_left(xs, x)
    x0, x1 = xs[i - 1], xs[i]
    if x1 <= x0:
        return ys[i]
    f = (x - x0) / (x1 - x0)
    return ys[i - 1] + f * (ys[i] - ys[i - 1])


def _build_straight_pill_arrow(origin, T, N, cos_lat, x_rear, x_neck,
                                perp_m, width_m, n_arc=16):
    """Straight pill-arrow in the local frame spanned by the unit tangent T
    (direction of travel) and right normal N at `origin` (lon, lat).

    The body spans frame positions x_rear..x_neck (metres along T) at
    lateral offset perp_m (positive = right of travel). A chevron tip of
    length width_m/2 extends beyond x_neck; a rounded cap closes x_rear.
    The whole shape shares ONE axis — no bending, so labels rotated by the
    same tangent align exactly with the body.

    Returns (ring, rear_center) — the closed polygon ring and the disc
    anchor at the body's rear centerline point — or None.
    """
    R = width_m / 2.0
    if x_neck <= x_rear:
        return None
    ox, oy = origin

    def pt(dx, dy):
        return _local_offset_to_lonlat(
            ox, oy, dx * T[0] + dy * N[0], dx * T[1] + dy * N[1], cos_lat)

    apex = pt(x_neck + R, perp_m)
    ring = [list(apex)]
    ring.append(list(pt(x_neck, perp_m - R)))
    ring.append(list(pt(x_rear, perp_m - R)))
    # Rounded back cap: half-circle from the inner edge over the back pole
    # to the outer edge.
    for i in range(1, n_arc):
        theta = pi * i / n_arc
        ring.append(list(pt(x_rear - R * sin(theta),
                            perp_m - R * cos(theta))))
    ring.append(list(pt(x_rear, perp_m + R)))
    ring.append(list(pt(x_neck, perp_m + R)))
    ring.append(list(apex))

    return ring, pt(x_rear, perp_m)


def _convex_hull_metric(pts):
    """Convex hull (Andrew monotone chain) of (x, y) metric points, in
    counter-clockwise order without the closing point."""
    pts = sorted(set(pts))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _rounded_hull_polygon(lonlat_pts, pad_m, arc_step_deg=12.0):
    """Rounded envelope around a point cloud: convex hull offset outward by
    pad_m, with circular arcs at the corners. The outline is convex
    everywhere — it never notches inward between the covered elements.
    Returns a closed [lon, lat] ring, or None for an empty/degenerate cloud.
    """
    if not lonlat_pts:
        return None
    lon0, lat0 = lonlat_pts[0]
    cl = cos(radians(lat0))
    if cl <= 0.0:
        cl = 1.0
    # To metric coords, deduped on a 0.5 m grid to keep the hull cheap.
    seen = set()
    metric = []
    for lon, lat in lonlat_pts:
        x = (lon - lon0) * 111320.0 * cl
        y = (lat - lat0) * 111320.0
        key = (round(x * 2.0), round(y * 2.0))
        if key in seen:
            continue
        seen.add(key)
        metric.append((x, y))
    hull = _convex_hull_metric(metric)
    if not hull:
        return None

    step = radians(arc_step_deg)
    out = []

    def _arc(cx, cy, a1, a2):
        """Arc points from angle a1 to a2 going counter-clockwise."""
        sweep = (a2 - a1) % (2.0 * pi)
        n = max(1, int(sweep / step))
        for j in range(n + 1):
            a = a1 + sweep * j / n
            out.append((cx + pad_m * cos(a), cy + pad_m * sin(a)))

    if len(hull) == 1:
        _arc(hull[0][0], hull[0][1], 0.0, 2.0 * pi - 1e-9)
    elif len(hull) == 2:
        # Capsule around the two points.
        (x1, y1), (x2, y2) = hull
        base = atan2(y2 - y1, x2 - x1)
        _arc(x2, y2, base - pi / 2.0, base + pi / 2.0)
        _arc(x1, y1, base + pi / 2.0, base + 3.0 * pi / 2.0)
    else:
        m_ = len(hull)

        def _norm_out(a, b):
            dx, dy = b[0] - a[0], b[1] - a[1]
            l = sqrt(dx * dx + dy * dy)
            if l <= 0.0:
                return None
            # CCW polygon → outward normal is right of the edge direction.
            return (dy / l, -dx / l)

        for i in range(m_):
            p_prev = hull[(i - 1) % m_]
            p = hull[i]
            p_next = hull[(i + 1) % m_]
            n_in = _norm_out(p_prev, p)
            n_out = _norm_out(p, p_next)
            if n_in is None or n_out is None:
                continue
            _arc(p[0], p[1], atan2(n_in[1], n_in[0]), atan2(n_out[1], n_out[0]))

    if len(out) < 3:
        return None
    ring = [list(_local_offset_to_lonlat(lon0, lat0, x, y, cl))
            for (x, y) in out]
    ring.append(ring[0])
    return ring


def _shorten_destination(dest: str, current_stop_name: str) -> str:
    """Destination shortening for pill-arrow labels.

    1. If the destination begins with the current stop's city — comma- or
       space-separated ("Bern, …" or "Bern …" on a pill in Bern) — strip the
       city prefix. The city is the part of the current stop's name before
       its first comma. The separator requirement keeps "Berneck" intact.
    2. If (afterwards) a comma remains, keep only the part before it
       ("Wabern, Tram-Endstation" → "Wabern").
    """
    if not dest:
        return dest
    city = (current_stop_name or "").split(",")[0].strip()
    d = dest.strip()
    if city:
        low_d, low_c = d.lower(), city.lower()
        if low_d.startswith(low_c + ",") or low_d.startswith(low_c + " "):
            d = d[len(city) + 1:].strip()
    if "," in d:
        d = d.split(",")[0].strip()
    return d or dest


def _text_width_em(s: str) -> float:
    """Width of `s` in ems, from the baked Noto Sans advance widths
    (kerning ignored). Falls back to a flat average per character when
    glyph_widths.json is absent."""
    return sum(GLYPH_WIDTHS.get(ch, GLYPH_WIDTH_DEFAULT) for ch in s)


def _wrap_label(text: str, max_w_em: float, max_lines: int) -> str:
    """Greedy-wrap `text` into at most `max_lines` lines of at most
    `max_w_em` measured ems, with baked "\\n" breaks (MapLibre honours
    them). Words wider than a line are shortened with a single abbreviation
    dot (no hyphen splitting — without linguistic hyphenation the break
    positions would be nonsense).

    The line breaks are computed FIRST; only then, if lines remain beyond
    the budget, the last kept line is trimmed until it fits with a trailing
    ellipsis — so the ellipsis lands at the true end of the last line, not
    at a character count guessed without knowing the break position."""
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        cand = (cur + " " + w) if cur else w
        if _text_width_em(cand) <= max_w_em:
            cur = cand
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if _text_width_em(w) <= max_w_em:
            cur = w
        else:
            # Single word wider than a line → abbreviate with a dot.
            cut = len(w)
            while cut > 1 and _text_width_em(w[:cut] + ".") > max_w_em:
                cut -= 1
            lines.append(w[:cut] + ".")
    if cur:
        lines.append(cur)
    if len(lines) <= max_lines:
        return "\n".join(lines)
    kept = lines[:max_lines]
    last = kept[-1]
    while last and _text_width_em(last + "…") > max_w_em:
        last = last[:-1].rstrip()
    last = last.rstrip(" .")
    kept[-1] = (last + "…") if last else "…"
    return "\n".join(kept)


def _blend_colors(hex_colors) -> str:
    """Mean-RGB blend of a list of #rrggbb colors."""
    rs = gs = bs = 0
    n = 0
    for h in hex_colors:
        h = (h or "").lstrip("#")
        if len(h) != 6:
            continue
        try:
            rs += int(h[0:2], 16)
            gs += int(h[2:4], 16)
            bs += int(h[4:6], 16)
            n += 1
        except ValueError:
            continue
    if n == 0:
        return "#ffe566"
    return "#%02x%02x%02x" % (rs // n, gs // n, bs // n)


def write_close_zoom_features(line_stops: dict, line_lookup: dict,
                                stop_meta: dict, stop_attrs: dict,
                                sibling_groups: dict, oid_sibling_key: dict,
                                end_of_platform_pairs: set,
                                skip_first_oids: set,
                                skip_last_oids: set) -> None:
    """Emit transit_close_zoom.geojson — pill-arrow polygons and backdrop
    line-segments that together produce the close-zoom (z17+) station
    representation. See .claude/concepts/close-zoom-stop-design.md.

    Pill-arrows are straight polygons queued along the stop's position line
    (the same fitted-to-the-line extent the debug platform lines draw),
    extended dead straight beyond its range. One pill-arrow per (stop, line
    ref, agency, direction) — same-direction variants collapse into a
    single pill listing every destination.

    The backdrop is one rounded convex-hull polygon per GTFS parent station,
    covering every pill-arrow and the line sections next to them plus a
    fixed outward padding — a single envelope shape with no overlaps and no
    inward notches.

    Every emitted feature carries `feature_type` ∈ {"pill_arrow",
    "pill_disc", "pill_ref", "pill_dest", "backdrop"} to gate style layers.
    Label features carry `font_m` (glyph height in metres, pre-shrunk so the
    text fits its region) and `text_rot` (map-space rotation, flipped when it
    would render upside-down).
    """
    # Per-line destination display names.
    line_dest = {}
    for oid, entry in line_stops.items():
        triplets = entry.get("stops", []) if isinstance(entry, dict) else entry
        if not triplets or len(triplets[-1]) < 3:
            continue
        last_sid = triplets[-1][2]
        line_dest[str(oid)] = stop_meta.get(last_sid, {}).get("name", "")

    # Loop-line apexes (close-zoom-stop-design.md § Text): when first and
    # last stop share a UIC, "to <terminus>" is useless at the terminus
    # itself (Bad Zurzach buses 1-4 all showing "Bahnhof" at Bahnhof).
    # Stops before the apex — the stop geographically furthest from the
    # terminus — show the apex as destination instead; the apex and every
    # later stop keep the terminus. Stops sharing the terminus UIC are
    # never apex candidates: loops may pass through the terminus mid-route
    # (Bad Zurzach bus 4), and picking that call would relabel the whole
    # outbound leg with the terminus name.
    line_loop_apex = {}   # osm_id → (apex_idx, apex_name)
    for oid, entry in line_stops.items():
        triplets = entry.get("stops", []) if isinstance(entry, dict) else entry
        n = len(triplets)
        if n < 3 or len(triplets[0]) < 3 or len(triplets[-1]) < 3:
            continue
        first_sid, last_sid = triplets[0][2], triplets[-1][2]
        if not first_sid or not last_sid:
            continue
        term_uic = first_sid.split(":")[0]
        if term_uic != last_sid.split(":")[0]:
            continue
        t_lon, t_lat = triplets[0][0], triplets[0][1]
        cos_lat = cos(radians(t_lat))
        if cos_lat <= 0.0:
            cos_lat = 1.0
        best = None
        for i in range(1, n - 1):
            sid = triplets[i][2] if len(triplets[i]) >= 3 else ""
            if not sid or sid.split(":")[0] == term_uic:
                continue
            dx = (triplets[i][0] - t_lon) * 111320.0 * cos_lat
            dy = (triplets[i][1] - t_lat) * 111320.0
            d2 = dx * dx + dy * dy
            if best is None or d2 > best[0]:
                best = (d2, i, sid)
        if best is None:
            continue
        apex_name = stop_meta.get(best[2], {}).get("name", "")
        if apex_name:
            line_loop_apex[str(oid)] = (best[1], apex_name)

    # ── Stop position lines ──────────────────────────────────────────────
    # Re-used from the stop/dot placement: the same fitted-to-the-line
    # extents the debug platform lines draw, under the same skip rules.
    # The skips are what make this find the right line automatically — at
    # a terminal the departure-side entry is skipped (skip_first) and the
    # ARRIVAL line's extent survives; its geometry approaches along the
    # street and ends at the stop, so the slice covers exactly the ground
    # behind the stop where the departing queue stands. Never extrapolated.
    stop_lines: dict = defaultdict(list)
    for osm_id_raw, ls_entry in line_stops.items():
        osm_id = str(osm_id_raw)
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        line = line_lookup.get(osm_id_raw) or line_lookup.get(osm_id)
        if not line or not triplets:
            continue
        mode = line["mode"]
        mo = line.get("mountain_origin")
        if _length_key(mode, mo) not in PILL_CFG.get("default_length_m", {}):
            continue
        polyline = flatten_coords(line["coords"])
        if len(polyline) < 2:
            continue
        skip_first_here = osm_id in skip_first_oids
        skip_last_here = osm_id in skip_last_oids
        sib_key = oid_sibling_key.get(osm_id)
        siblings = sibling_groups.get(sib_key, []) if sib_key else []
        last_idx = len(triplets) - 1
        for idx, trip in enumerate(triplets):
            if idx == 0 and skip_first_here:
                continue
            if idx == last_idx and skip_last_here:
                continue
            if len(trip) < 3 or not trip[2]:
                continue
            stop_lon, stop_lat, sid = trip[0], trip[1], trip[2]
            atlas_len = (stop_attrs.get(sid, {}) or {}).get("length")
            ext = _platform_extent(
                stop_lon, stop_lat, polyline, mode, atlas_len, PILL_CFG,
                osm_id=osm_id, siblings=siblings,
                end_of_platform=(osm_id, sid) in end_of_platform_pairs,
                mountain_origin=mo)
            if ext is None or len(ext) < 2:
                continue
            stop_lines[sid].append(
                {"osm_id": osm_id, "mode": mode,
                 "ref": line.get("ref", ""),
                 "agency_id": line.get("agency_id", ""),
                 "extent": ext})

    # ── Collect visits per stop_id ───────────────────────────────────────
    per_stop_visits: dict = defaultdict(list)
    for osm_id_raw, ls_entry in line_stops.items():
        osm_id = str(osm_id_raw)
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        line = line_lookup.get(osm_id_raw) or line_lookup.get(osm_id)
        if not line:
            continue
        mode = line["mode"]
        mo = line.get("mountain_origin")
        if mode not in CLOSE_ZOOM_PILL_MODES and not (
                mode == "mountain" and mo in CLOSE_ZOOM_RAIL_MOUNTAIN_ORIGINS):
            continue
        polyline = flatten_coords(line["coords"])
        if len(polyline) < 2:
            continue
        dists = _cum_dist_m(polyline)
        if dists[-1] <= 0:
            continue
        # Layover-departure dedup — the departure-side mirror of the
        # pill/dot arrival-drop rule 2 (see compute_terminus_skip_oids):
        # the feature's FIRST stop is skipped when it has no platform_code
        # and the same feature calls again at the same UIC at a
        # platform-coded stop later (non-final, so the revisit itself gets
        # the pill). Canonical case: Bern bus 30 departs the bare :10001
        # layover, then serves platform :A of the same station — without
        # the skip the line shows twice at the station. NOT copied from
        # the dot side: skip_first_oids, which drops the departure whenever
        # any sibling ARRIVES at the same stop_id — fine for dots (the
        # arrival dot survives) but fatal here, where arrivals get no pill
        # and the departure pill is the line's only presence at a terminus.
        skip_first_layover = False
        first_sid = triplets[0][2] if len(triplets[0]) >= 3 else ""
        if first_sid and not (stop_meta.get(first_sid, {})
                              or {}).get("platform_code"):
            first_uic = first_sid.split(":")[0]
            for later in triplets[1:-1]:
                l_sid = later[2] if len(later) >= 3 else ""
                if (l_sid and l_sid.split(":")[0] == first_uic
                        and (stop_meta.get(l_sid, {})
                             or {}).get("platform_code")):
                    skip_first_layover = True
                    break

        last_idx = len(triplets) - 1
        for idx, trip in enumerate(triplets):
            if len(trip) < 3:
                continue
            # Departures only: the line's last stop is an arrival — no pill
            # there ("17 to Bern, Bahnhof" at Bern, Bahnhof makes no sense).
            if idx == last_idx:
                continue
            if idx == 0 and skip_first_layover:
                continue
            stop_lon, stop_lat, sid = trip[0], trip[1], trip[2]
            if not sid:
                continue
            cos_lat = cos(radians(stop_lat))
            if cos_lat <= 0.0:
                cos_lat = 1.0
            t_stop = _project_meters(stop_lon, stop_lat, polyline, dists)
            T = _unit_tangent_metric(polyline, dists, t_stop, cos_lat)
            if T is None:
                continue
            N = (T[1], -T[0])  # right normal in direction of travel
            # Signed lateral offset of the raw GTFS coord from the line:
            # positive = stop sits right of the line in direction of travel.
            sx, sy = _interp_at(polyline, dists, t_stop)
            dxm = (stop_lon - sx) * 111320.0 * cos_lat
            dym = (stop_lat - sy) * 111320.0
            signed_d = dxm * N[0] + dym * N[1]
            is_rail_like = (
                mode in CLOSE_ZOOM_RAIL_MODES
                or (mode == "mountain" and mo in CLOSE_ZOOM_RAIL_MOUNTAIN_ORIGINS)
            )
            # Full platform extent along the line (atlas length; same logic
            # as the debug platform overlay) — feeds the backdrop hull so
            # the yellow area covers the whole platform, not just the
            # pill-arrow span.
            extent = None
            if is_rail_like:
                atlas_len = (stop_attrs.get(sid, {}) or {}).get("length")
                sib_key = oid_sibling_key.get(osm_id)
                siblings = sibling_groups.get(sib_key, []) if sib_key else []
                extent = _platform_extent(
                    stop_lon, stop_lat, polyline, mode, atlas_len, PILL_CFG,
                    osm_id=osm_id, siblings=siblings,
                    end_of_platform=(osm_id, sid) in end_of_platform_pairs,
                    mountain_origin=mo)
            per_stop_visits[sid].append({
                "sid":             sid,
                "osm_id":          osm_id,
                "mode":            mode,
                "mountain_origin": mo,
                "color":           line["color"],
                "ref":             line.get("ref", ""),
                "agency_id":       line.get("agency_id", ""),
                "speed_kmh":       line.get("speed_kmh") or 0.0,
                "f_weighted":      line.get("f_weighted") or 0.0,
                "polyline":        polyline,
                "dists":           dists,
                "t_stop":          t_stop,
                "stop_lon":        stop_lon,
                "stop_lat":        stop_lat,
                "cos_lat":         cos_lat,
                "tangent":         T,
                "signed_d":        signed_d,
                "is_rail_like":    is_rail_like,
                "extent":          extent,
                "destination":     _shorten_destination(
                    (line_loop_apex[osm_id][1]
                     if osm_id in line_loop_apex
                     and idx < line_loop_apex[osm_id][0]
                     else line_dest.get(osm_id, "")),
                    stop_meta.get(sid, {}).get("name", "")),
            })

    features = []
    # Point cloud per parent station; hulled into the backdrop afterwards.
    parent_cloud: dict = defaultdict(list)
    # Distinct line colors per parent — the backdrop takes the single line
    # color, or a blend when several lines with different colors call.
    parent_colors: dict = defaultdict(set)

    def _parent_of(sid):
        return stop_meta.get(sid, {}).get("parent") or sid.split(":")[0] or sid

    max_L = max(bc["length_m"] for bc in CLOSE_ZOOM_BANDS.values())
    max_step = max(bc["length_m"] + CLOSE_ZOOM_STACK_GAP_M
                   for bc in CLOSE_ZOOM_BANDS.values())

    def _rightmost(group):
        """The group's rightmost cluster — the one whose on-line stop
        position sits furthest right (in direction of travel) in the frame
        of the group's first cluster."""
        g0 = group[0]
        N0 = (g0["tangent"][1], -g0["tangent"][0])
        P0 = _interp_at(g0["polyline"], g0["dists"], g0["t_stop"])
        path = g0
        best_off = float("-inf")
        for c in group:
            Pj = _interp_at(c["polyline"], c["dists"], c["t_stop"])
            dxm = (Pj[0] - P0[0]) * 111320.0 * g0["cos_lat"]
            dym = (Pj[1] - P0[1]) * 111320.0
            off = dxm * N0[0] + dym * N0[1]
            if off > best_off:
                best_off = off
                path = c
        return path

    def _build_group_recs(pool_sids):
        """Per track (list of stop_ids pooled into one queue for rail
        platform-sector merge; a one-item list for every other case):
        variant collapse → direction groups → recs carrying the group's
        chosen path and its stop position line.

        Rail platform-sector merge (close-zoom-stop-design.md § Rail
        platform-sector merge): stop_ids at one parent whose platform_code
        shares the same numeric leading-digit run refer to one physical
        track; their visits pool into one queue on the full platform
        extent. The rep sid — used for atlas length / extent lookups — is
        the pooled sid with the longest atlas length, so the queue rides
        the full platform, not a sector's sub-extent.

        Rail direction ordering (close-zoom-stop-design.md § Rail): all
        rail clusters at one track form ONE stack. Same-direction pills
        stay contiguous, and opposite-direction sub-groups sit at
        opposite ends of the stack with their fastest line at the
        outward-most position, so no two adjacent pills point at each
        other and each sub-group's chevrons point outward from the
        platform middle."""
        visits = []
        for s in pool_sids:
            visits.extend(per_stop_visits.get(s, []))
        if not visits:
            return []
        # Rep sid for extent / atlas-length lookups. For a rail pool, the
        # longest atlas length is the full-platform stop_id (a "7" sid
        # over a "7A-C" sid); solo sids pick themselves.
        if len(pool_sids) == 1:
            rep_sid = pool_sids[0]
        else:
            rep_sid = max(
                pool_sids,
                key=lambda s: float(
                    (stop_attrs.get(s, {}) or {}).get("length") or 0.0))

        # ── Collapse variants: one pill per (ref, agency, direction) ────
        # Same line number + agency in the same direction of travel
        # (tangent dot product within 45°) merges into one pill;
        # destinations are collected across the merged variants, highest-
        # priority variant first. Priority is frequency (f_weighted) for
        # tram / bus / regional_bus — a rare short-turn variant terminating
        # mid-route must not out-rank the through variants and hijack the
        # pill's geometry (Sevgein) — and speed for rail-like modes.
        visits.sort(key=lambda v: (-_variant_priority(v), v["osm_id"]))
        clusters = []
        for v in visits:
            merged = False
            for c in clusters:
                if c["ref"] != v["ref"] or c["agency_id"] != v["agency_id"]:
                    continue
                dot = (c["tangent"][0] * v["tangent"][0]
                       + c["tangent"][1] * v["tangent"][1])
                if dot >= CLOSE_ZOOM_DIR_CLUSTER_COS:
                    if v["destination"] and v["destination"] not in c["destinations"]:
                        c["destinations"].append(v["destination"])
                    merged = True
                    break
            if not merged:
                c = dict(v)
                c["destinations"] = [v["destination"]] if v["destination"] else []
                c["dir_forward"] = True
                clusters.append(c)

        # ── Direction groups ────────────────────────────────────────────
        # Non-rail: clusters heading the same way (tangent dot within 45°)
        # share a stack and, crucially, one path — when parallel lines
        # (e.g. tram + bus on the same street) serve the same stop, every
        # pill-arrow in the group follows the RIGHTMOST line so they line
        # up. Opposite directions form their own stack on the other side.
        #
        # Rail: ALL clusters at one track form ONE stack — opposite
        # directions do not form a separate group. cluster[0] (fastest
        # overall) defines the "forward" direction. Forward clusters
        # queue fastest→slowest from the forward end; reverse clusters
        # queue slowest→fastest from the same end, so at the boundary the
        # two sub-groups meet round-cap-to-round-cap and each sub-group's
        # fastest sits at the outward-most position with its chevron
        # pointing out. The dir_forward flag is read later by the pill
        # build loop, which flips T for reverse pills so their chevrons
        # actually point backward.
        rail_pool = clusters and clusters[0]["is_rail_like"]
        if rail_pool:
            fwd_ref = clusters[0]["tangent"]
            forwards, reverses = [], []
            for c in clusters:
                dot = (fwd_ref[0] * c["tangent"][0]
                       + fwd_ref[1] * c["tangent"][1])
                if dot >= 0.0:
                    c["dir_forward"] = True
                    forwards.append(c)
                else:
                    c["dir_forward"] = False
                    reverses.append(c)
            # forwards already sorted highest-priority-first by the visits
            # sort; reverses need lowest-first so their highest-priority
            # pill lands at the outward (backward) end of the stack.
            groups = [forwards + list(reversed(reverses))]
        else:
            groups = []
            for c in clusters:
                placed = False
                for g in groups:
                    dot = (g[0]["tangent"][0] * c["tangent"][0]
                           + g[0]["tangent"][1] * c["tangent"][1])
                    if dot >= CLOSE_ZOOM_DIR_CLUSTER_COS:
                        g.append(c)
                        placed = True
                        break
                if not placed:
                    groups.append([c])

        recs = []
        for group in groups:
            if not group:
                continue
            # Rail: path is the fastest forward cluster (cluster[0]) — its
            # tangent orients the queue course. _rightmost across a
            # mixed-direction group would be meaningless (opposite tangents
            # skew the right-normal projection). Non-rail groups are
            # single-direction, so _rightmost picks the rightmost parallel
            # line as before.
            if rail_pool:
                path = group[0]
            else:
                path = _rightmost(group)

            # Backbone: the group's stop position line. Prefer the extent
            # the stop/dot placement computed for the path line itself; a
            # terminal departure has none (its entry is skip_first-skipped),
            # so fall back to another extent at this stop — best is the
            # same line's ARRIVAL counterpart (same ref + agency), whose
            # geometry ends at the stop and covers exactly the ground
            # behind it where the queue stands. No direction gate: at
            # corner terminals the arrival approaches on a different
            # street, near-perpendicular to the departure tangent, and
            # that is precisely the ground the queue belongs on. Last
            # resort: compute the extent from the path's own geometry
            # (still fitted, never extrapolated). For a rail pool, the
            # search widens across every pooled sid's stop_lines and, at
            # equal key rank, prefers the LONGEST extent — the full
            # platform beats any sector's sub-extent.
            pool_lines = []
            for s in pool_sids:
                pool_lines.extend(stop_lines.get(s, []))
            ext = None
            best_key = None
            best_len = -1.0
            for cand in pool_lines:
                if cand["osm_id"] == path["osm_id"] and not rail_pool:
                    ext = cand["extent"]
                    break
                key = ((cand["ref"], cand["agency_id"])
                       == (path["ref"], path["agency_id"]),
                       cand["mode"] == path["mode"],
                       cand["osm_id"] == path["osm_id"])
                cand_len = _cum_dist_m(cand["extent"])[-1] if cand["extent"] and len(cand["extent"]) >= 2 else 0.0
                if (best_key is None or key > best_key
                        or (key == best_key and cand_len > best_len)):
                    best_key = key
                    best_len = cand_len
                    ext = cand["extent"]
            if ext is None:
                atlas_len = (stop_attrs.get(rep_sid, {}) or {}).get("length")
                sib_key = oid_sibling_key.get(path["osm_id"])
                sibs = sibling_groups.get(sib_key, []) if sib_key else []
                ext = _platform_extent(
                    path["stop_lon"], path["stop_lat"], path["polyline"],
                    path["mode"], atlas_len, PILL_CFG,
                    osm_id=path["osm_id"], siblings=sibs,
                    end_of_platform=(path["osm_id"], rep_sid)
                                    in end_of_platform_pairs,
                    mountain_origin=path["mountain_origin"])
            if ext is None or len(ext) < 2:
                continue
            recs.append({"sid": rep_sid, "group": group, "path": path,
                         "ext": ext, "cut_pts": []})
        return recs

    per_parent_sids: dict = defaultdict(list)
    for s in per_stop_visits:
        per_parent_sids[_parent_of(s)].append(s)

    for parent, parent_sids in per_parent_sids.items():
        recs = []
        # Rail platform-sector merge: rail sids at this parent whose
        # platform_code shares a numeric leading-digit run pool into one
        # track queue; non-rail sids and rail sids without a numeric
        # prefix stay solo.
        track_pool: dict = defaultdict(list)
        for sid in sorted(parent_sids):
            visits = per_stop_visits.get(sid, [])
            if not visits:
                continue
            code = (stop_meta.get(sid, {}) or {}).get("platform_code", "")
            prefix = _platform_number(code)
            is_rail = any(v["is_rail_like"] for v in visits)
            if is_rail and prefix:
                track_pool[("P:" + prefix,)].append(sid)
            else:
                track_pool[("S:" + sid,)].append(sid)
        for key in sorted(track_pool):
            recs.extend(_build_group_recs(track_pool[key]))

        # ── Same-curb resolution (non-rail) ──────────────────────────────
        # Same-direction groups of one station can sit on the same ground
        # under different GTFS platform ids (Bern, Schanzenstrasse:
        # southbound city bus 20 at :10001, southbound regional 100/101 at
        # :10000, one curb). Stop position lines laterally closer than
        # CLOSE_ZOOM_CURB_LATERAL_M resolve by along-line overlap: above
        # the merge fraction → one stop with the union platform line;
        # anything less → both lines shortened to the overlap middle so
        # they just touch (queues that no longer fit shift forward past
        # the stop — see the cut_pts handling in the band loop). Rail is
        # excluded for now — rail overlap needs its own treatment later.
        def _same_curb(a, b):
            if a["path"]["is_rail_like"] or b["path"]["is_rail_like"]:
                return None
            dot = (a["path"]["tangent"][0] * b["path"]["tangent"][0]
                   + a["path"]["tangent"][1] * b["path"]["tangent"][1])
            if dot < CLOSE_ZOOM_DIR_CLUSTER_COS:
                return None
            m = _extent_overlap(a["ext"], b["ext"], a["path"]["cos_lat"])
            if m is None or m[1] >= CLOSE_ZOOM_CURB_LATERAL_M:
                return None
            return m

        if len(recs) > 1:
            # Merge pass: transitive (union-find) on the original lines.
            uf = list(range(len(recs)))

            def _find(i):
                while uf[i] != i:
                    uf[i] = uf[uf[i]]
                    i = uf[i]
                return i

            for i in range(len(recs)):
                for j in range(i + 1, len(recs)):
                    m = _same_curb(recs[i], recs[j])
                    if m and m[0] > CLOSE_ZOOM_CURB_MERGE_FRAC:
                        uf[_find(i)] = _find(j)
            by_root: dict = defaultdict(list)
            for i in range(len(recs)):
                by_root[_find(i)].append(i)
            new_recs = []
            for idxs in by_root.values():
                if len(idxs) == 1:
                    new_recs.append(recs[idxs[0]])
                    continue
                members = [recs[i] for i in idxs]
                # One stop: pool the clusters, re-collapse same
                # (ref, agency, direction) across the platform ids,
                # fastest first; rightmost path over the pooled set;
                # union platform line.
                pooled = []
                for r in members:
                    for c in r["group"]:
                        tgt = None
                        for p in pooled:
                            if (p["ref"] != c["ref"]
                                    or p["agency_id"] != c["agency_id"]):
                                continue
                            dot = (p["tangent"][0] * c["tangent"][0]
                                   + p["tangent"][1] * c["tangent"][1])
                            if dot >= CLOSE_ZOOM_DIR_CLUSTER_COS:
                                tgt = p
                                break
                        if tgt is None:
                            pooled.append(c)
                        else:
                            for dst in c["destinations"]:
                                if dst and dst not in tgt["destinations"]:
                                    tgt["destinations"].append(dst)
                pooled.sort(key=lambda c: (-_variant_priority(c),
                                           c["osm_id"]))
                path = _rightmost(pooled)
                ext = _union_extents([r["ext"] for r in members],
                                     path["cos_lat"], chord_w=max_L)
                new_recs.append({"sid": path["sid"], "group": pooled,
                                 "path": path, "ext": ext, "cut_pts": []})
            recs = new_recs

            # Shorten pass — pair by pair on the current (post-merge)
            # geometry, recomputing the overlap before each cut.
            for i in range(len(recs)):
                for j in range(i + 1, len(recs)):
                    m = _same_curb(recs[i], recs[j])
                    if m is None:
                        continue
                    _shorten_curb(recs[i], m[2])
                    _shorten_curb(recs[j], m[3])

        # ── Queue course + per-pill work items ───────────────────────────
        work = []
        for rec in recs:
            group, path, ext = rec["group"], rec["path"], rec["ext"]
            # Queue course: the stop position line (own point order — its
            # front end is the stop end by construction) extended dead
            # straight far enough for the deepest band of this stack.
            # Anchors come from the stop position line ALONE (its forward
            # end for road-mode queues, its middle for rail stacks) — the
            # raw GTFS stop coordinate plays no part in placement.
            reach = (len(group) - 1) * max_step + max_L
            built = _stop_course(ext, path["cos_lat"],
                                 back_m=reach + 2.0 * max_L,
                                 fwd_m=reach / 2.0 + 2.0 * max_L,
                                 chord_w=max_L)
            if built is None:
                continue
            course, cdists, t_front, t_mid = built
            for k, c in enumerate(group):
                work.append((c, path, course, cdists, t_front, t_mid,
                             rec["cut_pts"], k, len(group)))

        # Offset placement tracks, shared per (group course, band, side) —
        # valid within this station only (courses are per-group objects).
        track_cache: dict = {}

        for c, path, course, cdists, t_front, t_mid, cut_pts, k, n in work:
            # Everything is placed along the group's queue course (stop
            # position line + straight extensions), not the raw line.
            polyline, dists = course, cdists
            cos_lat = path["cos_lat"]
            # Queue anchor on the course: rail stacks center on the stop
            # position line's middle; road-mode queues put the lead tip at
            # its forward end (the vehicle pulled fully forward).
            t_stop = t_mid if c["is_rail_like"] else t_front
            # Merged same-curb groups pool pills from several platform
            # ids; each pill keeps its own.
            sid = c["sid"]

            # Side of the line: bus/tram always to the right in direction of
            # travel; rail on the side the GTFS stop position snapped from.
            side = 1.0
            if c["is_rail_like"] and path["signed_d"] < 0.0:
                side = -1.0

            dest_full = " / ".join(c["destinations"])
            ref_text = c["ref"] or ""

            common = {
                "mode":           c["mode"],
                "color":          c["color"],
                "ref":            ref_text,
                "stop_id":        sid,
                "parent_station": parent,
            }

            parent_colors[parent].add(c["color"])

            # Rail: the full platform extent joins the hull cloud so the
            # backdrop covers the whole platform.
            if c["is_rail_like"] and c.get("extent"):
                parent_cloud[parent].extend(
                    (p[0], p[1]) for p in c["extent"])

            for band_id, bc in CLOSE_ZOOM_BANDS.items():
                L = bc["length_m"]
                W = bc["width_m"]
                R = W / 2.0
                # Full occupied length is exactly L: back cap (R) + body +
                # chevron tip (R). The body is the frame range rear → neck.
                body_len = L - 2.0 * R
                stack_step = L + CLOSE_ZOOM_STACK_GAP_M
                tipp = {"minzoom": bc["tipp_min"], "maxzoom": bc["tipp_max"]}
                # Offset of the pill CENTER line from the path: consistent
                # clear gap between the line and the pill's inner edge, on
                # the side chosen above.
                perp = side * (CLOSE_ZOOM_LINE_GAP_M + W / 2.0)

                # Placement track: the path shifted sideways by perp — the
                # curve the pill centers actually sit on. Stepping, spans
                # and axes are all measured along THIS track, not the
                # centerline: measured on the centerline, every degree of
                # bend stretches the gaps between pills on the outside of
                # the curve and squeezes them on the inside.
                tkey = (id(course), band_id, side)
                track = track_cache.get(tkey)
                if track is None:
                    reach = (n - 1) * stack_step + L
                    track = _offset_track(polyline, dists,
                                          t_stop - reach - L,
                                          t_stop + reach / 2.0 + L,
                                          perp, cos_lat)
                    track_cache[tkey] = track if track else False
                if not track:
                    continue
                tpts, tdists, tcts = track
                o_stop = _track_pos(t_stop, tcts, tdists)

                # Track span this pill occupies.
                if c["is_rail_like"]:
                    # Stack centered on the platform middle along the track;
                    # fastest (k=0) sits furthest forward.
                    o_center = o_stop + (n - 1 - 2 * k) * (stack_step / 2.0)
                else:
                    # Same-curb shorten overflow: the queue must not cross
                    # a cut boundary (the neighbouring platform line starts
                    # there). If it doesn't fit behind the stop, the whole
                    # stack shifts forward past the stop point — better in
                    # front than overlapping the neighbour.
                    o_shift = 0.0
                    if cut_pts:
                        rear_lim = None
                        for cp in cut_pts:
                            t_cp = _project_meters(cp[0], cp[1],
                                                   polyline, dists)
                            o_cp = _track_pos(t_cp, tcts, tdists)
                            if o_cp < o_stop and (rear_lim is None
                                                  or o_cp > rear_lim):
                                rear_lim = o_cp
                        if rear_lim is not None:
                            rear_need = o_stop - ((n - 1) * stack_step + L)
                            if rear_need < rear_lim:
                                o_shift = rear_lim - rear_need
                    # Stack extends upstream from the stop point; the fastest
                    # pill's chevron tip lands exactly on the stop (unless
                    # shifted forward by the rule above).
                    o_center = o_stop + o_shift - k * stack_step - L / 2.0
                o0 = o_center - L / 2.0
                o1 = o_center + L / 2.0

                # Per-pill straight frame: the axis is the AVERAGE direction
                # of the track part the pill occupies (the chord over its
                # own span), anchored at that part's midpoint. A
                # single-point tangent at the stop tilts the whole stack
                # against the line near bends, and deep stack positions can
                # sit tens of metres from the stop.
                A = _point_at_extrap(tpts, tdists, o0)
                B = _point_at_extrap(tpts, tdists, o1)
                dxm = (B[0] - A[0]) * 111320.0 * cos_lat
                dym = (B[1] - A[1]) * 111320.0
                norm = sqrt(dxm * dxm + dym * dym)
                if norm > 1e-9:
                    T = (dxm / norm, dym / norm)
                else:
                    T = path["tangent"]
                # Rail pool: reverse-direction pills flip T so their
                # chevron tip points backward along the course (outward
                # toward the negative-o end of the platform). The pill's
                # map footprint is unchanged — the rectangle rotates 180°
                # around origin — but the chevron and label direction flip
                # to reflect the actual direction of travel.
                if not c.get("dir_forward", True):
                    T = (-T[0], -T[1])
                N = (T[1], -T[0])  # right normal in direction of travel
                origin = _point_at_extrap(tpts, tdists, o_center)

                heading_deg_map = (90.0 - degrees(atan2(T[1], T[0]))) % 360.0
                # Label rotation: along the pill axis, flipped upside-down.
                text_rot = (heading_deg_map - 90.0) % 360.0
                flipped = 90.0 < text_rot < 270.0
                if flipped:
                    text_rot = (text_rot + 180.0) % 360.0

                def _frame_pt(dx, dy, origin=origin, T=T, N=N):
                    return _local_offset_to_lonlat(
                        origin[0], origin[1],
                        dx * T[0] + dy * N[0], dx * T[1] + dy * N[1], cos_lat)

                # Body range in the pill's own frame (origin = span middle;
                # the lateral offset is already baked into the track, so the
                # pill sits ON its frame axis at zero perpendicular offset).
                x_neck = body_len / 2.0
                x_rear = -body_len / 2.0

                built = _build_straight_pill_arrow(origin, T, N, cos_lat,
                                                   x_rear, x_neck, 0.0, W)
                if built is None:
                    continue
                ring, rear_center = built

                # Destination, pre-wrapped at build time with baked line
                # breaks. The text region runs from the disc's forward edge
                # (plus margin) to the neck (minus margin; a negative tip
                # margin lets text reach into the chevron base).
                region_start = x_rear + R + bc["margin_disc_m"]
                region_end = x_neck - bc["margin_tip_m"]
                text_avail_m = max(region_end - region_start, 0.0)
                dest_text = ""
                if bc["font_dest_m"] and dest_full and text_avail_m > 0.0:
                    avail_em = text_avail_m / bc["font_dest_m"]
                    dest_text = _wrap_label(dest_full, avail_em,
                                            bc["max_lines"])

                features.append({
                    "type": "Feature",
                    "tippecanoe": dict(tipp),
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                    "properties": {
                        **common,
                        "feature_type":    "pill_arrow",
                        "band":            band_id,
                        "mountain_origin": c["mountain_origin"] or "",
                        "destination":     dest_text,
                        "n_variants":      len(c["destinations"]),
                        "speed_kmh":       c["speed_kmh"],
                        "osm_id":          c["osm_id"],
                        "heading_deg":     round(heading_deg_map, 2),
                        "stack_idx":       k,
                    },
                })

                # Solid band (no destination): the whole pill renders in the
                # line color with just the centered number — no disc.
                solid = bc["font_dest_m"] is None

                rear_cx, rear_cy = rear_center
                if not solid:
                    # Disc at the round end, filled with the line color.
                    disc = []
                    for i in range(24):
                        a = 2.0 * pi * i / 24.0
                        disc.append(list(_local_offset_to_lonlat(
                            rear_cx, rear_cy, R * cos(a), R * sin(a), cos_lat)))
                    disc.append(disc[0])
                    features.append({
                        "type": "Feature",
                        "tippecanoe": dict(tipp),
                        "geometry": {"type": "Polygon", "coordinates": [disc]},
                        "properties": {**common,
                                       "feature_type": "pill_disc",
                                       "band":         band_id},
                    })

                # Line number: centered in the disc, or in the whole pill
                # for solid bands.
                if ref_text and bc["font_ref_m"]:
                    if solid:
                        ref_x, ref_y = _frame_pt(0.0, 0.0)
                    else:
                        ref_x, ref_y = rear_cx, rear_cy
                    features.append({
                        "type": "Feature",
                        "tippecanoe": dict(tipp),
                        "geometry": {"type": "Point",
                                     "coordinates": [ref_x, ref_y]},
                        "properties": {
                            **common,
                            "feature_type": "pill_ref",
                            "band":         band_id,
                            "font_m":       round(bc["font_ref_m"], 3),
                            "text_rot":     round(text_rot, 2),
                        },
                    })

                # Destination, left-aligned in the text region: the anchor
                # sits at the region end that is the text's visual left —
                # the disc side normally, the neck side when the label is
                # flipped (the style anchors the text's left edge here).
                if dest_text:
                    x_text = region_end if flipped else region_start
                    tx, ty = _frame_pt(x_text, 0.0)
                    features.append({
                        "type": "Feature",
                        "tippecanoe": dict(tipp),
                        "geometry": {"type": "Point",
                                     "coordinates": [tx, ty]},
                        "properties": {
                            **common,
                            "feature_type": "pill_dest",
                            "band":         band_id,
                            "destination":  dest_text,
                            "font_m":       round(bc["font_dest_m"], 3),
                            "text_rot":     round(text_rot, 2),
                        },
                    })

                # Hull cloud: pill outline plus the adjacent line section
                # (largest band only — it covers the smaller ones; the line
                # section stays arc-based since the LINE itself may curve).
                if band_id == CLOSE_ZOOM_HULL_BAND:
                    cloud = parent_cloud[parent]
                    cloud.extend((p[0], p[1]) for p in ring)
                    # Map the pill's track span back to centerline arc
                    # positions before sampling the line itself.
                    ct0 = _track_pos(o0, tdists, tcts)
                    ct1 = _track_pos(o1, tdists, tcts)
                    for t in _sample_ts(dists, ct0, ct1):
                        cloud.append(_point_at_extrap(polyline, dists, t))

    # ── Backdrop: one rounded hull polygon per parent station ────────────
    n_backdrops = 0
    for parent, cloud in parent_cloud.items():
        hull_ring = _rounded_hull_polygon(cloud, CLOSE_ZOOM_BACKDROP_PAD_M,
                                          CLOSE_ZOOM_ARC_STEP_DEG)
        if hull_ring is None:
            continue
        colors = sorted(parent_colors.get(parent, set()))
        bg_color = colors[0] if len(colors) == 1 else _blend_colors(colors)
        features.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": 15, "maxzoom": 18},
            "geometry": {"type": "Polygon", "coordinates": [hull_ring]},
            "properties": {
                "feature_type":   "backdrop",
                "parent_station": parent,
                "bg_color":       bg_color,
                "n_colors":       len(colors),
            },
        })
        n_backdrops += 1

    OUT_CLOSE_ZOOM.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": features,
    }, ensure_ascii=False))
    pill_count = sum(1 for f in features if f["properties"]["feature_type"] == "pill_arrow")
    print(f"  Close-zoom: {pill_count:,} pill-arrows, {n_backdrops:,} station "
          f"backdrops → {OUT_CLOSE_ZOOM}")


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
    dots (see pill-rendering.md § "Ferry stops")."""
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


def _polyline_midpoint(coords):
    """Return the (lon, lat) midpoint of a polyline by arc length."""
    if not coords:
        return (0.0, 0.0)
    if len(coords) == 1:
        return (coords[0][0], coords[0][1])
    seg_lens = []
    total = 0.0
    for i in range(len(coords) - 1):
        a, b = coords[i], coords[i + 1]
        d = haversine_km(a[0], a[1], b[0], b[1])
        seg_lens.append(d)
        total += d
    half = total / 2.0
    acc = 0.0
    for i, d in enumerate(seg_lens):
        if acc + d >= half:
            t = (half - acc) / d if d > 0 else 0.0
            a, b = coords[i], coords[i + 1]
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        acc += d
    return (coords[-1][0], coords[-1][1])


def _polyline_midpoint_and_tangent_deg(coords):
    """
    Return ((lon, lat), tangent_deg) for the polyline midpoint by arc length.

    The tangent angle is in degrees clockwise from east (MapLibre
    `text-rotate` convention with `text-rotation-alignment: map`).
    Returns 0° tangent for degenerate polylines.
    """
    if not coords or len(coords) < 2:
        if coords:
            return (coords[0][0], coords[0][1]), 0.0
        return (0.0, 0.0), 0.0
    seg_lens = []
    total = 0.0
    for i in range(len(coords) - 1):
        a, b = coords[i], coords[i + 1]
        d = haversine_km(a[0], a[1], b[0], b[1])
        seg_lens.append(d)
        total += d
    half = total / 2.0
    acc = 0.0
    seg_idx = len(coords) - 2
    t = 1.0
    for i, d in enumerate(seg_lens):
        if acc + d >= half:
            seg_idx = i
            t = (half - acc) / d if d > 0 else 0.0
            break
        acc += d
    a, b = coords[seg_idx], coords[seg_idx + 1]
    mid_lon = a[0] + (b[0] - a[0]) * t
    mid_lat = a[1] + (b[1] - a[1]) * t
    # Tangent in metric local frame, so angle is in real geography.
    cl = cos(radians(mid_lat)) or 1.0
    dx_m = (b[0] - a[0]) * 111320.0 * cl
    dy_m = (b[1] - a[1]) * 111320.0
    # MapLibre y-axis on screen is down; `text-rotate` with
    # `text-rotation-alignment: map` rotates clockwise from east in map
    # space (north up). atan2(-dy, dx) converts our north-up tangent
    # vector into that clockwise convention.
    tangent_deg = degrees(atan2(-dy_m, dx_m))
    return (mid_lon, mid_lat), tangent_deg


# =============================================================================
# Far-zoom dot positioning — see .claude/concepts/far-zoom-stop-markers.md
# =============================================================================

def _meters_per_deg(lat):
    """Return (mx_per_deg_lon, my_per_deg_lat) for local equirectangular
    scaling at the given latitude."""
    return 111320.0 * cos(radians(lat)), 111320.0


def _logical_line_key(oid, line_lookup):
    """Return the logical-line key `(ref, mode, agency_id)` for an osm_id.
    Direction and terminus variants of one route share this key — counting
    by it (not by osm_id) is what prevents the four parallel direction-
    variants of one bus from scoring as a four-way intersection or as a
    four-line pill. See far-zoom-stop-markers.md § 'Intersection search'."""
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
    .claude/concepts/far-zoom-stop-markers.md § 'Intersection search'.
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
    rank, per .claude/concepts/far-zoom-stop-markers.md § 'Position rule
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
    .claude/concepts/far-zoom-stop-markers.md § 'Bad-intersection gate'.
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
    .claude/concepts/far-zoom-stop-markers.md § 'Position rule by mode family'.

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
    return fallback_pos


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

    Uniform z14 for every mode — see `pill-rendering.md`
    § "Dot-to-pill zoom switch". Design bands A/B/C tag features with
    per-band minzoom/maxzoom on top of this — see the pill-design-band
    bake in `main()`.
    """
    if stop_count < 2:
        return None
    return 14


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


def build_indicator_features(stops_at_location, lon, lat, line_lookup,
                              tangent_deg=0.0,
                              parent_width_base=None, parent_mode=None,
                              parent_type="disc"):
    """
    Emit color-indicator Point features for a single rendered location.

    Groups the stops by color-group (per MODE_TO_COLOR_GROUP), picks the
    fastest line (highest freq_score) within each group, and yields one
    Point feature per group at the parent's center coordinate.

    `tangent_deg` is the orientation of the indicator row in degrees
    (clockwise from east in map space, MapLibre `text-rotate` convention).
    Pass the pill's local tangent angle for pill indicators; leave 0 for
    dot / disc indicators (screen-horizontal row).

    `parent_width_base` / `parent_mode`: the parent stop's effective (clamped)
    width_base and mode. Stamped on every emitted indicator so the style
    can size + shrink the row to fit the parent. When omitted, derived
    from `stops_at_location` via dominant_line + the per-mode floor.

    Each feature carries `color`, `slot_units`, `tangent_deg`,
    `n_indicators`, `parent_width_base`. See
    `.claude/concepts/stop-color-indicators.md` and
    `.claude/concepts/pill-zoom-stop-tweaks.md`.
    """
    by_group: dict = {}
    seen_modes_wb: list = []
    for s in stops_at_location:
        oid = str(s.get("osm_id", ""))
        line = line_lookup.get(oid)
        if not line:
            continue
        mode = line.get("mode", "")
        seen_modes_wb.append((mode, float(line.get("width_base", 1.0))))
        group = MODE_TO_COLOR_GROUP.get(mode)
        if not group:
            continue
        fs = line.get("freq_score", 0.0)
        ref = line.get("gtfs_ref") or line.get("ref", "")
        cur = by_group.get(group)
        cand = (fs, ref, line.get("color", "#888888"))
        if cur is None or (fs > cur[0]) or (fs == cur[0] and ref < cur[1]):
            by_group[group] = cand

    if not by_group:
        return []

    if parent_mode is None or parent_width_base is None:
        # Derive from the visible lines at this location.
        if seen_modes_wb:
            dom_rank = min(MODE_RANK.get(m, 99) for m, _ in seen_modes_wb)
            derived_mode = next(m for m, _ in seen_modes_wb
                                if MODE_RANK.get(m, 99) == dom_rank)
            derived_max_wb = max(wb for _, wb in seen_modes_wb)
        else:
            derived_mode = "bus"
            derived_max_wb = 1.0
        if parent_mode is None:
            parent_mode = derived_mode
        if parent_width_base is None:
            parent_width_base = _stop_wb(derived_max_wb, parent_mode)

    groups_present = [g for g in COLOR_GROUP_ORDER if g in by_group]
    n = len(groups_present)

    feats = []
    # slot_units = 2*i - (n-1) gives a centered, integer-stepped sequence
    # that's symmetric around 0: e.g. n=2 → {-1, +1}; n=3 → {-2, 0, +2};
    # n=6 → {-5, -3, -1, +1, +3, +5}. The style layer applies
    # text-offset = slot_units × half_spacing_em and text-rotate = tangent_deg
    # in map-aligned space, so the row rotates with the parent's tangent.
    # row_factor (em) — the multiple of text-size that the binding
    # dimension of the parent must accommodate. Pill parents bind on
    # their short axis (one glyph diameter through the pill thickness,
    # row length is unbounded along the long axis). Disc/dot parents
    # bind on the full row span (glyph diameters + inter-glyph gaps).
    # See `.claude/concepts/pill-zoom-stop-tweaks.md` § "Indicators
    # must not overflow the parent".
    if parent_type == "pill":
        row_factor = 0.70
    else:
        row_factor = 0.56 * n + 0.14

    for i, group in enumerate(groups_present):
        _fs, _ref, color = by_group[group]
        slot_units = 2 * i - (n - 1)
        feats.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": INDICATOR_MIN_ZOOM},
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "feature_type":      "indicator",
                "color":             color,
                "slot_units":        slot_units,
                "tangent_deg":       round(tangent_deg, 2),
                "n_indicators":      n,
                "row_factor":        round(row_factor, 3),
                "parent_width_base": round(float(parent_width_base), 3),
            },
        })
    return feats


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


def _remove_pill_kinks(coords, cos_lat, pill_diameter_m):
    """Post-DP cleanup: iteratively drop any interior vertex where both
    adjacent arm segments are shorter than the pill's rendered diameter
    at the current band's target zoom.

    Rationale: when an interior arm is shorter than the pill's width,
    the arm can't extend beyond the round join at the corner — it's
    buried inside the pill body — so the vertex can only produce a
    "kink" bulge, never a real corner. A real L- or T-shape platform
    has arms much longer than the pill diameter, so its corner is
    preserved. No angle threshold: a shallow bend with sub-diameter
    arms is still a bulge; a steep bend with long arms is still a
    corner.

    `pill_diameter_m` is the pill's estimated diameter in metres for
    the current cluster and band (computed from `PILL_MIN_D_PX`,
    `PILL_SLOPE_PX_PER_WB`, `PILL_BAND_ZOOM`, cluster wb, and cluster
    cos_lat). Convergence: at most one vertex dropped per pass; loop
    until stable.
    """
    if len(coords) <= 2 or pill_diameter_m is None or pill_diameter_m <= 0:
        return list(coords)
    result = list(coords)
    while len(result) >= 3:
        lon0 = result[0][0]
        lat0 = result[0][1]
        xy = [_lonlat_to_xy(p[0], p[1], lon0, lat0, cos_lat) for p in result]
        drop_i = -1
        for i in range(1, len(xy) - 1):
            dx_in = xy[i][0] - xy[i-1][0]
            dy_in = xy[i][1] - xy[i-1][1]
            dx_out = xy[i+1][0] - xy[i][0]
            dy_out = xy[i+1][1] - xy[i][1]
            arm_in = sqrt(dx_in * dx_in + dy_in * dy_in)
            arm_out = sqrt(dx_out * dx_out + dy_out * dy_out)
            if arm_in < pill_diameter_m and arm_out < pill_diameter_m:
                drop_i = i
                break
        if drop_i < 0:
            break
        result = result[:drop_i] + result[drop_i+1:]
    return result


def _current_pill_diameter_m(wb, cos_lat):
    """Pill diameter estimate in metres for a stop with width_base `wb`
    at the currently-set band's target zoom (via `_set_pill_design_band`)
    and the given cluster latitude. Formula matches the paint expression
    in `generate_style.py`: `min_d + slope × min(wb, WB_HIGH)`, then
    convert pixels to metres using the standard Web-Mercator m/px."""
    wb_clamped = min(wb, PILL_WB_HIGH)
    d_px = PILL_MIN_D_PX + PILL_SLOPE_PX_PER_WB * wb_clamped
    # Web-Mercator m/px at latitude with MapLibre's 512-px tiles.
    # Matches `apply_stop_dedup`'s `m_per_px` (EARTH_M = 40075016.7).
    mp_per_px = (40075016.7 * cos_lat) / (512.0 * (2 ** PILL_BAND_ZOOM))
    return d_px * mp_per_px


def _simplify_pill_lonlat(coords, cos_lat, tol_m=None, pill_diameter_m=None):
    """Douglas-Peucker simplification of a pill polyline followed by
    kink removal. DP drops interior vertices whose perpendicular
    deviation from the chord through kept neighbours is below `tol_m`;
    `_remove_pill_kinks` then drops any surviving vertex whose adjacent
    arm segments are both shorter than `pill_diameter_m` (the pill's
    rendered diameter at the current band's target zoom). Works in
    metric (x, y) space anchored at the first vertex so the tolerance
    is in true metres.

    `tol_m=None` reads the current band's `PILL_SIMPLIFY_TOL_M` at call
    time (band-swapped via `_set_pill_design_band`).
    `pill_diameter_m=None` disables kink removal — callers that don't
    know the pill diameter (e.g. `_tangent_candidates` when the wb
    isn't threaded through) get DP-only simplification.
    """
    if tol_m is None:
        tol_m = PILL_SIMPLIFY_TOL_M
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
    simplified = [coords[i] for i in range(n) if keep[i]]
    if pill_diameter_m is None:
        return simplified
    return _remove_pill_kinks(simplified, cos_lat, pill_diameter_m)


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


def _pill_mid_attach_candidates(simp, cluster_cos_lat, max_cos_bend=0.5):
    """For a simplified pill polyline (>= 3 vertices), return a list of
    (pos, outer_tangent_xy) for interior vertices where the bend angle
    is at least 60° (cos(bend) <= 0.5 with the default). Bend angle is
    the deviation from a straight continuation: 0° = straight, 90° =
    right angle, 180° = U-turn.

    `pos` is the (lon, lat) tuple of the vertex.
    `outer_tangent_xy` is the outward unit tangent in cluster-xy space
    (opposite the bisector of the interior angle, so it points away
    from the concave side of the corner).
    """
    if len(simp) < 3:
        return []
    lon0, lat0 = simp[0][0], simp[0][1]
    xy = [_lonlat_to_xy(p[0], p[1], lon0, lat0, cluster_cos_lat) for p in simp]
    out = []
    for i in range(1, len(simp) - 1):
        prev_xy, v_xy, next_xy = xy[i - 1], xy[i], xy[i + 1]
        u_in = _norm2((v_xy[0] - prev_xy[0], v_xy[1] - prev_xy[1]))
        u_out = _norm2((next_xy[0] - v_xy[0], next_xy[1] - v_xy[1]))
        if u_in is None or u_out is None:
            continue
        cos_bend = u_in[0] * u_out[0] + u_in[1] * u_out[1]
        if cos_bend > max_cos_bend:
            continue  # not bent enough
        u_v_prev = _norm2((prev_xy[0] - v_xy[0], prev_xy[1] - v_xy[1]))
        u_v_next = _norm2((next_xy[0] - v_xy[0], next_xy[1] - v_xy[1]))
        if u_v_prev is None or u_v_next is None:
            continue
        bisector = (u_v_prev[0] + u_v_next[0], u_v_prev[1] + u_v_next[1])
        outer = _norm2((-bisector[0], -bisector[1]))
        if outer is None:
            continue
        out.append((simp[i], outer))
    return out


def _tangent_candidates(group, endpoint, lon0, lat0, cos_lat):
    """Candidate outward-pointing unit tangents at `endpoint` within `group`.

    Connectors attach only at pill endpoints, so `endpoint` is always the
    first or last dot of a pill group, never an interior dot.

    Returns a list of (tangent, is_default) tuples in metric (x, y) space:
    - Singleton group (disc): [] — tangent is unconstrained, derived from
      symmetry by the caller.
    - Pill tip: [(axial, True), (perp_left, False), (perp_right, False)].
    """
    if len(group) <= 1:
        return []

    # Compute OUT from the simplified polyline (what the renderer draws), not
    # the raw NN-path group. The path can zig-zag through pill vertices (e.g.
    # disc → middle → south → north at Bethlehem Kirche), which makes the raw
    # next-vertex point into the pill body instead of away from it.
    simplified = _simplify_pill_lonlat(group, cos_lat,
                                       pill_diameter_m=_CURRENT_CLUSTER_PILL_DIAMETER_M)
    if len(simplified) < 2:
        return []

    if endpoint[0] == simplified[0][0] and endpoint[1] == simplified[0][1]:
        idx, neighbor = 0, 1
    else:
        idx, neighbor = len(simplified) - 1, len(simplified) - 2

    xy_e = _lonlat_to_xy(simplified[idx][0], simplified[idx][1], lon0, lat0, cos_lat)
    xy_n = _lonlat_to_xy(simplified[neighbor][0], simplified[neighbor][1], lon0, lat0, cos_lat)
    axial = _norm2((xy_e[0] - xy_n[0], xy_e[1] - xy_n[1]))
    if axial is None:
        return []
    return [
        (axial, True),
        (_rotate2(axial, pi / 2), False),
        (_rotate2(axial, -pi / 2), False),
    ]


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
        # connector candidate and must surface to the picker as a 2-point
        # result so that, when no combo produces a real curve, the picker has
        # a last-resort straight to fall back on. Real curves at other tangent
        # combos outrank this chord in the picker. Only emit when the chord
        # actually aligns with tA — otherwise a "straight line" between A and
        # B has hard kinks at both ends and the combo is geometrically
        # inconsistent.
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
        # Disc exactly on the line of tA: the chord IS the tangent-continuous
        # connector. Emit it directly so the picker sees a valid candidate at
        # the right length instead of dropping axial and falling through to a
        # swooping perpendicular arc.
        return [A, B]

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

    if best is None:
        return None
    if best[0] < 1e-6:
        # Sweep is essentially zero — tA is already aligned with the chord A→B
        # to within float precision. The chord IS the tangent-continuous answer;
        # emit it directly so the picker sees a valid candidate instead of
        # falling through to a perpendicular arc.
        return [A, B]
    _, delta = best

    # Sub-degree sweep: tA is essentially aligned with the chord A→B, so the
    # straight chord is the right answer. Emitting it here avoids the arc
    # samples collapsing under dedup and triggering the degenerate-curve
    # rejection below. The tangent error at A stays under ~1.5° (invisible).
    if abs(delta) < radians(1.5):
        return [A, B]

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
# pill-rendering.md` § Disc anchoring → Per-cluster strategy choice.
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
    globals()["_CURRENT_CLUSTER_PILL_DIAMETER_M"] = pill_diameter_m
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
    # `_pill_mid_attach_candidates` and pill-rendering.md § "Pills and
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
    # See pill-rendering.md § "Curved-length overshoot rescan".
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


# Stop tier hierarchy. Higher index = higher priority for tier assignment
# when a stop is served by multiple modes. Ferry / mountain are evaluated
# only when no hierarchy mode is present.
STOP_TIER_HIERARCHY = ("train", "metro", "tram", "bus", "regional_bus")
STOP_TIER_ISOLATED  = ("ferry", "mountain")
STOP_TIER_RANK = {m: i for i, m in enumerate(STOP_TIER_HIERARCHY)}

# Min_zoom assigned when no per-mode rule matches. Effectively "never visible"
# at any rendered zoom level.
UNREACH_Z = 13


def _resolve_stop_tier(modes_present: set) -> str:
    """Return the tier for a stop served by `modes_present`. Hierarchy
    modes win over isolated pools when both present."""
    best = None
    best_rank = -1
    for m in modes_present:
        r = STOP_TIER_RANK.get(m, -1)
        if r > best_rank:
            best_rank = r
            best = m
    if best is not None:
        return best
    for m in STOP_TIER_ISOLATED:
        if m in modes_present:
            return m
    return ""


# ── Zoom-level rules: data loaders ──────────────────────────────────────────
# See .claude/concepts/zoom-level-rules.md.

BUILDINGS_GEOJSON = ROOT / "data" / "osm" / "buildings.geojson"
GTFS_STOP_TIMES   = ROOT / "data" / "gtfs_routed" / "stop_times.txt"
OUT_URBANNESS     = ROOT / "data" / "transit" / "urbanness.json"


def _zoom_rules_cfg() -> dict:
    sc = _transit_cfg.get("zoom_level_rules") or {}
    if not sc:
        print("  WARNING: config.yaml has no `zoom_level_rules` section — "
              "stop min_zoom defaults to mode minzoom only.")
    return sc


def _parse_time_secs(t: str) -> int:
    """HH:MM:SS → seconds. Caller catches ValueError."""
    p = t.strip().split(":")
    return int(p[0]) * 3600 + int(p[1]) * 60 + int(p[2])


def _uic_of(sid: str, stop_meta: dict) -> str:
    """Canonical UIC for a stop_id — parent_station if present, else the
    `:`-prefix base of the stop_id (which is the SBB-style UIC)."""
    if not sid:
        return ""
    meta = stop_meta.get(sid) or stop_meta.get(sid.split(":")[0]) or {}
    return meta.get("parent") or sid.split(":")[0]


def load_buildings():
    """Return a flat [(lon, lat), ...] from data/osm/buildings.geojson.
    Format is the custom `{"coords": [[lon, lat], ...]}` blob written by
    03_bbox_osm.py — not strict GeoJSON, just a compact coord list."""
    if not BUILDINGS_GEOJSON.exists():
        print(f"  WARNING: {BUILDINGS_GEOJSON} missing — urbanness brackets "
              "default to rural. Re-run step 03 to populate.")
        return []
    data = json.loads(BUILDINGS_GEOJSON.read_text())
    return [(float(c[0]), float(c[1])) for c in data.get("coords", [])]


def count_buildings_in_radii(coords_by_uic, buildings,
                              r_inner_m, r_outer_m):
    """{uic: (c_inner, c_outer)} via grid bucketing at the outer radius."""
    if not buildings or not coords_by_uic:
        return {uic: (0, 0) for uic in coords_by_uic}
    cell_m = max(r_inner_m, r_outer_m)
    lat0 = 46.8
    cos_lat0 = cos(radians(lat0))
    cell_lat_deg = cell_m / _M_PER_DEG
    cell_lon_deg = cell_lat_deg / cos_lat0
    grid: dict = defaultdict(list)
    for lon, lat in buildings:
        cx = int(floor(lon / cell_lon_deg))
        cy = int(floor(lat / cell_lat_deg))
        grid[(cx, cy)].append((lon, lat))
    r_in_sq = r_inner_m * r_inner_m
    r_out_sq = r_outer_m * r_outer_m
    out: dict = {}
    for uic, (lon, lat) in coords_by_uic.items():
        cx = int(floor(lon / cell_lon_deg))
        cy = int(floor(lat / cell_lat_deg))
        c_in = c_out = 0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (blon, blat) in grid.get((cx + dx, cy + dy), ()):
                    mdx = (blon - lon) * cos_lat0 * _M_PER_DEG
                    mdy = (blat - lat) * _M_PER_DEG
                    d_sq = mdx * mdx + mdy * mdy
                    if d_sq <= r_out_sq:
                        c_out += 1
                        if d_sq <= r_in_sq:
                            c_in += 1
        out[uic] = (c_in, c_out)
    return out


def compute_urbanness(building_counts, urb_cfg):
    """{uic: {c_inner, c_outer, bracket}}. Bracket assigned by evaluating
    rules top-to-bottom (elseif semantics):
        c_outer > city_c500   → city
        c_outer > town_c500   → town
        c_inner > village_c200 → village
        else                  → rural
    See concept § "Urbanness bracket"."""
    city_th    = float(urb_cfg.get("city_c500",    600))
    town_th    = float(urb_cfg.get("town_c500",    300))
    village_th = float(urb_cfg.get("village_c200",  30))
    out: dict = {}
    for uic, (c_in, c_out) in building_counts.items():
        if c_out > city_th:
            b = "city"
        elif c_out > town_th:
            b = "town"
        elif c_in > village_th:
            b = "village"
        else:
            b = "rural"
        out[uic] = {"c200": c_in, "c500": c_out, "bracket": b}
    return out


def compute_dwell_per_uic(stop_meta):
    """{uic: avg_dwell_seconds} streamed from data/gtfs_routed/stop_times.txt.
    avg (dep − arr) across every trip-stop row. Rows with arr == dep or
    missing fields are folded in as 0 — they count toward the average but
    pull it down, matching the concept's "average departure − arrival
    across trips visiting the stop".
    """
    if not GTFS_STOP_TIMES.exists():
        print(f"  WARNING: {GTFS_STOP_TIMES} missing — dwell points default "
              "to 0.")
        return {}
    sum_secs: dict = defaultdict(float)
    cnt: dict = defaultdict(int)
    with open(GTFS_STOP_TIMES, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            arr = row.get("arrival_time", "")
            dep = row.get("departure_time", "")
            sid = row.get("stop_id", "")
            if not sid or sid.startswith("WPT:"):
                # Synthetic pfaedle waypoints (gtfs-trip-overrides) are not
                # stops.
                continue
            try:
                a = _parse_time_secs(arr)
                d = _parse_time_secs(dep)
            except (ValueError, IndexError):
                continue
            uic = _uic_of(sid, stop_meta)
            if not uic:
                continue
            sum_secs[uic] += max(0, d - a)
            cnt[uic] += 1
    return {uic: sum_secs[uic] / cnt[uic] for uic in sum_secs if cnt[uic] > 0}


def compute_stop_importance(uic_serving, coords_by_uic,
                             urbanness, dwell_by_uic,
                             nearby_transit_radius_m):
    """Per-stop importance score = dwell + urbanness + nearby_transit + interchange.
    See concept § "Stop importance score". Returns {uic: int}.

    Per-category points are hard-coded in this function; only the radius
    for the nearby-transit category lives in config (see
    `zoom_level_rules.stop_importance.nearby_transit_radius_m`).
    """
    # Per-uic: distinct line-key set. line_key = (ref, agency, mode) here —
    # mode-typed so "Bus 10 BernMobil" and "Train 10 SBB" count as distinct.
    line_keys_by_uic: dict = defaultdict(set)
    bus_tram_keys_by_uic: dict = defaultdict(set)
    modes_by_uic: dict = defaultdict(set)
    for uic, entries in uic_serving.items():
        for e in entries:
            lk = e["line_key"]
            line_keys_by_uic[uic].add(lk)
            modes_by_uic[uic].add(e["mode"])
            if e["mode"] in ("bus", "tram", "regional_bus"):
                bus_tram_keys_by_uic[uic].add(lk)

    # Spatial grid for nearby-transit lookup (train stop → bus/tram lines
    # within radius). Index keys are uic, value is the bus/tram line_key set
    # at that uic.
    lat0 = 46.8
    cos_lat0 = cos(radians(lat0))
    cell_m = nearby_transit_radius_m
    cell_lat_deg = cell_m / _M_PER_DEG
    cell_lon_deg = cell_lat_deg / cos_lat0
    bt_grid: dict = defaultdict(list)
    for uic, keys in bus_tram_keys_by_uic.items():
        coord = coords_by_uic.get(uic)
        if not coord:
            continue
        lon, lat = coord
        cx = int(floor(lon / cell_lon_deg))
        cy = int(floor(lat / cell_lat_deg))
        bt_grid[(cx, cy)].append((uic, lon, lat, keys))
    r_sq = nearby_transit_radius_m * nearby_transit_radius_m

    URBANNESS_POINTS = {"city": 3, "town": 2, "village": 1, "rural": 0}
    out: dict = {}
    for uic in uic_serving:
        score = 0
        # Dwell: > 3 min → 3; > 0 min → 2; else 0.
        dwell = dwell_by_uic.get(uic, 0.0)
        if dwell > 180:
            score += 3
        elif dwell > 0:
            score += 2
        # Urbanness bracket.
        bracket = urbanness.get(uic, {}).get("bracket", "rural")
        score += URBANNESS_POINTS.get(bracket, 0)
        # Nearby transit (train stops only).
        my_modes = modes_by_uic.get(uic, set())
        if "train" in my_modes:
            coord = coords_by_uic.get(uic)
            if coord is not None:
                lon, lat = coord
                cx = int(floor(lon / cell_lon_deg))
                cy = int(floor(lat / cell_lat_deg))
                my_keys = line_keys_by_uic.get(uic, set())
                found_keys: set = set()
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for (_other_uic, olon, olat, keys) in \
                                bt_grid.get((cx + dx, cy + dy), ()):
                            mdx = (olon - lon) * cos_lat0 * _M_PER_DEG
                            mdy = (olat - lat) * _M_PER_DEG
                            if mdx * mdx + mdy * mdy > r_sq:
                                continue
                            found_keys.update(keys - my_keys)
                if len(found_keys) > 3:
                    score += 3
                elif len(found_keys) > 0:
                    score += 2
        # Interchange.
        keys_here = line_keys_by_uic.get(uic, set())
        if len(keys_here) >= 2:
            if "train" in my_modes:
                score += 3
            else:
                score += 2
        out[uic] = score
    return out


def _build_uic_serving(line_lookup, line_stops, stop_meta):
    """Build the per-UIC line-membership index used by every stop-rule
    function. Each entry carries {oid, mode, idx, is_first, is_last,
    line_key}. uic = parent_station if present, else stop_id base.
    """
    uic_serving: dict = defaultdict(list)
    coords_by_uic: dict = {}
    for oid, entry in line_stops.items():
        info = line_lookup.get(str(oid))
        if not info:
            continue
        stops = entry.get("stops", []) if isinstance(entry, dict) else entry
        if not stops:
            continue
        mode = info.get("mode", "")
        ref = info.get("ref", "")
        agency_id = info.get("agency_id", "")
        line_key = (ref, agency_id, mode)
        last_idx = len(stops) - 1
        for idx, stop in enumerate(stops):
            if len(stop) < 3 or not stop[2]:
                continue
            sid = stop[2]
            lon, lat = float(stop[0]), float(stop[1])
            uic = _uic_of(sid, stop_meta)
            if not uic:
                continue
            uic_serving[uic].append({
                "oid": str(oid), "mode": mode, "idx": idx,
                "is_first": idx == 0, "is_last": idx == last_idx,
                "line_key": line_key,
            })
            coords_by_uic.setdefault(uic, (lon, lat))
    return uic_serving, coords_by_uic


def compute_stop_min_zoom(line_lookup, line_stops, stop_meta,
                           importance_by_uic, intercity_oids,
                           uic_serving, coords_by_uic):
    """Apply per-mode stop rules → candidate min_zoom per UIC, then
    raise to the smallest min_zoom of any line serving the UIC
    (stops-follow-lines). Returns {uic: {min_zoom, rule_label,
    is_intersection, is_terminus, tier}}.
    """
    # Per-line cumulative km along the polyline at each stop index.
    cum_km_by_oid: dict = {}
    for oid, entry in line_stops.items():
        stops = entry.get("stops", []) if isinstance(entry, dict) else entry
        if not stops:
            cum_km_by_oid[str(oid)] = []
            continue
        cum = [0.0]
        for i in range(1, len(stops)):
            cum.append(cum[-1] + haversine_km(
                stops[i - 1][0], stops[i - 1][1],
                stops[i][0], stops[i][1]))
        cum_km_by_oid[str(oid)] = cum

    # Per-uic per-line index map (so a line can be checked at a uic without
    # rescanning its stops).
    uic_indices_on_oid: dict = defaultdict(dict)  # (uic, oid) → idx
    for uic, entries in uic_serving.items():
        for e in entries:
            uic_indices_on_oid[(uic, e["oid"])] = e["idx"]

    # Pre-bucket lines by mode for fast lookup.
    oids_by_mode: dict = defaultdict(list)
    for oid, info in line_lookup.items():
        oids_by_mode[info.get("mode", "")].append(str(oid))

    def _line_mz(oid: str) -> int:
        mz = line_lookup.get(oid, {}).get("min_zoom")
        try:
            return int(mz) if mz is not None else UNREACH_Z
        except (TypeError, ValueError):
            return UNREACH_Z

    def _visible_oids_in_mode(mode: str, level: int) -> set:
        return {o for o in oids_by_mode.get(mode, [])
                if _line_mz(o) <= level}

    candidate_mz: dict = {uic: UNREACH_Z for uic in uic_serving}
    rule_label: dict = {uic: "" for uic in uic_serving}
    # `is_intersection` / `is_terminus` are computed against the FINAL set of
    # visible lines (using the final per-line min_zoom). Recorded for diag
    # output; not used to gate further rules.
    is_intersection_flag: dict = {uic: False for uic in uic_serving}
    is_terminus_flag: dict = {uic: False for uic in uic_serving}

    def _maybe_set(uic: str, level: int, label: str):
        if candidate_mz[uic] > level:
            candidate_mz[uic] = level
            rule_label[uic] = label

    # Pre-compute the canonical-UIC stop set per line — used by the
    # intersection rule below to test "how many stops do these two lines share?"
    # See concept § "Metrics referenced below" → is_intersection.
    uic_stops_by_oid: dict = {}
    for oid, entry in line_stops.items():
        stops = entry.get("stops", []) if isinstance(entry, dict) else entry
        s: set = set()
        for stop in stops:
            if len(stop) >= 3 and stop[2]:
                uic = _uic_of(stop[2], stop_meta)
                if uic:
                    s.add(uic)
        uic_stops_by_oid[str(oid)] = s

    # Two lines can share at most this many UIC stops and still count as an
    # intersection. The tolerance keeps parallel-corridor stops out of
    # intersection status while keeping real hubs that happen to share a
    # secondary stop on top of the hub.
    INTERSECTION_MAX_SHARED_STOPS = 2

    def _apply_intersection_or_terminus(mode: str, level: int):
        vis = _visible_oids_in_mode(mode, level)
        if not vis:
            return
        for uic, entries in uic_serving.items():
            # Visible mode-entries at this UIC.
            mode_entries = [e for e in entries
                            if e["mode"] == mode and e["oid"] in vis]
            terminus = any(e["is_first"] or e["is_last"]
                           for e in mode_entries)
            # Group by line_key (distinct logical lines). Multiple variants of
            # the same logical line don't count as a separate line for the
            # intersection test.
            oids_by_key: dict = defaultdict(list)
            for e in mode_entries:
                oids_by_key[e["line_key"]].append(e["oid"])
            # Per-line-key UIC set = union over variants.
            stops_by_key: dict = {
                k: set().union(*(uic_stops_by_oid.get(o, set())
                                 for o in oids))
                for k, oids in oids_by_key.items()
            }
            intersection = False
            keys = list(oids_by_key.keys())
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    shared = stops_by_key[keys[i]] & stops_by_key[keys[j]]
                    if len(shared) <= INTERSECTION_MAX_SHARED_STOPS:
                        intersection = True
                        break
                if intersection:
                    break
            if mode_entries and (intersection or terminus):
                _maybe_set(uic, level, f"{mode}: intersection_or_terminus")
                if intersection:
                    is_intersection_flag[uic] = True
                if terminus:
                    is_terminus_flag[uic] = True

    def _apply_intercity_train_stops(level: int):
        # Every stop on a visible intercity train line.
        for oid in intercity_oids:
            if _line_mz(oid) > level:
                continue
            entry = line_stops.get(oid) or line_stops.get(str(oid)) or {}
            stops = entry.get("stops", []) if isinstance(entry, dict) else entry
            for stop in stops:
                if len(stop) < 3 or not stop[2]:
                    continue
                uic = _uic_of(stop[2], stop_meta)
                if uic and uic in candidate_mz:
                    _maybe_set(uic, level, "train: served by intercity line")

    def _apply_importance_greedy(mode: str, level: int, min_km: float):
        vis = _visible_oids_in_mode(mode, level)
        for oid in vis:
            entry = line_stops.get(oid) or line_stops.get(str(oid)) or {}
            stops = entry.get("stops", []) if isinstance(entry, dict) else entry
            cum_km = cum_km_by_oid.get(str(oid), [])
            if not stops or not cum_km:
                continue
            uic_per_idx: list = []
            for stop in stops:
                if len(stop) < 3 or not stop[2]:
                    uic_per_idx.append("")
                else:
                    uic_per_idx.append(_uic_of(stop[2], stop_meta))
            order = sorted(
                range(len(stops)),
                key=lambda i: (
                    -importance_by_uic.get(uic_per_idx[i], 0),
                    uic_per_idx[i] or "",
                    i,
                ),
            )
            accepted_km: list = []
            for i in order:
                uic = uic_per_idx[i]
                if not uic:
                    continue
                ki = cum_km[i] if i < len(cum_km) else 0.0
                if any(abs(ki - aj) < min_km for aj in accepted_km):
                    continue
                accepted_km.append(ki)
                _maybe_set(uic, level,
                           f"{mode}: importance-greedy <= 1 / {min_km:g} km")

    def _apply_all_stops_on_visible_mode(mode: str, base_level: int):
        # Stops on visible lines of `mode` get base_level (or the line's own
        # min_zoom if later — "lines first becoming visible at z11 bring their
        # stops with them at z11"). Used by ferry + mountain stop rules.
        for oid in oids_by_mode.get(mode, []):
            line_mz = _line_mz(oid)
            effective = max(base_level, line_mz)
            entry = line_stops.get(oid) or line_stops.get(str(oid)) or {}
            stops = entry.get("stops", []) if isinstance(entry, dict) else entry
            for stop in stops:
                if len(stop) < 3 or not stop[2]:
                    continue
                uic = _uic_of(stop[2], stop_meta)
                if uic and uic in candidate_mz:
                    _maybe_set(uic, effective,
                               f"{mode}: all stops on visible line")

    def _apply_all_remaining(mode: str, level: int):
        # Every stop on every line of this mode gets capped at level (or the
        # line's min_zoom if later).
        for oid in oids_by_mode.get(mode, []):
            line_mz = _line_mz(oid)
            effective = max(level, line_mz)
            entry = line_stops.get(oid) or line_stops.get(str(oid)) or {}
            stops = entry.get("stops", []) if isinstance(entry, dict) else entry
            for stop in stops:
                if len(stop) < 3 or not stop[2]:
                    continue
                uic = _uic_of(stop[2], stop_meta)
                if uic and uic in candidate_mz:
                    _maybe_set(uic, effective, f"{mode}: all remaining")

    # ── Apply the per-mode tables ───────────────────────────────────────────
    # Train
    _apply_intersection_or_terminus("train", 7)
    _apply_intercity_train_stops(8)
    _apply_importance_greedy("train", 9, 5.0)
    _apply_importance_greedy("train", 10, 3.0)
    _apply_all_remaining("train", 11)
    # Metro
    _apply_intersection_or_terminus("metro", 10)
    _apply_importance_greedy("metro", 11, 1.0)
    _apply_all_remaining("metro", 12)
    # Ferry — single rule at z10.
    _apply_all_stops_on_visible_mode("ferry", 10)
    # Mountain — single rule at z10 with line-min_zoom carry.
    _apply_all_stops_on_visible_mode("mountain", 10)
    # Regional bus
    _apply_intersection_or_terminus("regional_bus", 10)
    _apply_importance_greedy("regional_bus", 11, 1.0)
    _apply_all_remaining("regional_bus", 12)
    # Tram
    _apply_intersection_or_terminus("tram", 10)
    _apply_importance_greedy("tram", 11, 1.0)
    _apply_all_remaining("tram", 12)
    # Bus
    _apply_intersection_or_terminus("bus", 10)
    _apply_importance_greedy("bus", 11, 1.0)
    _apply_all_remaining("bus", 12)

    # ── Stops follow lines ──────────────────────────────────────────────────
    final: dict = {}
    for uic, entries in uic_serving.items():
        line_mzs = [_line_mz(e["oid"]) for e in entries]
        min_line = min(line_mzs) if line_mzs else UNREACH_Z
        cand = candidate_mz.get(uic, UNREACH_Z)
        mz = max(cand, min_line)
        modes_here = {e["mode"] for e in entries}
        final[uic] = {
            "min_zoom":        int(mz),
            "candidate_min_zoom": int(cand),
            "rule_label":      rule_label.get(uic, ""),
            "is_intersection": is_intersection_flag.get(uic, False),
            "is_terminus":     is_terminus_flag.get(uic, False),
            "tier":            _resolve_stop_tier(modes_here),
        }
    return final


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
# Far-zoom dot dedup
# =============================================================================

def apply_stop_dedup(dot_features):
    """Per-zoom-level dedup pass over far-zoom stop dots. See
    `.claude/concepts/far-zoom-stop-dot-redesign.md` § "Dedup of overlapping
    dots".

    For each integer zoom z ∈ {13, 12, …, 7} (descending), each surviving
    dot may absorb touching lower-priority neighbours. Priority is:

      1. Mode hierarchy — train > mountain/ferry > everything else. A
         strictly higher-ranked dot absorbs a lower-ranked neighbour
         regardless of score.
      2. Within the same rank, higher score absorbs lower score.
      3. Tiebreak on equal score by `stop_id` (lower absorbs).

    Absorption is VISUAL only — the absorber's tier and diameter are NOT
    touched. Only the per-zoom popup list (`lines_json_zN`) grows. The
    absorbed dot's `tippecanoe.minzoom` is raised so it disappears at the
    zoom it was eaten and below.

    Mutates `dot_features` in place. Adds `score_z7..score_z13` (debug
    only) and `lines_json_z7..lines_json_z13` (popup) to participating
    features.
    """
    sd_cfg = _transit_cfg.get("stop_dot_sizing") or {}
    tier_sizes_cfg = sd_cfg.get("tier_sizes") or {}
    tier_diam = {}
    for name, corners in tier_sizes_cfg.items():
        if not isinstance(corners, dict):
            continue
        try:
            tier_diam[name] = (float(corners.get("z7", 2.0)),
                               float(corners.get("z13", 4.0)))
        except (TypeError, ValueError):
            continue
    default_tier = "small_bus"

    dedup_cfg = _transit_cfg.get("stop_dot_dedup") or {}
    min_spacing_px = float(dedup_cfg.get("min_spacing_px", 2.0))

    EARTH_M = 40075016.7
    MEAN_LAT_DEG = 46.5
    cos_lat = cos(radians(MEAN_LAT_DEG))

    def tier_diameter_at(zoom, tier):
        # Slope-continue past z13 so the far-zoom layer keeps growing
        # linearly through z13.99 (the pill takes over at z14). Clamp
        # only at the lower edge z7.
        corners = tier_diam.get(tier) or tier_diam.get(default_tier, (2.0, 4.0))
        z = max(7.0, float(zoom))
        t = (z - 7.0) / 6.0
        return corners[0] + t * (corners[1] - corners[0])

    max_z13_diam = max((c[1] for c in tier_diam.values()), default=18.0)

    def m_per_px(zoom):
        # MapLibre renders at half the standard Web Mercator m/px (it uses
        # 512-px tiles internally, so a given MapLibre zoom corresponds to
        # one zoom higher under the standard 256-px convention). Verified
        # against `map.project([lng, lat])` at z=13 in the browser.
        return (EARTH_M * cos_lat) / (512.0 * (2 ** zoom))

    # Mode hierarchy for dedup. Higher = stronger absorber.
    def _dedup_rank(mode: str) -> int:
        if mode == "train":
            return 2
        if mode in ("mountain", "ferry"):
            return 1
        return 0

    states = []
    for i, feat in enumerate(dot_features):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        p = feat["properties"]
        base_score = float(p.get("stop_score", 0))
        tier = p.get("stop_tier") or default_tier
        mode = p.get("mode", "")
        rank = _dedup_rank(mode)
        lines_raw = p.get("lines_json") or ""
        try:
            lines = json.loads(lines_raw) if lines_raw else []
            if not isinstance(lines, list):
                lines = []
        except (json.JSONDecodeError, TypeError):
            lines = []
        # Effective minzoom: the lowest zoom at which this dot actually
        # renders. Take max of the layer floor (MODE_MINZOOM, baked into
        # the style as the source's minzoom) and the feature's own
        # tippecanoe.minzoom (which may have been raised by salience).
        # A stop not visible at zoom z must not participate in dedup at
        # zoom z — neither as absorber nor as absorbed.
        layer_floor = MODE_MINZOOM.get(mode, 11)
        tipp_minzoom = int((feat.get("tippecanoe") or {}).get("minzoom", layer_floor))
        eff_minzoom = max(layer_floor, tipp_minzoom)
        states.append({
            "idx": i,
            "lon": float(coords[0]),
            "lat": float(coords[1]),
            "stop_id": str(p.get("stop_id", "") or i),
            "tier": tier,
            "rank": rank,
            "base_score": base_score,
            # Per-zoom score: starts at base, grows with absorption. The
            # absorber's own diameter does NOT read this — it stays fixed
            # via `tier`. Kept for popup / debug diagnostics only.
            "score": {z: base_score for z in range(7, 14)},
            "alive": {z: (z >= eff_minzoom) for z in range(7, 14)},
            "eff_minzoom": eff_minzoom,
            "absorbed_max_z": None,
            "lines_per_z": {z: list(lines) for z in range(7, 14)},
            "lines_dirty": False,
        })

    if not states:
        return

    n_absorptions = 0
    for z in range(13, 6, -1):
        mpp = m_per_px(z)
        # Cell size in degrees lat covering the max possible touch distance
        # (two largest possible radii + spacing).
        max_touch_px = max_z13_diam + min_spacing_px
        max_touch_m = max_touch_px * mpp
        cell_deg = max(0.001, max_touch_m / 111320.0)

        for _ in range(20):  # inner stability loop; converges in 2–3 normally
            survivors = [s for s in states if s["alive"][z]]
            # Sort by (rank desc, score desc, stop_id asc) — highest priority
            # absorbers processed first so their claim over an area is stable.
            survivors.sort(key=lambda s: (-s["rank"], -s["score"][z], s["stop_id"]))

            grid = defaultdict(list)
            for s in survivors:
                cx = int(s["lon"] / cell_deg)
                cy = int(s["lat"] / cell_deg)
                grid[(cx, cy)].append(s)

            absorbed_any = False
            for sa in survivors:
                if not sa["alive"][z]:
                    continue
                ra = tier_diameter_at(z, sa["tier"]) / 2.0
                cx_a = int(sa["lon"] / cell_deg)
                cy_a = int(sa["lat"] / cell_deg)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for sb in grid.get((cx_a + dx, cy_a + dy), ()):
                            if sb is sa or not sb["alive"][z]:
                                continue
                            # Mode hierarchy gate. Higher-rank never gets
                            # absorbed by lower-rank, regardless of score.
                            if sb["rank"] > sa["rank"]:
                                continue
                            # Within same rank, break by score / stop_id.
                            if sb["rank"] == sa["rank"]:
                                score_a = sa["score"][z]
                                score_b = sb["score"][z]
                                if score_b > score_a:
                                    continue
                                if score_b == score_a and sb["stop_id"] < sa["stop_id"]:
                                    continue
                            rb = tier_diameter_at(z, sb["tier"]) / 2.0
                            dist_m = haversine_km(sa["lon"], sa["lat"],
                                                  sb["lon"], sb["lat"]) * 1000.0
                            dist_px = dist_m / mpp
                            if dist_px > ra + rb + min_spacing_px:
                                continue
                            # B's contribution only propagates down to zooms
                            # where B itself would render. At zooms below B's
                            # effective minzoom, B isn't visible — absorbing
                            # it there has no visual meaning and must not
                            # inflate the absorber's debug score.
                            z_lo_start = max(7, sb["eff_minzoom"])
                            for z_lo in range(z_lo_start, z + 1):
                                sa["score"][z_lo] += sb["score"][z_lo]
                                sb["alive"][z_lo] = False
                                # Merge absorbed lines into the absorber AT
                                # THIS zoom only — the popup at zoom k only
                                # shows lines folded in at or above k, not
                                # the union across every zoom.
                                # Dedup by (ref, mode) at this zoom.
                                sa_lines = sa["lines_per_z"][z_lo]
                                existing_keys = {(ln.get("ref", ""), ln.get("mode", ""))
                                                 for ln in sa_lines}
                                for ln in sb["lines_per_z"][z_lo]:
                                    key = (ln.get("ref", ""), ln.get("mode", ""))
                                    if key in existing_keys:
                                        continue
                                    existing_keys.add(key)
                                    sa_lines.append(ln)
                                    sa["lines_dirty"] = True
                            sb["absorbed_max_z"] = (z if sb["absorbed_max_z"] is None
                                                    else max(sb["absorbed_max_z"], z))
                            absorbed_any = True
                            n_absorptions += 1
                            # Absorber diameter does NOT grow with score.
                            # `ra` stays as the tier's fixed radius at z.
            if not absorbed_any:
                break

    n_full = n_partial = n_lines_rewritten = 0
    for s in states:
        feat = dot_features[s["idx"]]
        p = feat["properties"]
        for z in range(7, 14):
            p[f"score_z{z}"] = round(s["score"][z], 4)
        if s["absorbed_max_z"] is not None:
            new_minzoom = s["absorbed_max_z"] + 1
            tipp = feat.setdefault("tippecanoe", {})
            old_minzoom = int(tipp.get("minzoom", 0))
            tipp["minzoom"] = max(old_minzoom, new_minzoom)
            if new_minzoom >= 14:
                n_full += 1
            else:
                n_partial += 1
        if s["lines_dirty"]:
            # Per-zoom lines_json: each `lines_json_zN` reflects the lines
            # this dot represents at zoom N (base lines plus everything
            # absorbed at or above N). Base `lines_json` is left untouched
            # — the pill-zoom layer (z=14+) reads it and shows the dot's
            # native lines without any far-zoom dedup growth.
            for z in range(7, 14):
                lns_sorted = sorted(s["lines_per_z"][z], key=lambda ln: (
                    MODE_RANK.get(ln.get("mode", ""), 99),
                    ln.get("ref", "")))
                p[f"lines_json_z{z}"] = json.dumps(lns_sorted, ensure_ascii=False)
            n_lines_rewritten += 1
    print(f"  Dedup: {n_absorptions:,} absorptions "
          f"({n_full:,} stops fully absorbed at far-zoom, "
          f"{n_partial:,} partially absorbed, "
          f"{n_lines_rewritten:,} absorber popups extended)")


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
                "color":           p["color"],
                "mode":            p["mode"],
                "mountain_origin": p.get("mountain_origin"),
                "width_base":      p.get("width_base", 3.0),
                "freq_score":      p.get("freq_score", 0.0),
                "f_weighted":      p.get("f_weighted", 0.0),
                "speed_kmh":       p.get("speed_kmh"),
                "salience":        p.get("salience"),
                "min_zoom":        p.get("min_zoom"),
                "coords":          feat["geometry"]["coordinates"],
                "ref":             p.get("ref", ""),
                "name":            p.get("name", ""),
                "agency_id":       p.get("agency_id", ""),
            }
        if p.get("gtfs_stops"):
            gtfs_stop_features.append(feat)
    print(f"  {len(line_lookup):,} lines, {len(gtfs_stop_features):,} with embedded gtfs_stops")

    # Sibling index for the missing-range fill rule (tram/bus/regional_bus):
    # {(ref, agency_id, mode) → [(osm_id, flat_polyline)]}. The two-metre
    # proximity gate inside _borrow_backward_segment does the real filtering;
    # this index just bounds the search to same-line variants.
    #
    # The all-lines spatial index alongside is the non-sibling-borrow
    # backing (concept step 2): same 2 m proximity + 15° tangent gates,
    # widened to any drawn polyline. Built once, read by _platform_extent
    # via the module-level `_ALL_LINES_INDEX`.
    global _ALL_LINES_INDEX
    sibling_groups: dict = defaultdict(list)
    oid_sibling_key: dict = {}
    _ALL_LINES_INDEX = _AllLinesIndex()
    for oid_s, info in line_lookup.items():
        key = (info.get("ref", ""), info.get("agency_id", ""), info.get("mode", ""))
        flat_poly = flatten_coords(info["coords"])
        if len(flat_poly) >= 2:
            sibling_groups[key].append((oid_s, flat_poly))
            oid_sibling_key[oid_s] = key
            _ALL_LINES_INDEX.add(oid_s, key, flat_poly, _cum_dist_m(flat_poly))

    print("Loading stop coordinates and metadata...")
    line_stops = json.loads(LINE_STOPS.read_text())
    stop_meta  = load_stop_meta()
    print(f"  {len(line_stops):,} lines with stops, {len(stop_meta):,} GTFS stop entries")

    # ── Zoom-level rules: per-mode stop min_zoom ─────────────────────────────
    # See .claude/concepts/zoom-level-rules.md.
    print("Building per-UIC line index...")
    uic_serving, coords_by_uic = _build_uic_serving(
        line_lookup, line_stops, stop_meta)
    print(f"  {len(uic_serving):,} canonical UICs across "
          f"{sum(len(v) for v in uic_serving.values()):,} (line, stop) pairs")

    zr_cfg = _zoom_rules_cfg()

    # Urbanness — building counts at two radii per UIC.
    print("Loading OSM building centroids...")
    buildings = load_buildings()
    print(f"  {len(buildings):,} building centroids")
    urb_cfg = zr_cfg.get("urbanness") or {}
    r_in = float(urb_cfg.get("radius_inner_m", 200))
    r_out = float(urb_cfg.get("radius_outer_m", 500))
    print(f"  Counting buildings within {r_in:g}m / {r_out:g}m per UIC...")
    building_counts = count_buildings_in_radii(coords_by_uic, buildings,
                                               r_in, r_out)
    urbanness = compute_urbanness(building_counts, urb_cfg)
    OUT_URBANNESS.write_text(json.dumps(urbanness, ensure_ascii=False))
    bracket_counts = defaultdict(int)
    for v in urbanness.values():
        bracket_counts[v["bracket"]] += 1
    print(f"  Urbanness brackets: " +
          ", ".join(f"{k}={v}" for k, v in sorted(bracket_counts.items())) +
          f" → {OUT_URBANNESS}")

    # Dwell per UIC (avg dep − arr across all trip-stop rows).
    print("Computing per-UIC dwell from stop_times.txt...")
    dwell_by_uic = compute_dwell_per_uic(stop_meta)
    if dwell_by_uic:
        avgs = list(dwell_by_uic.values())
        print(f"  {len(dwell_by_uic):,} UICs with dwell data; "
              f"mean {sum(avgs)/len(avgs):.1f}s, "
              f"max {max(avgs):.0f}s")

    # Stop importance score (4 categories, sum).
    si_cfg = zr_cfg.get("stop_importance") or {}
    nt_radius = float(si_cfg.get("nearby_transit_radius_m", 1000))
    importance_by_uic = compute_stop_importance(
        uic_serving, coords_by_uic, urbanness, dwell_by_uic, nt_radius)
    imp_counts = defaultdict(int)
    for s in importance_by_uic.values():
        imp_counts[s] += 1
    print(f"  Importance scores: " +
          ", ".join(f"{k}={imp_counts[k]}" for k in sorted(imp_counts.keys())))

    # Intercity oid set (matches the train rule in 06).
    intercity_prefixes_cfg = zr_cfg.get("intercity_route_prefixes") or \
        ["IC", "ICE", "EC"]
    intercity_prefixes = tuple(str(p).upper() for p in intercity_prefixes_cfg)
    intercity_oids: set = set()
    for oid, info in line_lookup.items():
        if info.get("mode") != "train":
            continue
        r = (info.get("ref") or "").strip().upper()
        if any(r.startswith(p) for p in intercity_prefixes):
            intercity_oids.add(str(oid))

    print("Applying per-mode stop rules...")
    stop_min_zoom = compute_stop_min_zoom(
        line_lookup, line_stops, stop_meta,
        importance_by_uic, intercity_oids,
        uic_serving, coords_by_uic,
    )
    if stop_min_zoom:
        mzs = [v["min_zoom"] for v in stop_min_zoom.values()]
        mz_counts = defaultdict(int)
        for v in mzs:
            mz_counts[v] += 1
        print(f"  {len(stop_min_zoom):,} UICs scored. "
              f"min_zoom distribution: " +
              ", ".join(f"z{k}={mz_counts[k]}"
                        for k in sorted(mz_counts.keys())))

    # Pack into `stop_salience` shape used by the rest of main() — every
    # downstream block reads `min_zoom` and the few diagnostic keys below.
    stop_salience: dict = {}
    for uic, v in stop_min_zoom.items():
        stop_salience[uic] = {
            "min_zoom":           v["min_zoom"],
            "candidate_min_zoom": v["candidate_min_zoom"],
            "rule_label":         v["rule_label"],
            "is_intersection":    v["is_intersection"],
            "is_terminus":        v["is_terminus"],
            "tier":               v["tier"],
            "importance_score":   importance_by_uic.get(uic, 0),
            "urbanness_bracket":  urbanness.get(uic, {}).get("bracket", "rural"),
        }

    print("Loading atlas platform attributes...")
    stop_attrs = write_stop_attributes_diag(line_stops)

    print("Loading OSM rail ways for terminal extension...")
    rail_idx = _load_rail_index(RAIL_WAYS_GEOJSON)

    print("Extending train and mountain rail-like polylines at terminal stops...")
    end_of_platform_pairs = _extend_polylines_at_terminals(
        line_lookup, line_stops, rail_idx, PILL_CFG, stop_attrs)

    # Sync extended polylines back into lines_data so transit_lines.geojson
    # on disk reflects the new geometry — step 08's pmtile build reads the file,
    # not the in-memory line_lookup. Same scope as _extend_polylines_at_terminals:
    # train + mountain rail-like (rebucketed_rail / rack).
    n_synced = 0
    for feat in lines_data["features"]:
        props = feat.get("properties") or {}
        mode = props.get("mode")
        mo = props.get("mountain_origin")
        if mode != "train" and not (
                mode == "mountain" and mo in MOUNTAIN_RAIL_ORIGINS):
            continue
        oid = str(props.get("osm_id", ""))
        if not oid:
            continue
        info = line_lookup.get(oid)
        if not info or "coords" not in info:
            continue
        feat["geometry"]["type"] = "LineString"
        feat["geometry"]["coordinates"] = [list(c) for c in info["coords"]]
        n_synced += 1
    LINES.write_text(json.dumps(lines_data, ensure_ascii=False))
    print(f"  Wrote {n_synced:,} extended polylines back to {LINES.name}")

    skip_first_oids, skip_last_oids = compute_terminus_skip_oids(
        line_stops, line_lookup, stop_meta)
    print(f"  Terminus dedup: {len(skip_first_oids):,} departure-side entries "
          f"will be omitted from rendering (popup retains both directions)")
    print(f"  Arrival drop (tram/bus/regional_bus): {len(skip_last_oids):,} "
          f"unpaired or layover-shadowed arrival entries omitted from pill construction")

    print("Emitting debug platform extents...")
    write_debug_platforms(line_stops, line_lookup, stop_attrs,
                          skip_first_oids, skip_last_oids,
                          sibling_groups, oid_sibling_key,
                          end_of_platform_pairs)

    print("Building stop dots and pill candidates...")

    rail_pill_raw     = []   # dicts for rail pill clustering (also used for dots)
    all_nonrail_pills = []   # ALL non-rail pill modes combined (tram+bus+metro+regional_bus)
    other_features    = []   # dot features for non-rail, ferry, mountain
    indicator_features = []  # mini per-color-group dots inside stop dots/discs/pills (z16+)
    # Per-line ferry-stop snap candidates; aggregated by parent_station after
    # the per-line loop. See pill-rendering.md § "Ferry stops".
    ferry_candidates  = []

    # --- Mountain / straight-line features with embedded gtfs_stops ---
    for feat in gtfs_stop_features:
        p       = feat["properties"]
        color   = p["color"]
        mode    = p["mode"]
        wb      = p.get("width_base", 3.0)
        coords  = feat["geometry"]["coordinates"]
        minzoom = MODE_MINZOOM.get(mode, 11)
        oid     = str(p.get("osm_id", ""))
        for lon, lat in p["gtfs_stops"]:
            slon, slat = snap_to_line(lon, lat, coords)
            other_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": minzoom},
                "geometry": {"type": "Point", "coordinates": [slon, slat]},
                "properties": {"color": color, "mode": mode,
                               "width_base": _stop_wb(wb, mode)},
            })
            indicator_features.extend(build_indicator_features(
                [{"osm_id": oid, "width_base": wb, "mode": mode}],
                slon, slat, line_lookup,
                parent_width_base=_stop_wb(wb, mode), parent_mode=mode))
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
        mo         = line.get("mountain_origin")
        width_base = line["width_base"]
        coords     = line["coords"]
        minzoom    = MODE_MINZOOM.get(mode, 11)
        flat       = flatten_coords(coords)

        skip_first_here = str(osm_id) in skip_first_oids
        skip_last_here = str(osm_id) in skip_last_oids
        last_idx = len(stop_coords) - 1
        sib_key = oid_sibling_key.get(str(osm_id))
        siblings = sibling_groups.get(sib_key, []) if sib_key else []

        # Rail clustering pool (300 m radius): train, plus mountain origins
        # that share station-scale geometry with rail — rebucketed_rail / rack
        # (centred ±L/2 with OSM rail walk at terminals) and aerial (fixed
        # dot, extent=None; in the rail pool so it co-clusters with rack at
        # Eigergletscher).
        # Funicular goes to the **non-rail** pool below: its endpoint stops
        # are often within 300 m of each other along a short line (Marzilibahn
        # 108 m), which the 300 m rail radius merges into a single centroid
        # dot.  The 50 m non-rail radius keeps each endpoint distinct while
        # still co-clustering with adjacent tram/bus stops (Polybahn at
        # Zürich Central etc.).
        in_rail_pool = (
            mode in RAIL_MODES
            or (mode == "mountain" and mo in MOUNTAIN_RAIL_ORIGINS | {"aerial"})
        )
        funicular_in_nonrail_pool = (mode == "mountain" and mo == "funicular")

        if in_rail_pool:
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
                is_eop = (str(osm_id), sid) in end_of_platform_pairs
                extent = _platform_extent(lon, lat, flat, mode, atlas_len, PILL_CFG,
                                          osm_id=str(osm_id), siblings=siblings,
                                          end_of_platform=is_eop,
                                          mountain_origin=mo)
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
                    "platform_code":  meta.get("platform_code", ""),
                    "extent":         extent,
                })

        elif mode == "ferry":
            # Defer ferry-stop emission to the post-loop aggregation pass —
            # the canonical on-line position depends on every line visiting
            # the pier, not just this one. See "Ferry stop aggregation" below.
            for idx, entry in enumerate(stop_coords):
                if idx == 0 and skip_first_here:
                    continue
                if idx == last_idx and skip_last_here:
                    continue
                lon, lat = entry[0], entry[1]
                sid      = entry[2] if len(entry) > 2 else ""
                meta     = stop_meta.get(sid, {})
                ferry_candidates.append({
                    "gtfs_lon":       lon,
                    "gtfs_lat":       lat,
                    "stop_id":        sid,
                    "stop_name":      meta.get("name", ""),
                    "parent_station": meta.get("parent", ""),
                    "color":          color,
                    "osm_id":         osm_id,
                    "line":           line,
                    "polyline":       flat,
                    "minzoom":        minzoom,
                })

        elif mode in PILL_MODES or funicular_in_nonrail_pool:
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
                atlas_len = (stop_attrs.get(sid, {}) or {}).get("length")
                # Funicular: pin the snap to the polyline endpoint when the
                # extent reaches it (mountain-line-pills concept). Otherwise
                # use the regular polyline projection.
                if funicular_in_nonrail_pool:
                    override = _funicular_snap_override(
                        lon, lat, flat, atlas_len, PILL_CFG)
                    cx, cy = override if override is not None else snap_to_line(lon, lat, flat)
                else:
                    cx, cy = snap_to_line(lon, lat, flat)
                extent = _platform_extent(lon, lat, flat, mode, atlas_len, PILL_CFG,
                                          osm_id=str(osm_id), siblings=siblings,
                                          mountain_origin=mo)
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
                        "width_base":     _stop_wb(width_base, mode),
                        "stop_id":        sid,
                        "stop_name":      meta.get("name", ""),
                        "parent_station": meta.get("parent", ""),
                        "lines_json":     line_lines_json,
                    },
                })
                indicator_features.extend(build_indicator_features(
                    [{"osm_id": str(osm_id), "width_base": width_base, "mode": mode}],
                    slon, slat, line_lookup,
                    parent_width_base=_stop_wb(width_base, mode),
                    parent_mode=mode))

    # --- Ferry stop aggregation (parent_station → one disc) ---------------
    # Group ferry candidates by parent_station (or stop_id when no parent),
    # run the closest-vertex medoid to find the pier's canonical OSM node,
    # and emit pill endpoint / connector features only. No separate dot
    # circle in transit_stops.geojson — every ferry stop renders through
    # the non-rail pill paint stack, so the connector seam handling comes
    # for free. Ferry stops are invisible below z11 (same as bus stops);
    # ferry lines themselves still appear from z9.
    #
    # Two-tier zoom split per pier:
    #
    #   z11–z12  (FERRY_PILL_MZ): every pier shows EXACTLY ONE endpoint
    #     at the canonical-vertex medoid — same "one dot per pier" pattern
    #     bus / tram stops follow at their own PILL_MINZOOM. No GTFS-side
    #     dot, no connector, no per-line detail.
    #
    #   z13+     (FERRY_PAIR_MZ): pier detail appears in addition to the
    #     canonical dot:
    #       * Convergent + split (GTFS↔canonical ≥ collapse_threshold_m):
    #           a GTFS-side endpoint + connector between the two dots.
    #       * Non-convergent (max-vertex-distance > convergence_threshold_m):
    #           per-line endpoints, one at each line's individual closest-
    #           point snap to GTFS. The canonical medoid emitted at z11
    #           still sits on (or very near) one of these — a small
    #           acceptable overlap.
    #       * Convergent + collapsed: nothing extra.
    #
    # See pill-rendering.md § "Ferry stops".
    ferry_by_pier: dict = {}
    for cand in ferry_candidates:
        pier_key = cand["parent_station"] or cand["stop_id"]
        ferry_by_pier.setdefault(pier_key, []).append(cand)

    ferry_pill_features = []
    n_ferry_collapsed = 0
    n_ferry_split = 0
    n_ferry_diverged = 0
    # Pill (medium-zoom) and pair (split detail) both appear from z14 — the
    # same zoom every other mode starts at (see pill-rendering.md § "Dot-to-
    # pill zoom switch"). The z9–z13 far-zoom marker for ferry is a low-zoom
    # dot emitted into `other_features` below, matching every other mode's
    # far-zoom behaviour. See far-zoom-stop-markers.md § "Ferry far-zoom
    # marker". Ferry uses a single variant only (no design bands — see
    # pill-rendering.md § "Ferry stops").
    FERRY_PILL_MZ = 14        # convergence-point endpoint, per-line endpoints
    FERRY_PAIR_MZ = 14        # split-case GTFS endpoint + connector
    FERRY_FAR_ZOOM_MZ = MODE_MINZOOM.get("ferry", 9)
    for pier_key, cands in ferry_by_pier.items():
        gtfs_repr = (cands[0]["gtfs_lon"], cands[0]["gtfs_lat"])

        # Aggregate all lines visiting this pier into one lines_json blob —
        # the popup at the pier should list every ferry line, not just the
        # one whose feature spawned the dot.
        lines_seen = set()
        lines_json_list = []
        for c in cands:
            line = c["line"] or {}
            ref = line.get("gtfs_ref") or line.get("ref", "")
            name = line.get("name", "")
            key = (ref, name)
            if key in lines_seen:
                continue
            lines_seen.add(key)
            lines_json_list.append({
                "ref":   ref,
                "color": c["color"],
                "mode":  "ferry",
                "name":  name,
            })
        lines_json_str = json.dumps(lines_json_list)

        rep = cands[0]
        base_props = {
            "color":          rep["color"],
            "mode":           "ferry",
            "width_base":     _stop_wb(FERRY_DOT_WB, "ferry"),
            "stop_id":        rep["stop_id"],
            "stop_name":      rep["stop_name"],
            "parent_station": rep["parent_station"],
            "lines_json":     lines_json_str,
        }
        indicator_stubs = [{"osm_id": str(c["osm_id"]), "mode": "ferry"} for c in cands]

        # Dedup polylines by osm_id — the same line can visit the pier twice
        # (e.g. an arrival + departure entry) and we only want it counted once.
        seen_oids = set()
        polylines = []
        for c in cands:
            oid = c["osm_id"]
            if oid in seen_oids:
                continue
            seen_oids.add(oid)
            polylines.append(c["polyline"])

        canon, max_vertex_dist_m = _ferry_canonical_snap(polylines, gtfs_repr)

        # Far-zoom dot at canonical pier position — the ferry intersection-
        # search result. Rendered through the low-zoom dot paint stack
        # (transit_stops.geojson), matching every other mode's far-zoom
        # behaviour. Maxzoom = FERRY_PILL_MZ - 1 so the dot disappears at
        # exactly the zoom where the medium-zoom endpoint disc takes over.
        other_features.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": FERRY_FAR_ZOOM_MZ,
                            "maxzoom": FERRY_PILL_MZ - 1},
            "geometry": {"type": "Point", "coordinates": [canon[0], canon[1]]},
            "properties": dict(base_props),
        })

        # Medium-zoom canonical-vertex endpoint at FERRY_PILL_MZ. From z13
        # this is the disc the user sees. Split-case GTFS endpoint and
        # per-line endpoints (non-convergent case) also appear from
        # FERRY_PAIR_MZ upward.
        ferry_pill_features.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": FERRY_PILL_MZ},
            "geometry": {"type": "Point", "coordinates": [canon[0], canon[1]]},
            "properties": {**base_props, "feature_type": "endpoint"},
        })
        indicator_features.extend(build_indicator_features(
            indicator_stubs, canon[0], canon[1], line_lookup))

        if max_vertex_dist_m > FERRY_CONVERGE_M:
            # Non-convergent: per-line endpoints at each line's own snap, as
            # detail above FERRY_PAIR_MZ. The canonical-vertex endpoint
            # emitted above is the medoid of the per-line closest-vertices,
            # so at z13+ it sits on (or very near) one of the per-line
            # endpoints — a small acceptable overlap.
            n_ferry_diverged += 1
            for c in cands:
                slon, slat = snap_to_line(c["gtfs_lon"], c["gtfs_lat"],
                                          c["polyline"])
                # Endpoint pull (see _ferry_canonical_snap docstring):
                # prefer the closer polyline endpoint when it's within
                # FERRY_ENDPOINT_PULL_M of the closest-segment snap, so
                # the dot lands on the OSM ferry-pier node rather than a
                # routing waypoint in the water.
                pl = c["polyline"]
                if pl:
                    endpoints = (pl[0], pl[-1])
                    closer_ep = min(endpoints,
                                    key=lambda v: (v[0] - slon) ** 2
                                                  + (v[1] - slat) ** 2)
                    if haversine_km(slon, slat, closer_ep[0], closer_ep[1]) \
                            * 1000.0 <= FERRY_ENDPOINT_PULL_M:
                        slon, slat = float(closer_ep[0]), float(closer_ep[1])
                ferry_pill_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": FERRY_PAIR_MZ},
                    "geometry": {"type": "Point", "coordinates": [slon, slat]},
                    "properties": {**base_props,
                                   "stop_id":      c["stop_id"],
                                   "stop_name":    c["stop_name"],
                                   "feature_type": "endpoint"},
                })
                indicator_features.extend(build_indicator_features(
                    [{"osm_id": str(c["osm_id"])}],
                    slon, slat, line_lookup))
            continue

        dist_m = haversine_km(gtfs_repr[0], gtfs_repr[1],
                              canon[0], canon[1]) * 1000.0
        if dist_m < FERRY_COLLAPSE_M:
            n_ferry_collapsed += 1
            continue

        # Convergent + split: add GTFS-side endpoint + connector at the pill
        # detail threshold. The canonical-vertex endpoint (emitted above)
        # plus this GTFS endpoint give the connector a disc at each end; the
        # existing pill paint stack (connector casing → connector fill →
        # endpoint disc) handles the dot↔connector seam at both joints.
        n_ferry_split += 1
        ferry_pill_features.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": FERRY_PAIR_MZ},
            "geometry": {"type": "Point",
                         "coordinates": [gtfs_repr[0], gtfs_repr[1]]},
            "properties": {**base_props, "feature_type": "endpoint"},
        })
        ferry_pill_features.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": FERRY_PAIR_MZ},
            "geometry": {"type": "LineString",
                         "coordinates": [
                             [gtfs_repr[0], gtfs_repr[1]],
                             [canon[0], canon[1]],
                         ]},
            "properties": {**base_props,
                           "width_base":   FERRY_CONNECTOR_WB,
                           "feature_type": "connector"},
        })
    print(f"  Ferry stops: {len(ferry_by_pier):,} piers "
          f"({n_ferry_split:,} split, {n_ferry_collapsed:,} collapsed, "
          f"{n_ferry_diverged:,} per-line fallback)")

    # Per-stop-id set of lines (osm_ids), used by cluster_stops_for_pills to
    # block merging of two stops served by the same drawn line. See
    # `pill-cluster-same-line-guard.md`.
    lines_of_stop: dict = defaultdict(set)
    for _oid_k, _ls_v in line_stops.items():
        _seq = _ls_v.get("stops", []) if isinstance(_ls_v, dict) else _ls_v
        for _entry in _seq:
            if len(_entry) > 2 and _entry[2]:
                lines_of_stop[_entry[2]].add(str(_oid_k))

    # --- Rail dots + pills (unified pass) ---
    print(f"  {len(rail_pill_raw):,} raw rail stop positions → clustering...")
    rail_pill_clusters = cluster_stops_for_pills(
        rail_pill_raw, PILL_CLUSTER_RAIL_KM, lines_of_stop)
    rail_pill_clusters = merge_clusters_by_parent_station(rail_pill_clusters)
    print(f"  → {len(rail_pill_clusters):,} rail station clusters")
    # Place dots via tangent grouping + perpendicular sweep along the central
    # member's platform extent (per-group). Stabbed dots get placed on the
    # perpendicular bar; leftovers run through the old algorithm.
    print(f"  Placing rail dots across {len(rail_pill_clusters):,} clusters...")
    for c in rail_pill_clusters:
        # Preserve pre-placement pfaedle snaps for the far-zoom-dot
        # intersection search and tiebreak centre. coordinate_dots_global_stab
        # rewrites lon/lat to the placed positions.
        for s in c:
            s["snap_lon"] = s["lon"]
            s["snap_lat"] = s["lat"]
        coordinate_dots_global_stab(c, PROTECTION_RADIUS_RAIL_M,
                                    LONE_OUTLIER_GAP_RAIL_METRO_M)
    print("  → rail dot placement done")

    rail_features = []
    pill_features_rail = []
    for cluster in rail_pill_clusters:
        stop_count = count_unique_lines(cluster)
        mz = pill_minzoom("train", stop_count)

        color, mode, max_wb, dom_stop = dominant_line(cluster)
        centroid_lon = sum(s["lon"] for s in cluster) / len(cluster)
        centroid_lat = sum(s["lat"] for s in cluster) / len(cluster)
        lines_json_str = json.dumps(cluster_lines(cluster, line_lookup))
        centroid_props = {
            "color":          color,
            "mode":           mode,
            "width_base":     _stop_wb(max_wb, mode),
            "stop_id":        dom_stop.get("stop_id", ""),
            "stop_name":      dom_stop.get("stop_name", ""),
            "parent_station": dom_stop.get("parent_station", ""),
            "lines_json":     lines_json_str,
        }

        if mz is None:
            # Single-line station: one cluster dot at all zooms. Rule chain
            # falls through to the centroid (no pill, no disc, single line
            # ⇒ no intersection).
            rail_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": 5},
                "geometry": {"type": "Point", "coordinates": [centroid_lon, centroid_lat]},
                "properties": centroid_props,
            })
            indicator_features.extend(build_indicator_features(
                cluster, centroid_lon, centroid_lat, line_lookup))
        else:
            # Bake band C first — its features drive the far-zoom-dot
            # decision and the pill-collapse fallback (matches previous
            # behavior). Then bake A and B on top with different
            # PILL_GAP_ANGLED_M / CURVE_MIN_RADIUS_M values, tagged with
            # per-feature `design_band` + tippecanoe zoom range.
            _set_pill_design_band(PILL_DESIGN_BANDS["C"])
            c_feats = make_pill_features(cluster, mz, lines_json_str, line_lookup)
            if c_feats:
                _tag_band_features(c_feats, "C", PILL_DESIGN_BANDS["C"])
                all_band_feats = list(c_feats)
                for _band_id in ("A", "B"):
                    _set_pill_design_band(PILL_DESIGN_BANDS[_band_id])
                    _bfeats = make_pill_features(cluster, mz, lines_json_str, line_lookup)
                    _tag_band_features(_bfeats, _band_id, PILL_DESIGN_BANDS[_band_id])
                    all_band_feats.extend(_bfeats)
                # Far-zoom dot from band C. Rail-like family skips the
                # intersection search; rule picks largest pill (by line
                # count) → largest disc → centroid.
                dot_lon, dot_lat = far_zoom_dot_position(
                    cluster, c_feats, line_lookup,
                    (centroid_lon, centroid_lat), rail_like=True)
                rail_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": 5, "maxzoom": mz - 1},
                    "geometry": {"type": "Point", "coordinates": [dot_lon, dot_lat]},
                    "properties": centroid_props,
                })
                pill_features_rail.extend(all_band_feats)
            else:
                # Multi-line cluster whose pill collapsed (all positions
                # deduped to one point) — no pill is emitted, so the
                # cluster dot stays visible at all zooms at the centroid.
                # Bands A and B share the same dot placement so they
                # collapse identically; no fallback bake needed.
                rail_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": 5},
                    "geometry": {"type": "Point", "coordinates": [centroid_lon, centroid_lat]},
                    "properties": centroid_props,
                })
                indicator_features.extend(build_indicator_features(
                    cluster, centroid_lon, centroid_lat, line_lookup))

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
    nonrail_clusters = cluster_stops_for_pills(
        all_nonrail_pills, PILL_CLUSTER_NONRAIL_KM, lines_of_stop)
    nonrail_clusters = merge_clusters_by_parent_station(nonrail_clusters)
    # Same global stabbing placement as rail.
    print(f"  Placing non-rail dots across {len(nonrail_clusters):,} clusters...")
    for c in nonrail_clusters:
        _, dom_mode, _, _ = dominant_line(c)
        lone_outlier_m = (LONE_OUTLIER_GAP_RAIL_METRO_M
                          if dom_mode == "metro"
                          else LONE_OUTLIER_GAP_BUS_TRAM_M)
        # Preserve pre-placement pfaedle snaps for the far-zoom-dot
        # intersection search and tiebreak centre.
        for s in c:
            s["snap_lon"] = s["lon"]
            s["snap_lat"] = s["lat"]
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

        # rail_like decides whether the far-zoom rule runs the intersection
        # search. Mountain rebucketed_rail / rack ride the non-rail pool
        # (rare — most rebucketed_rail trips cluster with regular train), so
        # detect them by dominant mode/mountain_origin here.
        cluster_rail_like = (dom_mode == "train") or (
            dom_mode == "mountain"
            and any(s.get("mountain_origin") in MOUNTAIN_RAIL_ORIGINS
                    for s in cluster))

        centroid_lon = sum(s["lon"] for s in cluster) / len(cluster)
        centroid_lat = sum(s["lat"] for s in cluster) / len(cluster)
        mode_minzoom = min(MODE_MINZOOM.get(s["mode"], 11) for s in cluster)
        lines_json_str = json.dumps(cluster_lines(cluster, line_lookup))
        centroid_props = {
            "color":          color,
            "mode":           dom_mode,
            "width_base":     _stop_wb(max_wb, dom_mode),
            "stop_id":        dom_stop.get("stop_id", ""),
            "stop_name":      dom_stop.get("stop_name", ""),
            "parent_station": dom_stop.get("parent_station", ""),
            "lines_json":     lines_json_str,
        }

        if mz is None:
            # Single-line stop: one cluster dot at all zooms. Rule chain
            # falls through to centroid (intersection needs ≥2 lines).
            nonrail_dot_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": mode_minzoom},
                "geometry": {"type": "Point", "coordinates": [centroid_lon, centroid_lat]},
                "properties": centroid_props,
            })
            indicator_features.extend(build_indicator_features(
                cluster, centroid_lon, centroid_lat, line_lookup))
        else:
            # Bake band C first (its features drive the far-zoom-dot and
            # collapse decision); then bake A and B on top with per-band
            # thresholds. See rail block above for rationale.
            _set_pill_design_band(PILL_DESIGN_BANDS["C"])
            c_feats = make_pill_features(cluster, mz, lines_json_str, line_lookup)
            if c_feats:
                _tag_band_features(c_feats, "C", PILL_DESIGN_BANDS["C"])
                all_band_feats = list(c_feats)
                for _band_id in ("A", "B"):
                    _set_pill_design_band(PILL_DESIGN_BANDS[_band_id])
                    _bfeats = make_pill_features(cluster, mz, lines_json_str, line_lookup)
                    _tag_band_features(_bfeats, _band_id, PILL_DESIGN_BANDS[_band_id])
                    all_band_feats.extend(_bfeats)
                # Non-rail family runs the intersection search first — at
                # a crossroads the dot sits at the junction, not at the
                # platform centroid.
                dot_lon, dot_lat = far_zoom_dot_position(
                    cluster, c_feats, line_lookup,
                    (centroid_lon, centroid_lat),
                    rail_like=cluster_rail_like)
                nonrail_dot_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": mode_minzoom, "maxzoom": mz - 1},
                    "geometry": {"type": "Point", "coordinates": [dot_lon, dot_lat]},
                    "properties": centroid_props,
                })
                pill_features.extend(all_band_feats)
                nonrail_pill_count += len(all_band_feats)
            else:
                # Pill collapsed — cluster dot stays at all zooms at the
                # centroid (no pill, no disc, fall-through case).
                nonrail_dot_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": mode_minzoom},
                    "geometry": {"type": "Point", "coordinates": [centroid_lon, centroid_lat]},
                    "properties": centroid_props,
                })
                indicator_features.extend(build_indicator_features(
                    cluster, centroid_lon, centroid_lat, line_lookup))

    print(f"  → {nonrail_pill_count} non-rail pill/connector features "
          f"from {len(nonrail_clusters):,} clusters")

    # ==========================================================================
    # Apply per-UIC min_zoom to stop dots
    # ==========================================================================
    # Each stop dot in transit_stops.geojson carries a stop_id / parent_station
    # in its properties. Resolve to canonical UIC and override the feature's
    # tippecanoe.minzoom from stop_min_zoom. Dots without a resolvable UIC
    # (mountain/straight-line embedded gtfs_stops without stop_id) keep their
    # mode-derived minzoom.
    dot_features = rail_features + other_features + nonrail_dot_features

    # ==========================================================================
    # Attach per-stop tier + score (far-zoom-stop-dot-redesign.md)
    # ==========================================================================
    # The style reads `stop_tier` and looks up the diameter from a per-tier
    # table. `stop_score` is kept alongside for debug / diagnostics. Both
    # are per parent UIC; for each dot we resolve the UIC from
    # `parent_station` (falling back to the platform-stripped `stop_id`).
    # Dots without a resolvable UIC fall back to `small_bus`.
    stop_scores_lookup = load_stop_scores()
    if stop_scores_lookup:
        n_scored = 0
        for feat in dot_features:
            p = feat["properties"]
            uic = p.get("parent_station") or (
                (p.get("stop_id") or "").split(":")[0])
            record = stop_scores_lookup.get(uic) if uic else None
            if record:
                p["stop_score"] = round(record["score"], 4)
                p["stop_tier"] = record["tier"]
                if record["score"] > 0:
                    n_scored += 1
            else:
                p["stop_score"] = 0.0
                p["stop_tier"] = "small_bus"
        print(f"  stop_score/stop_tier attached to {len(dot_features):,} "
              f"dot features ({n_scored:,} with non-zero score)")
    else:
        print(f"  WARNING: {STOP_SCORES.name} not found — every dot will "
              "render at the smallest tier")
        for feat in dot_features:
            feat["properties"]["stop_score"] = 0.0
            feat["properties"]["stop_tier"] = "small_bus"

    if stop_salience:
        n_applied = 0
        for feat in dot_features:
            p = feat["properties"]
            uic = p.get("parent_station") or (
                (p.get("stop_id") or "").split(":")[0])
            if not uic:
                continue
            sal = stop_salience.get(uic)
            if not sal:
                continue
            tipp = feat.setdefault("tippecanoe", {})
            tipp["minzoom"] = int(sal["min_zoom"])
            p["min_zoom"] = sal["min_zoom"]
            p["tier"] = sal["tier"]
            p["importance_score"] = sal["importance_score"]
            p["urbanness_bracket"] = sal["urbanness_bracket"]
            p["is_intersection"] = sal["is_intersection"]
            p["is_terminus"] = sal["is_terminus"]
            n_applied += 1
        print(f"  min_zoom applied to {n_applied:,}/{len(dot_features):,} dot features")

    # ==========================================================================
    # Far-zoom dot dedup
    # ==========================================================================
    print("Applying far-zoom dot dedup...")
    apply_stop_dedup(dot_features)

    # ==========================================================================
    # Salience diagnostic (per-line salience + per-stop rule placement)
    # ==========================================================================
    OUT_SALIENCE = ROOT / "data" / "transit" / "salience.json"
    line_diag = []
    for oid, info in line_lookup.items():
        if info.get("salience") is None:
            continue
        line_diag.append({
            "osm_id":     oid,
            "ref":        info.get("ref", ""),
            "name":       info.get("name", ""),
            "mode":       info.get("mode", ""),
            "agency_id":  info.get("agency_id", ""),
            "f_weighted": info.get("f_weighted", 0.0),
            "speed_kmh":  info.get("speed_kmh"),
            "salience":   info.get("salience"),
            "min_zoom":   info.get("min_zoom"),
        })
    stop_diag = []
    for uic, v in stop_salience.items():
        stop_diag.append({"uic": uic, **v})
    OUT_SALIENCE.write_text(json.dumps(
        {"lines": line_diag, "stops": stop_diag}, ensure_ascii=False))
    print(f"  Diagnostic: {len(line_diag)} lines, "
          f"{len(stop_diag)} stops → {OUT_SALIENCE}")

    # ==========================================================================
    # Write outputs
    # ==========================================================================

    OUT_DOTS.parent.mkdir(parents=True, exist_ok=True)
    OUT_DOTS.write_text(json.dumps({"type": "FeatureCollection", "features": dot_features}))
    pill_features.extend(ferry_pill_features)
    pill_features.extend(indicator_features)
    OUT_PILLS.write_text(json.dumps({"type": "FeatureCollection", "features": pill_features}))

    print("Emitting close-zoom stop features...")
    write_close_zoom_features(line_stops, line_lookup, stop_meta, stop_attrs,
                              sibling_groups, oid_sibling_key,
                              end_of_platform_pairs,
                              skip_first_oids, skip_last_oids)

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
