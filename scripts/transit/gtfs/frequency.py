"""Frequency sampling, weighting, gates, and color / width mapping.

Holds all config-driven frequency scoring:
  - sample-date sets (weekday / weekend, plus seasonal splits)
  - per-window frequency weighting (core / evening / weekend)
  - per-mode score curve (best_freq / worst_freq / SCORE_POWER)
  - mode → color and score → width_base derived from freq
"""
import colorsys
import sys
from math import log

import yaml

from common import CFG_PATH

# ── Sample dates ─────────────────────────────────────────────────────────────

_SAMPLE_DATES_CACHE: dict = {}

# Seasonal windows for the regional-bus rescue multi-window gates. See
# .claude/concepts/seasonal-regional-bus-rescue.md. Months are inclusive
# (1..12). "winter" = Jan-Mar covers the heart of the ski season; "summer" =
# Jun-Aug covers the core alpine season. A bus running Dec-Apr passes via
# winter, Jun-Oct via summer; one running only in December does not pass.
_WINTER_MONTHS = frozenset({1, 2, 3})
_SUMMER_MONTHS = frozenset({6, 7, 8})
SEASONS = ("annual", "winter", "summer")


def _date_in_season(date_str: str, season: str) -> bool:
    """date_str = YYYYMMDD. season ∈ ("annual","winter","summer")."""
    if season == "annual":
        return True
    try:
        month = int(date_str[4:6])
    except (ValueError, IndexError):
        return False
    if season == "winter":
        return month in _WINTER_MONTHS
    if season == "summer":
        return month in _SUMMER_MONTHS
    return False


def _sample_dates() -> tuple:
    """Return (weekday_dates_set, weekend_dates_set, n_weekday, n_weekend)."""
    if _SAMPLE_DATES_CACHE:
        return (_SAMPLE_DATES_CACHE["wd_set"], _SAMPLE_DATES_CACHE["we_set"],
                _SAMPLE_DATES_CACHE["n_wd"], _SAMPLE_DATES_CACHE["n_we"])
    cfg = yaml.safe_load(CFG_PATH.read_text())
    fs = cfg.get("freq_sampling", {})
    wd = fs.get("weekday_dates", []) or []
    we = fs.get("weekend_dates", []) or []
    if not wd or not we:
        sys.exit(
            "config.yaml is missing freq_sampling.weekday_dates / weekend_dates.\n"
            "Re-run generate_sample_dates.py and paste its output into config.yaml."
        )
    _SAMPLE_DATES_CACHE.update({
        "wd_set": frozenset(wd), "we_set": frozenset(we),
        "n_wd": len(wd), "n_we": len(we),
    })
    return _sample_dates()


def _sample_dates_seasonal() -> dict:
    """Return {season: (wd_set, we_set, n_wd, n_we)} for SEASONS. n_wd/n_we
    are the per-season sample counts (n_wd in annual = total weekday samples)."""
    wd_set, we_set, _n_wd, _n_we = _sample_dates()
    out = {}
    for s in SEASONS:
        wd_s = frozenset(d for d in wd_set if _date_in_season(d, s))
        we_s = frozenset(d for d in we_set if _date_in_season(d, s))
        out[s] = (wd_s, we_s, len(wd_s), len(we_s))
    return out


# ── Freq-scoring windows ─────────────────────────────────────────────────────

CORE_START    = 7 * 3600
CORE_END      = 19 * 3600
EVENING_START = 19 * 3600
EVENING_END   = 23 * 3600
WEEKEND_START = 7 * 3600
WEEKEND_END   = 20 * 3600

CORE_HOURS    = (CORE_END - CORE_START) / 3600        # 12 h
EVENING_HOURS = (EVENING_END - EVENING_START) / 3600  # 4 h
WEEKEND_HOURS = (WEEKEND_END - WEEKEND_START) / 3600  # 13 h

# Power applied to the log-score so the mid range falls off faster. See
# .claude/concepts/frequency-weighted-line-scoring.md.
SCORE_POWER = 2.5

# Frequency endpoints per mode (trips/hour) loaded from config.yaml. The score
# curve is log in frequency between worst_freq (score=0.0) and best_freq
# (score=1.0), then raised to SCORE_POWER. See
# .claude/concepts/frequency-weighted-line-scoring.md.

_FREQ_CACHE: dict = {}
_WEIGHTS_CACHE: dict = {}
_LINE_WIDTH_CACHE: dict = {}


def _frequencies() -> tuple:
    """Return (best_freq_dict, worst_freq_dict) in trips/hour. Loaded lazily;
    both tables must cover the same bucket set or the pipeline aborts."""
    if _FREQ_CACHE:
        return _FREQ_CACHE["best"], _FREQ_CACHE["worst"]
    cfg = yaml.safe_load(CFG_PATH.read_text())
    fq = cfg.get("frequency") or {}
    best = fq.get("best_freq") or {}
    worst = fq.get("worst_freq") or {}
    if not best or not worst:
        sys.exit(
            "config.yaml is missing frequency.best_freq / frequency.worst_freq."
        )
    missing_worst = set(best) - set(worst)
    missing_best  = set(worst) - set(best)
    if missing_worst or missing_best:
        sys.exit(
            "config.yaml frequency tables are inconsistent: "
            f"buckets missing worst_freq={sorted(missing_worst)}, "
            f"buckets missing best_freq={sorted(missing_best)}."
        )
    _FREQ_CACHE["best"]  = {k: float(v) for k, v in best.items()}
    _FREQ_CACHE["worst"] = {k: float(v) for k, v in worst.items()}
    return _FREQ_CACHE["best"], _FREQ_CACHE["worst"]


def _window_weights() -> tuple:
    """Return (w_core, w_eve, w_we). Must sum to 1.0 (±1e-6)."""
    if _WEIGHTS_CACHE:
        return _WEIGHTS_CACHE["core"], _WEIGHTS_CACHE["eve"], _WEIGHTS_CACHE["we"]
    cfg = yaml.safe_load(CFG_PATH.read_text())
    ww = cfg.get("window_weights") or {}
    try:
        w_core = float(ww["core"])
        w_eve  = float(ww["eve"])
        w_we   = float(ww["we"])
    except KeyError as e:
        sys.exit(f"config.yaml window_weights missing key: {e}")
    total = w_core + w_eve + w_we
    if abs(total - 1.0) > 1e-6:
        sys.exit(f"config.yaml window_weights must sum to 1.0 (got {total}).")
    _WEIGHTS_CACHE.update({"core": w_core, "eve": w_eve, "we": w_we})
    return w_core, w_eve, w_we


def _line_width_bounds() -> dict:
    """Return {mode: (min, max)} from line_width config block. Every mode the
    pipeline can emit must have an entry."""
    if _LINE_WIDTH_CACHE:
        return _LINE_WIDTH_CACHE["bounds"]
    cfg = yaml.safe_load(CFG_PATH.read_text())
    lw = cfg.get("line_width") or {}
    if not lw:
        sys.exit("config.yaml is missing line_width.")
    bounds = {}
    for mode, vals in lw.items():
        try:
            bounds[mode] = (float(vals["min"]), float(vals["max"]))
        except (KeyError, TypeError):
            sys.exit(f"config.yaml line_width.{mode} must have min and max.")
    _LINE_WIDTH_CACHE["bounds"] = bounds
    return bounds


# ── Per-window frequency scoring ─────────────────────────────────────────────

def weighted_freq(freq: dict) -> float:
    """Combine the three per-window frequencies into a single weighted
    trips/hour value using window_weights from config."""
    if not freq:
        return 0.0
    w_core, w_eve, w_we = _window_weights()
    return w_core * freq.get("f_core", 0.0) \
         + w_eve  * freq.get("f_eve",  0.0) \
         + w_we   * freq.get("f_we",   0.0)


def compute_freq_score(freq: dict, mode: str) -> float:
    """Map a per-window frequency dict to a [0, 1] score using the per-mode
    frequency endpoints and the log-based curve with SCORE_POWER. The score is
    the powered/clamped output that drives width and the freq-score gate."""
    best_map, worst_map = _frequencies()
    if mode not in best_map:
        sys.exit(f"compute_freq_score: mode {mode!r} missing from frequency config.")
    best_f = best_map[mode]
    worst_f = worst_map[mode]
    f_weighted = weighted_freq(freq)
    if f_weighted <= worst_f:
        return 0.0
    if f_weighted >= best_f:
        return 1.0
    score_log = (log(f_weighted) - log(worst_f)) / (log(best_f) - log(worst_f))
    score_log = max(0.0, min(1.0, score_log))
    return round(score_log ** SCORE_POWER, 4)


# ── Color / width from mode + freq ───────────────────────────────────────────

MODE_HUE = {
    "train":        0,    # red
    "tram":       180,    # turquoise
    "metro":      120,    # green
    "bus":        220,    # blue
    "regional_bus": 290,  # purple-red
    "ferry":      220,    # blue
    "mountain":   320,    # deep pink (not used; mountain has fixed color)
}

MODE_MAX_SPEED = {
    "train":        100,
    "tram":          25,
    "metro":         50,
    "bus":           35,
    "regional_bus":  65,
    "ferry":         22,
}


def speed_to_color(mode: str, speed_kmh) -> str:
    """Convert mode + speed to hex color via HSL. Faster = darker + more saturated."""
    if mode == "mountain":
        return "#ffe566"
    hue = MODE_HUE.get(mode, 220) / 360.0
    if speed_kmh is None:
        speed_score = 0.5
    else:
        max_speed = MODE_MAX_SPEED.get(mode, 80)
        speed_score = min(1.0, speed_kmh / max_speed)
    s = 0.20 + speed_score * 0.72
    l = 0.77 - speed_score * 0.50
    r, g, b = colorsys.hls_to_rgb(hue, l, s)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


def score_to_width_base(score, mode) -> float:
    """Map score ∈ [0, 1] to width_base using the per-mode line_width bounds.
    Mountain's bounds are (0.75, 0.75) so the score has no effect there."""
    bounds = _line_width_bounds()
    if mode not in bounds:
        sys.exit(f"score_to_width_base: mode {mode!r} missing from line_width config.")
    w_min, w_max = bounds[mode]
    if score is None:
        return round(w_min, 2)
    return round(w_min + (w_max - w_min) * score, 2)
