"""Close-zoom (z17+) pill-arrows + station backdrop."""
import json
from collections import defaultdict
from math import atan2, cos, degrees, pi, radians, sin, sqrt

from _state import *  # noqa: F401,F403
from stops.extent import _platform_extent
from geometry import (
    _cum_dist_m, _directional_tangent_at, _interp_at, _meters_per_deg,
    _project_meters, _slice_polyline, flatten_coords, haversine_km,
)


# Close-zoom stop design (z17+): pill-arrows + yellow station backdrop
# See .claude/concepts/stops-close-zoom.md
# =============================================================================

# First-draft seed values. Refine after visual review.
CLOSE_ZOOM_STACK_GAP_M         = 0.8    # polygon-edge gap; 0.4 m outside-border visible after the 0.4 m centered border
CLOSE_ZOOM_DIR_CLUSTER_COS     = cos(radians(45.0))  # same-direction threshold
# Transit-line casing formula from generate_style.py (z18+ endpoint,
# clamped past z18): line-width in pixels = width_base * 4 + 2.
# Bands B–E widen the side-anchored perp offset by half this width in
# metres, evaluated at z19 (the anchor zoom), using the widest
# width_base among the pill-arrows in the stack. 1 m = 2.455 * 4 px
# at z19 (matches PX_PER_M_Z17 * 4 in generate_style.py).
CLOSE_ZOOM_PX_PER_M_Z19        = 2.455 * 4.0
CLOSE_ZOOM_BACKDROP_PAD_M      = 8.0    # outward padding of the station hull
CLOSE_ZOOM_CURB_LATERAL_M      = 2.0    # same-curb: max lateral gap between stop position lines (tram/bus/regional_bus)
CLOSE_ZOOM_CURB_LATERAL_RAIL_M = 1.0    # same-track: max lateral gap for rail (train + mountain rack rail)
CLOSE_ZOOM_CURB_MERGE_FRAC     = 0.30   # same-curb: overlap share above which stops merge
CLOSE_ZOOM_RAIL_CLUSTER_MIN_FRAC = 0.30 # rail per-track clustering: min overlap share (of the shorter extent) required alongside the 1 m lateral test — a shared switch node briefly touching two extents doesn't fuse whole platforms
CLOSE_ZOOM_ARC_STEP_DEG        = 12.0   # hull corner rounding granularity
# Label sizing (glyph height in metres; the style converts to px per zoom).
# Uniform within a band — destinations are pre-wrapped at build time and get
# an ellipsis beyond the band's line budget. Wrapping measures real advance
# widths from glyph_widths.json (see gen_glyph_widths.py); the flat fallback
# below only applies when that table is missing.
CLOSE_ZOOM_CHAR_W_EM           = 0.60   # fallback avg glyph width (em)

GLYPH_WIDTHS_PATH = ROOT / "scripts" / "transit" / "tools" / "glyph_widths.json"
try:
    _gw_raw = json.loads(GLYPH_WIDTHS_PATH.read_text())
    GLYPH_WIDTHS = _gw_raw.get("regular") or {}
    GLYPH_WIDTH_DEFAULT = float(_gw_raw.get("default_regular",
                                            CLOSE_ZOOM_CHAR_W_EM))
    GLYPH_WIDTHS_BOLD = _gw_raw.get("bold") or {}
    GLYPH_WIDTH_DEFAULT_BOLD = float(_gw_raw.get("default_bold",
                                                 CLOSE_ZOOM_CHAR_W_EM))
    del _gw_raw
except (FileNotFoundError, ValueError):
    GLYPH_WIDTHS = {}
    GLYPH_WIDTH_DEFAULT = CLOSE_ZOOM_CHAR_W_EM
    GLYPH_WIDTHS_BOLD = {}
    GLYPH_WIDTH_DEFAULT_BOLD = CLOSE_ZOOM_CHAR_W_EM

# Border width of the pill-arrow outline, in metres. Kept in sync with
# generate_style.py's _metric_px(0.4) on the close-zoom-pill-arrow-border
# layers. The shrink-to-fit ref logic uses this to compute the inner
# container the number must sit inside.
CLOSE_ZOOM_BORDER_M = 0.4

# Zoom bands: each pill-arrow is emitted once per band with band-specific sizing;
# the style gates them by display zoom (A: z17, B: z18, C: z19+). Bands B
# and C both live in the z18 tiles (z19+ overzooms them), band A in the
# z15–17 tiles.
#
# The arrow does NOT grow across bands (all 10 m long): zooming in itself
# provides the extra pixels, which the higher bands spend on destination
# text (B: one line, C: two lines) while the glyph height in metres shrinks.
# Band A has no destination (font_dest_m None) — it renders as a solid pill-arrow
# in the line color with just the centered line number, no disc.
#   length_m / width_m — pill-arrow geometry
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
    "A": {"length_m": 10.0, "width_m": 5.6, "font_ref_m": 2.5,
          "font_dest_m": None, "max_lines": 0,
          "margin_disc_m": 0.0, "margin_tip_m": 0.0,
          "line_gap_m": -0.5,
          "tipp_min": 15, "tipp_max": 17},
    # font_dest_m values include a 10% size bump for the destination text
    # relative to the earlier calibration — Saira Semi Condensed rendered
    # small vs the disc width, so both the measured budget and the rendered
    # size grow together, keeping wrap-fit coherent.
    "B": {"length_m": 10.0, "width_m": 2.8, "font_ref_m": 1.8,
          "font_dest_m": 1.232, "max_lines": 1,
          "margin_disc_m": 0.2, "margin_tip_m": -0.5,
          "flipped_shift_m": 0.3,
          "line_gap_m": 0.5,
          "tipp_min": 18, "tipp_max": 18},
    "C": {"length_m": 10.0, "width_m": 2.8, "font_ref_m": 1.6,
          "font_dest_m": 0.924, "max_lines": 2,
          "margin_disc_m": 0.15, "margin_tip_m": -0.15,
          "flipped_shift_m": 0.3,
          "line_gap_m": 0.5,
          "tipp_min": 18, "tipp_max": 18},
    "D": {"length_m": 10.0, "width_m": 2.8, "font_ref_m": 1.6,
          "font_dest_m": 0.693, "max_lines": 3,
          "margin_disc_m": 0.15, "margin_tip_m": -0.08,
          "flipped_shift_m": 0.2,
          "line_gap_m": 0.5,
          "tipp_min": 18, "tipp_max": 18},
    "E": {"length_m": 10.0, "width_m": 2.8, "font_ref_m": 1.6,
          "font_dest_m": 0.517, "max_lines": 4,
          "margin_disc_m": 0.15, "margin_tip_m": -0.04,
          "flipped_shift_m": 0.15,
          "line_gap_m": 0.5,
          "tipp_min": 18, "tipp_max": 18},
}
# Band whose geometry feeds the backdrop hull (largest, so it covers all).
CLOSE_ZOOM_HULL_BAND = "C"

# Rail-style modes: on-line placement (no sideways offset), no direction
# split, one centered stack per stop. See stops-close-zoom.md
# § "Side of the line" and § "Rail-style".
CLOSE_ZOOM_RAIL_MODES = {"train", "ferry"}
# All mountain sub-types join the rail-style group. Aerial and funicular
# are extentless — see the emit loop for the endpoint-anchor terminal rule
# and the aerial synthetic-slice fallback.
CLOSE_ZOOM_RAIL_MOUNTAIN_ORIGINS = {"rack", "rebucketed_rail",
                                     "aerial", "funicular"}

# Modes that get a close-zoom pill-arrow at all.
CLOSE_ZOOM_PILL_MODES = {"train", "tram", "metro", "bus", "regional_bus",
                          "ferry", "mountain"}

# Extentless origins that need synthetic anchoring (no natural platform
# extent from stop/dot placement). Funicular has an extent already; aerial
# does not. Ferry uses its own +10 m pier-offset anchor.
CLOSE_ZOOM_MOUNTAIN_EXTENTLESS = {"aerial"}

# Hybrid tram detection tolerance (metres). A tram stop whose shaped
# geometry lies within this distance of a narrow_gauge / light_rail OSM way
# is treated as rail-style. See stops-close-zoom.md § "Hybrid tram
# detection".
CLOSE_ZOOM_HYBRID_TRAM_TOL_M = 2.0

# Ferry pill-arrow offset from the pier's on-line position (metres,
# in direction of travel). See stops-close-zoom.md § "Ferry".
CLOSE_ZOOM_FERRY_OFFSET_M = 10.0

# Ferry pier clustering (stops-close-zoom.md § "Ferry pier
# clustering"): two ferry lines at one pier merge into ONE rail-style
# stack when their polylines run laterally within
# CLOSE_ZOOM_FERRY_CLUSTER_LATERAL_M of each other for at least
# CLOSE_ZOOM_FERRY_CLUSTER_MIN_FRAC of the shorter slice's length,
# measured over the first CLOSE_ZOOM_FERRY_CLUSTER_WINDOW_M metres out
# of the pier — same physical departure line, different GTFS routes.
# Direction gate (same-side of 45°) still applies via
# CLOSE_ZOOM_DIR_CLUSTER_COS below.
CLOSE_ZOOM_FERRY_CLUSTER_WINDOW_M = float(
    PILL_CFG.get("close_zoom_ferry_cluster_window_m", 50.0))
CLOSE_ZOOM_FERRY_CLUSTER_LATERAL_M = float(
    PILL_CFG.get("close_zoom_ferry_cluster_lateral_m", 1.0))
CLOSE_ZOOM_FERRY_CLUSTER_MIN_FRAC = float(
    PILL_CFG.get("close_zoom_ferry_cluster_min_frac", 0.5))

# End-of-platform rail: shift the whole stack backward past the polyline
# endpoint by this many metres so the fastest pill-arrow's rear cap covers
# the transit line's rounded end-cap. The line-cap radius is line-width/2
# in PIXELS, which converts to a metres-scale radius that grows as you zoom
# out — invisible at z22, a visible stub at z17–z19. Fixed 4 m covers the
# widest transit line (train) at the lowest close-zoom band.
# See stops-close-zoom.md § "End-of-platform line-end overhang".
CLOSE_ZOOM_LINE_END_OVERHANG_M = float(
    PILL_CFG.get("close_zoom_line_end_overhang_m", 4.0))

# Terminal-snap tolerance for mountain rail-style modes: distance from a
# polyline endpoint below which the projected stop position is treated as
# a terminal, activating the endpoint-anchor rule. Safety net for the
# rare case where pfaedle's shape overshoots the terminal stop; the
# primary check is the trip's first-stop index (idx == 0).
CLOSE_ZOOM_TERMINAL_SNAP_M = 100.0

# Station label (stop-labels.md § close-zoom): one white label per
# parent station, inside the hull, swept perpendicular ("rather up")
# to the dominant pill-arrow direction until clear of every pill-arrow,
# then aligned with the NEAREST pill-arrow stack. Seed values — tune
# after visual review.
# Glyph height in metres per stop tier — large stations get very large
# labels. Fallback when the tier is unknown: small_bus.
CLOSE_ZOOM_STATION_LABEL_FONT_BY_TIER = {
    "major_train":     40.0,
    "main_train":      30.0,
    "important_train": 24.0,
    "train_station":   18.0,
    "small_train":     14.0,
    "major_mountain":  14.0,
    "ferry_stop":      12.0,
    "mountain_stop":   10.0,
    "major_hub":       14.0,
    "big_station":     10.0,
    "normal_stop":      8.0,
    "small_bus":        6.0,
}
CLOSE_ZOOM_STATION_LABEL_CLEAR_M = 1.5    # clearance around the label box
CLOSE_ZOOM_STATION_LABEL_STEP_M = 0.5     # sweep step
CLOSE_ZOOM_STATION_LABEL_MAX_SWEEP_M = 300.0
# Axial angle difference above which the nearest-stack alignment redoes
# the sweep with the aligned axis (stop-labels.md § close-zoom).
CLOSE_ZOOM_STATION_LABEL_ANGLE_TOL_DEG = 5.0
# Half-height factor of the label box relative to font_m: visible glyphs
# don't fill the full em box, but descenders reach below the baseline.
CLOSE_ZOOM_STATION_LABEL_HALF_H_EM = 0.6

# Modes whose variant priority (representative pick + pill-arrow stacking order)
# is frequency rather than speed. Frequency is the better proxy for "the
# canonical variant" on road modes: a rare short-turn variant terminating
# mid-route must not out-rank the through variants.
CLOSE_ZOOM_FREQ_PRIORITY_MODES = {"tram", "bus", "regional_bus"}


