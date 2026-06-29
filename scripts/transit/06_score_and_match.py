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

import csv
import json
import colorsys
import sys
from collections import defaultdict
from math import radians, cos, sin, sqrt, atan2, log, ceil, floor
from pathlib import Path
from typing import Optional

import yaml

ROOT = Path(__file__).resolve().parents[2]
GTFS = ROOT / "data" / "gtfs_routed"
CFG_PATH = ROOT / "scripts" / "transit" / "config.yaml"
OUT = ROOT / "data" / "transit" / "transit_lines.geojson"
OUT_STOPS = ROOT / "data" / "transit" / "line_stops.json"
OUT_GTFS_UNMATCHED = ROOT / "data" / "transit" / "gtfs_unmatched.json"
OUT_TRIP_GROUPS = ROOT / "data" / "transit" / "trip_groups.json"
OUT_PFAEDLE_UNROUTED = ROOT / "data" / "transit" / "pfaedle_unrouted.json"
OUT_STOP_SCORES = ROOT / "data" / "transit" / "stop_size_scores.json"

# ── Frequency sample dates ──────────────────────────────────────────────────
# Loaded from config.yaml (populated by scripts/transit/generate_sample_dates.py).
# Lazy because import-time loading would break diagnostic scripts that import
# this module without having config populated yet.

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


# Meters per degree at equator; lon component is additionally scaled by
# cos(latitude) for equal-distance projection. Used by the line-graph
# UIC clustering for connectivity.
_M_PER_DEG = 111319.49


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

# Bus mode classification — see .claude/concepts/bus-mode-classification.md.
# Agencies that follow the "1-digit ref = city, 2-digit ref = regional"
# numbering convention. 1-digit refs on these operators are still city buses.
TWO_DIGIT_REGIONAL_AGENCIES = {
    "146",   # STI Bus AG
    "605",   # STI Berg
    "859",   # STI-gwb
    "766",   # BuS/cb (Bus und Service AG, Chur)
    "236",   # BCD (Chur-Dreibündenstein)
    "801",   # PAG (PostAuto AG)
    "7088",  # THP (Trägerverein Historische Postautolinie)
}
# 765 (PAG/BCS, PostAuto AG Bus Commune Sion) is deliberately excluded:
# PostAuto-operated city service whose 2-digit refs are city lines.

# transN city carve-out: agencies whose 3-digit refs starting with 1 or 3 are
# city lines, overriding the default n>=3 → regional rule. transN numbers its
# urban networks in the 100s (Neuchâtel) and 300s (La Chaux-de-Fonds / Le Locle).
TRANSN_CITY_AGENCIES = {
    "153",   # TRN-tn (Neuchâtel)
    "792",   # TRN/tc (La Chaux-de-Fonds + Le Locle)
}

# Length fallback for pure-letter refs (km).
LETTER_REF_REGIONAL_MIN_LENGTH = 10.0


# ── Config loading ───────────────────────────────────────────────────────────

def load_cfg() -> dict:
    return yaml.safe_load(CFG_PATH.read_text())


# ── Mode classification (GTFS-side) ──────────────────────────────────────────

# Mapping from the extended GTFS route_type code space (used by the official
# opentransportdata.swiss feed) to rendering buckets. See
# .claude/concepts/gtfs-source-switch.md for the rationale per code.
# Returning "" excludes the route from emission entirely.
_TRAIN_TYPES    = frozenset({"100", "101", "102", "103", "105", "106", "107", "109"})
_MOUNTAIN_TYPES = frozenset({"116", "1300", "1303", "1400"})

def gtfs_type_to_bucket(route_type: str) -> str:
    t = route_type.strip()
    if t in _TRAIN_TYPES:    return "train"
    if t in _MOUNTAIN_TYPES: return "mountain"
    if t == "401":           return "metro"
    if t in ("700", "702"):  return "bus"
    if t == "800":           return "bus"    # trolleybus — fixed city_bus, see gtfs_to_mode
    if t == "900":           return "tram"
    if t == "1000":          return "ferry"
    # Excluded by concept: 117 EXT, 202 National Coach, 705 BN, 710 Sightseeing,
    # 715 Demand & Response, 1500 Taxi, and any unknown extended code.
    return ""


def gtfs_to_mode(bucket: str, agency_id: str,
                 short_name: str = "",
                 length_km: Optional[float] = None,
                 route_type: str = "") -> str:
    """Map a GTFS bucket + agency to one of the rendering modes.

    - bucket == "bus" → bus / regional_bus by ref digit count, agency,
      and a length fallback for pure-letter refs.
      See .claude/concepts/bus-mode-classification.md for the full rule.
      Trolleybuses (route_type=800) bypass the regional reclassification
      and stay city bus per .claude/concepts/gtfs-source-switch.md.
    - Other buckets pass through. Mountain classification comes solely from
      GTFS route_type at the bucket layer.
    """
    if bucket == "bus":
        if route_type == "800":
            return "bus"
        ref = short_name.strip()
        digits = "".join(c for c in ref if c.isdigit())
        n = len(digits)
        if n == 3 and agency_id in TRANSN_CITY_AGENCIES and digits[0] in ("1", "3"):
            return "bus"
        if n >= 3:
            return "regional_bus"
        if n == 2 and agency_id in TWO_DIGIT_REGIONAL_AGENCIES:
            return "regional_bus"
        if n == 0 and length_km is not None \
                and length_km >= LETTER_REF_REGIONAL_MIN_LENGTH:
            return "regional_bus"
        return "bus"
    return bucket


# ── Color scheme ─────────────────────────────────────────────────────────────

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


# ── Geometry helpers ─────────────────────────────────────────────────────────

def haversine_km(lon1, lat1, lon2, lat2) -> float:
    R = 6371
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi, dlam = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlam / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def polyline_length_km(coords: list) -> float:
    if len(coords) < 2:
        return 0.0
    total = 0.0
    for i in range(len(coords) - 1):
        total += haversine_km(coords[i][0], coords[i][1],
                              coords[i + 1][0], coords[i + 1][1])
    return total


def parse_time(t: str) -> int:
    parts = t.strip().split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])


def line_bbox(coords):
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lons), min(lats), max(lons), max(lats)


def _bbox_overlap_fraction(b1, b2) -> float:
    ix0, iy0 = max(b1[0], b2[0]), max(b1[1], b2[1])
    ix1, iy1 = min(b1[2], b2[2]), min(b1[3], b2[3])
    if ix0 >= ix1 or iy0 >= iy1:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    a1 = (b1[2] - b1[0]) * (b1[3] - b1[1])
    a2 = (b2[2] - b2[0]) * (b2[3] - b2[1])
    smaller = min(a1, a2)
    return inter / smaller if smaller > 0 else 0.0


# ── GTFS loading ─────────────────────────────────────────────────────────────

def load_frequencies() -> dict:
    """Return {trip_id: [(start_secs, end_secs, headway_secs)]} from frequencies.txt."""
    freq_file = GTFS / "frequencies.txt"
    result: dict = defaultdict(list)
    if not freq_file.exists():
        return {}
    with open(freq_file, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            result[row["trip_id"]].append((
                parse_time(row["start_time"]),
                parse_time(row["end_time"]),
                int(row["headway_secs"]),
            ))
    return dict(result)


def load_stops() -> dict:
    coords = {}
    with open(GTFS / "stops.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sid = row["stop_id"]
            if sid.startswith("0000"):
                continue
            try:
                lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
                coords[sid] = (lon, lat)
                base = sid.split(":")[0]
                if base not in coords:
                    coords[base] = (lon, lat)
            except ValueError:
                pass
    return coords


def load_stop_meta() -> dict:
    meta = {}
    with open(GTFS / "stops.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sid = row["stop_id"]
            if sid.startswith("0000"):
                continue
            parent = row.get("parent_station", "").removeprefix("Parent")
            meta[sid] = (row.get("stop_name", ""), parent)
            base = sid.split(":")[0]
            if base not in meta:
                meta[base] = meta[sid]
    return meta


def load_calendar_dates() -> dict:
    from datetime import datetime

    svc_dates: dict = defaultdict(set)
    removals: dict = defaultdict(set)
    with open(GTFS / "calendar_dates.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["exception_type"] == "1":
                svc_dates[row["service_id"]].add(row["date"])
            elif row["exception_type"] == "2":
                removals[row["service_id"]].add(row["date"])

    DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday",
                 "friday", "saturday", "sunday"]
    cal_path = GTFS / "calendar.txt"
    if cal_path.exists():
        wd_set, we_set, _, _ = _sample_dates()
        sample_dates = list(wd_set | we_set)
        # Group sample dates by their weekday column to avoid re-streaming
        # calendar.txt once per date.
        by_col: dict = defaultdict(list)
        for date_str in sample_dates:
            col = DAY_NAMES[datetime.strptime(date_str, "%Y%m%d").weekday()]
            by_col[col].append(date_str)
        for weekday_col, date_strs in by_col.items():
            with open(cal_path, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    if row.get(weekday_col, "0") != "1":
                        continue
                    s, e = row["start_date"], row["end_date"]
                    for date_str in date_strs:
                        if s <= date_str <= e:
                            svc_dates[row["service_id"]].add(date_str)

    for svc_id, removed in removals.items():
        svc_dates[svc_id] -= removed

    return svc_dates


def load_calendar_dates_full() -> dict:
    """Return {service_id: set(date_str YYYYMMDD)} — every active date for
    each service across the full feed validity period, applying both
    calendar.txt weekday patterns and calendar_dates.txt add/remove rows.

    Distinct from load_calendar_dates() which only keeps the dates that are
    in the freq-sampling window. This one is needed for the active-days
    filter, which must count every day a service actually runs.
    """
    from datetime import date, timedelta

    DAY_COLS = ["monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday"]

    def parse_d(s: str) -> date:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))

    def fmt_d(d: date) -> str:
        return f"{d.year:04d}{d.month:02d}{d.day:02d}"

    active: dict = defaultdict(set)
    cal_path = GTFS / "calendar.txt"
    if cal_path.exists():
        with open(cal_path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                wd = [row.get(col, "0") == "1" for col in DAY_COLS]
                try:
                    start = parse_d(row["start_date"])
                    end = parse_d(row["end_date"])
                except (KeyError, ValueError):
                    continue
                sid = row["service_id"]
                d = start
                while d <= end:
                    if wd[d.weekday()]:
                        active[sid].add(fmt_d(d))
                    d += timedelta(days=1)

    cd_path = GTFS / "calendar_dates.txt"
    if cd_path.exists():
        with open(cd_path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                sid = row["service_id"]
                d_str = row["date"]
                if row["exception_type"] == "1":
                    active[sid].add(d_str)
                elif row["exception_type"] == "2":
                    active[sid].discard(d_str)

    return dict(active)


def load_routes() -> dict:
    routes = {}
    with open(GTFS / "routes.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            routes[row["route_id"]] = {
                "short_name": row["route_short_name"],
                "long_name":  row.get("route_long_name", ""),
                "type": row["route_type"],
                "agency_id": row.get("agency_id", ""),
            }
    return routes


def load_agencies() -> dict:
    """{agency_id: agency_name}"""
    out = {}
    with open(GTFS / "agency.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            out[row["agency_id"]] = row.get("agency_name", "")
    return out


def load_trips(route_lookup: dict, mountain_aids: set) -> dict:
    """{trip_id: {line_key, service_id, agency_id, shape_id, direction_id, route_id}}

    `mountain_aids` rebuckets the listed agencies' `route_type=2` rail to
    `mountain` at load time, so every downstream bucket-keyed exemption
    (active-days, freq gate, mode→yellow) flows from a single decision. The
    rail shape pfaedle produced is still preferred at emit time because the
    straight-line fallback only fires when no pfaedle shape exists.
    """
    trips = {}
    with open(GTFS / "trips.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            r = route_lookup.get(row["route_id"])
            if not r:
                continue
            bucket = gtfs_type_to_bucket(r["type"])
            if not bucket:
                continue
            agency_id = r.get("agency_id", "")
            if bucket == "train" and agency_id in mountain_aids:
                bucket = "mountain"
            line_key = (r["short_name"], r["long_name"], bucket)
            trips[row["trip_id"]] = {
                "line_key": line_key,
                "service_id": row["service_id"],
                "agency_id": r.get("agency_id", ""),
                "shape_id": row.get("shape_id", "") or "",
                "direction_id": row.get("direction_id", "") or "",
                "route_id": row["route_id"],
            }
    return trips


def load_shapes() -> dict:
    """{shape_id: [(lon, lat), ...]} from pfaedle's shapes.txt.

    Streams in one pass — shapes.txt is sorted by shape_id and shape_pt_sequence
    in pfaedle output, but we don't rely on it: sequence is enforced inside the
    accumulator.
    """
    path = GTFS / "shapes.txt"
    if not path.exists():
        sys.exit(f"{path} missing — run 05_run_pfaedle.py first")

    buf: dict = defaultdict(list)
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                lon = float(row["shape_pt_lon"])
                lat = float(row["shape_pt_lat"])
                seq = int(row["shape_pt_sequence"])
            except (KeyError, ValueError):
                continue
            buf[row["shape_id"]].append((seq, lon, lat))

    out: dict = {}
    for sid, pts in buf.items():
        pts.sort(key=lambda x: x[0])
        out[sid] = [(p[1], p[2]) for p in pts]
    return out


# ── Trip-grouping exports ────────────────────────────────────────────────────

_trip_group_export: dict = {}        # trip_id → (line_key, trip_group_id, agency_id)
_trip_stops_export: dict = {}        # trip_id → [stop_id, ...]   (sequence)
_trip_merged_export: dict = {}       # trip_id → frozenset(merged_stop_id)  (variant identity)
_trip_weight_export: dict = {}       # trip_id → int (≈ trip-runs across calendar)
_trip_weight_seasonal_export: dict = {}  # trip_id → {"annual": n, "winter": n, "summer": n}
_trip_direction_export: dict = {}    # trip_id → (first_merged_uic, last_merged_uic)

_BUCKET_MODE_APPROX = {
    "train": "train", "tram": "tram", "metro": "metro",
    "ferry": "ferry", "bus": "regional_bus", "regional_bus": "regional_bus",
}


def _mountain_origin(bucket: str, route_type: str):
    """Classify the origin of a mountain-bucket trip group. Returns one of
    `aerial`, `funicular`, `rack`, `rebucketed_rail`, or None for non-mountain
    buckets.

    aerial / funicular are exempt from the freq-score and active-days gates;
    rack and rebucketed_rail are not (they run regular timetables, same gate
    behaviour as train).
    """
    if bucket != "mountain":
        return None
    if route_type == "1400":
        return "funicular"
    if route_type in ("1300", "1303"):
        return "aerial"
    if route_type == "116":
        return "rack"
    if route_type in _TRAIN_TYPES:
        return "rebucketed_rail"
    return None


def _gate_exempt(bucket: str, mountain_origin) -> bool:
    """True if a trip group is exempt from the per-direction split and the
    active-days gate. Ferries collapse direction-wise (one polyline serves
    both directions) and run seasonally, so both rules don't apply to them.
    True mountain (aerial, funicular) is exempt for the same reasons.
    Rebucketed rail is treated like normal train.

    Ferries are NOT exempt from the freq-score gate — see _freq_gate_exempt."""
    if bucket == "ferry":
        return True
    if bucket == "mountain" and mountain_origin in ("aerial", "funicular"):
        return True
    return False


def _freq_gate_exempt(bucket: str, mountain_origin) -> bool:
    """True if a trip group is exempt from the freq-score gate
    (f_weighted > worst_freq). Restricted to true mountain (aerial, funicular)
    — these are scenic / on-demand services where any frequency is meaningful.
    Ferries used to be exempt here; they are now gated like normal modes, with
    a low `worst_freq.ferry` to preserve once-a-week-ish lake services."""
    if bucket == "mountain" and mountain_origin in ("aerial", "funicular"):
        return True
    return False


def stream_stop_times(trips, stop_coords, svc_dates, trip_frequencies, stop_meta):
    """One streaming pass → raw trip counts + speed per line, plus trip-group
    partitioning. Populates module-level exports `_trip_group_export`,
    `_trip_stops_export`, `_trip_merged_export`, `_trip_weight_export`,
    and `_trip_direction_export`.
    """
    global _trip_group_export, _trip_stops_export, _trip_merged_export, _trip_weight_export, _trip_direction_export

    stop_merge: dict = {}
    for sid, (_name, parent) in stop_meta.items():
        stop_merge[sid] = parent if parent else sid.split(":")[0]

    wd_set, we_set, n_wd_samples, n_we_samples = _sample_dates()
    season_dates = _sample_dates_seasonal()
    print(f"  Sample dates: {n_wd_samples} weekday + {n_we_samples} weekend "
          f"(winter: {season_dates['winter'][2]}+{season_dates['winter'][3]}, "
          f"summer: {season_dates['summer'][2]}+{season_dates['summer'][3]})")
    print("  Streaming stop_times.txt ...")

    # Per-trip freq contribution buffered here, summed into tg_freq once trip
    # groups are assigned. No per-line_key aggregation exists — trip group is
    # the only line identity in this pipeline (see
    # .claude/concepts/trip-group-as-sole-line-identity.md).
    # Per-season: trip_freq[tid] = {season: (core, eve, we)} for seasons
    # ("annual","winter","summer"). The "annual" entry is the existing value.
    trip_freq: dict = {}

    # Per-trip buffer for the post-stream grouping phase.
    # trip_id → (line_key, agency_id, weight, raw_variant_frozenset,
    #            merged_stop_frozenset, sequence_list)
    trip_buf: dict = {}

    # Per-trip seasonal activity weights. trip_weight_seasonal[tid][season]
    # = number of active calendar dates in that season (annual = total).
    # Consumed by the multi-window rare-variant filter for groups containing
    # at least one regional_bus_rescued variant.
    trip_weight_seasonal: dict = {}

    current_trip_id = None
    current_stops: list = []

    def process_trip(trip_id, stops):
        if not stops or trip_id not in trips:
            return
        trip = trips[trip_id]
        line_key = trip["line_key"]
        service_id = trip["service_id"]
        active_dates = svc_dates.get(service_id, set())
        first_dep = stops[0][3]

        # Count of sample dates this trip is active on, per season. The annual
        # multiplier downscales construction lines (active on 1/26 weekday
        # samples → contributes 1/26 of its raw count); the seasonal versions
        # support the multi-window freq gate for regional-bus-rescued groups.
        per_season_hits = {}
        for s, (wd_s, we_s, _nw, _nwe) in season_dates.items():
            per_season_hits[s] = (
                sum(1 for d in wd_s if d in active_dates),
                sum(1 for d in we_s if d in active_dates),
            )

        core_n = eve_n = we_n = 0
        freq_entries = trip_frequencies.get(trip_id, [])
        if freq_entries:
            for start, end, headway in freq_entries:
                if headway <= 0:
                    continue
                core_n += max(0, (min(end, CORE_END) - max(start, CORE_START)) // headway)
                eve_n  += max(0, (min(end, EVENING_END) - max(start, EVENING_START)) // headway)
                we_n   += max(0, (min(end, WEEKEND_END) - max(start, WEEKEND_START)) // headway)
        else:
            if CORE_START <= first_dep < CORE_END:
                core_n = 1
            elif EVENING_START <= first_dep < EVENING_END:
                eve_n = 1
            if WEEKEND_START <= first_dep < WEEKEND_END:
                we_n = 1
        trip_freq[trip_id] = {
            s: (core_n * wd_h, eve_n * wd_h, we_n * we_h)
            for s, (wd_h, we_h) in per_season_hits.items()
        }

        raw_variant = frozenset(s[1] for s in stops)
        merged_set = frozenset(stop_merge.get(s[1]) or s[1].split(":")[0] for s in stops)
        sequence   = [(s[1], s[2], s[3]) for s in stops]
        annual_w = max(1, len(active_dates))
        winter_w = sum(1 for d in active_dates if _date_in_season(d, "winter"))
        summer_w = sum(1 for d in active_dates if _date_in_season(d, "summer"))
        trip_weight_seasonal[trip_id] = {
            "annual": annual_w, "winter": winter_w, "summer": summer_w,
        }
        trip_buf[trip_id] = (
            line_key, trip.get("agency_id", ""),
            annual_w,
            raw_variant, merged_set, sequence,
        )

    with open(GTFS / "stop_times.txt", encoding="utf-8-sig") as f:
        row_count = 0
        for row in csv.DictReader(f):
            tid = row["trip_id"]
            try:
                arr = parse_time(row["arrival_time"])
                dep = parse_time(row["departure_time"])
            except (ValueError, IndexError):
                continue
            stop_id = row["stop_id"]
            seq = int(row["stop_sequence"])

            if tid != current_trip_id:
                process_trip(current_trip_id, current_stops)
                current_trip_id = tid
                current_stops = []

            current_stops.append((seq, stop_id, arr, dep))
            row_count += 1
            if row_count % 2_000_000 == 0:
                print(f"    {row_count // 1_000_000}M rows...")

        process_trip(current_trip_id, current_stops)
    print(f"  Done. {row_count:,} rows processed, {len(trip_buf):,} trips buffered.")

    # ── Trip-group partitioning ──────────────────────────────────────────────
    print("  Partitioning trips and computing trip-groups...")
    partition_trips: dict = defaultdict(list)
    for tid, (lk, aid, _w, _rv, _ms, _seq) in trip_buf.items():
        sn, ln, bkt = lk
        ln_norm = ln.replace(" ", "").lower()
        partition_str = ln_norm if ln_norm else sn.replace(" ", "").lower()
        partition_trips[(partition_str, aid, bkt)].append(tid)

    trip_group: dict = {}
    n_groups_total = 0
    for tids in partition_trips.values():
        patterns: dict = defaultdict(list)
        for tid in tids:
            patterns[trip_buf[tid][4]].append(tid)
        pattern_sets = list(patterns.keys())
        pattern_tids = list(patterns.values())
        P = len(pattern_sets)
        if P == 1:
            for tid in pattern_tids[0]:
                trip_group[tid] = 0
            n_groups_total += 1
            continue

        parent = list(range(P))
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(P):
            si = pattern_sets[i]
            for j in range(i + 1, P):
                ri, rj = find(i), find(j)
                if ri == rj:
                    continue
                sj = pattern_sets[j]
                small, big = (si, sj) if len(si) <= len(sj) else (sj, si)
                count = 0
                for s in small:
                    if s in big:
                        count += 1
                        if count >= 2:
                            parent[ri] = rj
                            break

        cc_ids: dict = {}
        next_id = 0
        for i in range(P):
            root = find(i)
            if root not in cc_ids:
                cc_ids[root] = next_id
                next_id += 1
            for tid in pattern_tids[i]:
                trip_group[tid] = cc_ids[root]
        n_groups_total += next_id

    print(f"  {len(partition_trips):,} partitions → {n_groups_total:,} trip-groups")

    # ── Per-tg_key aggregation ───────────────────────────────────────────────
    # tg_key = (line_key, agency_id, trip_group_id) — the only line identity in
    # this pipeline. Frequency and the canonical trip are both summed/picked
    # here, in the single loop over trip_buf, against the trip-group partition
    # built above. No second partition, no name-based fallback.
    # tg_freq[tg_key][season] = [core_sum, eve_sum, we_sum] across trips.
    tg_freq: dict = defaultdict(
        lambda: {s: [0, 0, 0] for s in SEASONS}
    )
    # var_freq[(tg_key, var_key)][season] = [core_sum, eve_sum, we_sum] —
    # parallel to tg_freq but aggregated per variant for per-direction
    # thickness; see .claude/concepts/seasonal-regional-bus-rescue.md
    # § "Per-variant freq for line thickness".
    var_freq: dict = defaultdict(
        lambda: {s: [0, 0, 0] for s in SEASONS}
    )
    tg_canon: dict = {}  # tg_key → {"canon_score": int, "stops": [(sid, arr, dep), ...]}

    for tid, (lk, aid, weight, raw_variant, merged_set, sequence) in trip_buf.items():
        tg = trip_group.get(tid)
        if tg is None:
            continue
        tg_key = (lk, aid, tg)
        # Expose per-trip group identity, stop sequence, and merged-stop variant
        # for downstream emission and per-group shape dedup.
        _trip_group_export[tid] = (lk, tg, aid)
        _trip_stops_export[tid] = [s[0] for s in sequence]
        _trip_merged_export[tid] = merged_set
        _trip_weight_export[tid] = weight
        _trip_weight_seasonal_export[tid] = trip_weight_seasonal.get(tid)
        first_sid = sequence[0][0]
        last_sid = sequence[-1][0]
        first_uic = stop_merge.get(first_sid) or first_sid.split(":")[0]
        last_uic = stop_merge.get(last_sid) or last_sid.split(":")[0]
        direction_key = (first_uic, last_uic)
        _trip_direction_export[tid] = direction_key
        var_key = (merged_set, direction_key)

        tc = trip_freq.get(tid)
        if tc is not None:
            tgf = tg_freq[tg_key]
            vf = var_freq[(tg_key, var_key)]
            for s in SEASONS:
                c, e, w = tc[s]
                bucket = tgf[s]
                bucket[0] += c
                bucket[1] += e
                bucket[2] += w
                vbucket = vf[s]
                vbucket[0] += c
                vbucket[1] += e
                vbucket[2] += w

        n = len(sequence)
        canon_score = n * weight
        existing = tg_canon.get(tg_key)
        if existing is None or canon_score > existing["canon_score"]:
            tg_canon[tg_key] = {"canon_score": canon_score, "stops": sequence}

    trip_buf.clear()
    trip_freq.clear()

    # Compute per-trip-group speed from each group's canonical trip.
    tg_speed: dict = {}
    for tg_key, canon in tg_canon.items():
        stops = canon["stops"]
        if len(stops) < 2:
            continue
        segments: list = [[]]
        for i, (stop_id, arr, dep) in enumerate(stops):
            segments[-1].append((stop_id, arr, dep))
            if 0 < i < len(stops) - 1 and (dep - arr) > 600:
                segments.append([])

        seg_speeds = []
        for seg in segments:
            if len(seg) < 2:
                continue
            total_time = seg[-1][2] - seg[0][2]
            if total_time <= 0:
                continue
            total_dist = sum(
                haversine_km(
                    *(stop_coords.get(seg[j][0]) or stop_coords.get(seg[j][0].split(":")[0]) or (0,0)),
                    *(stop_coords.get(seg[j+1][0]) or stop_coords.get(seg[j+1][0].split(":")[0]) or (0,0)),
                )
                for j in range(len(seg) - 1)
                if (stop_coords.get(seg[j][0]) or stop_coords.get(seg[j][0].split(":")[0]))
                and (stop_coords.get(seg[j+1][0]) or stop_coords.get(seg[j+1][0].split(":")[0]))
            )
            if total_dist > 0:
                seg_speeds.append(total_dist / (total_time / 3600))

        if seg_speeds:
            tg_speed[tg_key] = round(sum(seg_speeds) / len(seg_speeds), 1)

    # Normalise tg_freq from "trip × sample-days-hit" totals to trips/hour per
    # window. f_core = trips_in_core_window / (n_weekday_samples · core_hours)
    # — the trip group's average trips-per-hour during the core window across
    # weekday sample dates. Same for eve and we.
    #
    # `tg_freq_out` holds the annual (legacy) values, used by every gate that
    # is not regional-bus-rescue aware.
    # `tg_freq_seasonal_out` adds per-season {f_core, f_eve, f_we} for the
    # multi-window freq gate. Seasons with zero sample dates collapse to 0
    # rather than dividing by zero — a rescued group with no sample dates in
    # a given window cannot pass that window's gate.
    tg_freq_out: dict = {}
    tg_freq_seasonal_out: dict = {}
    season_norms = {
        s: (max(1, ns[2]) * CORE_HOURS,
            max(1, ns[2]) * EVENING_HOURS,
            max(1, ns[3]) * WEEKEND_HOURS)
        for s, ns in season_dates.items()
    }
    for tg_key, per_season in tg_freq.items():
        seasonal = {}
        for s, (c, e, w) in per_season.items():
            cn, en, wen = season_norms[s]
            seasonal[s] = {
                "f_core": c / cn,
                "f_eve":  e / en,
                "f_we":   w / wen,
            }
        tg_freq_seasonal_out[tg_key] = seasonal
        tg_freq_out[tg_key] = seasonal["annual"]

    # Normalise var_freq the same way. var_freq_seasonal_out[(tg_key, var_key)]
    # mirrors tg_freq_seasonal_out's per-season {f_core, f_eve, f_we} shape.
    var_freq_seasonal_out: dict = {}
    for key, per_season in var_freq.items():
        seasonal = {}
        for s, (c, e, w) in per_season.items():
            cn, en, wen = season_norms[s]
            seasonal[s] = {
                "f_core": c / cn,
                "f_eve":  e / en,
                "f_we":   w / wen,
            }
        var_freq_seasonal_out[key] = seasonal

    return (tg_freq_out, tg_freq_seasonal_out,
            var_freq_seasonal_out, tg_speed, tg_canon)


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


# ── Mountain feature deduplication ───────────────────────────────────────────

def _feat_bbox(feat):
    coords = feat["geometry"]["coordinates"]
    if feat["geometry"]["type"] == "MultiLineString":
        pts = [c for seg in coords for c in seg]
    else:
        pts = coords
    if not pts:
        return None
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return (min(lons), min(lats), max(lons), max(lats))


def _n_pts(feat) -> int:
    coords = feat["geometry"]["coordinates"]
    if feat["geometry"]["type"] == "MultiLineString":
        return sum(len(s) for s in coords)
    return len(coords)


_AERIAL_ROUTE_TYPES = {"1300", "1303"}


def deduplicate_mountain(features: list) -> list:
    """Drop overlapping aerial features (cable cars, gondolas) sharing the same
    ref. Best (most geometry vertices) wins.

    Restricted to mountain_origin == "aerial" (GTFS route_type 5/6). The
    problem this solves is multiple OSM route relations for the same physical
    haul cable. Aerial is exempt from the per-direction split (see
    direction-coverage Mode-exemptions), so the dedup key is `ref` alone.
    Funiculars, rebucketed mountain rail, and every other mode are not
    collapsed.
    """
    aerial_idx = [(i, f) for i, f in enumerate(features)
                  if f["properties"].get("mountain_origin") == "aerial"]
    aerial_set = {i for i, _ in aerial_idx}
    keep = set(i for i in range(len(features)) if i not in aerial_set)

    by_ref: dict = defaultdict(list)
    for i, f in aerial_idx:
        ref = f["properties"]["ref"]
        by_ref[ref].append((i, f, _feat_bbox(f), _n_pts(f)))

    n_dropped = 0
    for ref, group in by_ref.items():
        if not ref:
            for i, f, b, n in group:
                keep.add(i)
            continue
        group.sort(key=lambda x: -x[3])
        kept_bboxes = []
        for i, f, b, n in group:
            if b is None:
                keep.add(i)
                continue
            is_dup = any(_bbox_overlap_fraction(b, kb) >= 0.65 for kb in kept_bboxes)
            if is_dup:
                n_dropped += 1
            else:
                keep.add(i)
                kept_bboxes.append(b)
    if n_dropped:
        print(f"  Aerial dedup: removed {n_dropped} duplicate features")
    return [f for i, f in enumerate(features) if i in keep]


# ── Pfaedle shape grouping ───────────────────────────────────────────────────

# Aerial GTFS route_types (extended codes 1300 = aerial lift, 1303 =
# Bern-style elevator) where OSM coverage is patchy enough that a missing
# pfaedle shape is treated as a straight-line fallback rather than a hard
# `pfaedle_unrouted` failure. Funiculars (1400) and every other mode drop
# the feature when pfaedle has no shape, same as rail / bus today.
_STRAIGHT_LINE_FALLBACK_ROUTE_TYPES = _AERIAL_ROUTE_TYPES


def stops_to_polyline(stop_ids: list, stop_coords: dict) -> list:
    """Build a polyline from a stop_id sequence, dropping unresolved stops."""
    out: list = []
    last = None
    for sid in stop_ids:
        c = stop_coords.get(sid) or stop_coords.get(sid.split(":")[0])
        if not c:
            continue
        if last is not None and c == last:
            continue
        out.append([c[0], c[1]])
        last = c
    return out


def best_trip_in_shape_group(trip_ids: list, trip_lookup: dict,
                              svc_dates: dict) -> str:
    """Pick a representative trip for a shape group — the one with the most
    active service days (proxy for "most canonical")."""
    best = None
    best_score = -1
    for tid in trip_ids:
        t = trip_lookup.get(tid)
        if not t:
            continue
        score = len(svc_dates.get(t["service_id"], set()))
        if score > best_score:
            best_score = score
            best = tid
    return best or trip_ids[0]


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_cfg()

    print("Loading GTFS data...")
    stop_coords  = load_stops()
    stop_meta    = load_stop_meta()
    svc_dates    = load_calendar_dates()
    svc_dates_full = load_calendar_dates_full()
    route_lookup = load_routes()
    agency_names = load_agencies()
    mountain_aids = set(cfg.get("mountain_agency_ids", []) or [])
    trip_lookup  = load_trips(route_lookup, mountain_aids)
    print(f"  {len(stop_coords):,} stop entries, {len(svc_dates):,} service IDs, "
          f"{len(trip_lookup):,} trips, {len(agency_names):,} agencies")
    if mountain_aids:
        print(f"  {len(mountain_aids)} agencies rebucketed train→mountain: "
              f"{sorted(mountain_aids)}")
    print(f"  {len(svc_dates_full):,} service IDs with full-calendar coverage")

    min_active_days_default = int(cfg.get("min_active_days", 150))
    min_active_days_by_bucket = {
        b: int(v) for b, v in (cfg.get("min_active_days_by_bucket") or {}).items()
    }
    min_active_days_regional_bus = int(
        cfg.get("min_active_days_regional_bus", min_active_days_default)
    )
    unique_stop_min_distance_m = float(
        cfg.get("unique_stop_min_distance_m", 1000)
    )
    unique_stop_min_share_pct = float(
        cfg.get("unique_stop_min_share_pct", 0.02)
    )

    def min_active_days_for(bucket: str) -> int:
        return min_active_days_by_bucket.get(bucket, min_active_days_default)

    trip_frequencies = load_frequencies()
    print(f"  {sum(len(v) for v in trip_frequencies.values()):,} frequency entries "
          f"for {len(trip_frequencies):,} trips")

    (tg_freq, tg_freq_seasonal, var_freq_seasonal,
     tg_speed, tg_canon) = stream_stop_times(
        trip_lookup, stop_coords, svc_dates, trip_frequencies, stop_meta)

    # Ferry / aerial / funicular trips keep their natural per-trip
    # (first_uic, last_uic) direction key — same as every other bucket.
    # See .claude/concepts/remove-exempt-direction-key.md.

    print("\nLoading pfaedle shapes...")
    shapes = load_shapes()
    print(f"  {len(shapes):,} shapes loaded")

    # ── Group trips per (line_key, agency_id, trip_group_id) by merged stops ─
    # The grouping pass partitions by (long_name_norm, agency_id, bucket), so
    # trip_group_id is unique only WITHIN a partition. Different agencies with
    # the same line_key restart tg_id at 0 — e.g. Bernmobil's bus 10 and
    # Stadtbus Winterthur's bus 10 both end up tg_id=0 in their own pools.
    # The emission key must include agency_id, otherwise the two cities'
    # trips collide and one silently overwrites the other.
    # Each variant key is (merged_set, direction_key) so opposite directions of
    # the same merged-stop set form distinct variants. Per direction-coverage
    # concept: directions are split end-to-end so each gets its own pfaedle
    # shape, rep trip, stop list, and filter outcome. Applies to every bucket
    # including ferry / aerial / funicular (see remove-exempt-direction-key
    # concept).
    groups: dict = defaultdict(lambda: defaultdict(list))
    variant_counts: dict = defaultdict(lambda: defaultdict(int))
    # Per-season weighted variant counts; consumed by the multi-window
    # rare-variant filter for groups containing at least one
    # regional_bus_rescued variant.
    variant_counts_seasonal: dict = {
        s: defaultdict(lambda: defaultdict(int)) for s in SEASONS
    }
    for tid, (line_key, tg_id, aid) in _trip_group_export.items():
        merged_set = _trip_merged_export.get(tid)
        if merged_set is None:
            continue
        direction_key = _trip_direction_export.get(tid)
        if direction_key is None:
            continue
        tg_key = (line_key, aid, tg_id)
        var_key = (merged_set, direction_key)
        groups[tg_key][var_key].append(tid)
        # Weighted by trip's active-date count so a depot run modelled as a few
        # trip_ids active on a single date counts as much smaller service than
        # the same trip_ids active every weekday. Same weight that the
        # in-stream rare-variant filter already uses.
        variant_counts[tg_key][var_key] += _trip_weight_export.get(tid, 1)
        sweights = _trip_weight_seasonal_export.get(tid) or {}
        for s in SEASONS:
            variant_counts_seasonal[s][tg_key][var_key] += sweights.get(s, 0)

    # Snapshot for the comprehensive diagnostic before the active-days,
    # rare-group, and rare-variant filters mutate `groups`. Variants are keyed
    # by (merged_set, direction_key) — the diagnostic preserves the same key
    # so per-direction outcomes can be reported.
    diag_original = {
        tg_key: {var_key: list(tids) for var_key, tids in vmap.items()}
        for tg_key, vmap in groups.items()
    }

    # Per-trip-group origin classification: ferry, mountain (aerial / funicular
    # / rebucketed_rail) or None. Drives the gate exemption — aerial and
    # funicular trips skip the freq-score and active-days gates, rebucketed
    # rail does not. route_type is taken from any trip in the group; trips in
    # one (line_key, agency_id, tg_id) share the same route_type in practice.
    tg_mountain_origin: dict = {}
    tg_route_type: dict = {}
    for tg_key, vmap in groups.items():
        line_key, _aid, _tg_id = tg_key
        bucket = line_key[2]
        rt = ""
        for tids in vmap.values():
            if tids:
                rt = (route_lookup.get(trip_lookup.get(tids[0], {})
                                       .get("route_id", ""), {})
                      .get("type", ""))
                break
        tg_mountain_origin[tg_key] = _mountain_origin(bucket, rt)
        tg_route_type[tg_key] = rt

    # ── Active-days per variant (concept: active-days-filter) ────────────────
    # For each emitted-feature unit (line_key, agency_id, trip_group_id,
    # merged_stop_set, direction_key), compute the union of active calendar
    # dates across every trip in that variant over the full feed validity
    # period. Variants below `min_active_days` are dropped before
    # supergroup/rare-variant filters run, so their weighted trips don't
    # pollute share calculations. Catches construction-replacement services
    # even when several distinct constructions share the same ref or the same
    # trip group. Per direction-coverage concept: ferry, aerial, and funicular
    # are exempt; rebucketed mountain rail is gated like normal train.
    variant_service_ids: dict = defaultdict(set)
    for tid, (lk, tg_id_v, aid) in _trip_group_export.items():
        merged_set = _trip_merged_export.get(tid)
        if merged_set is None:
            continue
        direction_key = _trip_direction_export.get(tid)
        if direction_key is None:
            continue
        t = trip_lookup.get(tid)
        if t:
            variant_service_ids[(lk, aid, tg_id_v, merged_set, direction_key)] \
                .add(t["service_id"])
    variant_active_days: dict = {}
    for vkey, sids in variant_service_ids.items():
        u: set = set()
        for sid in sids:
            u |= svc_dates_full.get(sid, set())
        variant_active_days[vkey] = len(u)
    # svc_dates_full is the heaviest object after stop_times; release.
    svc_dates_full.clear()
    variant_service_ids.clear()

    short_active_variants: dict = defaultdict(set)  # tg_key → {var_key,...}
    # Bus variants below `min_active_days` but at or above
    # `min_active_days_regional_bus` are kept and tagged. Dropped later at
    # emission if the line classifies as city bus.
    regional_bus_rescued: dict = defaultdict(set)   # tg_key → {var_key,...}
    tg_keys_all_short_active: set = set()
    for tg_key in list(groups.keys()):
        line_key, aid, tg_id_v = tg_key
        bucket = line_key[2]
        if _gate_exempt(bucket, tg_mountain_origin.get(tg_key)):
            continue
        vmap = groups[tg_key]
        threshold = min_active_days_for(bucket)
        rescue_floor = (min_active_days_regional_bus if bucket == "bus"
                        else threshold)
        to_drop: list = []
        for var_key in vmap:
            ad = variant_active_days.get(
                (line_key, aid, tg_id_v, var_key[0], var_key[1]), 0
            )
            if ad >= threshold:
                continue
            if bucket == "bus" and ad >= rescue_floor:
                regional_bus_rescued[tg_key].add(var_key)
                continue
            to_drop.append(var_key)
        if not to_drop:
            continue
        short_active_variants[tg_key].update(to_drop)
        for var_key in to_drop:
            del vmap[var_key]
            variant_counts[tg_key].pop(var_key, None)
        if not vmap:
            tg_keys_all_short_active.add(tg_key)
            del groups[tg_key]
    n_dropped_var = sum(len(s) for s in short_active_variants.values())
    n_rescued_var = sum(len(s) for s in regional_bus_rescued.values())
    threshold_summary = f"default={min_active_days_default}"
    if min_active_days_by_bucket:
        overrides = ", ".join(f"{b}={v}" for b, v in
                              sorted(min_active_days_by_bucket.items()))
        threshold_summary += f"; {overrides}"
    print(f"  {n_dropped_var:,} variants dropped by min_active_days "
          f"({threshold_summary}) "
          f"(across {len(short_active_variants)} trip groups; "
          f"{len(tg_keys_all_short_active)} groups fully dropped)")
    print(f"  {n_rescued_var:,} bus variants tentatively rescued "
          f"(active_days in [{min_active_days_regional_bus}, "
          f"{min_active_days_default}); dropped at emission if classified as city bus)")

    # ── Supergroup formation + rare-group filter ─────────────────────────────
    # A supergroup is a transient classification used only for the rare-group
    # drop below: trip groups inside one partition (short_name, agency, bucket)
    # that share ≥1 merged stop are unioned. Once classified, drop trip groups
    # whose weighted share of their supergroup's total is below 10% (5%
    # fallback, then keep-all). Catches depot runs that were isolated as their
    # own trip group because they share <2 merged stops with the main service.
    tg_total_weight: dict = {
        tg_key: sum(variant_counts[tg_key].values()) for tg_key in groups.keys()
    }
    tg_merged_union: dict = {}
    for tg_key, vmap in groups.items():
        u: set = set()
        for var_key in vmap.keys():
            merged_set = var_key[0]
            u |= merged_set
        tg_merged_union[tg_key] = frozenset(u)

    partition_to_tgkeys: dict = defaultdict(list)
    for tg_key in groups.keys():
        line_key, aid, _tg_id = tg_key
        sn, ln, bkt = line_key
        ln_norm = ln.replace(" ", "").lower()
        partition_str = ln_norm if ln_norm else sn.replace(" ", "").lower()
        partition_to_tgkeys[(partition_str, aid, bkt)].append(tg_key)

    supergroup_id_by_tg: dict = {}        # tg_key → int (sequential)
    supergroup_members: dict = defaultdict(list)  # sg_id → [tg_key, ...]
    sg_counter = 0
    for partition_key, tg_keys in partition_to_tgkeys.items():
        K = len(tg_keys)
        if K == 1:
            sg_id = sg_counter
            sg_counter += 1
            supergroup_id_by_tg[tg_keys[0]] = sg_id
            supergroup_members[sg_id].append(tg_keys[0])
            continue

        parent = list(range(K))
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        unions = [tg_merged_union[tg_keys[i]] for i in range(K)]
        for i in range(K):
            for j in range(i + 1, K):
                ri, rj = find(i), find(j)
                if ri == rj:
                    continue
                si, sj = unions[i], unions[j]
                small, big = (si, sj) if len(si) <= len(sj) else (sj, si)
                if any(s in big for s in small):
                    parent[ri] = rj

        cc_to_id: dict = {}
        for i in range(K):
            root = find(i)
            if root not in cc_to_id:
                cc_to_id[root] = sg_counter
                sg_counter += 1
            sg_id = cc_to_id[root]
            supergroup_id_by_tg[tg_keys[i]] = sg_id
            supergroup_members[sg_id].append(tg_keys[i])

    supergroup_total_weight: dict = {
        sg_id: sum(tg_total_weight[tg] for tg in members)
        for sg_id, members in supergroup_members.items()
    }

    rare_group_dropped: set = set()
    rare_group_threshold_by_sg: dict = {}
    for sg_id, members in supergroup_members.items():
        if len(members) == 1:
            rare_group_threshold_by_sg[sg_id] = None
            continue
        sg_total = supergroup_total_weight[sg_id]
        threshold_used = None
        for pct in (0.10, 0.05):
            threshold = max(1, sg_total * pct)
            kept = [tg for tg in members if tg_total_weight[tg] >= threshold]
            if kept:
                threshold_used = pct
                kept_set = set(kept)
                for tg in members:
                    if tg in kept_set:
                        continue
                    # Mountain bucket and CC-train carve-out: same exemption
                    # the freq-score gate applies. Never drop these via the
                    # rare-group filter.
                    line_key_drop = tg[0]
                    if line_key_drop[2] == "mountain" or (
                        line_key_drop[0] == "CC" and line_key_drop[2] == "train"
                    ):
                        continue
                    rare_group_dropped.add(tg)
                break
        rare_group_threshold_by_sg[sg_id] = threshold_used

    for tg_key in rare_group_dropped:
        groups.pop(tg_key, None)

    # filter_outcomes[tg_key] = {var_key: (outcome, threshold_pct_used)}
    # where outcome ∈ {"kept", "rare_variant", "short_active_period"} and
    # var_key = (merged_set, direction_key).
    diag_filter: dict = {}

    # Record variants dropped by the active-days filter. These don't reach
    # the rare-variant loop below (they're already gone from `groups`).
    for tg_key, dropped in short_active_variants.items():
        bucket_entry = diag_filter.setdefault(tg_key, {})
        for var_key in dropped:
            bucket_entry[var_key] = ("short_active_period", None)

    # Rare-variant filter — two phases for groups with at least one
    # regional_bus_rescued variant; legacy single-phase otherwise. See
    # .claude/concepts/seasonal-regional-bus-rescue.md.
    #
    # Phase 1 (share gate):
    #   - Rescued-bearing group: 10% per window (annual/winter/summer). No 5%
    #     fallback. A variant is "kept-by-share" if it clears 10% in any
    #     window.
    #   - Other group: legacy 10%/5%-fallback against the annual window only.
    # Phase 2 (built only after phase 1 has run for every group):
    #   - global_kept_uics = union of parent UICs served by any kept-by-share
    #     variant across the whole dataset.
    # Phase 3 (rescued-bearing groups only):
    #   - Unique-stop rescue. A non-kept variant is rescued if it serves a
    #     parent UIC not in global_kept_uics AND that UIC is ≥
    #     unique_stop_min_distance_m from every UIC in *this* group's kept-by-
    #     share set AND the variant's weighted-share is ≥
    #     unique_stop_min_share_pct in at least one window.
    #
    # Diagnostics:
    #   rare_variant_window_passed[(tg_key, var_key)] ∈
    #     {"annual", "winter", "summer", "unique_stop", None}
    #   rare_variant_threshold_pct_passed[(tg_key, var_key)] = 0.10 / 0.05 /
    #     None (None for unique_stop rescues or for drops).
    rare_variant_window_passed: dict = {}
    rare_variant_threshold_pct_passed: dict = {}

    def _pct_pass(counts: dict, vmap_keys, pct: float) -> set:
        total = sum(counts.values())
        threshold = max(1, total * pct)
        return {vk for vk in vmap_keys if counts.get(vk, 0) >= threshold}

    def _legacy_rare_variant(counts: dict, vmap_keys) -> tuple:
        """Annual 10% then 5% fallback; if both pass nothing, keep all."""
        for pct in (0.10, 0.05):
            kept = _pct_pass(counts, vmap_keys, pct)
            if kept:
                return kept, pct
        return set(vmap_keys), None

    # ── Phase 1: standard share gate for every group ─────────────────────
    kept_by_share: dict = {}  # tg_key → set(var_key)
    for tg_key, vmap in list(groups.items()):
        is_rescued_group = bool(regional_bus_rescued.get(tg_key))
        if is_rescued_group:
            per_window_kept: dict = {}
            for s in SEASONS:
                counts_s = (variant_counts if s == "annual"
                            else variant_counts_seasonal[s])[tg_key]
                per_window_kept[s] = _pct_pass(counts_s, vmap, 0.10)
            kept: set = set()
            for var_key in vmap:
                for s in SEASONS:
                    if var_key in per_window_kept[s]:
                        kept.add(var_key)
                        rare_variant_window_passed[(tg_key, var_key)] = s
                        rare_variant_threshold_pct_passed[(tg_key, var_key)] = 0.10
                        break
            kept_by_share[tg_key] = kept
        else:
            kept, pct = _legacy_rare_variant(variant_counts[tg_key], vmap)
            kept_by_share[tg_key] = kept
            for var_key in kept:
                rare_variant_window_passed[(tg_key, var_key)] = "annual"
                rare_variant_threshold_pct_passed[(tg_key, var_key)] = pct

    # ── Phase 2: global kept-by-share parent UIC set ─────────────────────
    # var_key[0] is the merged-stop frozenset = the variant's parent UICs.
    global_kept_uics: set = set()
    for tg_key, kept in kept_by_share.items():
        for var_key in kept:
            global_kept_uics |= var_key[0]

    # ── Phase 3: unique-stop rescue (rescued-bearing groups only) ────────
    n_unique_stop_rescued = 0
    for tg_key, vmap in groups.items():
        if not regional_bus_rescued.get(tg_key):
            continue
        kept = kept_by_share[tg_key]
        group_kept_uics: set = set()
        for vk in kept:
            group_kept_uics |= vk[0]
        # Pre-resolve this group's kept UIC coordinates once.
        group_kept_coords: list = []
        for uic in group_kept_uics:
            c = stop_coords.get(uic) or stop_coords.get(uic.split(":")[0])
            if c:
                group_kept_coords.append(c)

        for var_key in vmap:
            if var_key in kept:
                continue
            candidate_uics = var_key[0] - global_kept_uics
            if not candidate_uics:
                continue
            qualifying = False
            for uic in candidate_uics:
                uic_coord = stop_coords.get(uic) \
                    or stop_coords.get(uic.split(":")[0])
                if uic_coord is None:
                    continue
                far_enough = True
                for kept_coord in group_kept_coords:
                    if haversine_km(uic_coord[0], uic_coord[1],
                                    kept_coord[0], kept_coord[1]) * 1000.0 \
                            < unique_stop_min_distance_m:
                        far_enough = False
                        break
                if far_enough:
                    qualifying = True
                    break
            if not qualifying:
                continue
            passes_floor = False
            for s in SEASONS:
                counts_s = (variant_counts if s == "annual"
                            else variant_counts_seasonal[s])[tg_key]
                total = sum(counts_s.values())
                share = (counts_s.get(var_key, 0) / total) if total else 0
                if share >= unique_stop_min_share_pct:
                    passes_floor = True
                    break
            if not passes_floor:
                continue
            kept.add(var_key)
            rare_variant_window_passed[(tg_key, var_key)] = "unique_stop"
            rare_variant_threshold_pct_passed[(tg_key, var_key)] = None
            n_unique_stop_rescued += 1
    if n_unique_stop_rescued:
        print(f"  {n_unique_stop_rescued:,} variants rescued by unique-stop rule")

    # ── Apply kept_by_share to groups + populate diag_filter ─────────────
    for tg_key, vmap in list(groups.items()):
        kept = kept_by_share.get(tg_key, set())
        if kept and kept != set(vmap.keys()):
            groups[tg_key] = {vk: vmap[vk] for vk in kept}
        bucket_entry = diag_filter.setdefault(tg_key, {})
        for var_key in vmap:
            if var_key in kept:
                bucket_entry[var_key] = (
                    "kept",
                    rare_variant_threshold_pct_passed.get((tg_key, var_key)),
                )
            else:
                rare_variant_window_passed.setdefault((tg_key, var_key), None)
                bucket_entry[var_key] = ("rare_variant", None)

    # Trip groups dropped by the supergroup filter never reached the per-variant
    # filter loop above; mark any of their variants we haven't already labelled
    # (i.e. weren't short_active) as "kept" so the group-level reason
    # `rare_group_dropped` is what surfaces for them.
    for tg_key in rare_group_dropped:
        bucket_entry = diag_filter.setdefault(tg_key, {})
        for var_key in diag_original.get(tg_key, {}):
            bucket_entry.setdefault(var_key, ("kept", None))

    # Pre-filter low-freq groups out so they don't waste downstream work. The
    # active-days gate has already run upstream at variant granularity. Per
    # direction-coverage concept: ferry + true mountain (aerial/funicular)
    # skip the gate; rebucketed rail does not.
    #
    # Groups containing at least one regional_bus_rescued variant are
    # evaluated against three windows (annual / winter Jan-Mar / summer
    # Jun-Aug) and pass if the f_weighted in any window exceeds worst_freq.
    # The winning window's raw freq becomes the group's effective freq for
    # downstream emission and salience — line thickness / visibility track
    # in-season cadence rather than annual dilution.
    # See .claude/concepts/seasonal-regional-bus-rescue.md.
    drawable_groups = {}
    freq_gate_window_passed: dict = {}  # tg_key → "annual"|"winter"|"summer"|None
    best_freq_map, worst_freq_map = _frequencies()
    for (line_key, aid, tg_id), variant_map in groups.items():
        bucket = line_key[2]
        mode_approx = _BUCKET_MODE_APPROX.get(bucket, "regional_bus")
        tg_key = (line_key, aid, tg_id)
        seasonal = tg_freq_seasonal.get(tg_key) or {}
        raw_annual = seasonal.get("annual") \
            or {"f_core": 0.0, "f_eve": 0.0, "f_we": 0.0}
        worst_f = worst_freq_map.get(mode_approx, 0.0)
        exempt = _freq_gate_exempt(bucket, tg_mountain_origin.get(tg_key)) or (
            line_key[0] == "CC" and bucket == "train"
        )
        is_rescued_group = bool(regional_bus_rescued.get(tg_key))
        passed = False
        if exempt:
            passed = True
            freq_gate_window_passed[tg_key] = "annual"
        else:
            windows = SEASONS if is_rescued_group else ("annual",)
            for s in windows:
                raw_s = seasonal.get(s) or {"f_core": 0.0, "f_eve": 0.0, "f_we": 0.0}
                if weighted_freq(raw_s) > worst_f:
                    passed = True
                    freq_gate_window_passed[tg_key] = s
                    if s != "annual":
                        # Rebind the group's effective annual freq to the
                        # winning window so downstream thickness / salience
                        # use in-season cadence.
                        tg_freq[tg_key] = dict(raw_s)
                    break
            if not passed:
                freq_gate_window_passed[tg_key] = None
        if passed:
            drawable_groups[tg_key] = variant_map
    n_rescued_drawable = sum(1 for k, v in drawable_groups.items()
                             if freq_gate_window_passed.get(k) not in (None, "annual"))
    print(f"  {len(drawable_groups):,} drawable (line_key, agency, trip_group) entries "
          f"({n_rescued_drawable} via seasonal window)")

    # ── Emit features ────────────────────────────────────────────────────────
    features: list = []
    line_stops_out: dict = {}
    pfaedle_unrouted: list = []
    trip_groups_diag: list = []
    matched_tg_keys: set = set()
    feature_id_counter = 0
    # Per-(tg_key, var_key) emission outcome for the comprehensive diagnostic.
    diag_emission: dict = {}

    _ZERO_FREQ = {"f_core": 0.0, "f_eve": 0.0, "f_we": 0.0}
    for (line_key, agency_id, tg_id), variant_map in drawable_groups.items():
        short_name, long_name, bucket = line_key
        all_trips = [tid for trips in variant_map.values() for tid in trips]

        tg_key = (line_key, agency_id, tg_id)
        # Group-level raw_freq retained for the diagnostic and as fallback.
        raw_freq  = tg_freq.get(tg_key, _ZERO_FREQ)
        # Winning window for this group's freq gate determines which window's
        # per-variant freq drives thickness — same window everywhere in the
        # group so a seasonal-rescued group's thickness reflects in-season
        # cadence per direction.
        gate_window = freq_gate_window_passed.get(tg_key) or "annual"
        speed_kmh = tg_speed.get(tg_key)
        for var_key, trip_ids in variant_map.items():
            merged_set, direction_key = var_key
            # Pick the rep from the most common platform sub-variant within
            # this direction so the drawn line tracks the dominant platform
            # pattern of the direction it represents. Ties resolve by smallest
            # min trip_id for stable output.
            by_raw: dict = defaultdict(list)
            for tid in trip_ids:
                raw = frozenset(_trip_stops_export.get(tid, ()))
                by_raw[raw].append(tid)
            popular_raw = sorted(
                by_raw.keys(),
                key=lambda r: (
                    -sum(_trip_weight_export.get(t, 1) for t in by_raw[r]),
                    min(by_raw[r]),
                ),
            )[0]
            popular_trips = by_raw[popular_raw]
            rep_tid = best_trip_in_shape_group(popular_trips, trip_lookup, svc_dates)
            rep_trip = trip_lookup.get(rep_tid, {})
            stop_ids = _trip_stops_export.get(rep_tid, [])

            # Shape fallback: prefer the popular sub-variant; fall back across
            # the rest of the direction sub-partition so a direction where
            # pfaedle routed only an unusual platform isn't silently dropped.
            popular_set = set(popular_trips)
            other_trips = [t for t in trip_ids if t not in popular_set]
            candidates = (
                [rep_tid]
                + [t for t in popular_trips if t != rep_tid]
                + other_trips
            )
            shape_id = ""
            for cand_tid in candidates[:51]:
                sid = trip_lookup.get(cand_tid, {}).get("shape_id", "")
                if sid and sid in shapes:
                    shape_id = sid
                    break

            route_type = (route_lookup.get(rep_trip.get("route_id", ""), {})
                          .get("type", ""))
            mountain_origin = _mountain_origin(bucket, route_type)
            direction_key_str = f"{direction_key[0]}-{direction_key[1]}"

            polyline = []
            geometry_source = "pfaedle"
            if shape_id:
                polyline = [list(p) for p in shapes[shape_id]]
                length_km = polyline_length_km(polyline)
            elif route_type in _STRAIGHT_LINE_FALLBACK_ROUTE_TYPES:
                polyline = stops_to_polyline(stop_ids, stop_coords)
                length_km = polyline_length_km(polyline)
                geometry_source = "straight_line_fallback"
            else:
                pfaedle_unrouted.append({
                    "trip_id": rep_tid,
                    "route_id": rep_trip.get("route_id", ""),
                    "short_name": short_name,
                    "long_name": long_name,
                    "bucket": bucket,
                    "trip_group_id": tg_id,
                    "direction_key": direction_key_str,
                })
                diag_emission[(tg_key, var_key)] = {
                    "feature_emitted": False,
                    "exclusion_reason": "pfaedle_unrouted",
                    "rep_trip_id": rep_tid, "shape_id": "",
                    "n_coords": 0, "line_km": 0.0,
                }
                continue

            if len(polyline) < 2:
                diag_emission[(tg_key, var_key)] = {
                    "feature_emitted": False,
                    "exclusion_reason": "polyline_too_short",
                    "rep_trip_id": rep_tid, "shape_id": shape_id,
                    "n_coords": len(polyline), "line_km": round(length_km, 2),
                }
                continue

            # Final mode classification.
            mode = gtfs_to_mode(bucket, agency_id,
                                short_name=short_name, length_km=length_km,
                                route_type=route_type)

            # Drop seasonal-rescue bus variants that landed in city `bus`.
            if (var_key in regional_bus_rescued.get(tg_key, ())
                    and mode == "bus"):
                diag_emission[(tg_key, var_key)] = {
                    "feature_emitted": False,
                    "exclusion_reason": "seasonal_rescue_city_bus",
                    "rep_trip_id": rep_tid, "shape_id": shape_id,
                    "n_coords": len(polyline), "line_km": round(length_km, 2),
                }
                continue

            # Per-variant freq for thickness — see
            # .claude/concepts/seasonal-regional-bus-rescue.md
            # § "Per-variant freq for line thickness". Falls back to group
            # freq if the variant has no per-variant data (shouldn't happen
            # since trip_buf populates both, but safe).
            var_seasonal = var_freq_seasonal.get((tg_key, var_key)) or {}
            variant_raw_freq = var_seasonal.get(gate_window) \
                or var_seasonal.get("annual") or _ZERO_FREQ
            freq_score = compute_freq_score(variant_raw_freq, mode)
            variant_f_weighted = weighted_freq(variant_raw_freq)

            color      = speed_to_color(mode, speed_kmh)
            width_base = score_to_width_base(freq_score, mode)

            feature_id_counter += 1
            feat_id = f"tg{tg_id}_s{feature_id_counter}"

            # Geometry — always LineString for new emission.
            geometry = {"type": "LineString", "coordinates": polyline}
            props = {
                "osm_id":       feat_id,
                "ref":          short_name,
                "name":         long_name,
                "operator":     agency_names.get(agency_id, ""),
                "agency_id":    agency_id,
                "mode":         mode,
                "route_type":   route_type,
                "freq_score":   freq_score,
                "f_weighted":   round(variant_f_weighted, 4),
                "speed_kmh":    speed_kmh,
                "color":        color,
                "width_base":   width_base,
                "line_km":      round(length_km, 1),
                "direction_id": rep_trip.get("direction_id", ""),
                "direction_key": direction_key_str,
                "trip_group_id": tg_id,
                "shape_id":     shape_id or "",
                "gtfs_matched": True,
                "geometry_source": geometry_source,
            }
            if mountain_origin:
                props["mountain_origin"] = mountain_origin
            features.append({
                "type": "Feature",
                "geometry": geometry,
                "properties": props,
            })

            # Per-feature stops.
            stop_entries: list = []
            for sid in stop_ids:
                c = stop_coords.get(sid) or stop_coords.get(sid.split(":")[0])
                if c:
                    stop_entries.append([c[0], c[1], sid])
            line_stops_out[feat_id] = {
                "osm_ref": short_name,
                "stops":   stop_entries,
                "gtfs_ref": short_name,
                "direction_key": direction_key_str,
            }

            matched_tg_keys.add(tg_key)
            diag_emission[(tg_key, var_key)] = {
                "feature_emitted": True,
                "exclusion_reason": None,
                "rep_trip_id": rep_tid,
                "shape_id": shape_id,
                "n_coords": len(polyline),
                "line_km": round(length_km, 2),
                "feature_id": feat_id,
                "geometry_source": geometry_source,
            }

        # Diagnostic snapshot for this trip group.
        trip_groups_diag.append({
            "short_name":    short_name,
            "long_name":     long_name,
            "bucket":        bucket,
            "agency_id":     agency_id,
            "agency_name":   agency_names.get(agency_id, ""),
            "trip_group_id": tg_id,
            "trip_count":    len(all_trips),
            "variant_count": len(variant_map),
        })

    # Aerial dedup: collapse duplicate haul-cable features (route_type 5/6)
    # that share a ref. Drop now-orphaned line_stops entries.
    features = deduplicate_mountain(features)
    kept_ids = {f["properties"]["osm_id"] for f in features}
    line_stops_out = {oid: v for oid, v in line_stops_out.items() if oid in kept_ids}

    # ── Per-stop "size score" for far-zoom dot rendering ─────────────────────
    # See .claude/concepts/far-zoom-stop-dot-redesign.md. Each emitted feature
    # contributes `mode_weight × (1 + freq_score)` once to every non-terminal
    # stop on its sequence — the (1 + freq_score) multiplier ranges from 1 at
    # worst_freq to 2 at best_freq, so a high-frequency line is worth twice
    # a low-frequency line of the same mode but a low-frequency line still
    # counts. Per direction: bidirectional service contributes from both
    # features. Terminal arrivals are excluded — a feature only counts at a
    # stop if it actually departs that stop. Loop pass-throughs do not
    # multiply: one contribution per feature regardless of how many times
    # the feature's stop sequence visits the stop. Aggregated per parent UIC
    # so platforms of the same physical station combine.
    stop_size_mw = (cfg.get("stop_dot_sizing") or {}).get("mode_weights") or {}
    stop_score: dict = defaultdict(float)
    for f in features:
        p = f["properties"]
        mw = float(stop_size_mw.get(p.get("mode", ""), 0.0))
        if mw <= 0:
            continue
        fs = float(p.get("freq_score", 0.0))
        contribution = mw * (1.0 + fs)
        feat_id = p["osm_id"]
        feat_stops = (line_stops_out.get(feat_id) or {}).get("stops") or []
        if len(feat_stops) < 2:
            continue
        seen_uics: set = set()
        for entry in feat_stops[:-1]:
            sid = entry[2] if len(entry) >= 3 else ""
            if not sid:
                continue
            meta = stop_meta.get(sid) or stop_meta.get(sid.split(":")[0])
            parent = meta[1] if meta else ""
            uic = parent if parent else sid.split(":")[0]
            if uic in seen_uics:
                continue
            seen_uics.add(uic)
            stop_score[uic] += contribution

    stop_score_out = {uic: round(v, 4) for uic, v in sorted(stop_score.items())}
    OUT_STOP_SCORES.write_text(json.dumps(stop_score_out, ensure_ascii=False))
    print(f"  {len(stop_score_out):,} stops scored → {OUT_STOP_SCORES.name}")

    # Print 20th / 80th percentile for re-pinning stop_dot_sizing.score_range.
    if stop_score_out:
        sorted_scores = sorted(stop_score_out.values())
        n = len(sorted_scores)
        p20 = sorted_scores[max(0, min(n - 1, int(0.20 * n)))]
        p80 = sorted_scores[max(0, min(n - 1, int(0.80 * n)))]
        print(f"  stop_score percentiles: p20 = {p20:.3f}, p80 = {p80:.3f}  "
              f"(pin into config stop_dot_sizing.score_range)")

    # ── Salience score (geometric, linear-falloff) ───────────────────────────
    # See .claude/concepts/zoom-level-rules.md § "Salience score".
    # For each line L: sample every `sample_step_m` along its polyline; for
    # each sample, find every other line whose mode is in comparators(L)
    # whose polyline passes within `radius` of the sample; each match
    # contributes (1 − distance / radius); the sample's score is the sum.
    # competition_count(L) = mean of per-sample scores.
    print("\nComputing line salience (linear-falloff competition density)...")
    zr_cfg = _zoom_rules_cfg()
    sal_cfg = zr_cfg.get("salience") or {}
    sample_step_m = float(sal_cfg.get("sample_step_m", 1000.0))
    radius_m_by_mode = {m: float(v) for m, v in
                        (sal_cfg.get("radius_m") or {}).items()}
    comparators_raw = sal_cfg.get("comparators") or {}
    comparators_by_mode = {m: frozenset(ms)
                           for m, ms in comparators_raw.items()}

    def _comparators_for(mode: str) -> frozenset:
        return comparators_by_mode.get(mode, frozenset({mode}))

    # Per-feature f_weighted and mode mapping for downstream use.
    tg_lookup: dict = {}
    for tg_key in tg_freq.keys():
        line_key, aid, tg_id = tg_key
        sn, _ln, _bkt = line_key
        tg_lookup[(sn, aid, tg_id)] = tg_key

    f_weighted_by_oid: dict = {}
    mode_by_oid: dict = {}
    for f in features:
        p = f["properties"]
        # f_weighted was set per-variant during emission — see the
        # per-variant freq concept. Use that value rather than recomputing
        # from the group-level tg_freq.
        fw = float(p.get("f_weighted", 0.0))
        f_weighted_by_oid[p["osm_id"]] = fw
        mode_by_oid[p["osm_id"]] = p["mode"]

    # Cache polylines (flat list of (lon, lat)) by oid.
    polyline_by_oid: dict = {}
    for f in features:
        oid = f["properties"]["osm_id"]
        coords = f["geometry"]["coordinates"]
        if f["geometry"]["type"] == "MultiLineString":
            flat = [tuple(c) for seg in coords for c in seg]
        else:
            flat = [tuple(c) for c in coords]
        polyline_by_oid[oid] = flat

    # Sample each polyline every sample_step_m. Stores (lon, lat) per sample.
    samples_by_oid: dict = {}
    for oid, poly in polyline_by_oid.items():
        if len(poly) < 2:
            samples_by_oid[oid] = []
            continue
        seg_lens_km = []
        for i in range(len(poly) - 1):
            seg_lens_km.append(
                haversine_km(poly[i][0], poly[i][1],
                             poly[i + 1][0], poly[i + 1][1]))
        total_km = sum(seg_lens_km)
        if total_km <= 0:
            samples_by_oid[oid] = [poly[0]]
            continue
        step_km = sample_step_m / 1000.0
        n_samples = max(1, int(total_km / step_km))
        # Distribute n_samples evenly along the polyline (excluding the very
        # endpoints to keep samples representative of the line's "middle").
        # First sample at step_km/2, then every step_km.
        targets = [(i + 0.5) / n_samples * total_km for i in range(n_samples)]
        out = []
        cum = 0.0
        seg = 0
        for t in targets:
            while seg < len(seg_lens_km) - 1 and cum + seg_lens_km[seg] < t:
                cum += seg_lens_km[seg]
                seg += 1
            seg_len = seg_lens_km[seg] or 1e-12
            frac = max(0.0, min(1.0, (t - cum) / seg_len))
            lon = poly[seg][0] + (poly[seg + 1][0] - poly[seg][0]) * frac
            lat = poly[seg][1] + (poly[seg + 1][1] - poly[seg][1]) * frac
            out.append((lon, lat))
        samples_by_oid[oid] = out

    # Build a grid index of every sample point keyed by mode for fast
    # radius-bounded lookup. Cell size = 1000 m (cuts into degree-equivalents
    # at CH latitude).
    GRID_M = 1000.0
    # Use CH-centric latitude for cell sizing.
    lat0 = 46.8
    cos_lat0 = cos(radians(lat0))
    cell_lat_deg = GRID_M / _M_PER_DEG
    cell_lon_deg = cell_lat_deg / cos_lat0

    grid_by_mode: dict = defaultdict(lambda: defaultdict(list))
    for oid, samples in samples_by_oid.items():
        mode = mode_by_oid.get(oid, "")
        for lon, lat in samples:
            cx = int(floor(lon / cell_lon_deg))
            cy = int(floor(lat / cell_lat_deg))
            grid_by_mode[mode][(cx, cy)].append((oid, lon, lat))

    competition_count_by_oid: dict = {}
    for oid, samples in samples_by_oid.items():
        if not samples:
            competition_count_by_oid[oid] = 0.0
            continue
        my_mode = mode_by_oid.get(oid, "")
        my_comparators = _comparators_for(my_mode)
        radius_m = radius_m_by_mode.get(my_mode, 5000.0)
        cells_radius = int(ceil(radius_m / GRID_M))
        radius_m_sq = radius_m * radius_m
        per_sample_scores: list = []
        for lon, lat in samples:
            cx = int(floor(lon / cell_lon_deg))
            cy = int(floor(lat / cell_lat_deg))
            nearest_by_other: dict = {}
            for comp_mode in my_comparators:
                g = grid_by_mode.get(comp_mode)
                if not g:
                    continue
                for dx in range(-cells_radius, cells_radius + 1):
                    for dy in range(-cells_radius, cells_radius + 1):
                        for (other_oid, olon, olat) in g.get((cx + dx, cy + dy), ()):
                            if other_oid == oid:
                                continue
                            mdx = (olon - lon) * cos_lat0 * _M_PER_DEG
                            mdy = (olat - lat) * _M_PER_DEG
                            d_sq = mdx * mdx + mdy * mdy
                            if d_sq > radius_m_sq:
                                continue
                            prev = nearest_by_other.get(other_oid)
                            if prev is None or d_sq < prev:
                                nearest_by_other[other_oid] = d_sq
            score = 0.0
            for d_sq in nearest_by_other.values():
                d = sqrt(d_sq)
                score += 1.0 - d / radius_m
            per_sample_scores.append(score)
        competition_count_by_oid[oid] = (
            sum(per_sample_scores) / len(per_sample_scores)
            if per_sample_scores else 0.0
        )

    # Per-mode normalisation: lowest competition → salience = 1.0; highest
    # → 0.0; intermediate linear.
    salience_by_oid: dict = {}
    by_mode_for_sal: dict = defaultdict(list)
    for oid, cc in competition_count_by_oid.items():
        by_mode_for_sal[mode_by_oid.get(oid, "")].append(oid)
    for mode, oids in by_mode_for_sal.items():
        ccs = [competition_count_by_oid.get(o, 0.0) for o in oids]
        c_min, c_max = min(ccs), max(ccs)
        span = c_max - c_min
        for o in oids:
            if span <= 0:
                salience_by_oid[o] = 1.0
            else:
                cc = competition_count_by_oid.get(o, 0.0)
                salience_by_oid[o] = 1.0 - (cc - c_min) / span

    for f in features:
        p = f["properties"]
        oid = p["osm_id"]
        p["salience"] = round(float(salience_by_oid.get(oid, 0.0)), 4)
        p["competition_count"] = round(
            float(competition_count_by_oid.get(oid, 0.0)), 4)

    # ── Per-mode line rules → candidate min_zoom ────────────────────────────
    # See concept § "Per-mode rules". Each rule at level N adds any line
    # matching the condition at that level; lines take the smallest such N.
    print("Applying per-mode line rules...")
    intercity_prefixes = tuple(
        str(p).upper()
        for p in (zr_cfg.get("intercity_route_prefixes") or ["IC", "ICE", "EC"])
    )

    def _is_intercity_train(ref: str, mode: str) -> bool:
        if mode != "train":
            return False
        r = (ref or "").strip().upper()
        return any(r.startswith(p) for p in intercity_prefixes)

    # Salience top-sets per (mode, pct), precomputed once. Used by the
    # per-mode rules below.
    def _salience_ranked(mode: str) -> list:
        oids = list(by_mode_for_sal.get(mode, []))
        oids.sort(key=lambda o: (
            -salience_by_oid.get(o, 0.0),
            -f_weighted_by_oid.get(o, 0.0),
            o,
        ))
        return oids
    _train_top50 = set(_salience_ranked("train")[
        :max(1, int(round(len(by_mode_for_sal.get("train", [])) * 0.50)))])
    _rb_top30 = set(_salience_ranked("regional_bus")[
        :max(1, int(round(len(by_mode_for_sal.get("regional_bus", [])) * 0.30)))])
    _rb_top50 = set(_salience_ranked("regional_bus")[
        :max(1, int(round(len(by_mode_for_sal.get("regional_bus", [])) * 0.50)))])

    # Per-feature spread (km) — geodesic distance between the two stops
    # farthest apart on the line. Also per-feature longest-gap (km) — the
    # geodesic distance between the two stops that are CONSECUTIVE IN THE
    # STOP SEQUENCE and furthest apart. Used by the ferry rule: a lake line's
    # reach is described by its longest water hop between piers, not its
    # end-to-end spread.
    spread_by_oid: dict = {}
    longest_gap_by_oid: dict = {}
    line_km_by_oid: dict = {}
    for f in features:
        oid = f["properties"]["osm_id"]
        line_km_by_oid[oid] = float(f["properties"].get("line_km") or 0.0)
        entry = line_stops_out.get(oid, {})
        stops = entry.get("stops", []) if isinstance(entry, dict) else entry
        if len(stops) < 2:
            spread_by_oid[oid] = 0.0
            longest_gap_by_oid[oid] = 0.0
            continue
        # Brute force O(n^2) for spread — fine for typical n ≤ 100 stops per
        # line.
        max_d = 0.0
        for i in range(len(stops)):
            for j in range(i + 1, len(stops)):
                d = haversine_km(stops[i][0], stops[i][1],
                                 stops[j][0], stops[j][1])
                if d > max_d:
                    max_d = d
        spread_by_oid[oid] = max_d
        # Longest gap between two stops adjacent in the stop sequence.
        max_gap = 0.0
        for i in range(len(stops) - 1):
            g = haversine_km(stops[i][0], stops[i][1],
                             stops[i + 1][0], stops[i + 1][1])
            if g > max_gap:
                max_gap = g
        longest_gap_by_oid[oid] = max_gap

    # Per-mode line-rule evaluator. Returns (min_zoom, rule_label) per oid.
    # Levels evaluated bottom-up (lowest first); first matching level wins.
    UNREACHABLE_Z = 13  # Lines that match no rule fall here (effectively hidden).

    def _candidate_min_zoom_train(oid: str, p: dict) -> tuple:
        ref = p.get("ref", "")
        if _is_intercity_train(ref, "train"):
            return 4, "intercity"
        if line_km_by_oid.get(oid, 0.0) >= 30.0 and oid in _train_top50:
            return 5, "length>=30km AND salience top50%"
        return 6, "all remaining"

    def _candidate_min_zoom_metro(oid: str, p: dict) -> tuple:
        if spread_by_oid.get(oid, 0.0) >= 20.0:
            return 8, "spread>=20km"
        return 9, "all remaining"

    def _candidate_min_zoom_ferry(oid: str, p: dict) -> tuple:
        g = longest_gap_by_oid.get(oid, 0.0)
        if g >= 20.0: return 6, "longest_gap>=20km"
        if g >= 10.0: return 7, "longest_gap>=10km"
        if g >=  5.0: return 8, "longest_gap>=5km"
        return 9, "all remaining"

    def _candidate_min_zoom_mountain(oid: str, p: dict) -> tuple:
        L = line_km_by_oid.get(oid, 0.0)
        if L >= 15.0:  return  6, "length>=15km"
        if L >=  8.0:  return  7, "length>=8km"
        if L >=  5.0:  return  8, "length>=5km"
        if L >=  2.0:  return  9, "length>=2km"
        if L >=  0.5:  return 10, "length>=0.5km"
        return 11, "all remaining"

    def _candidate_min_zoom_regional_bus(oid: str, p: dict) -> tuple:
        s = spread_by_oid.get(oid, 0.0)
        if s >= 25.0 and oid in _rb_top30:
            return 7, "spread>=25km AND salience top30%"
        if s >= 15.0 and oid in _rb_top50:
            return 8, "spread>=15km AND salience top50%"
        if s >= 5.0:
            return 9, "spread>=5km"
        return 10, "all remaining"

    def _candidate_min_zoom_tram(oid: str, p: dict) -> tuple:
        if spread_by_oid.get(oid, 0.0) >= 8.0:
            return 9, "spread>=8km"
        return 10, "all remaining"

    def _candidate_min_zoom_bus(oid: str, p: dict) -> tuple:
        if spread_by_oid.get(oid, 0.0) >= 5.0:
            return 10, "spread>=5km"
        return 11, "all remaining"

    RULE_BY_MODE = {
        "train":        _candidate_min_zoom_train,
        "metro":        _candidate_min_zoom_metro,
        "ferry":        _candidate_min_zoom_ferry,
        "mountain":     _candidate_min_zoom_mountain,
        "regional_bus": _candidate_min_zoom_regional_bus,
        "tram":         _candidate_min_zoom_tram,
        "bus":          _candidate_min_zoom_bus,
    }

    candidate_mz_by_oid: dict = {}
    rule_label_by_oid: dict = {}
    for f in features:
        p = f["properties"]
        oid = p["osm_id"]
        mode = p["mode"]
        fn = RULE_BY_MODE.get(mode)
        if fn is None:
            candidate_mz_by_oid[oid] = UNREACHABLE_Z
            rule_label_by_oid[oid] = "no rule for mode"
            continue
        mz, label = fn(oid, p)
        candidate_mz_by_oid[oid] = mz
        rule_label_by_oid[oid] = label

    # ── Line graph + MBST + stop-weighted-average connectivity ──────────────
    # See concept § "Line graph and base set" and § "Connectivity".
    print("Building line graph (super-UIC clustering + MBST)...")
    lg_cfg = zr_cfg.get("line_graph") or {}
    cluster_m = float(lg_cfg.get("cluster_threshold_m", 250.0))

    # First-seen UIC → coords.
    uic_coords: dict = {}
    for entry in line_stops_out.values():
        for stop in entry.get("stops", []):
            if len(stop) >= 3 and stop[2]:
                uic = stop[2].split(":")[0]
                uic_coords.setdefault(uic, (float(stop[0]), float(stop[1])))

    super_of_uic = _cluster_uics(uic_coords, cluster_m)

    # Lines per super-cluster (deduped).
    lines_at_super: dict = defaultdict(set)
    for oid, entry in line_stops_out.items():
        for stop in entry.get("stops", []):
            if len(stop) >= 3 and stop[2]:
                uic = stop[2].split(":")[0]
                super_id = super_of_uic.get(uic)
                if super_id is not None:
                    lines_at_super[super_id].add(oid)

    # Per-line station count (length of the per-feature stop list).
    station_count: dict = {oid: len(entry.get("stops", []))
                           for oid, entry in line_stops_out.items()}

    line_oids: list = [f["properties"]["osm_id"] for f in features]
    oid_set: set = set(line_oids)

    # Travel duration (minutes) — edge weight basis.
    duration_by_oid: dict = {}
    for f in features:
        p = f["properties"]
        oid = p["osm_id"]
        km = float(p.get("line_km") or 0.0)
        sp = float(p.get("speed_kmh") or 0.0)
        duration_by_oid[oid] = (km / sp * 60.0) if sp > 0 else 1e9

    # Raw line graph: each pair of lines sharing a super-cluster is an edge.
    # Used both for (a) finding the intercity base's connected component and
    # (b) running the per-line shortest-path-to-base search below.
    # Adjacency is deduped — many lines share many super-clusters, but we
    # only need one edge between any pair in the graph.
    line_graph_adj: dict = defaultdict(set)
    for super_id, oids in lines_at_super.items():
        oids_here = [o for o in oids if o in oid_set]
        n = len(oids_here)
        if n < 2:
            continue
        for i in range(n):
            u = oids_here[i]
            for j in range(i + 1, n):
                v = oids_here[j]
                line_graph_adj[u].add(v)
                line_graph_adj[v].add(u)

    # Base set: the LARGEST connected component of intercity train lines in
    # the RAW line graph. (Computing CCs over MBST edges, as we used to,
    # silently fragmented the IC backbone because MBST routed IC↔IC via
    # cheaper non-IC connectors and the direct IC↔IC edges were never added.)
    feature_by_oid = {f["properties"]["osm_id"]: f for f in features}
    intercity_oids = [
        o for o in line_oids
        if _is_intercity_train(
            feature_by_oid[o]["properties"].get("ref", ""),
            mode_by_oid.get(o, ""))
    ]
    intercity_set: set = set(intercity_oids)

    visited_ic: set = set()
    components: list = []
    for o in intercity_oids:
        if o in visited_ic:
            continue
        comp: list = []
        stack = [o]
        visited_ic.add(o)
        while stack:
            u = stack.pop()
            comp.append(u)
            for v in line_graph_adj.get(u, ()):
                if v in intercity_set and v not in visited_ic:
                    visited_ic.add(v)
                    stack.append(v)
        components.append(comp)
    components.sort(key=len, reverse=True)
    base_oids: set = set(components[0]) if components else set()

    # ── Per-level cluster gating ─────────────────────────────────────────────
    # Sole purpose: avoid floating clusters at each zoom level. Algorithm:
    #
    #   For Z = 4, 5, 6, …, UNREACHABLE_Z:
    #     1. eligible_at_Z = {oid already assigned final ≤ Z}
    #                       ∪ {oid not yet assigned whose candidate ≤ Z}
    #     2. Connected components of the line graph induced on eligible_at_Z.
    #     3. main = the CC containing the IC base.
    #     4. Newly arrived in main (not yet assigned) → assign Z.
    #     5. For each other CC ("isolated cluster"):
    #        a. Find the shortest bridge (least non-eligible intermediates)
    #           from the cluster to main in the full line graph.
    #        b. weighted_avg = stop-weighted average of (cluster + bridge)
    #           candidates.
    #        c. If avg ≤ Z: accept — pull every bridge line and every cluster
    #           line down to Z. Bridge lines become eligible immediately so
    #           later clusters at the same Z benefit.
    #        d. Else: defer — leave unassigned, retry at Z+1.
    #   After all levels: any still-unassigned line is truly disconnected from
    #   base in the line graph; assign its candidate as fallback.
    import heapq
    final_mz_by_oid: dict = {}
    for oid in base_oids:
        final_mz_by_oid[oid] = candidate_mz_by_oid.get(oid, UNREACHABLE_Z)

    def _shortest_bridge(cluster_set, main_set, eligible_set):
        """Multi-source Dijkstra from cluster outward. Edge cost u→v is 0 if v
        is in eligible_set (or in main_set), else 1. Returns the list of
        non-eligible intermediate lines on the cheapest path from any cluster
        node to any main node, or None if no path exists in the line graph.
        """
        INF = 10**9
        dist: dict = {c: 0 for c in cluster_set}
        parent: dict = {}
        heap: list = [(0, c) for c in cluster_set]
        target = None
        while heap:
            d, u = heapq.heappop(heap)
            if d > dist.get(u, INF):
                continue
            if u in main_set:
                target = u
                break
            for v in line_graph_adj.get(u, ()):
                step = 0 if (v in eligible_set or v in main_set) else 1
                v_d = d + step
                if v_d < dist.get(v, INF):
                    dist[v] = v_d
                    parent[v] = u
                    heapq.heappush(heap, (v_d, v))
        if target is None:
            return None
        # Reconstruct path target → … → cluster_anchor
        path: list = [target]
        cur = target
        while cur in parent:
            cur = parent[cur]
            path.append(cur)
        path.reverse()
        return [n for n in path
                if n not in cluster_set and n not in main_set
                and n not in eligible_set]

    # Iterate zoom levels from base (z4) up to UNREACHABLE_Z. At each Z, the
    # "main" component may absorb new clusters either via natural eligibility
    # or via pull-down bridges. The loop is bounded by UNREACHABLE_Z so any
    # cluster that survives all levels is logged as truly isolated.
    for Z in range(4, UNREACHABLE_Z + 1):
        eligible_oids: set = set()
        for oid in line_oids:
            if oid in final_mz_by_oid:
                if final_mz_by_oid[oid] <= Z:
                    eligible_oids.add(oid)
            else:
                if candidate_mz_by_oid.get(oid, UNREACHABLE_Z) <= Z:
                    eligible_oids.add(oid)

        # CCs in line_graph_adj restricted to eligible_oids.
        visited_cc: set = set()
        comps: list = []
        for o in eligible_oids:
            if o in visited_cc:
                continue
            comp: set = set()
            stack: list = [o]
            visited_cc.add(o)
            while stack:
                u = stack.pop()
                comp.add(u)
                for v in line_graph_adj.get(u, ()):
                    if v in eligible_oids and v not in visited_cc:
                        visited_cc.add(v)
                        stack.append(v)
            comps.append(comp)

        main_set: set = set()
        for c in comps:
            if base_oids & c:
                main_set = c
                break
        if not main_set:
            continue

        # New arrivals in main: assign Z.
        for line in main_set:
            if line not in final_mz_by_oid:
                final_mz_by_oid[line] = Z

        # Process each isolated cluster.
        for cluster in comps:
            if cluster is main_set:
                continue
            # Skip clusters whose lines already all got assigned (e.g. as part
            # of a previously-pulled bridge at this Z).
            if all(l in final_mz_by_oid for l in cluster):
                continue
            bridge = _shortest_bridge(cluster, main_set, eligible_oids)
            if bridge is None:
                continue
            nodes_for_avg = list(cluster) + bridge
            total_stops = sum(station_count.get(n, 1) for n in nodes_for_avg)
            if total_stops == 0:
                continue
            numer = sum(station_count.get(n, 1)
                        * candidate_mz_by_oid.get(n, UNREACHABLE_Z)
                        for n in nodes_for_avg)
            avg = numer / total_stops
            if avg > Z:
                # Defer: cluster sits this level out, re-evaluate at Z+1.
                continue
            # Accept: pull bridge + cluster down to Z.
            for line in bridge:
                if line not in final_mz_by_oid:
                    final_mz_by_oid[line] = Z
                eligible_oids.add(line)
                main_set.add(line)
            for line in cluster:
                if line not in final_mz_by_oid:
                    final_mz_by_oid[line] = Z
                main_set.add(line)

    # Truly disconnected (no path to base in the line graph at any Z) → use
    # candidate as the visibility level. Mark as isolated for diagnostics.
    isolated_oids: set = set()
    for oid in line_oids:
        if oid not in final_mz_by_oid:
            final_mz_by_oid[oid] = candidate_mz_by_oid.get(oid, UNREACHABLE_Z)
            isolated_oids.add(oid)

    # Apply to features.
    for f in features:
        p = f["properties"]
        oid = p["osm_id"]
        mz = int(final_mz_by_oid.get(oid, UNREACHABLE_Z))
        p["min_zoom"] = mz
        p["candidate_min_zoom"] = int(candidate_mz_by_oid.get(oid, UNREACHABLE_Z))
        p["rule_label"] = rule_label_by_oid.get(oid, "")
        f["tippecanoe"] = {"minzoom": mz}

    n_iso = len(isolated_oids)
    n_iso_mountain = sum(1 for o in isolated_oids
                         if mode_by_oid.get(o) == "mountain")
    n_iso_other = n_iso - n_iso_mountain
    n_line_edges = sum(len(v) for v in line_graph_adj.values()) // 2
    print(f"  Line graph: {len(super_of_uic):,} UICs → "
          f"{len(set(super_of_uic.values())):,} super-clusters, "
          f"{n_line_edges:,} line-graph edges")
    base_str = (f"{len(base_oids)} intercity train(s), largest of "
                f"{len(components)} intercity component(s) in line graph"
                if base_oids else "EMPTY (no intercity train lines)")
    print(f"  Base set: {base_str}")
    n_promoted = sum(1 for oid, mz in final_mz_by_oid.items()
                     if oid not in base_oids and oid not in isolated_oids
                     and mz < candidate_mz_by_oid.get(oid, UNREACHABLE_Z))
    print(f"  Connectivity-promoted: {n_promoted:,}  "
          f"Isolated: {n_iso} ({n_iso_mountain} mountain, {n_iso_other} other)")
    if features:
        mzs = [int(final_mz_by_oid.get(f["properties"]["osm_id"], UNREACHABLE_Z))
               for f in features]
        cmzs = [int(candidate_mz_by_oid.get(f["properties"]["osm_id"], UNREACHABLE_Z))
                for f in features]
        print(f"  candidate min_zoom: range {min(cmzs)}–{max(cmzs)}, "
              f"mean {sum(cmzs)/len(cmzs):.2f}")
        print(f"  final min_zoom:     range {min(mzs)}–{max(mzs)}, "
              f"mean {sum(mzs)/len(mzs):.2f}")

    # ── Write outputs ────────────────────────────────────────────────────────
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    OUT_STOPS.write_text(json.dumps(line_stops_out))
    print(f"\n  {len(features):,} features → {OUT}")
    print(f"  {sum(len(v['stops']) for v in line_stops_out.values()):,} stops "
          f"across {len(line_stops_out):,} features → {OUT_STOPS}")

    # gtfs_unmatched: trip groups with no emitted feature (after grouping).
    # Drawable tg_keys come straight from drawable_groups — that dict has
    # already passed the freq-score / mountain / CC exemptions used during
    # emission, so the difference against matched_tg_keys is exactly the set
    # of trip groups that we thought we should draw but pfaedle never shaped
    # (or whose polylines collapsed to < 2 coords).
    unmatched_tg = set(drawable_groups.keys()) - matched_tg_keys
    unmatched_out = []
    for tg_key in sorted(unmatched_tg,
                         key=lambda k: (k[0][2], k[0][0], k[0][1], k[1], k[2])):
        line_key, aid, tg_id = tg_key
        short_name, long_name, bucket = line_key
        freq = tg_freq.get(tg_key, {"f_core": 0.0, "f_eve": 0.0, "f_we": 0.0})
        mode_approx = _BUCKET_MODE_APPROX.get(bucket, "regional_bus")
        fs = compute_freq_score(freq, mode_approx)
        unmatched_out.append({
            "short_name":    short_name,
            "long_name":     long_name,
            "bucket":        bucket,
            "agency_id":     aid,
            "trip_group_id": tg_id,
            "f_weighted":    round(weighted_freq(freq), 3),
            "freq_score":    round(fs, 4),
        })
    OUT_GTFS_UNMATCHED.write_text(json.dumps(unmatched_out, ensure_ascii=False))
    print(f"  GTFS unmatched: {len(unmatched_out)} trip groups with service but no feature → {OUT_GTFS_UNMATCHED}")

    OUT_TRIP_GROUPS.write_text(json.dumps(trip_groups_diag, ensure_ascii=False))
    print(f"  Trip groups:   {len(trip_groups_diag)} groups → {OUT_TRIP_GROUPS}")

    OUT_PFAEDLE_UNROUTED.write_text(json.dumps(pfaedle_unrouted, ensure_ascii=False))
    print(f"  Pfaedle unrouted: {len(pfaedle_unrouted)} trips → {OUT_PFAEDLE_UNROUTED}")

    # ── Comprehensive grouping diagnostic ──────────────────────────────────
    # One entry per (line_key, agency_id, trip_group_id) including groups that
    # never reached emission (low_frequency). One sub-entry per merged-stop
    # variant including those dropped by the rare-variant filter. Read this
    # file directly instead of re-running stream_stop_times to debug missing
    # or unexpected lines.
    diag_out = []
    _, worst_freq_map_diag = _frequencies()
    for tg_key, var_outcomes in diag_filter.items():
        line_key, aid, tg_id = tg_key
        short_name, long_name, bucket = line_key
        original_vmap = diag_original.get(tg_key, {})
        raw_freq = dict(tg_freq.get(tg_key, {"f_core": 0.0, "f_eve": 0.0, "f_we": 0.0}))
        f_weighted = weighted_freq(raw_freq)
        mode_approx = _BUCKET_MODE_APPROX.get(bucket, "regional_bus")
        fscore = compute_freq_score(raw_freq, mode_approx)
        worst_f_diag = worst_freq_map_diag.get(mode_approx, 0.0)

        mountain_origin = tg_mountain_origin.get(tg_key)
        drawable = (line_key, aid, tg_id) in drawable_groups
        if drawable:
            group_reason = None
        elif tg_key in rare_group_dropped:
            group_reason = "rare_group_dropped"
        elif tg_key in tg_keys_all_short_active:
            group_reason = "short_active_period"
        elif _freq_gate_exempt(bucket, mountain_origin) or (
            short_name == "CC" and bucket == "train"
        ):
            # These should have been drawable; only here if neither emitted
            # nor low-freq. Real shouldn't-happen branch — record it.
            group_reason = "unknown_skipped"
        elif f_weighted <= worst_f_diag:
            group_reason = "low_frequency"
        else:
            group_reason = "unknown_skipped"

        group_trip_total = sum(len(t) for t in original_vmap.values())
        group_weighted_total = tg_total_weight.get(tg_key, 0)
        sg_id = supergroup_id_by_tg.get(tg_key)
        sg_weighted_total = supergroup_total_weight.get(sg_id, 0)
        sg_share = (group_weighted_total / sg_weighted_total) if sg_weighted_total else 0.0
        sg_threshold = rare_group_threshold_by_sg.get(sg_id)
        variant_weighted_total_for_group = sum(variant_counts[tg_key].values())

        variants_out = []
        for var_key, (filt_outcome, threshold_pct) in var_outcomes.items():
            merged_set, direction_key = var_key
            ms_trips = original_vmap.get(var_key, [])
            stations: list = []
            first_terminus = "?"
            last_terminus = "?"
            if ms_trips:
                any_stops = _trip_stops_export.get(ms_trips[0], [])
                for sid in any_stops:
                    uic = sid.split(":")[0]
                    name = (stop_meta.get(sid,
                              stop_meta.get(uic, ("?", "")))[0] or "?")
                    stations.append({"stop_id": sid, "name": name})
                if stations:
                    first_terminus = stations[0]["name"]
                    last_terminus = stations[-1]["name"]

            em = diag_emission.get((tg_key, var_key), {})
            kept_by_filter = (filt_outcome == "kept")
            # Variant-level outcomes ("short_active_period", "rare_variant")
            # surface directly. Otherwise: if the whole group is gone,
            # propagate the group reason; if the variant survived but never
            # emitted, take the emission reason.
            if filt_outcome in ("short_active_period", "rare_variant"):
                v_reason = filt_outcome
            elif not drawable:
                v_reason = group_reason
            else:
                v_reason = em.get("exclusion_reason")

            share = (len(ms_trips) / group_trip_total) if group_trip_total else 0.0
            ms_weight = variant_counts[tg_key].get(var_key, 0)
            weighted_share = (ms_weight / variant_weighted_total_for_group
                              if variant_weighted_total_for_group else 0.0)
            v_active_days = variant_active_days.get(
                (line_key, aid, tg_id, merged_set, direction_key), 0)

            v_seasonal = var_freq_seasonal.get((tg_key, var_key)) or {}
            v_raw = v_seasonal.get("annual") or _ZERO_FREQ
            v_entry = {
                "direction_key": f"{direction_key[0]}-{direction_key[1]}",
                "trip_count": len(ms_trips),
                "trip_share_pct": round(share * 100, 1),
                "weighted_trip_count": ms_weight,
                "variant_share_of_group": round(weighted_share, 4),
                "active_days": v_active_days,
                "first_terminus": first_terminus,
                "last_terminus": last_terminus,
                "kept_by_variant_filter": kept_by_filter,
                "rare_variant_threshold_pct": threshold_pct,
                "rare_variant_window_passed":
                    rare_variant_window_passed.get((tg_key, var_key)),
                "regional_bus_rescued": var_key in regional_bus_rescued.get(tg_key, ()),
                "raw_freq": dict(v_raw),
                "f_weighted": round(weighted_freq(v_raw), 3),
                "exclusion_reason": v_reason,
                "feature_emitted": em.get("feature_emitted", False),
                "stations": stations,
            }
            if em.get("feature_emitted"):
                v_entry["feature_id"] = em.get("feature_id", "")
                v_entry["shape_id"] = em.get("shape_id", "")
                v_entry["n_coords"] = em.get("n_coords", 0)
                v_entry["line_km"] = em.get("line_km", 0.0)
                v_entry["rep_trip_id"] = em.get("rep_trip_id", "")
                v_entry["geometry_source"] = em.get("geometry_source", "pfaedle")
            elif em:
                # Reached emission but didn't produce a feature.
                v_entry["shape_id"] = em.get("shape_id", "")
                v_entry["n_coords"] = em.get("n_coords", 0)
                v_entry["line_km"] = em.get("line_km", 0.0)
                v_entry["rep_trip_id"] = em.get("rep_trip_id", "")
            variants_out.append(v_entry)

        threshold_field = (None if _gate_exempt(bucket, mountain_origin)
                           else min_active_days_for(bucket))
        diag_out.append({
            "ref": short_name,
            "long_name": long_name,
            "bucket": bucket,
            "route_type": tg_route_type.get(tg_key, ""),
            "mountain_origin": mountain_origin,
            "agency_id": aid,
            "agency_name": agency_names.get(aid, ""),
            "trip_group_id": tg_id,
            "total_trip_count": group_trip_total,
            "weighted_trip_count": group_weighted_total,
            "supergroup_id": sg_id,
            "supergroup_weighted_trip_count": sg_weighted_total,
            "group_share_of_supergroup": round(sg_share, 4),
            "rare_group_share_threshold": sg_threshold,
            "raw_freq": raw_freq,
            "f_weighted": round(f_weighted, 3),
            "freq_score": round(fscore, 4),
            "min_active_days_threshold": threshold_field,
            "drawable": drawable,
            "group_exclusion_reason": group_reason,
            "freq_gate_window_passed": freq_gate_window_passed.get(tg_key),
            "variants": variants_out,
        })

    diag_out.sort(key=lambda e: (e["bucket"], e["ref"], e["agency_id"],
                                  e["trip_group_id"]))
    OUT_GROUPS_FULL = ROOT / "data" / "transit" / "gtfs_groups_full.json"
    OUT_GROUPS_FULL.write_text(json.dumps(diag_out, ensure_ascii=False))
    drawable_count = sum(1 for e in diag_out if e["drawable"])
    emitted_count = sum(1 for e in diag_out for v in e["variants"]
                        if v.get("feature_emitted"))
    print(f"  Full groups:   {len(diag_out)} entries "
          f"({drawable_count} drawable, {emitted_count} variants emitted) → {OUT_GROUPS_FULL}")

    # Summary
    mode_counts: dict = defaultdict(int)
    for f in features:
        mode_counts[f["properties"]["mode"]] += 1
    print("\nBy mode:")
    for m, c in sorted(mode_counts.items(), key=lambda x: -x[1]):
        print(f"  {m:<20} {c:>5}")

    scores = [f["properties"]["freq_score"] for f in features
              if f["properties"].get("freq_score") is not None]
    if scores:
        buckets = [0] * 10
        for s in scores:
            buckets[min(9, int(s * 10))] += 1
        print("\nFrequency score distribution:")
        for i, c in enumerate(buckets):
            bar = "█" * (c * 40 // max(buckets, default=1))
            print(f"  {i/10:.1f}–{(i+1)/10:.1f}  {bar} {c}")


if __name__ == "__main__":
    main()
