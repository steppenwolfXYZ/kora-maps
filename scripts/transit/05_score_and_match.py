#!/usr/bin/env python3
"""
Build the final transit GeoJSON from a pfaedle-routed GTFS feed.

Pipeline:
  1. Load the filtered + pfaedle-routed GTFS feed at data/gtfs_routed/.
  2. Stream stop_times with trip-grouping (gtfs-line-grouping concept) →
     `_line_canonical_export` keyed by (line_key, trip_group_id), plus
     `_trip_group_export` (trip_id → (line_key, tg_id, agency_id)) and
     `_trip_stops_export` (trip_id → [stop_id, …]).
  3. Score per-line frequency & speed (GTFS-side, unchanged from before).
  4. Load pfaedle shapes (shapes.txt) and per-trip shape_id from trips.txt.
  5. For each (line_key, trip_group_id), group trips by shape_id and emit
     one feature per distinct shape. Mode comes from the GTFS route_type
     with an agency-based mountain rack override.
  6. Mountain bucket and ferry bucket without a pfaedle-usable shape fall
     back to straight-line geometry between GTFS stops.

Outputs:
  data/transit/transit_lines.geojson    one feature per distinct shape
  data/transit/line_stops.json          per-feature ordered stops
  data/transit/gtfs_unmatched.json      GTFS lines with no emitted feature
  data/transit/trip_groups.json         trip-group composition (diagnostic)
  data/transit/pfaedle_unrouted.json    trips pfaedle didn't shape

Mode categories (unchanged):
  train, tram, metro, bus, regional_bus, ferry, mountain

Long-distance coaches are dropped upstream in 04b (agency denylist).
"""

import csv
import json
import colorsys
import sys
from collections import defaultdict
from math import radians, cos, sin, sqrt, atan2
from pathlib import Path
from typing import Optional, NamedTuple

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

# Best headway per mode (minutes) — at this headway, core_score = 1.0
BEST_HEADWAY = {
    "train":        15,
    "tram":          7,
    "metro":         5,
    "bus":           6,
    "regional_bus": 30,
    "ferry":        45,
    "mountain":     60,
}

# Trip-length threshold for bus → regional_bus reclassification (km).
# Applied to the canonical trip's GTFS stop coordinates.
REGIONAL_BUS_MIN_LENGTH = 12.0


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


def mountain_rack_agency_ids(cfg: dict, agency_names: dict) -> set:
    """Resolve config mountain_rack_agencies tokens (case-insensitive substrings of
    agency_name) to the set of agency_ids in the loaded feed.
    """
    tokens = [t.lower() for t in cfg.get("mountain_rack_agencies", [])]
    out = set()
    for aid, name in agency_names.items():
        n = name.lower()
        if any(tok in n for tok in tokens):
            out.add(aid)
    return out


def gtfs_to_mode(bucket: str, agency_id: str,
                 mountain_rack_aids: set,
                 length_km: Optional[float] = None) -> str:
    """Map a GTFS bucket + agency to one of the rendering modes.

    - bucket == "train" with agency in mountain_rack_aids → mountain
      (Jungfraubahn, WAB, BVB, etc.: route_type=2 but visually mountain).
    - bucket == "bus" → bus / regional_bus by trip length.
    - Other buckets pass through.
    """
    if bucket == "train" and agency_id in mountain_rack_aids:
        return "mountain"
    if bucket == "bus":
        if length_km is not None and length_km >= REGIONAL_BUS_MIN_LENGTH:
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


def load_trips(route_lookup: dict) -> dict:
    """{trip_id: {line_key, service_id, agency_id, shape_id, direction_id, route_id}}"""
    trips = {}
    with open(GTFS / "trips.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            r = route_lookup.get(row["route_id"])
            if not r:
                continue
            bucket = gtfs_type_to_bucket(r["type"])
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
        sys.exit(f"{path} missing — run 04c_run_pfaedle.py first")

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


# ── Canonical line table and trip-grouping ───────────────────────────────────

class CanonEntry(NamedTuple):
    line_key: tuple        # (short_name, long_name, bucket)
    stops: list            # [(stop_id, arr, dep), ...]
    dir_aware: bool        # True when variants have genuinely different stop sets
    agency_id: str
    no_draw: Optional[str] # None = drawable; "low_frequency" = freq < MIN_FREQ_SCORE
    trip_group_id: int     # connected-component id within (long_norm, agency_id, bucket) partition


_line_canonical_export: dict = defaultdict(list)
_trip_group_export: dict = {}        # trip_id → (line_key, trip_group_id, agency_id)
_trip_stops_export: dict = {}        # trip_id → [stop_id, ...]   (sequence)
_trip_merged_export: dict = {}       # trip_id → frozenset(merged_stop_id)  (variant identity)

_BUCKET_MODE_APPROX = {
    "train": "train", "tram": "tram", "metro": "metro",
    "ferry": "ferry", "bus": "regional_bus", "regional_bus": "regional_bus",
}


def stream_stop_times(trips, stop_coords, svc_dates, trip_frequencies, stop_meta):
    """One streaming pass → raw trip counts + speed per line, plus trip-group
    partitioning. Populates module-level exports `_line_canonical_export`,
    `_trip_group_export`, and `_trip_stops_export`.
    """
    global _line_canonical_export, _trip_group_export, _trip_stops_export

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

    # Low-frequency flag (kept for diagnostic / unmatched accounting).
    _zero_freq = {"core_wd": 0, "eve_wd": 0, "we": 0}
    low_freq_keys: set = {
        tg_key[0] for tg_key in line_canonical_tg_stops
        if tg_key[0][2] != "mountain"
        and not (tg_key[0][0] == "CC" and tg_key[0][2] == "train")
        and compute_freq_score(
            line_freq.get(tg_key[0], _zero_freq),
            _BUCKET_MODE_APPROX.get(tg_key[0][2], "regional_bus"),
        ) < MIN_FREQ_SCORE
    }

    _line_canonical_export.clear()
    for (line_key, tg_id), canons in line_canonical_tg_stops.items():
        short_name, long_name, bucket = line_key
        long_norm = long_name.replace(" ", "")
        agency_id = line_canonical_tg_agency.get((line_key, tg_id), "")
        no_draw = "low_frequency" if line_key in low_freq_keys else None
        for canon in canons:
            _line_canonical_export[(short_name, bucket)].append(
                CanonEntry(line_key, canon["stops"], False, agency_id, no_draw, tg_id))
            if long_norm and long_norm != short_name:
                _line_canonical_export[(long_norm, bucket)].append(
                    CanonEntry(line_key, canon["stops"], False, agency_id, no_draw, tg_id))

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
    best_hw = BEST_HEADWAY.get(mode, 15)
    core_trips = raw_freq.get("core_wd", 0)
    eve_trips  = raw_freq.get("eve_wd",  0)
    we_trips   = raw_freq.get("we",      0)

    if core_trips >= 2:
        actual_headway = CORE_MINUTES / core_trips
        core_score = min(1.0, best_hw / actual_headway)
    elif core_trips >= 1:
        # Average between 1 and 2 trips per sample day — same tiny score as the
        # original single-date "exactly 1 trip" branch.
        core_score = min(0.15, best_hw / CORE_MINUTES)
    elif core_trips > 0:
        # Fractional average (line runs on some samples but averages < 1
        # trip/day). Pro-rate the floor score so the no-draw threshold is hit
        # smoothly rather than at a cliff edge.
        core_score = core_trips * min(0.15, best_hw / CORE_MINUTES)
    else:
        return 0.0

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


def deduplicate_mountain(features: list) -> list:
    """Drop overlapping mountain features sharing the same ref. Best (most
    geometry vertices) wins.
    """
    mountain_idx = [(i, f) for i, f in enumerate(features)
                    if f["properties"]["mode"] == "mountain"]
    keep = set(i for i, f in enumerate(features)
               if f["properties"]["mode"] != "mountain")

    by_ref: dict = defaultdict(list)
    for i, f in mountain_idx:
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
        print(f"  Mountain dedup: removed {n_dropped} duplicate features")
    return [f for i, f in enumerate(features) if i in keep]


# ── Pfaedle shape grouping ───────────────────────────────────────────────────

# Mountain bucket and ferry bucket where pfaedle cannot route — pipeline falls
# back to straight-line geometry between GTFS stops.
_NO_PFAEDLE_BUCKETS = {"mountain", "ferry"}


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
    route_lookup = load_routes()
    agency_names = load_agencies()
    trip_lookup  = load_trips(route_lookup)
    print(f"  {len(stop_coords):,} stop entries, {len(svc_dates):,} service IDs, "
          f"{len(trip_lookup):,} trips, {len(agency_names):,} agencies")

    trip_frequencies = load_frequencies()
    print(f"  {sum(len(v) for v in trip_frequencies.values()):,} frequency entries "
          f"for {len(trip_frequencies):,} trips")

    line_freq, line_speed, line_canonical = stream_stop_times(
        trip_lookup, stop_coords, svc_dates, trip_frequencies, stop_meta)

    # Drop low-frequency canonical entries from the export pool (we don't
    # emit features for those).
    _removed_lk = 0
    for _key in list(_line_canonical_export.keys()):
        _kept = [e for e in _line_canonical_export[_key] if e.no_draw is None]
        if not _kept:
            del _line_canonical_export[_key]
            _removed_lk += 1
        elif len(_kept) != len(_line_canonical_export[_key]):
            _line_canonical_export[_key] = _kept
    print(f"  Removed {_removed_lk} low-freq keys from _line_canonical_export")

    for line_key in line_canonical:
        _ = line_freq[line_key]

    gtfs_index, gtfs_long_index = build_gtfs_index(line_freq, line_speed)
    print(f"  {len(gtfs_index):,} GTFS short-name entries, "
          f"{len(gtfs_long_index):,} long-name entries")

    print("  Building corridor stop-pair frequency table...")
    pair_freq = build_stop_pair_freq(line_freq, line_canonical)
    print(f"  {len(pair_freq):,} stop pairs indexed")

    # Resolve mountain-rack agency_ids.
    mountain_rack_aids = mountain_rack_agency_ids(cfg, agency_names)
    print(f"  {len(mountain_rack_aids)} mountain-rack agencies "
          f"(treated as mountain mode despite route_type=2)")

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
        variant_counts[tg_key][merged_set] += 1

    # Snapshot for the comprehensive diagnostic before the rare-variant filter
    # mutates `groups`. We deep-copy variant→trips so we still know who was in
    # each dropped variant.
    diag_original = {
        tg_key: {ms: list(tids) for ms, tids in vmap.items()}
        for tg_key, vmap in groups.items()
    }
    # filter_outcomes[tg_key] = {merged_set: ("kept" | "rare_variant", threshold_pct_used)}
    diag_filter: dict = {}

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
        diag_filter[tg_key] = {
            ms: ("kept" if ms in kept_keys else "rare_variant", threshold_used)
            for ms in vmap
        }

    # Pre-filter low-freq groups out so they don't waste downstream work.
    drawable_groups = {}
    for (line_key, aid, tg_id), variant_map in groups.items():
        mode_approx = _BUCKET_MODE_APPROX.get(line_key[2], "regional_bus")
        raw = line_freq.get(line_key, {"core_wd": 0, "eve_wd": 0, "we": 0})
        if line_key[2] == "mountain" or (
            line_key[0] == "CC" and line_key[2] == "train"
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
            rep_tid = best_trip_in_shape_group(trip_ids, trip_lookup, svc_dates)
            rep_trip = trip_lookup.get(rep_tid, {})
            stop_ids = _trip_stops_export.get(rep_tid, [])

            # Find a usable shape for this variant. Trips with the same merged
            # stop set may have different pfaedle shape_ids due to platform
            # differences; pick the first one whose shape actually exists.
            shape_id = ""
            for cand_tid in [rep_tid] + [t for t in trip_ids if t != rep_tid][:50]:
                sid = trip_lookup.get(cand_tid, {}).get("shape_id", "")
                if sid and sid in shapes:
                    shape_id = sid
                    break

            polyline = []
            if shape_id:
                polyline = [list(p) for p in shapes[shape_id]]
                length_km = polyline_length_km(polyline)
            elif bucket in _NO_PFAEDLE_BUCKETS:
                polyline = stops_to_polyline(stop_ids, stop_coords)
                length_km = polyline_length_km(polyline)
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
            mode = gtfs_to_mode(bucket, agency_id, mountain_rack_aids, length_km)

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
                    "freq_score":   freq_score,
                    "speed_kmh":    speed_kmh,
                    "color":        color,
                    "width_base":   width_base,
                    "line_km":      round(length_km, 1),
                    "direction_id": rep_trip.get("direction_id", ""),
                    "trip_group_id": tg_id,
                    "shape_id":     shape_id or "",
                    "gtfs_matched": True,
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

    # ── Mountain bucket fallback for groups pfaedle has no usable shape ──
    # Already handled inline above (bucket == "mountain" branch). Apply
    # mountain dedup so multiple shapes for the same physical cable car
    # collapse to one feature, then drop the now-orphaned line_stops entries.
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
    all_line_keys: set = set()
    for trips in _line_canonical_export.values():
        for entry in trips:
            all_line_keys.add(entry.line_key)
    unmatched = all_line_keys - matched_line_keys
    unmatched_out = []
    for lk in sorted(unmatched, key=lambda x: (x[2], x[0], x[1])):
        short_name, long_name, bucket = lk
        freq = line_freq.get(lk, {"core_wd": 0, "eve_wd": 0, "we": 0})
        mode_approx = _BUCKET_MODE_APPROX.get(bucket, "regional_bus")
        fs = compute_freq_score(freq, mode_approx)
        if fs < MIN_FREQ_SCORE and bucket != "mountain":
            continue
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
        elif bucket == "mountain" or (short_name == "CC" and bucket == "train"):
            # These should have been drawable; only here if neither emitted
            # nor low-freq. Real shouldn't-happen branch — record it.
            group_reason = "unknown_skipped"
        elif fscore < MIN_FREQ_SCORE:
            group_reason = "low_frequency"
        else:
            group_reason = "unknown_skipped"

        variants_out = []
        for ms, (filt_outcome, threshold_pct) in var_outcomes.items():
            ms_trips = original_vmap.get(ms, [])
            first_terminus = "?"
            last_terminus = "?"
            if ms_trips:
                any_stops = _trip_stops_export.get(ms_trips[0], [])
                if any_stops:
                    f_uic = any_stops[0].split(":")[0]
                    l_uic = any_stops[-1].split(":")[0]
                    first_terminus = (stop_meta.get(any_stops[0],
                                       stop_meta.get(f_uic, ("?", "")))[0] or "?")
                    last_terminus = (stop_meta.get(any_stops[-1],
                                       stop_meta.get(l_uic, ("?", "")))[0] or "?")

            em = diag_emission.get((tg_key, ms), {})
            kept_by_filter = (filt_outcome == "kept")
            # Effective exclusion: if drawable_groups didn't include the group,
            # the variant wasn't reached for emission, so propagate the group
            # reason; otherwise use the filter reason or the emission reason.
            if not drawable:
                v_reason = group_reason
            elif not kept_by_filter:
                v_reason = "rare_variant"
            else:
                v_reason = em.get("exclusion_reason")

            v_entry = {
                "merged_stop_count": len(ms),
                "trip_count": len(ms_trips),
                "first_terminus": first_terminus,
                "last_terminus": last_terminus,
                "kept_by_variant_filter": kept_by_filter,
                "rare_variant_threshold_pct": threshold_pct,
                "exclusion_reason": v_reason,
                "feature_emitted": em.get("feature_emitted", False),
            }
            if em.get("feature_emitted"):
                v_entry["feature_id"] = em.get("feature_id", "")
                v_entry["shape_id"] = em.get("shape_id", "")
                v_entry["n_coords"] = em.get("n_coords", 0)
                v_entry["line_km"] = em.get("line_km", 0.0)
                v_entry["rep_trip_id"] = em.get("rep_trip_id", "")
            elif em:
                # Reached emission but didn't produce a feature.
                v_entry["shape_id"] = em.get("shape_id", "")
                v_entry["n_coords"] = em.get("n_coords", 0)
                v_entry["line_km"] = em.get("line_km", 0.0)
                v_entry["rep_trip_id"] = em.get("rep_trip_id", "")
            variants_out.append(v_entry)

        total_trips_in_group = sum(len(t) for t in original_vmap.values())
        diag_out.append({
            "ref": short_name,
            "long_name": long_name,
            "bucket": bucket,
            "agency_id": aid,
            "agency_name": agency_names.get(aid, ""),
            "trip_group_id": tg_id,
            "total_trip_count": total_trips_in_group,
            "raw_freq": raw_freq,
            "freq_score": round(fscore, 3),
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
