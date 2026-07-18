"""Shared module-level state for step 07 and its extracted domain modules.

Every module-level constant that step 07 exposed via bare names lives here
so extracted modules can `from _state import *` and keep their existing
references working.

The mutable pill-band constants (`PILL_GAP_STRAIGHT_M` etc.) also live
here; `_set_pill_design_band()` overwrites them per bake pass in every
module listed in `PILL_BAND_TARGET_MODULES` (star-importing gives each
module its own binding, so the setter must mirror updates into each one).
Everything else is a real constant and safe to share.
"""
import json
import time
from contextlib import contextmanager
from math import sqrt

import yaml

from common import PROJECT_ROOT as ROOT
from geometry import _M_PER_DEG  # noqa: F401 — re-exported for stops modules

_transit_cfg = yaml.safe_load((ROOT / "scripts" / "transit" / "config.yaml").read_text())

LINES               = ROOT / "data" / "transit" / "transit_lines.geojson"
LINES_EXTENDED      = ROOT / "data" / "transit" / "transit_lines_extended.geojson"
LINE_STOPS          = ROOT / "data" / "transit" / "line_stops.json"
RAIL_WAYS_GEOJSON   = ROOT / "data" / "osm" / "rail_ways.geojson"
TRAM_WAYS_GEOJSON   = ROOT / "data" / "osm" / "tram_ways.geojson"
STREET_WAYS_GEOJSON = ROOT / "data" / "osm" / "street_ways.geojson"
OUT_DOTS            = ROOT / "data" / "transit" / "transit_stops.geojson"
OUT_PILLS           = ROOT / "data" / "transit" / "transit_stop_pills.geojson"
OUT_STOP_SEARCH_INDEX = ROOT / "static" / "map-assets" / "stop_search_index.json"
OUT_STOP_EXTENT_FILL = ROOT / "data" / "transit" / "stop_extent_fill.json"
OUT_CLOSE_ZOOM      = ROOT / "data" / "transit" / "transit_close_zoom.geojson"

# Per-mode platform-length defaults and sanity ranges from config.
PILL_CFG = _transit_cfg.get("pill_rendering", {})

# Ferry stop rendering: see config.yaml `ferry_stops` and stops-pill-zoom.md
# § "Ferry stops".
FERRY_STOPS_CFG = _transit_cfg.get("ferry_stops", {}) or {}
FERRY_DOT_WB           = float(FERRY_STOPS_CFG.get("dot_width_base", 2.5))
FERRY_CONNECTOR_WB     = float(FERRY_STOPS_CFG.get("connector_width_base", 1.0))
FERRY_COLLAPSE_M       = float(FERRY_STOPS_CFG.get("collapse_threshold_m", 15.0))
FERRY_CONVERGE_M       = float(FERRY_STOPS_CFG.get("convergence_threshold_m", 20.0))
FERRY_ENDPOINT_PULL_M  = float(FERRY_STOPS_CFG.get("endpoint_pull_threshold_m", 75.0))

# Pill design bands (see stops-pill-zoom.md § "Pills and connectors").
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

PILL_WB_HIGH = 5.0

# Far-zoom dot positioning.
FAR_ZOOM_CFG = _transit_cfg.get("far_zoom_marker", {}) or {}
FAR_ZOOM_INTERSECTION_TOL_M      = float(FAR_ZOOM_CFG.get("intersection_tol_m", 8.0))
FAR_ZOOM_RAIL_LIKE_MODES = {"train"}

RAIL_MODES = {"train"}
PILL_MODES = {"train", "tram", "metro", "bus", "regional_bus"}

MOUNTAIN_RAIL_ORIGINS = {"rebucketed_rail", "rack"}
MOUNTAIN_EXTENT_ORIGINS = MOUNTAIN_RAIL_ORIGINS | {"funicular"}
MOUNTAIN_PILL_ORIGINS = MOUNTAIN_EXTENT_ORIGINS | {"aerial"}

CLUSTER_DEG = 0.003

MODE_RANK = {
    "train":        0,
    "metro":        1,
    "tram":         2,
    "bus":          3,
    "mountain":     4,
    "ferry":        5,
    "regional_bus": 6,
}

MODE_MINZOOM = {
    "train":        5,
    "tram":        10,
    "metro":        9,
    "regional_bus": 9,
    "ferry":        9,
    "bus":         11,
    "mountain":    11,
}

INDICATOR_MIN_ZOOM = 13

MODE_TO_COLOR_GROUP = {
    "train":        "train",
    "metro":        "metro",
    "tram":         "tram",
    "bus":          "bus",
    "ferry":        "bus",
    "regional_bus": "regional_bus",
    "mountain":     "mountain",
}

COLOR_GROUP_ORDER = ["train", "metro", "tram", "bus", "regional_bus", "mountain"]

PILL_CLUSTER_RAIL_KM    = 0.300
PILL_CLUSTER_NONRAIL_KM = 0.050

LONE_OUTLIER_GAP_RAIL_METRO_M = 50
LONE_OUTLIER_GAP_BUS_TRAM_M = 20

SAME_TRACK_PERP_M = 2.0

PERP_PLATFORM_TOL_DEG = float(PILL_CFG.get("perp_platform_tol_deg", 2.0))

# Connector curving constants (see pill-rendering concept § Connector curving).
CURVE_PERP_PREF_RATIO = 0.75
CURVE_MAX_RADIUS_M_BY_MODE = {
    "train":        30.0,
    "metro":        30.0,
    "tram":         20.0,
    "bus":          20.0,
    "regional_bus": 20.0,
}
CURVE_MAX_RADIUS_M_DEFAULT = 20.0
CURVE_TARGET_SAGITTA_M = 0.05
CURVE_MAX_SAMPLES = 64
CURVE_DEDUP_TOL_M = 0.5


# Mutable pill-band constants. `_set_pill_design_band()` overwrites them
# per bake pass. Any module holding a bare reference to one of these names
# gets the current value only if the setter mirrors updates into that
# module's `__dict__` — see below.
PILL_GAP_STRAIGHT_M = 50
PILL_GAP_ANGLED_M = 15
CURVE_MIN_RADIUS_M = 5.0
# 0.5 is the effective value for the whole rail placement pass, which runs
# before the first band bake (the nonrail passes overwrite it per band).
DEDUP_TOL_M = 0.5
PILL_SIMPLIFY_TOL_M = 0.1
PILL_MIN_D_PX = 4.5
PILL_SLOPE_PX_PER_WB = 2.3
PILL_BAND_ZOOM = 14

# Modules that hold bare references to the mutable constants above. Kept as
# a module-level tuple so callers can extend it (e.g. plugin modules) at
# import time.
PILL_BAND_TARGET_MODULES = (
    "_state",
    "stops.pill_zoom.geom",
    "stops.pill_zoom.options",
    "stops.pill_zoom.place",
    "stops.pill_zoom.nn_path",
    "stops.pill_zoom.lines",
    "stops.pill_zoom.polyline",
    "stops.pill_zoom.curves",
    "stops.pill_zoom.connectors",
    "stops.pill_zoom.make",
    # far_zoom reads DEDUP_TOL_M in _largest_pill_or_disc_position — in the
    # monolith that read saw the band-current value at call time.
    "stops.far_zoom",
)


def _set_pill_design_band(band_cfg):
    """Overwrite the mutable pill-band constants in every module that
    imported them by bare name. Called once per bake pass from step 07's
    main() before invoking the pill construction."""
    import sys
    updates = {
        "PILL_GAP_STRAIGHT_M":  band_cfg["pill_gap_straight_m"],
        "PILL_GAP_ANGLED_M":    band_cfg["pill_gap_angled_m"],
        "CURVE_MIN_RADIUS_M":   band_cfg["curve_min_radius_m"],
        "DEDUP_TOL_M":          band_cfg["dedup_tol_m"],
        "PILL_SIMPLIFY_TOL_M":  band_cfg["pill_simplify_tol_m"],
        "PILL_MIN_D_PX":        band_cfg["pill_min_d_px"],
        "PILL_SLOPE_PX_PER_WB": band_cfg["pill_slope_px_per_wb"],
        "PILL_BAND_ZOOM":       band_cfg["zoom_min"],
    }
    for mod_name in PILL_BAND_TARGET_MODULES:
        mod = sys.modules.get(mod_name)
        if mod is not None:
            for k, v in updates.items():
                setattr(mod, k, v)


@contextmanager
def _timed(label: str):
    """Print `  [   X.Xs] <label>` after the wrapped block completes.
    Used by step 07's pipeline to surface per-section wall-clock times."""
    t0 = time.perf_counter()
    yield
    dt = time.perf_counter() - t0
    print(f"  [{dt:6.1f}s] {label}")


def _stop_wb(wb: float, mode: str) -> float:
    return wb


def _tag_band_features(feats, band_id, band_cfg):
    """Stamp `design_band` and per-feature tippecanoe zoom limits."""
    for f in feats:
        f["properties"]["design_band"] = band_id
        tipp = f.setdefault("tippecanoe", {})
        tipp["minzoom"] = max(int(tipp.get("minzoom", 0)), band_cfg["zoom_min"])
        if band_cfg["zoom_max"] is not None:
            tipp["maxzoom"] = band_cfg["zoom_max"]


def _curve_max_radius(mode: str) -> float:
    return CURVE_MAX_RADIUS_M_BY_MODE.get(mode, CURVE_MAX_RADIUS_M_DEFAULT)


def _arc_chord_samples(radius: float, arc_length: float) -> int:
    """Number of chord segments to sample an arc at."""
    chord = sqrt(8.0 * radius * CURVE_TARGET_SAGITTA_M)
    return max(2, min(CURVE_MAX_SAMPLES,
                      int(arc_length / chord + 0.999)))
