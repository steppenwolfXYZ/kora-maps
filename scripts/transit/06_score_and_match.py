#!/usr/bin/env python3
"""
Build the final transit GeoJSON from a pfaedle-routed GTFS feed.

Pipeline:
  1. Load the filtered + pfaedle-routed GTFS feed at data/gtfs_routed/.
  2. Stream stop_times with trip-grouping (gtfs-line-grouping concept) →
     `_trip_group_export` (trip_id → (line_key, tg_id, agency_id)),
     `_trip_stops_export` (trip_id → [stop_id, …]), and
     `_trip_merged_export` (trip_id → frozenset(merged_stop_id)).
  3. Score per-line frequency & speed (GTFS-side, unchanged from before).
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
from math import radians, cos, sin, sqrt, atan2
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

# ── Frequency sample dates ──────────────────────────────────────────────────
# Loaded from config.yaml (populated by scripts/transit/generate_sample_dates.py).
# Lazy because import-time loading would break diagnostic scripts that import
# this module without having config populated yet.

_SAMPLE_DATES_CACHE: dict = {}


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


CORE_START    = 7 * 3600
CORE_END      = 18 * 3600
EVENING_START = 18 * 3600
EVENING_END   = 22 * 3600
WEEKEND_START = 7 * 3600
WEEKEND_END   = 20 * 3600

CORE_MINUTES    = (CORE_END - CORE_START) / 60        # 660 min
EVENING_MINUTES = (EVENING_END - EVENING_START) / 60  # 240 min
WEEKEND_MINUTES = (WEEKEND_END - WEEKEND_START) / 60  # 780 min

# Off-peak malus factors (multiplicative).
MALUS_LOW = 0.10
MALUS_NO  = 0.20

# Minimum freq_score required to draw a line (all modes; mountain exempt).
MIN_FREQ_SCORE = 0.075

# Low-service evening/weekend headway thresholds per mode (minutes).
LOW_EVE_HEADWAY = {
    "train": 60, "tram": 20, "metro": 15,
    "bus": 20, "regional_bus": 60, "ferry": 90, "mountain": 120,
}
LOW_WE_HEADWAY = {
    "train": 60, "tram": 30, "metro": 20,
    "bus": 30, "regional_bus": 90, "ferry": 120, "mountain": 120,
}

# Headways per mode (minutes) — loaded from config.yaml at first call. The
# core_score curve is linear in actual headway between best_headway (score=1.0)
# and worst_headway (score=0.0). See .claude/concepts/bucket-worst-headway.md.

_HEADWAY_CACHE: dict = {}


def _headways() -> tuple:
    """Return (best_headway_dict, worst_headway_dict). Loaded lazily; both
    tables must cover the same bucket set or the pipeline aborts."""
    if _HEADWAY_CACHE:
        return _HEADWAY_CACHE["best"], _HEADWAY_CACHE["worst"]
    cfg = yaml.safe_load(CFG_PATH.read_text())
    hw = cfg.get("headway") or {}
    best = hw.get("best_headway") or {}
    worst = hw.get("worst_headway") or {}
    if not best or not worst:
        sys.exit(
            "config.yaml is missing headway.best_headway / headway.worst_headway."
        )
    missing_worst = set(best) - set(worst)
    missing_best  = set(worst) - set(best)
    if missing_worst or missing_best:
        sys.exit(
            "config.yaml headway tables are inconsistent: "
            f"buckets missing worst_headway={sorted(missing_worst)}, "
            f"buckets missing best_headway={sorted(missing_best)}."
        )
    _HEADWAY_CACHE["best"]  = {k: float(v) for k, v in best.items()}
    _HEADWAY_CACHE["worst"] = {k: float(v) for k, v in worst.items()}
    return _HEADWAY_CACHE["best"], _HEADWAY_CACHE["worst"]

# Bus mode classification — see .claude/concepts/bus-mode-classification.md.
# Agencies that follow the "1-digit ref = city, 2-digit ref = regional"
# numbering convention. 1-digit refs on these operators are still city buses.
TWO_DIGIT_REGIONAL_AGENCIES = {
    "000146",  # STI Bus AG
    "000605",  # STI Berg
    "000859",  # STI-gwb
    "000766",  # BuS/cb (Bus und Service AG, Chur)
    "000236",  # BCD (Chur-Dreibündenstein)
    "000801",  # PAG (PostAuto AG)
    "007088",  # THP (Trägerverein Historische Postautolinie)
}
# 000765 (PAG/BCS, PostAuto AG Bus Commune Sion) is deliberately excluded:
# PostAuto-operated city service whose 2-digit refs are city lines.

# transN city carve-out: agencies whose 3-digit refs starting with 1 or 3 are
# city lines, overriding the default n>=3 → regional rule. transN numbers its
# urban networks in the 100s (Neuchâtel) and 300s (La Chaux-de-Fonds / Le Locle).
TRANSN_CITY_AGENCIES = {
    "000153",  # TRN-tn (Neuchâtel)
    "000792",  # TRN/tc (La Chaux-de-Fonds + Le Locle)
}

# Length fallback for pure-letter refs (km).
LETTER_REF_REGIONAL_MIN_LENGTH = 10.0


# ── Config loading ───────────────────────────────────────────────────────────

def load_cfg() -> dict:
    return yaml.safe_load(CFG_PATH.read_text())


# ── Mode classification (GTFS-side) ──────────────────────────────────────────

def gtfs_type_to_bucket(route_type: str) -> str:
    t = route_type.strip()
    if t == "0":  return "tram"
    if t == "1":  return "metro"
    if t == "2":  return "train"
    if t == "3":  return "bus"
    if t == "4":  return "ferry"
    if t == "5":  return "mountain"
    if t == "6":  return "mountain"
    if t == "7":  return "mountain"
    if t == "11": return "bus"    # trolleybus → bus bucket
    return "bus"


def gtfs_to_mode(bucket: str, agency_id: str,
                 short_name: str = "",
                 length_km: Optional[float] = None) -> str:
    """Map a GTFS bucket + agency to one of the rendering modes.

    - bucket == "bus" → bus / regional_bus by ref digit count, agency,
      and a length fallback for pure-letter refs.
      See .claude/concepts/bus-mode-classification.md for the full rule.
    - Other buckets pass through. Mountain classification comes solely from
      GTFS route_type (5/6/7) at the bucket layer.
    """
    if bucket == "bus":
        ref = short_name.strip()
        if ref.upper() == "EV":
            return "regional_bus"
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


def freq_to_width_base(freq_score, mode) -> float:
    if mode == "mountain":  return 0.75
    if freq_score is None:  return 1.1
    return round(1.1 + freq_score * 1.5, 1)


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
            meta[sid] = (row.get("stop_name", ""), row.get("parent_station", ""))
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

_BUCKET_MODE_APPROX = {
    "train": "train", "tram": "tram", "metro": "metro",
    "ferry": "ferry", "bus": "regional_bus", "regional_bus": "regional_bus",
}


def stream_stop_times(trips, stop_coords, svc_dates, trip_frequencies, stop_meta):
    """One streaming pass → raw trip counts + speed per line, plus trip-group
    partitioning. Populates module-level exports `_trip_group_export`,
    `_trip_stops_export`, and `_trip_merged_export`.
    """
    global _trip_group_export, _trip_stops_export, _trip_merged_export, _trip_weight_export

    stop_merge: dict = {}
    for sid, (_name, parent) in stop_meta.items():
        stop_merge[sid] = parent if parent else sid.split(":")[0]

    wd_set, we_set, n_wd_samples, n_we_samples = _sample_dates()
    print(f"  Sample dates: {n_wd_samples} weekday + {n_we_samples} weekend")
    print("  Streaming stop_times.txt ...")

    line_freq: dict = defaultdict(lambda: {"core_wd": 0, "eve_wd": 0, "we": 0})
    line_canonical: dict = {}

    # Per-trip buffer for the post-stream grouping phase.
    # trip_id → (line_key, agency_id, weight, raw_variant_frozenset,
    #            merged_stop_frozenset, sequence_list)
    trip_buf: dict = {}

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

        # Count of sample dates this trip is active on — used as the multiplier
        # so a construction line active on 1/26 weekday samples contributes 1/26
        # of its raw count after normalisation.
        wd_hits = sum(1 for d in wd_set if d in active_dates)
        we_hits = sum(1 for d in we_set if d in active_dates)

        freq_entries = trip_frequencies.get(trip_id, [])
        if freq_entries:
            for start, end, headway in freq_entries:
                if headway <= 0:
                    continue
                n_core = max(0, (min(end, CORE_END) - max(start, CORE_START)) // headway)
                n_eve  = max(0, (min(end, EVENING_END) - max(start, EVENING_START)) // headway)
                n_we   = max(0, (min(end, WEEKEND_END) - max(start, WEEKEND_START)) // headway)
                line_freq[line_key]["core_wd"] += n_core * wd_hits
                line_freq[line_key]["eve_wd"]  += n_eve  * wd_hits
                line_freq[line_key]["we"]      += n_we   * we_hits
        else:
            if CORE_START <= first_dep < CORE_END:
                line_freq[line_key]["core_wd"] += wd_hits
            elif EVENING_START <= first_dep < EVENING_END:
                line_freq[line_key]["eve_wd"] += wd_hits
            if WEEKEND_START <= first_dep < WEEKEND_END:
                line_freq[line_key]["we"] += we_hits

        n = len(stops)
        canon_score = n * len(active_dates)
        if canon_score > line_canonical.get(line_key, {}).get("canon_score", 0):
            line_canonical[line_key] = {
                "stop_count": n,
                "canon_score": canon_score,
                "stops": [(s[1], s[2], s[3]) for s in stops],
            }

        raw_variant = frozenset(s[1] for s in stops)
        merged_set = frozenset(stop_merge.get(s[1]) or s[1].split(":")[0] for s in stops)
        sequence   = [(s[1], s[2], s[3]) for s in stops]
        trip_buf[trip_id] = (
            line_key, trip.get("agency_id", ""),
            max(1, len(active_dates)),
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

    # ── Per-(line_key, trip_group_id) accumulators ───────────────────────────
    line_canonical_tg_stops: dict = {}
    line_variant_counts: dict = defaultdict(lambda: defaultdict(int))
    line_variant_sequences: dict = {}
    line_canonical_tg_agency: dict = {}

    for tid, (lk, aid, weight, raw_variant, merged_set, sequence) in trip_buf.items():
        tg = trip_group.get(tid)
        if tg is None:
            continue
        tg_key = (lk, tg)
        # Expose per-trip group identity, stop sequence, and merged-stop variant
        # for downstream emission and per-group shape dedup.
        _trip_group_export[tid] = (lk, tg, aid)
        _trip_stops_export[tid] = [s[0] for s in sequence]
        _trip_merged_export[tid] = merged_set
        _trip_weight_export[tid] = weight

        if tg_key not in line_canonical_tg_agency:
            line_canonical_tg_agency[tg_key] = aid
        line_variant_counts[tg_key][raw_variant] += weight
        if (tg_key, raw_variant) not in line_variant_sequences:
            line_variant_sequences[(tg_key, raw_variant)] = sequence
        n = len(sequence)
        new_sid_set = raw_variant
        existing_list = line_canonical_tg_stops.get(tg_key)
        if existing_list is None:
            line_canonical_tg_stops[tg_key] = [{"stop_count": n, "stops": sequence}]
        elif not any(frozenset(s[0] for s in e["stops"]) == new_sid_set for e in existing_list):
            existing_list.append({"stop_count": n, "stops": sequence})
            existing_list.sort(key=lambda e: -e["stop_count"])

    trip_buf.clear()

    # 10% / 5% rare-variant filter.
    for tg_key, variant_counts in list(line_variant_counts.items()):
        total = sum(variant_counts.values())
        for pct in (0.10, 0.05):
            threshold = max(1, total * pct)
            filtered = {v: c for v, c in variant_counts.items() if c >= threshold}
            if filtered:
                line_variant_counts[tg_key] = filtered
                break

    for tg_key, canons in line_canonical_tg_stops.items():
        variant_counts = line_variant_counts.get(tg_key, {})
        line_canonical_tg_stops[tg_key] = [
            c for c in canons
            if frozenset(s[0] for s in c["stops"]) in variant_counts
        ]

    # Compute per-line speed from canonical trips.
    line_speed: dict = {}
    for line_key, canon in line_canonical.items():
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
            line_speed[line_key] = round(sum(seg_speeds) / len(seg_speeds), 1)

    # Normalise the freq counters from "trip × sample-days-hit" totals to
    # "average trips per sample day". compute_freq_score treats these the same
    # way it treated the old single-date integer counts.
    for freq in line_freq.values():
        freq["core_wd"] = freq["core_wd"] / n_wd_samples
        freq["eve_wd"]  = freq["eve_wd"]  / n_wd_samples
        freq["we"]      = freq["we"]      / n_we_samples

    return line_freq, line_speed, line_canonical


# ── Indexes & scoring (GTFS-side, unchanged) ─────────────────────────────────

def build_gtfs_index(line_freq, line_speed) -> tuple:
    """Build short-name and long-name keyed indexes of (raw_freq, speed_kmh)."""
    short_acc, long_acc = {}, {}
    for line_key, freq in line_freq.items():
        short_name, long_name, bucket = line_key
        speed = line_speed.get(line_key)
        skey = (bucket, short_name)
        if skey not in short_acc:
            short_acc[skey] = {"freqs": [], "speeds": []}
        short_acc[skey]["freqs"].append(dict(freq))
        if speed:
            short_acc[skey]["speeds"].append(speed)
        long_norm = long_name.replace(" ", "")
        if long_norm and long_norm != short_name and long_norm != short_name.replace(" ", ""):
            lkey = (bucket, long_norm)
            if lkey not in long_acc:
                long_acc[lkey] = {"freqs": [], "speeds": []}
            long_acc[lkey]["freqs"].append(dict(freq))
            if speed:
                long_acc[lkey]["speeds"].append(speed)

    def _finalise(acc):
        result = {}
        for key, data in acc.items():
            merged = {"core_wd": 0, "eve_wd": 0, "we": 0}
            for f in data["freqs"]:
                merged["core_wd"] += f["core_wd"]
                merged["eve_wd"]  += f["eve_wd"]
                merged["we"]      += f["we"]
            speeds = data["speeds"]
            result[key] = {
                "raw_freq": merged,
                "speed_kmh": round(sum(speeds) / len(speeds), 1) if speeds else None,
            }
        return result

    return _finalise(short_acc), _finalise(long_acc)


def build_stop_pair_freq(line_freq: dict, line_canonical: dict) -> dict:
    pair_freq: dict = defaultdict(lambda: {"core_wd": 0, "eve_wd": 0, "we": 0})
    for line_key, canon in line_canonical.items():
        freq = line_freq.get(line_key)
        if not freq:
            continue
        stops = canon["stops"]
        uics = []
        for stop_id, _arr, _dep in stops:
            uic = stop_id.split(":")[0]
            if not uics or uics[-1] != uic:
                uics.append(uic)
        for i in range(len(uics) - 1):
            pair = (uics[i], uics[i + 1])
            pair_freq[pair]["core_wd"] += freq["core_wd"]
            pair_freq[pair]["eve_wd"]  += freq["eve_wd"]
            pair_freq[pair]["we"]      += freq["we"]
    return dict(pair_freq)


def corridor_freq(canon_stops: list, pair_freq: dict):
    uics = []
    for stop_id, _arr, _dep in canon_stops:
        uic = stop_id.split(":")[0]
        if not uics or uics[-1] != uic:
            uics.append(uic)
    best = None
    for i in range(len(uics) - 1):
        pf = pair_freq.get((uics[i], uics[i + 1]))
        if pf and (best is None or pf["core_wd"] > best["core_wd"]):
            best = pf
    return best


def compute_freq_score(raw_freq: dict, mode: str) -> float:
    best_map, worst_map = _headways()
    if mode not in best_map:
        sys.exit(f"compute_freq_score: bucket {mode!r} missing from headway config.")
    best_hw = best_map[mode]
    worst_hw = worst_map[mode]
    core_trips = raw_freq.get("core_wd", 0)
    eve_trips  = raw_freq.get("eve_wd",  0)
    we_trips   = raw_freq.get("we",      0)

    if core_trips <= 0:
        return 0.0
    actual_hw = CORE_MINUTES / core_trips
    core_score = (worst_hw - actual_hw) / (worst_hw - best_hw)
    core_score = max(0.0, min(1.0, core_score))

    low_eve = LOW_EVE_HEADWAY.get(mode, 30)
    if eve_trips >= 2:
        eve_factor = MALUS_LOW if EVENING_MINUTES / eve_trips > low_eve else 0.0
    elif eve_trips == 0:
        eve_factor = MALUS_NO
    else:
        eve_factor = 0.0

    low_we = LOW_WE_HEADWAY.get(mode, 60)
    if we_trips >= 2:
        we_factor = MALUS_LOW if WEEKEND_MINUTES / we_trips > low_we else 0.0
    elif we_trips == 0:
        we_factor = MALUS_NO
    else:
        we_factor = 0.0

    final = core_score * (1 - eve_factor) * (1 - we_factor)
    return round(max(0.0, min(1.0, final)), 3)


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


_AERIAL_ROUTE_TYPES = {"5", "6"}


def deduplicate_mountain(features: list) -> list:
    """Drop overlapping aerial features (cable cars, gondolas) sharing the same
    ref. Best (most geometry vertices) wins.

    Restricted to GTFS route_type 5/6 — the historic problem this solved was
    multiple OSM route relations for the same physical haul cable. Funiculars
    (route_type 7) and rack rail are not collapsed because there a shared
    bbox usually means two genuinely different branches off the same stem.
    """
    aerial_idx = [(i, f) for i, f in enumerate(features)
                  if f["properties"].get("route_type") in _AERIAL_ROUTE_TYPES]
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

# Aerial GTFS route_types (5 = cable car, 6 = gondola / aerial lift) where
# OSM coverage is patchy enough that a missing pfaedle shape is treated as a
# straight-line fallback rather than a hard `pfaedle_unrouted` failure. All
# other modes (incl. funicular = 7) drop the feature when pfaedle has no
# shape, same as rail / bus today.
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

    min_active_days = int(cfg.get("min_active_days", 150))

    trip_frequencies = load_frequencies()
    print(f"  {sum(len(v) for v in trip_frequencies.values()):,} frequency entries "
          f"for {len(trip_frequencies):,} trips")

    line_freq, line_speed, line_canonical = stream_stop_times(
        trip_lookup, stop_coords, svc_dates, trip_frequencies, stop_meta)

    for line_key in line_canonical:
        _ = line_freq[line_key]

    gtfs_index, gtfs_long_index = build_gtfs_index(line_freq, line_speed)
    print(f"  {len(gtfs_index):,} GTFS short-name entries, "
          f"{len(gtfs_long_index):,} long-name entries")

    print("  Building corridor stop-pair frequency table...")
    pair_freq = build_stop_pair_freq(line_freq, line_canonical)
    print(f"  {len(pair_freq):,} stop pairs indexed")

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
    groups: dict = defaultdict(lambda: defaultdict(list))
    variant_counts: dict = defaultdict(lambda: defaultdict(int))
    for tid, (line_key, tg_id, aid) in _trip_group_export.items():
        merged_set = _trip_merged_export.get(tid)
        if merged_set is None:
            continue
        tg_key = (line_key, aid, tg_id)
        groups[tg_key][merged_set].append(tid)
        # Weighted by trip's active-date count so a depot run modelled as a few
        # trip_ids active on a single date counts as much smaller service than
        # the same trip_ids active every weekday. Same weight that the
        # in-stream rare-variant filter already uses.
        variant_counts[tg_key][merged_set] += _trip_weight_export.get(tid, 1)

    # Snapshot for the comprehensive diagnostic before the active-days,
    # rare-group, and rare-variant filters mutate `groups`. We deep-copy
    # variant→trips so we still know who was in each dropped variant.
    diag_original = {
        tg_key: {ms: list(tids) for ms, tids in vmap.items()}
        for tg_key, vmap in groups.items()
    }

    # ── Active-days per variant (concept: active-days-filter) ────────────────
    # For each emitted-feature unit (line_key, agency_id, trip_group_id,
    # merged_stop_set), compute the union of active calendar dates across
    # every trip in that variant over the full feed validity period. Variants
    # below `min_active_days` are dropped before supergroup/rare-variant
    # filters run, so their weighted trips don't pollute share calculations.
    # Catches construction-replacement services even when several distinct
    # constructions share the same ref or the same trip group.
    # Mountain and ferry buckets are exempt (seasonal services).
    variant_service_ids: dict = defaultdict(set)
    for tid, (lk, tg_id_v, aid) in _trip_group_export.items():
        merged_set = _trip_merged_export.get(tid)
        if merged_set is None:
            continue
        t = trip_lookup.get(tid)
        if t:
            variant_service_ids[(lk, aid, tg_id_v, merged_set)].add(t["service_id"])
    variant_active_days: dict = {}
    for vkey, sids in variant_service_ids.items():
        u: set = set()
        for sid in sids:
            u |= svc_dates_full.get(sid, set())
        variant_active_days[vkey] = len(u)
    # svc_dates_full is the heaviest object after stop_times; release.
    svc_dates_full.clear()
    variant_service_ids.clear()

    short_active_variants: dict = defaultdict(set)  # tg_key → {merged_set,...}
    tg_keys_all_short_active: set = set()
    for tg_key in list(groups.keys()):
        line_key, aid, tg_id_v = tg_key
        bucket = line_key[2]
        if bucket in {"mountain", "ferry"}:
            continue
        vmap = groups[tg_key]
        to_drop = [ms for ms in vmap
                   if variant_active_days.get((line_key, aid, tg_id_v, ms), 0)
                      < min_active_days]
        if not to_drop:
            continue
        short_active_variants[tg_key].update(to_drop)
        for ms in to_drop:
            del vmap[ms]
            variant_counts[tg_key].pop(ms, None)
        if not vmap:
            tg_keys_all_short_active.add(tg_key)
            del groups[tg_key]
    n_dropped_var = sum(len(s) for s in short_active_variants.values())
    print(f"  {n_dropped_var:,} variants dropped by min_active_days={min_active_days} "
          f"(across {len(short_active_variants)} trip groups; "
          f"{len(tg_keys_all_short_active)} groups fully dropped)")

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
        for ms in vmap.keys():
            u |= ms
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

    # filter_outcomes[tg_key] = {merged_set: (outcome, threshold_pct_used)}
    # where outcome ∈ {"kept", "rare_variant", "short_active_period"}.
    diag_filter: dict = {}

    # Record variants dropped by the active-days filter. These don't reach
    # the rare-variant loop below (they're already gone from `groups`).
    for tg_key, dropped in short_active_variants.items():
        bucket_entry = diag_filter.setdefault(tg_key, {})
        for ms in dropped:
            bucket_entry[ms] = ("short_active_period", None)

    # Rare-variant filter: drop merged sets representing <10% of group trips
    # (garage runs, one-off detours). Fall back to 5% if nothing clears 10%.
    # If nothing clears 5% either, keep all (the group's variants are all rare;
    # filtering to empty would erase a real but sparse line).
    for tg_key, vmap in list(groups.items()):
        counts = variant_counts[tg_key]
        total = sum(counts.values())
        threshold_used = None
        for pct in (0.10, 0.05):
            threshold = max(1, total * pct)
            kept = {ms: tids for ms, tids in vmap.items() if counts[ms] >= threshold}
            if kept:
                groups[tg_key] = kept
                threshold_used = pct
                break
        kept_keys = set(groups.get(tg_key, vmap).keys())
        bucket_entry = diag_filter.setdefault(tg_key, {})
        for ms in vmap:
            bucket_entry[ms] = (
                "kept" if ms in kept_keys else "rare_variant",
                threshold_used,
            )

    # Trip groups dropped by the supergroup filter never reached the per-variant
    # filter loop above; mark any of their variants we haven't already labelled
    # (i.e. weren't short_active) as "kept" so the group-level reason
    # `rare_group_dropped` is what surfaces for them.
    for tg_key in rare_group_dropped:
        bucket_entry = diag_filter.setdefault(tg_key, {})
        for ms in diag_original.get(tg_key, {}):
            bucket_entry.setdefault(ms, ("kept", None))

    # Pre-filter low-freq groups out so they don't waste downstream work. The
    # active-days gate has already run upstream at variant granularity.
    drawable_groups = {}
    for (line_key, aid, tg_id), variant_map in groups.items():
        bucket = line_key[2]
        mode_approx = _BUCKET_MODE_APPROX.get(bucket, "regional_bus")
        raw = line_freq.get(line_key, {"core_wd": 0, "eve_wd": 0, "we": 0})
        if bucket == "mountain" or (
            line_key[0] == "CC" and bucket == "train"
        ) or compute_freq_score(raw, mode_approx) >= MIN_FREQ_SCORE:
            drawable_groups[(line_key, aid, tg_id)] = variant_map
    print(f"  {len(drawable_groups):,} drawable (line_key, agency, trip_group) entries")

    # ── Emit features ────────────────────────────────────────────────────────
    features: list = []
    line_stops_out: dict = {}
    pfaedle_unrouted: list = []
    trip_groups_diag: list = []
    matched_line_keys: set = set()
    feature_id_counter = 0
    # Per-(tg_key, merged_set) emission outcome for the comprehensive diagnostic.
    diag_emission: dict = {}

    for (line_key, agency_id, tg_id), variant_map in drawable_groups.items():
        short_name, long_name, bucket = line_key
        all_trips = [tid for trips in variant_map.values() for tid in trips]

        long_norm = long_name.replace(" ", "")
        gtfs_meta = (gtfs_long_index.get((bucket, long_norm))
                     or gtfs_index.get((bucket, short_name)))
        raw_freq  = (gtfs_meta or {}).get("raw_freq",
                                          {"core_wd": 0, "eve_wd": 0, "we": 0})
        speed_kmh = (gtfs_meta or {}).get("speed_kmh")

        tg_key = (line_key, agency_id, tg_id)
        for merged_set, trip_ids in variant_map.items():
            # Pick the rep from the most common platform sub-variant so the
            # drawn line tracks its dominant platform pattern instead of
            # whichever trip happens to run on the most service days. Ties
            # resolve by smallest min trip_id for stable output.
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
            # the rest of the merged-set variant so a variant where pfaedle
            # routed only an unusual platform isn't silently dropped.
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
                })
                diag_emission[(tg_key, merged_set)] = {
                    "feature_emitted": False,
                    "exclusion_reason": "pfaedle_unrouted",
                    "rep_trip_id": rep_tid, "shape_id": "",
                    "n_coords": 0, "line_km": 0.0,
                }
                continue

            if len(polyline) < 2:
                diag_emission[(tg_key, merged_set)] = {
                    "feature_emitted": False,
                    "exclusion_reason": "polyline_too_short",
                    "rep_trip_id": rep_tid, "shape_id": shape_id,
                    "n_coords": len(polyline), "line_km": round(length_km, 2),
                }
                continue

            # Final mode classification.
            mode = gtfs_to_mode(bucket, agency_id,
                                short_name=short_name, length_km=length_km)

            # Frequency: try corridor boost (use rep trip's stop pair freq).
            stop_seq = [(sid, 0, 0) for sid in stop_ids]
            corr_raw = corridor_freq(stop_seq, pair_freq) if stop_seq else None
            own_raw = dict(raw_freq)
            if corr_raw and own_raw["core_wd"] > 0 and corr_raw["core_wd"] > own_raw["core_wd"]:
                eff_raw = corr_raw
            else:
                eff_raw = own_raw

            freq_score = compute_freq_score(eff_raw, mode)
            if mode == "mountain" and freq_score < 0.4:
                freq_score = 0.4

            color      = speed_to_color(mode, speed_kmh)
            width_base = freq_to_width_base(freq_score, mode)

            feature_id_counter += 1
            feat_id = f"tg{tg_id}_s{feature_id_counter}"

            # Geometry — always LineString for new emission.
            geometry = {"type": "LineString", "coordinates": polyline}
            features.append({
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "osm_id":       feat_id,
                    "ref":          short_name,
                    "name":         long_name,
                    "operator":     agency_names.get(agency_id, ""),
                    "agency_id":    agency_id,
                    "mode":         mode,
                    "route_type":   route_type,
                    "freq_score":   freq_score,
                    "speed_kmh":    speed_kmh,
                    "color":        color,
                    "width_base":   width_base,
                    "line_km":      round(length_km, 1),
                    "direction_id": rep_trip.get("direction_id", ""),
                    "trip_group_id": tg_id,
                    "shape_id":     shape_id or "",
                    "gtfs_matched": True,
                    "geometry_source": geometry_source,
                },
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
            }

            matched_line_keys.add(line_key)
            diag_emission[(tg_key, merged_set)] = {
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

    # ── Write outputs ────────────────────────────────────────────────────────
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    OUT_STOPS.write_text(json.dumps(line_stops_out))
    print(f"\n  {len(features):,} features → {OUT}")
    print(f"  {sum(len(v['stops']) for v in line_stops_out.values()):,} stops "
          f"across {len(line_stops_out):,} features → {OUT_STOPS}")

    # gtfs_unmatched: GTFS lines with no emitted feature (after grouping).
    # Drawable line_keys come straight from drawable_groups — that dict has
    # already passed the freq-score / mountain / CC exemptions used during
    # emission, so the difference against matched_line_keys is exactly the set
    # of lines that we thought we should draw but pfaedle never shaped (or
    # whose polylines collapsed to < 2 coords).
    all_line_keys = {lk for (lk, _aid, _tg) in drawable_groups}
    unmatched = all_line_keys - matched_line_keys
    unmatched_out = []
    for lk in sorted(unmatched, key=lambda x: (x[2], x[0], x[1])):
        short_name, long_name, bucket = lk
        freq = line_freq.get(lk, {"core_wd": 0, "eve_wd": 0, "we": 0})
        mode_approx = _BUCKET_MODE_APPROX.get(bucket, "regional_bus")
        fs = compute_freq_score(freq, mode_approx)
        unmatched_out.append({
            "short_name":  short_name,
            "long_name":   long_name,
            "bucket":      bucket,
            "freq_score":  round(fs, 3),
            "total_trips": sum(freq.values()),
        })
    OUT_GTFS_UNMATCHED.write_text(json.dumps(unmatched_out, ensure_ascii=False))
    print(f"  GTFS unmatched: {len(unmatched_out)} lines with service but no feature → {OUT_GTFS_UNMATCHED}")

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
    for tg_key, var_outcomes in diag_filter.items():
        line_key, aid, tg_id = tg_key
        short_name, long_name, bucket = line_key
        original_vmap = diag_original.get(tg_key, {})
        raw_freq = dict(line_freq.get(line_key, {"core_wd": 0, "eve_wd": 0, "we": 0}))
        mode_approx = _BUCKET_MODE_APPROX.get(bucket, "regional_bus")
        fscore = compute_freq_score(raw_freq, mode_approx)

        drawable = (line_key, aid, tg_id) in drawable_groups
        if drawable:
            group_reason = None
        elif tg_key in rare_group_dropped:
            group_reason = "rare_group_dropped"
        elif tg_key in tg_keys_all_short_active:
            group_reason = "short_active_period"
        elif bucket == "mountain" or (short_name == "CC" and bucket == "train"):
            # These should have been drawable; only here if neither emitted
            # nor low-freq. Real shouldn't-happen branch — record it.
            group_reason = "unknown_skipped"
        elif fscore < MIN_FREQ_SCORE:
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
        for ms, (filt_outcome, threshold_pct) in var_outcomes.items():
            ms_trips = original_vmap.get(ms, [])
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

            em = diag_emission.get((tg_key, ms), {})
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
            ms_weight = variant_counts[tg_key].get(ms, 0)
            weighted_share = (ms_weight / variant_weighted_total_for_group
                              if variant_weighted_total_for_group else 0.0)
            v_active_days = variant_active_days.get(
                (line_key, aid, tg_id, ms), 0)

            v_entry = {
                "trip_count": len(ms_trips),
                "trip_share_pct": round(share * 100, 1),
                "weighted_trip_count": ms_weight,
                "variant_share_of_group": round(weighted_share, 4),
                "active_days": v_active_days,
                "first_terminus": first_terminus,
                "last_terminus": last_terminus,
                "kept_by_variant_filter": kept_by_filter,
                "rare_variant_threshold_pct": threshold_pct,
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

        threshold_field = (None if bucket in {"mountain", "ferry"}
                           else min_active_days)
        diag_out.append({
            "ref": short_name,
            "long_name": long_name,
            "bucket": bucket,
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
            "freq_score": round(fscore, 3),
            "min_active_days_threshold": threshold_field,
            "drawable": drawable,
            "group_exclusion_reason": group_reason,
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
