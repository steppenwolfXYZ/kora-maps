#!/usr/bin/env python3
"""
Build the final transit GeoJSON by:
  1. Loading OSM route geometries (data/osm/routes.geojson)
  2. Loading GTFS schedule data (data/gtfs/)
  3. Computing speed (km/h) and raw trip counts per line from GTFS
  4. Matching OSM routes to GTFS lines by (mode, ref)
  5. Assigning final mode (city bus vs regional bus split by speed)
  6. Computing mode-aware frequency score with per-mode "best headway"
  7. Writing data/transit/transit_lines.geojson

Mode categories:
  train          — IC, IR, S-Bahn, RegioExpress, RE, R, TER (red)
  tram           — trams, light rail (turquoise)
  metro          — underground (green)
  bus            — city buses, avg speed <30 km/h (blue)
  regional_bus   — PostBus/regional, avg speed ≥30 km/h (purple)
  ferry          — boats (blue)
  mountain       — funicular, gondola, cable car (pink)

  Long-distance coaches (Flixbus etc.) → excluded entirely.
  Trolleybuses → treated as bus.

Frequency scoring:
  score = min(1.0, best_headway / actual_headway)
  Best headways: train=15min, tram=7min, metro=5min,
                 bus=6min, regional_bus=30min, ferry=45min, mountain=60min
  Malus applied for sparse evening/weekend service.
"""

import csv
import json
import re
import colorsys
import math
from collections import defaultdict
from math import radians, cos, sin, sqrt, atan2
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GTFS = ROOT / "data" / "gtfs"
OSM_ROUTES = ROOT / "data" / "osm" / "routes.geojson"
OUT = ROOT / "data" / "transit" / "transit_lines.geojson"
OUT_STOPS = ROOT / "data" / "transit" / "line_stops.json"

# ── Representative dates ─────────────────────────────────────────────────────
WEEKDAY_DATE = "20260407"   # Tuesday 7 Apr 2026
WEEKEND_DATE = "20260412"   # Saturday 12 Apr 2026

CORE_START    = 7 * 3600
CORE_END      = 18 * 3600
EVENING_START = 18 * 3600
EVENING_END   = 22 * 3600
WEEKEND_START = 7 * 3600
WEEKEND_END   = 20 * 3600

CORE_MINUTES    = (CORE_END - CORE_START) / 60        # 660 min
EVENING_MINUTES = (EVENING_END - EVENING_START) / 60  # 240 min
WEEKEND_MINUTES = (WEEKEND_END - WEEKEND_START) / 60  # 780 min

# Malus for sparse off-peak service (subtracted from core score)
MALUS_LOW_EVENING = 0.08   # sparse (but present) evening service — shared across modes
MALUS_LOW_WEEKEND = 0.06   # sparse (but present) weekend service — shared across modes

# No evening/weekend malus per mode.
# Lower for modes where off-peak absence is structurally normal (ferries don't run at night,
# rural trains don't run evenings in remote valleys).  Higher for city modes where it signals
# a real gap in service.  Values calibrated so that 3 core trips/day with no off-peak service
# produces a small but positive freq_score (= visible pale colour, not dropped).
MALUS_NO_EVENING = {
    "train":        0.03,
    "regional_bus": 0.07, "ferry":        0.10, "mountain": 0.00,
    "bus":          0.18, "tram":         0.18, "metro":    0.18,
}
MALUS_NO_WEEKEND = {
    "train":        0.02,
    "regional_bus": 0.05, "ferry":        0.08, "mountain": 0.00,
    "bus":          0.14, "tram":         0.14, "metro":    0.14,
}

# "Low service" evening/weekend headway thresholds per mode
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

# Operators to exclude entirely (long-distance coaches)
EXCLUDED_OPERATORS = {"flixbus", "flixcoach", "eurolines", "deinbus", "megabus", "ic bus"}

# OSM route length threshold for bus → regional_bus classification.
# Using OSM line length (already computed, per-route, no GTFS cross-city confusion):
#   city buses typically < 12 km total; regional PostBus/rural typically > 12 km.
REGIONAL_BUS_MIN_LENGTH = 12.0   # km

# ── Mode classification ───────────────────────────────────────────────────────

def osm_to_mode(route_tag: str, ref: str, operator: str, length_km: float):
    """Return mode string, or None to exclude this route entirely."""
    r = route_tag.lower()
    op = operator.lower()

    # Exclude long-distance coaches
    if any(x in op for x in EXCLUDED_OPERATORS):
        return None
    # Also exclude very long bus routes without a known operator (likely Flixbus variants)
    if r in ("bus", "coach") and length_km > 200:
        return None

    if r == "railway":
        return None   # OSM infrastructure track sections, not passenger services
    if r in ("train", "rail", "light_rail"):
        return "train"
    if r == "tram":
        return "tram"
    if r == "trolleybus":
        return "bus"   # trolleybus = bus with overhead wire, same category
    if r == "subway":
        return "metro"
    if r in ("ferry", "boat"):
        return "ferry"
    if r in ("funicular", "cable_car", "gondola", "aerial_lift", "aerialway"):
        return "mountain"
    if r in ("bus", "coach"):
        return "bus"   # city/regional split happens after speed is known
    return "bus"


def gtfs_type_to_bucket(route_type: str) -> str:
    t = route_type.strip()
    if t == "0":  return "tram"
    if t == "1":  return "metro"
    if t == "2":  return "train"
    if t == "3":  return "bus"
    if t == "4":  return "ferry"
    if t == "6":  return "mountain"
    if t == "7":  return "mountain"
    if t == "11": return "bus"    # trolleybus → bus bucket
    return "bus"


# ── Color scheme ─────────────────────────────────────────────────────────────
# Base hue per mode (HSL degrees 0–360)
MODE_HUE = {
    "train":        0,    # red
    "tram":       180,    # turquoise (better contrast in warm urban areas)
    "metro":      120,    # green
    "bus":        220,    # blue
    "regional_bus": 290,  # purple-red (better contrast in rural areas)
    "ferry":      220,    # blue (same as bus; no geographic overlap)
    "mountain":   320,    # deep pink / magenta
}

# Max reference speed per mode (km/h) for normalising speed to a 0–1 score.
# These are realistic average segment speeds (stop-to-stop from GTFS times),
# not theoretical top speeds.
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
        # Mountain lines: fixed light yellow, no speed variance
        return "#ffe566"
    hue = MODE_HUE.get(mode, 220) / 360.0
    if speed_kmh is None:
        speed_score = 0.5   # mid-score fallback when no speed data
    else:
        max_speed = MODE_MAX_SPEED.get(mode, 80)
        speed_score = min(1.0, speed_kmh / max_speed)
    # Low speed → light + desaturated.  High speed → dark + vivid.
    s = 0.20 + speed_score * 0.72   # 20% → 92%
    l = 0.77 - speed_score * 0.50   # 77% → 27%
    r, g, b = colorsys.hls_to_rgb(hue, l, s)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


# ── Geometry helpers ──────────────────────────────────────────────────────────
def haversine_km(lon1, lat1, lon2, lat2) -> float:
    R = 6371
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi, dlam = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlam / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))

def parse_time(t: str) -> int:
    parts = t.strip().split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])


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
            try:
                lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
                coords[sid] = (lon, lat)
                base = sid.split(":")[0]
                if base not in coords:
                    coords[base] = (lon, lat)
            except ValueError:
                pass
    return coords


def load_calendar_dates() -> dict:
    from datetime import datetime

    svc_dates: dict = defaultdict(set)

    # 1. Explicit date additions/removals from calendar_dates.txt
    removals: dict = defaultdict(set)
    with open(GTFS / "calendar_dates.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["exception_type"] == "1":
                svc_dates[row["service_id"]].add(row["date"])
            elif row["exception_type"] == "2":
                removals[row["service_id"]].add(row["date"])

    # 2. Weekly patterns from calendar.txt (catches services not in calendar_dates.txt,
    #    e.g. MGB service_id '000000' running Mon-Sun year-round).
    #    We only need to resolve the two sample dates, not expand every date in the range.
    DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday",
                 "friday", "saturday", "sunday"]
    cal_path = GTFS / "calendar.txt"
    if cal_path.exists():
        for date_str in (WEEKDAY_DATE, WEEKEND_DATE):
            weekday_col = DAY_NAMES[datetime.strptime(date_str, "%Y%m%d").weekday()]
            with open(cal_path, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    if (row.get(weekday_col, "0") == "1"
                            and row["start_date"] <= date_str <= row["end_date"]):
                        svc_dates[row["service_id"]].add(date_str)

    # 3. Apply removal exceptions to all services (handles calendar.txt services
    #    that have exception_type=2 overrides on specific dates).
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
            }
    return routes


def load_trips(route_lookup: dict) -> dict:
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
            }
    return trips


_line_canonical_export: dict = defaultdict(list)  # (short_name|long_norm, bucket) → [stop_list, ...]

# Coarse geo-grid for canonical trip bucketing: ~0.5° ≈ 40 km per cell
GEO_BUCKET_DEG = 0.5


def stream_stop_times(trips, stop_coords, svc_dates, trip_frequencies):
    """One streaming pass → raw trip counts + speed per line."""
    global _line_canonical_export
    print("  Streaming stop_times.txt (~1–2 min)...")

    # Raw trip counts per line: {line_key: {core_wd, eve_wd, we}}
    line_freq: dict = defaultdict(lambda: {"core_wd": 0, "eve_wd": 0, "we": 0})

    # Canonical trip (most stops) per line for speed/pair-freq computation
    line_canonical: dict = {}

    # Best (longest) trip per (line_key, geo_bucket): captures geographic variants
    # that share the same line_key (e.g., S6 Bern vs S6 Zürich share ("S6","S 6","train"))
    line_canonical_geo: dict = {}   # (line_key, geo_bucket) → {"stop_count", "stops"}

    # Separate canonical for stop display: selects by max n_stops (not n × active_dates).
    # Prevents short high-frequency trips from hiding stops of the full-length route.
    # E.g. BOB "Grindelwald Terminal Express" (4 stops, 192 active days) would win over
    # full Grindelwald service (9 stops, 0 active days on sample dates) in line_canonical_geo.
    line_canonical_geo_stops: dict = {}  # (line_key, geo_bucket) → {"stop_count", "stops"}
    line_stop_union: dict = {}   # (line_key, geo_bucket) → set of all stop_ids seen across all trip variants

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

        is_weekday = WEEKDAY_DATE in active_dates
        is_weekend = WEEKEND_DATE in active_dates

        freq_entries = trip_frequencies.get(trip_id, [])
        if freq_entries:
            # frequencies.txt: service defined as headway intervals, not individual trips
            for start, end, headway in freq_entries:
                if headway <= 0:
                    continue
                if is_weekday:
                    n_core = max(0, (min(end, CORE_END) - max(start, CORE_START)) // headway)
                    n_eve  = max(0, (min(end, EVENING_END) - max(start, EVENING_START)) // headway)
                    line_freq[line_key]["core_wd"] += n_core
                    line_freq[line_key]["eve_wd"]  += n_eve
                if is_weekend:
                    n_we = max(0, (min(end, WEEKEND_END) - max(start, WEEKEND_START)) // headway)
                    line_freq[line_key]["we"] += n_we
        else:
            if is_weekday:
                if CORE_START <= first_dep < CORE_END:
                    line_freq[line_key]["core_wd"] += 1
                elif EVENING_START <= first_dep < EVENING_END:
                    line_freq[line_key]["eve_wd"] += 1
            if is_weekend:
                if WEEKEND_START <= first_dep < WEEKEND_END:
                    line_freq[line_key]["we"] += 1

        n = len(stops)
        canon_score = n * len(active_dates)
        if canon_score > line_canonical.get(line_key, {}).get("canon_score", 0):
            line_canonical[line_key] = {
                "stop_count": n,
                "canon_score": canon_score,
                "stops": [(s[1], s[2], s[3]) for s in stops],
            }

        # Find geographic bucket from the first stop with known coordinates
        gb = None
        for seq, sid, arr, dep in stops[:5]:
            c = stop_coords.get(sid) or stop_coords.get(sid.split(":")[0])
            if c:
                gb = (int(c[0] / GEO_BUCKET_DEG), int(c[1] / GEO_BUCKET_DEG))
                break
        if gb is None:
            return

        geo_key = (line_key, gb)
        line_stop_union.setdefault(geo_key, set()).update(s[1] for s in stops)
        existing = line_canonical_geo.get(geo_key)
        if existing is None or canon_score > existing.get("canon_score", 0):
            line_canonical_geo[geo_key] = {
                "stop_count": n,
                "canon_score": canon_score,
                "stops": [(s[1], s[2], s[3]) for s in stops],
            }

        # Stop-display canonical: prefer longest trip regardless of active_dates.
        existing_sd = line_canonical_geo_stops.get(geo_key)
        if existing_sd is None or n > existing_sd["stop_count"]:
            line_canonical_geo_stops[geo_key] = {
                "stop_count": n,
                "stops": [(s[1], s[2], s[3]) for s in stops],
            }

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
    print(f"  Done. {row_count:,} rows processed.")

    # Build canonical export: one candidate per geographic cell per line_key.
    # This gives separate candidates for "S6 Bern" and "S6 Zürich" even though they
    # share the same GTFS line_key = ("S6", "S 6", "train").
    # Uses line_canonical_geo_stops (longest trip per cell) so that short high-frequency
    # services don't hide stops of full-length routes (e.g. BOB Grindelwald leg).
    # Also adds the frequency-weighted canonical (line_canonical_geo) as a second candidate
    # when it has a different stop set. This handles cases like GoldenPass where an express
    # tourist train has more total stops (wins line_canonical_geo_stops) but skips many
    # intermediate stations, while the frequent regular train (wins line_canonical_geo by
    # n×active_days) stops everywhere — the stop assignment picks whichever candidate has
    # more stops inside the OSM feature's bbox.
    _line_canonical_export.clear()
    for (line_key, gb), canon in line_canonical_geo_stops.items():
        short_name, long_name, bucket = line_key
        _line_canonical_export[(short_name, bucket)].append(canon["stops"])
        long_norm = long_name.replace(" ", "")
        if long_norm and long_norm != short_name:
            _line_canonical_export[(long_norm, bucket)].append(canon["stops"])
        # Add frequency-weighted canonical as an extra candidate if it differs
        freq_canon = line_canonical_geo.get((line_key, gb))
        if freq_canon and freq_canon["stops"] != canon["stops"]:
            _line_canonical_export[(short_name, bucket)].append(freq_canon["stops"])
            if long_norm and long_norm != short_name:
                _line_canonical_export[(long_norm, bucket)].append(freq_canon["stops"])

    # Union candidate: aggregates ALL stop_ids from every trip variant in each geo_bucket.
    # Handles lines like GoldenPass where the longest trip (PE Express Montreux→Interlaken)
    # skips intermediate stations that PE30 (Montreux→Zweisimmen) stops at. The stop
    # assignment code filters by stop_near_bbox, so only in-bbox stops survive.
    for (line_key, _gb), all_sids in line_stop_union.items():
        short_name, long_name, bucket = line_key
        union_cand = [(sid, 0, 0) for sid in all_sids]
        _line_canonical_export[(short_name, bucket)].append(union_cand)
        long_norm = long_name.replace(" ", "")
        if long_norm and long_norm != short_name:
            _line_canonical_export[(long_norm, bucket)].append(union_cand)

    # Compute speed from canonical trips
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

    return line_freq, line_speed, line_canonical


def build_gtfs_index(line_freq, line_speed) -> tuple:
    """Build two lookups keyed by (bucket, ref):
      - short_index: keyed by short_name  (e.g. ('train', 'RE'))
      - long_index:  keyed by normalised long_name with spaces stripped
                     (e.g. ('train', 'RE1') from long_name 'RE 1')
    Returns (short_index, long_index).
    """
    short_acc = {}
    long_acc  = {}

    for line_key, freq in line_freq.items():
        short_name, long_name, bucket = line_key
        speed = line_speed.get(line_key)

        # Short-name index
        skey = (bucket, short_name)
        if skey not in short_acc:
            short_acc[skey] = {"freqs": [], "speeds": []}
        short_acc[skey]["freqs"].append(dict(freq))
        if speed:
            short_acc[skey]["speeds"].append(speed)

        # Long-name index — only when long_name adds information beyond short_name
        # Normalise by stripping spaces so 'RE 1' → 'RE1'
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
    """
    Build a stop-pair frequency table that aggregates all GTFS lines.

    For every consecutive stop pair (uic_A, uic_B) in each line's canonical trip,
    add that line's trip counts to the pair's totals.  Because every trip on a line
    passes through every stop pair on its route, this correctly captures combined
    corridor demand: Bern→Thun will sum IC1 + IC5 + IC8 + IC21 + IR15 + RE1 + …

    Returns {(uic_A, uic_B): {"core_wd": N, "eve_wd": N, "we": N}}
    """
    pair_freq: dict = defaultdict(lambda: {"core_wd": 0, "eve_wd": 0, "we": 0})

    for line_key, canon in line_canonical.items():
        freq = line_freq.get(line_key)
        if not freq:
            continue
        stops = canon["stops"]   # [(stop_id, arr, dep), ...]
        # Normalise stop IDs to their base UIC code (strip ":variant" suffixes)
        uics = []
        for stop_id, _arr, _dep in stops:
            uic = stop_id.split(":")[0]
            if not uics or uics[-1] != uic:   # skip duplicate consecutive stations
                uics.append(uic)

        for i in range(len(uics) - 1):
            pair = (uics[i], uics[i + 1])
            pair_freq[pair]["core_wd"] += freq["core_wd"]
            pair_freq[pair]["eve_wd"]  += freq["eve_wd"]
            pair_freq[pair]["we"]      += freq["we"]

    return dict(pair_freq)


def corridor_freq(canon_stops: list, pair_freq: dict):
    """
    Given a canonical stop list [(stop_id, arr, dep), ...] for one OSM route,
    return the raw-freq dict of the busiest stop pair on that route (max core_wd).
    Returns None if no stop pairs are found in pair_freq.
    """
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
    """
    Mode-aware frequency score.
    Core: score = min(1.0, best_headway / actual_headway)
    Off-peak malus applied for sparse evening/weekend service.
    """
    best_hw = BEST_HEADWAY.get(mode, 15)
    core_trips = raw_freq.get("core_wd", 0)
    eve_trips  = raw_freq.get("eve_wd",  0)
    we_trips   = raw_freq.get("we",      0)

    if core_trips >= 2:
        actual_headway = CORE_MINUTES / core_trips
        core_score = min(1.0, best_hw / actual_headway)
    elif core_trips == 1:
        core_score = min(0.15, best_hw / CORE_MINUTES)
    else:
        return 0.0

    # Evening malus
    low_eve = LOW_EVE_HEADWAY.get(mode, 30)
    no_eve  = MALUS_NO_EVENING.get(mode, 0.18)
    if eve_trips >= 2:
        eve_hw = EVENING_MINUTES / eve_trips
        if eve_hw > low_eve:
            core_score -= MALUS_LOW_EVENING
    elif eve_trips == 0:
        core_score -= no_eve

    # Weekend malus
    low_we = LOW_WE_HEADWAY.get(mode, 60)
    no_we  = MALUS_NO_WEEKEND.get(mode, 0.14)
    if we_trips >= 2:
        we_hw = WEEKEND_MINUTES / we_trips
        if we_hw > low_we:
            core_score -= MALUS_LOW_WEEKEND
    elif we_trips == 0:
        core_score -= no_we

    return round(max(0.0, min(1.0, core_score)), 3)


def line_bbox(coords):
    """Return (min_lon, min_lat, max_lon, max_lat) for a list of [lon, lat] points."""
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lons), min(lats), max(lons), max(lats)


def stop_near_bbox(lon, lat, bbox, margin=0.02):
    """True if (lon, lat) is within bbox expanded by margin degrees (~2km at CH latitude)."""
    return (bbox[0] - margin <= lon <= bbox[2] + margin and
            bbox[1] - margin <= lat <= bbox[3] + margin)


ENDPOINT_THRESHOLD_KM = 5.0

def _covers_endpoints(osm_pts: list, stops: list) -> bool:
    """True if stops include a point within ENDPOINT_THRESHOLD_KM of both
    the first and last OSM coordinate.  A canonical GTFS match that fails
    this check has likely resolved to the wrong GTFS service (e.g. SBB RE
    for an MGB RE41 ref) and should be superseded by the geo fallback."""
    if not stops or len(osm_pts) < 2:
        return True   # can't determine — don't force fallback
    start, end = osm_pts[0], osm_pts[-1]
    near_start = any(
        haversine_km(s[0], s[1], start[0], start[1]) <= ENDPOINT_THRESHOLD_KM
        for s in stops)
    near_end = any(
        haversine_km(s[0], s[1], end[0], end[1]) <= ENDPOINT_THRESHOLD_KM
        for s in stops)
    return near_start and near_end


def freq_to_width_base(freq_score, mode) -> float:
    if mode == "mountain":  return 0.75  # narrow accent lines
    if freq_score is None:  return 1.1
    return round(1.1 + freq_score * 1.5, 1)        # 1.1 → 2.6


# ── Mountain deduplication ────────────────────────────────────────────────────

def _feat_bbox(feat):
    """Return (minlon, minlat, maxlon, maxlat) for a feature, or None."""
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


def _bbox_overlap_fraction(b1, b2) -> float:
    """Fraction of the SMALLER bbox that is covered by the intersection."""
    ix0, iy0 = max(b1[0], b2[0]), max(b1[1], b2[1])
    ix1, iy1 = min(b1[2], b2[2]), min(b1[3], b2[3])
    if ix0 >= ix1 or iy0 >= iy1:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    a1 = (b1[2] - b1[0]) * (b1[3] - b1[1])
    a2 = (b2[2] - b2[0]) * (b2[3] - b2[1])
    smaller = min(a1, a2)
    return inter / smaller if smaller > 0 else 0.0


def _n_pts(feat) -> int:
    coords = feat["geometry"]["coordinates"]
    if feat["geometry"]["type"] == "MultiLineString":
        return sum(len(s) for s in coords)
    return len(coords)


def deduplicate_mountain(features: list) -> list:
    """
    Remove duplicate cable car / aerialway features that represent the same physical
    line in OSM.  Multiple OSM route relations sometimes exist for the same cable car
    (different service variants, incomplete relations, or both directions that diverge
    only slightly from the shared haul cable).

    Strategy: group mountain features by ref.  Within each ref group, sort by
    geometry point count (most points = best OSM coverage).  Drop any feature whose
    bounding box is ≥70% covered by a better (more-points) feature's bbox — they are
    rendering the same physical line.  Non-mountain features are kept unchanged.
    """
    mountain_idx = [(i, f) for i, f in enumerate(features)
                    if f["properties"]["mode"] == "mountain"]
    keep = set(i for i, f in enumerate(features)
               if f["properties"]["mode"] != "mountain")

    # Group by ref (empty ref keeps all — can't compare without a ref)
    by_ref: dict = defaultdict(list)
    for i, f in mountain_idx:
        ref = f["properties"]["ref"]
        by_ref[ref].append((i, f, _feat_bbox(f), _n_pts(f)))

    n_dropped = 0
    for ref, group in by_ref.items():
        if not ref:
            # No ref: fall through to name+bbox dedup below
            pass
        else:
            # Best geometry first
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
            continue

        # For empty-ref features: dedup by name similarity + bbox overlap.
        # Catches old/historic OSM relations for the same physical cable car.
        def _name_root(name: str) -> str:
            """Normalise name: lowercase, drop parenthetical year suffixes."""
            import re
            name = name.lower().strip()
            name = re.sub(r"\s*\([\d\-–]+\)\s*$", "", name)  # strip "(1933-2017)"
            return name

        group.sort(key=lambda x: -x[3])
        kept: list = []  # list of (i, bbox, name_root)
        for i, f, b, n in group:
            if b is None:
                keep.add(i)
                continue
            name_r = _name_root(f["properties"].get("name", ""))
            is_dup = False
            for ki, kb, kname in kept:
                if _bbox_overlap_fraction(b, kb) >= 0.65:
                    is_dup = True; break
                # Same name (after stripping year) + bboxes within ~1 km → dup
                if name_r and name_r == kname:
                    lat_mid = (b[1] + b[3]) / 2
                    dx = abs(b[0] - kb[0]) * 111000 * abs(lat_mid * 3.14159 / 180)
                    dy = abs(b[1] - kb[1]) * 111000
                    if (dx**2 + dy**2) ** 0.5 < 1000:
                        is_dup = True; break
            if is_dup:
                n_dropped += 1
            else:
                keep.add(i)
                kept.append((i, b, _name_root(f["properties"].get("name", ""))))

    if n_dropped:
        print(f"  Deduplication: removed {n_dropped} duplicate mountain features")

    return [f for i, f in enumerate(features) if i in keep]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)

    print("Loading GTFS data...")
    stop_coords  = load_stops()
    svc_dates    = load_calendar_dates()
    route_lookup = load_routes()
    trip_lookup  = load_trips(route_lookup)
    print(f"  {len(stop_coords):,} stop entries, {len(svc_dates):,} service IDs, "
          f"{len(trip_lookup):,} trips")

    trip_frequencies = load_frequencies()
    print(f"  {sum(len(v) for v in trip_frequencies.values()):,} frequency entries for {len(trip_frequencies):,} trips")
    line_freq, line_speed, line_canonical = stream_stop_times(trip_lookup, stop_coords, svc_dates, trip_frequencies)

    # Ensure all routes with any trips are indexed, even if they don't run on our sample dates
    # (e.g. summer-only tourist railways like Jungfraubahn, Harder Kulm funicular).
    # MUST happen before build_gtfs_index so these routes are included in the index.
    for line_key in line_canonical:
        _ = line_freq[line_key]   # defaultdict: creates {core_wd:0,eve_wd:0,we:0} if absent

    gtfs_index, gtfs_long_index = build_gtfs_index(line_freq, line_speed)
    print(f"  {len(gtfs_index):,} GTFS short-name entries, {len(gtfs_long_index):,} long-name entries")

    print("  Building corridor stop-pair frequency table...")
    pair_freq = build_stop_pair_freq(line_freq, line_canonical)
    print(f"  {len(pair_freq):,} stop pairs indexed")

    print("\nLoading OSM routes...")
    osm_routes = json.loads(OSM_ROUTES.read_text())["features"]
    print(f"  {len(osm_routes):,} OSM route features")

    # Build OSM mountain geometry lookup for GTFS-first mountain processing.
    # Indexed by ref (short_name) → list of OSM route features sorted by point count desc.
    # Includes:
    #   • Routes that osm_to_mode() classifies as mountain (route=funicular/cable_car/…)
    #   • Routes tagged route=train whose ref matches a GTFS mountain entry (rack railways
    #     like Niesenbahn tagged as train in OSM but type=5/6/7 in GTFS). These are handled
    #     by the GTFS-first loop and must NOT also appear in the main OSM loop.
    osm_mountain_by_ref: dict = defaultdict(list)
    osm_train_refs_in_mountain_gtfs: set = set()   # track to skip in main loop
    for _mfeat in osm_routes:
        _mp = _mfeat["properties"]
        _route_tag = _mp.get("route", "")
        if _route_tag in ("fitness_trail", "hiking", "cycling", "foot"):
            continue
        _ref = _mp.get("ref", "").strip()
        _mode = osm_to_mode(_route_tag, _ref, _mp.get("operator", ""), _mp.get("length_km", 0))
        _is_mountain_osm = (_mode == "mountain")
        _is_train_in_mountain_gtfs = False
        if _mode == "train" and gtfs_index.get(("mountain", _ref)) is not None:
            # Guard against ref collisions with unrelated funiculars elsewhere in Switzerland.
            # e.g. FUN 311 (Stanserhornbahn, near Stans) and FUN 312 (VerticAlp, Martigny)
            # share short_names "311"/"312" with the BOB/WAB/JB railways near Interlaken.
            # Only flag this OSM route if at least one canonical GTFS mountain stop for
            # this ref actually falls within the OSM route's bounding box.
            _osm_pts_chk = ([c for seg in _mfeat["geometry"]["coordinates"] for c in seg]
                            if _mfeat["geometry"]["type"] == "MultiLineString"
                            else _mfeat["geometry"]["coordinates"])
            _osm_bbox_chk = line_bbox(_osm_pts_chk)
            for _cand_stops in _line_canonical_export.get((_ref, "mountain"), []):
                if any(
                    (_sc := stop_coords.get(_sid) or stop_coords.get(_sid.split(":")[0]))
                    and stop_near_bbox(_sc[0], _sc[1], _osm_bbox_chk)
                    for _sid, _arr, _dep in _cand_stops
                ):
                    _is_train_in_mountain_gtfs = True
                    break
        if _is_mountain_osm or _is_train_in_mountain_gtfs:
            _geom = _mfeat["geometry"]
            _pts = ([c for seg in _geom["coordinates"] for c in seg]
                    if _geom["type"] == "MultiLineString" else _geom["coordinates"])
            osm_mountain_by_ref[_ref].append((_mfeat, len(_pts)))
            if _is_train_in_mountain_gtfs:
                osm_train_refs_in_mountain_gtfs.add(_ref)
    # Sort each ref's candidates best-first (most points = most detailed geometry)
    for _ref in osm_mountain_by_ref:
        osm_mountain_by_ref[_ref].sort(key=lambda x: -x[1])
    print(f"  {sum(len(v) for v in osm_mountain_by_ref.values())} OSM mountain route relations indexed "
          f"({len(osm_train_refs_in_mountain_gtfs)} train-tagged rack/cog railways)")

    # Build a geo-indexed list of all GTFS ferry lines for bbox-based fallback matching.
    # Used for ferries where OSM ref doesn't match GTFS short_name (e.g. BLS 3310→59).
    ferry_geo_index = []   # list of (gtfs_entry, canonical_stops)
    for line_key, canon in line_canonical.items():
        short_name, long_name, bucket = line_key
        if bucket != "ferry":
            continue
        idx_key = (bucket, short_name)
        gtfs_entry = gtfs_index.get(idx_key)
        if gtfs_entry is None:
            continue
        ferry_geo_index.append((gtfs_entry, canon["stops"]))

    print("\nMatching and scoring...")
    features = []
    stats = defaultdict(int)

    MODE_TO_BUCKET = {
        "train": "train",
        "tram": "tram", "metro": "metro",
        "bus": "bus", "regional_bus": "bus",
        "ferry": "ferry", "mountain": "mountain",
    }

    for feat in osm_routes:
        props = feat["properties"]
        route_tag = props.get("route", "")
        ref       = props.get("ref", "").strip()
        operator  = props.get("operator", "")
        length_km = props.get("length_km", 0)

        # Skip non-transit
        if route_tag in ("fitness_trail", "hiking", "cycling", "foot"):
            continue

        mode = osm_to_mode(route_tag, ref, operator, length_km)
        if mode is None:
            stats["excluded"] += 1
            continue

        # Mountain lines (funicular, gondola, cable car, aerialway) are processed
        # GTFS-first after this loop. Skip them here so OSM geometry alone never
        # draws a line — the timetable is the authority for what runs.
        # Also skip train-tagged rack/cog railways whose ref is in the mountain GTFS
        # bucket (e.g. Niesenbahn tagged route=train but GTFS type=5/6/7): the
        # GTFS-first loop will draw them using the OSM geometry we collected above.
        if mode == "mountain":
            continue
        if mode == "train" and ref in osm_train_refs_in_mountain_gtfs:
            continue

        # Exclude TER (French/Swiss regional rail-replacement buses).
        # These are cross-border or French-domestic services, not relevant for this map.
        if ref.upper().startswith("TER"):
            stats["excluded"] += 1
            continue

        bucket = MODE_TO_BUCKET.get(mode, "bus")

        # GTFS lookup — short-name index first, then long-name index
        ref_norm = ref.replace(" ", "")
        gtfs = gtfs_index.get((bucket, ref))
        if gtfs is None:
            for k in [(bucket, ref_norm),
                      (bucket, ref.upper()),
                      (bucket, ref.lower())]:
                gtfs = gtfs_index.get(k)
                if gtfs: break
        # Fallback: long-name index (catches e.g. OSM 'RE1' → GTFS long_name 'RE 1')
        if gtfs is None:
            gtfs = gtfs_long_index.get((bucket, ref_norm)) or \
                   gtfs_long_index.get((bucket, ref_norm.upper()))

        # Fallback: extract first word of OSM name as additional ref candidate.
        # Handles cases like OSM name="R 311: Interlaken..." where GTFS short_name="R".
        if gtfs is None:
            osm_name_prefix = props.get("name", "").split(":")[0].strip()
            for token in osm_name_prefix.split():
                if token != ref and len(token) <= 6:
                    candidate = gtfs_index.get((bucket, token)) or \
                                gtfs_index.get((bucket, token.upper()))
                    if candidate:
                        gtfs = candidate
                        break

        # Fallback: alpha-prefix — strip trailing digits from ref.
        # Handles MGB-style refs like "R43", "R44", "R45" → GTFS short_name "R".
        if gtfs is None:
            m = re.match(r'^([A-Za-z ]+)\d', ref)
            if m:
                alpha = m.group(1).strip()
                if alpha and alpha != ref:
                    gtfs = gtfs_index.get((bucket, alpha)) or \
                           gtfs_index.get((bucket, alpha.upper()))

        # OSM route bbox (used for geographic checks below)
        geom = feat["geometry"]
        osm_pts = ([c for seg in geom["coordinates"] for c in seg]
                   if geom["type"] == "MultiLineString" else geom["coordinates"])
        osm_bbox = line_bbox(osm_pts)

        # Geographic validation for bus GTFS matches: require at least one canonical
        # stop from any candidate trip for this ref to fall within the OSM route bbox.
        # This prevents defunct OSM route relations from picking up GTFS stats from a
        # coincidentally-matching line elsewhere in Switzerland.
        # (Mountain/ferry modes already do the equivalent check; this extends it to bus.)
        if gtfs is not None and bucket == "bus":
            geo_ok = False
            for lk in [(ref, "bus"), (ref_norm, "bus"),
                       (ref.upper(), "bus"), (ref.lower(), "bus")]:
                for cand_stops in _line_canonical_export.get(lk, []):
                    n_inside = sum(1 for sid, arr, dep in cand_stops
                                   if (c := stop_coords.get(sid) or stop_coords.get(sid.split(":")[0]))
                                   and stop_near_bbox(c[0], c[1], osm_bbox, margin=0.05))
                    if n_inside >= 1:
                        geo_ok = True
                        break
                if geo_ok:
                    break
            if not geo_ok:
                gtfs = None
                stats["gtfs_geo_mismatch"] += 1

        # Geo-based ferry fallback: OSM ferry ref may differ from GTFS short_name entirely
        # (e.g. BLS Thuner-/Brienzersee: OSM ref=3310/3470, GTFS short=59-68).
        # Find the GTFS ferry line whose canonical stops best overlap the OSM bbox.
        if gtfs is None and mode == "ferry":
            best_n = 1   # require at least 2 stops inside
            for gtfs_entry, cand_stops in ferry_geo_index:
                n_inside = sum(1 for sid, arr, dep in cand_stops
                               if (c := stop_coords.get(sid) or stop_coords.get(sid.split(":")[0]))
                               and stop_near_bbox(c[0], c[1], osm_bbox, margin=0.05))
                if n_inside > best_n:
                    best_n = n_inside
                    gtfs = gtfs_entry

        # Mountain name keyword override: routes with high-alpine destination names in OSM
        # are tourist mountain railways regardless of GTFS classification.
        # This catches WAB (Lauterbrunnen→Kleine Scheidegg) and JB (→Jungfraujoch) which
        # are tagged route=train in OSM and type=2 in GTFS but are clearly tourist railways.
        MOUNTAIN_PLACE_KEYWORDS = {
            "Kleine Scheidegg", "Jungfraujoch", "Schilthorn",
            "Eigergletscher", "Jungfrau",
        }
        osm_name = props.get("name", "")
        if any(kw in osm_name for kw in MOUNTAIN_PLACE_KEYWORDS):
            mode = "mountain"
            bucket = "mountain"

        speed_kmh = gtfs["speed_kmh"] if gtfs else None

        # Refine bus → regional_bus using ref structure + STI/EV exceptions.
        #
        # For refs that contain at least one digit: strip all letters/symbols and
        # evaluate the numeric remainder.  E.g. "X33" → "33" (2 digits → city),
        # "200 (Höribus)" → "200" (3 digits → regional).
        #
        # Special cases:
        #   • "EV" ref → always regional (Ersatzverkehr train-replacement bus).
        #   • STI operator + 2-digit numeric part → regional (Thun mountain buses).
        #   • PAG / PostAuto AG: regional operator across all CH; 2-digit refs are
        #     inter-village/inter-town lines, never city bus circulators.
        #
        # Pure-letter refs (A, G, TEL, Rot …) use a 10 km length fallback:
        # short city circulator vs. long regional connector.
        if mode == "bus":
            ref_upper = ref.strip().upper()
            digits_only = "".join(c for c in ref if c.isdigit())
            n_digits = len(digits_only)
            op_lower = operator.lower()
            net_lower = props.get("network", "").lower()
            # Operators/networks where 2-digit line numbers are regional, not city
            is_regional_2digit_net = (
                "sti" in op_lower                  # STI Thun area mountain buses
                or "chur" in op_lower              # ChurBus city-regional network
                or "transreno" in net_lower        # TransReno network (Chur/PostAuto)
                or "pag" in op_lower               # PostAuto Graubünden abbreviation
                or "postauto" in op_lower          # PostAuto AG full name
            )

            if ref_upper == "EV":
                # Ersatzverkehr train-replacement bus — always regional
                mode = "regional_bus"
            elif digits_only:
                # Ref contains a numeric component — classify by digit count
                if n_digits >= 3:
                    mode = "regional_bus"
                elif is_regional_2digit_net and n_digits == 2:
                    mode = "regional_bus"
                # else: 0-2 digit numeric part → keep as city bus
            else:
                # Pure letter ref (A, G, TEL, Rot, …) → 10 km length rule
                line_length_km = props.get("raw_length_km", props.get("length_km", 0))
                if line_length_km >= 10.0:
                    mode = "regional_bus"

        # Compute frequency score with the final mode
        # Use corridor-level frequency (all lines sharing any stop pair on this route)
        # rather than this line's own frequency alone, so that shared corridors
        # like Bern–Spiez or Arth-Goldau–Bellinzona reflect their true combined service.
        if gtfs:
            own_raw = gtfs["raw_freq"]
            canon_candidates = None
            for lk in [(ref, bucket), (ref_norm, bucket),
                       (ref_norm.upper(), bucket), (ref.lower(), bucket)]:
                if lk in _line_canonical_export:
                    canon_candidates = _line_canonical_export[lk]
                    break
            # Use first candidate for corridor freq (any will do — just needs stop IDs)
            canon = canon_candidates[0] if canon_candidates else None
            corr_raw = corridor_freq(canon, pair_freq) if canon else None
            # Only boost via corridor if the line itself has some own service on sample dates.
            # Night-only lines (own_raw core_wd == 0) must NOT inherit frequency from daytime
            # buses sharing the same stops (e.g. M82 Moonliner ← bus 82 daytime service).
            if corr_raw and own_raw["core_wd"] > 0 and corr_raw["core_wd"] > own_raw["core_wd"]:
                raw_freq = corr_raw
            else:
                raw_freq = own_raw
            freq_score = compute_freq_score(raw_freq, mode)
            stats["matched"] += 1
        elif mode == "mountain":
            # Reached only for OSM train routes that were overridden to mountain via
            # MOUNTAIN_PLACE_KEYWORDS (e.g. WAB Lauterbrunnen→Kleine Scheidegg).
            # These have no GTFS match in the mountain bucket but are real tourist railways.
            freq_score = 0.6
            stats["matched"] += 1
        else:
            freq_score = None   # unmatched → skip (don't draw)
            stats["unmatched"] += 1

        # Skip routes with no service on sample dates (freq_score == 0.0).
        # Mountain mode is exempt: seasonal railways may not run on our specific
        # sample date but are still worth showing (they get clamped to 0.4 below).
        if freq_score is None or (freq_score == 0.0 and mode != "mountain"):
            continue

        # Mountain railways are always worth showing; clamp to a visible minimum
        if mode == "mountain" and freq_score < 0.4:
            freq_score = 0.4

        color      = speed_to_color(mode, speed_kmh)
        width_base = freq_to_width_base(freq_score, mode)

        features.append({
            "type": "Feature",
            "geometry": feat["geometry"],
            "properties": {
                "osm_id":     props.get("osm_id"),
                "ref":        ref,
                "name":       props.get("name", ""),
                "operator":   operator,
                "mode":       mode,
                "freq_score": freq_score,
                "speed_kmh":  speed_kmh,
                "color":      color,
                "width_base": width_base,
                "gtfs_matched": True,
            },
        })

    # ── GTFS-first mountain processing ──────────────────────────────────────────
    # Every cable car / gondola / funicular in the timetable (GTFS route type 5/6/7)
    # gets a line on the map.  Use OSM route geometry when a matching relation exists
    # (matched by GTFS short_name == OSM ref); otherwise draw a straight-line segment
    # between the canonical GTFS stop coordinates.
    #
    # Source: _line_canonical_export[(ref, "mountain")] → list of canonical stop sequences,
    # one per ~40 km geographic cell.  This naturally deduplicates direction variants
    # (up/down) of the same gondola while preserving same-named lines in different cities.
    print("\nGTFS-first mountain processing...")
    n_gtfs_mountain = 0
    n_osm_shape = 0
    n_straight_line = 0

    # Track bboxes already committed per ref to suppress direction-variant duplicates.
    # Same ref, overlapping bbox → same physical cable car in the same place → skip.
    mountain_added_bboxes: dict = defaultdict(list)  # ref → [bbox, ...]

    for (ref, bucket), stop_list_candidates in _line_canonical_export.items():
        if bucket != "mountain":
            continue

        gtfs_entry = gtfs_index.get(("mountain", ref))
        if gtfs_entry is None:
            continue

        raw_freq   = gtfs_entry["raw_freq"]
        speed_kmh  = gtfs_entry["speed_kmh"]
        freq_score = compute_freq_score(raw_freq, "mountain")
        freq_score = max(freq_score, 0.4)  # seasonal railways may not run on sample dates

        # Each entry in stop_list_candidates is one geographic location for this ref.
        # Produce one map feature per location.
        for stop_list in stop_list_candidates:
            # Resolve stop coordinates
            stop_pts = []
            for stop_id, _arr, _dep in stop_list:
                c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                if c:
                    stop_pts.append(list(c))
            if len(stop_pts) < 2:
                continue

            stop_bbox = line_bbox(stop_pts)

            # Suppress direction duplicates: same ref + significantly overlapping bbox
            # → the same physical cable car running up vs. down.  Different cities sharing
            # the same ref will have non-overlapping bboxes and pass through.
            if any(_bbox_overlap_fraction(stop_bbox, prev) >= 0.5
                   for prev in mountain_added_bboxes[ref]):
                continue
            mountain_added_bboxes[ref].append(stop_bbox)

            osm_bbox = stop_bbox

            # Find best OSM geometry: ref match + at least one GTFS stop near OSM route
            best_osm_feat = None
            best_n_pts = 0
            for osm_feat, n_pts in osm_mountain_by_ref.get(ref, []):
                geom = osm_feat["geometry"]
                osm_pts = ([c for seg in geom["coordinates"] for c in seg]
                           if geom["type"] == "MultiLineString" else geom["coordinates"])
                osm_route_bbox = line_bbox(osm_pts)
                if any(stop_near_bbox(p[0], p[1], osm_route_bbox) for p in stop_pts):
                    if n_pts > best_n_pts:
                        best_n_pts = n_pts
                        best_osm_feat = osm_feat

            if best_osm_feat:
                geometry   = best_osm_feat["geometry"]
                osm_id     = best_osm_feat["properties"].get("osm_id")
                feat_name  = best_osm_feat["properties"].get("name", "") or ref
                operator   = best_osm_feat["properties"].get("operator", "")
                gtfs_stops = None   # OSM-shaped: existing line_stops.json mechanism handles stops
                n_osm_shape += 1
            else:
                # No OSM relation → straight line through GTFS stop coordinates.
                # Embed stop coords directly so 07_extract_stops.py can render them
                # without needing an osm_id key.
                geometry   = {"type": "LineString", "coordinates": stop_pts}
                osm_id     = None
                feat_name  = ref
                operator   = ""
                gtfs_stops = stop_pts   # [[lon,lat], ...]
                n_straight_line += 1

            color      = speed_to_color("mountain", speed_kmh)
            width_base = freq_to_width_base(freq_score, "mountain")

            props = {
                "osm_id":      osm_id,
                "ref":         ref,
                "name":        feat_name,
                "operator":    operator,
                "mode":        "mountain",
                "freq_score":  freq_score,
                "speed_kmh":   speed_kmh,
                "color":       color,
                "width_base":  width_base,
                "gtfs_matched": True,
            }
            if gtfs_stops is not None:
                props["gtfs_stops"] = gtfs_stops

            features.append({
                "type": "Feature",
                "geometry": geometry,
                "properties": props,
            })
            n_gtfs_mountain += 1
            stats["matched"] += 1

    print(f"  {n_gtfs_mountain} mountain lines: {n_osm_shape} with OSM shape, "
          f"{n_straight_line} straight-line fallback")

    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}))

    # Save stop coordinates per line (osm_id → [[lon,lat], ...]) for stop dot rendering
    line_stops_out = {}
    for feat in features:
        osm_id = str(feat["properties"]["osm_id"])
        ref    = feat["properties"]["ref"]
        mode   = feat["properties"]["mode"]
        bucket = MODE_TO_BUCKET.get(mode, "bus")
        ref_norm = ref.replace(" ", "")

        # GTFS lookup — mirror the same fallback cascade used in the main OSM loop
        # so that lines drawn via a fallback there also get stop coordinates here.
        matched_gtfs_ref: str | None = None

        gtfs = gtfs_index.get((bucket, ref))
        if gtfs: matched_gtfs_ref = ref
        if gtfs is None:
            for k_ref in [ref_norm, ref.upper(), ref.lower(), ref_norm.upper()]:
                cand = gtfs_index.get((bucket, k_ref))
                if cand:
                    gtfs = cand
                    matched_gtfs_ref = k_ref
                    break
        if gtfs is None:
            for lk in [(bucket, ref_norm), (bucket, ref_norm.upper())]:
                cand = gtfs_long_index.get(lk)
                if cand:
                    gtfs = cand
                    matched_gtfs_ref = ref_norm
                    break
        # First-word-of-name fallback: "R 311: Interlaken…" → try "R", "311"
        if gtfs is None:
            osm_name_prefix = feat["properties"].get("name", "").split(":")[0].strip()
            for token in osm_name_prefix.split():
                if token != ref and len(token) <= 6:
                    cand = gtfs_index.get((bucket, token)) or \
                           gtfs_index.get((bucket, token.upper()))
                    if cand:
                        gtfs = cand
                        matched_gtfs_ref = token if gtfs_index.get((bucket, token)) else token.upper()
                        break
        # Alpha-prefix fallback: "R43" → "R", "R44" → "R", etc.
        if gtfs is None:
            m = re.match(r'^([A-Za-z ]+)\d', ref)
            if m:
                alpha = m.group(1).strip()
                if alpha and alpha != ref:
                    cand = gtfs_index.get((bucket, alpha)) or \
                           gtfs_index.get((bucket, alpha.upper()))
                    if cand:
                        gtfs = cand
                        matched_gtfs_ref = alpha if gtfs_index.get((bucket, alpha)) else alpha.upper()

        if gtfs is None:
            if mode == "ferry":
                # No direct ref match (OSM ref=3310 ≠ GTFS short_name=7–22).
                # Collect all ferry pier stops from any GTFS ferry route whose stops
                # fall within this OSM route's bbox.
                geom = feat["geometry"]
                osm_pts = ([c for seg in geom["coordinates"] for c in seg]
                           if geom["type"] == "MultiLineString" else geom["coordinates"])
                bbox = line_bbox(osm_pts)
                seen_pos: set = set()
                pier_coords: list = []
                for (lk_ref, lk_bucket), lk_candidates in _line_canonical_export.items():
                    if lk_bucket != "ferry":
                        continue
                    for cand in lk_candidates:
                        for stop_id, _a, _d in cand:
                            c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                            if c and stop_near_bbox(c[0], c[1], bbox, margin=0.01):
                                key = (round(c[0], 4), round(c[1], 4))
                                if key not in seen_pos:
                                    seen_pos.add(key)
                                    pier_coords.append(list(c))
                if pier_coords:
                    line_stops_out[osm_id] = pier_coords
            continue

        # Compute OSM line bbox (needed for stop filtering and geo fallback)
        geom = feat["geometry"]
        if geom["type"] == "MultiLineString":
            osm_pts = [c for seg in geom["coordinates"] for c in seg]
        else:
            osm_pts = geom["coordinates"]
        bbox = line_bbox(osm_pts)

        # Reconstruct stop coords from canonical trip.
        # Try OSM ref variants first, then the matched GTFS short_name.
        canon = None
        ref_keys = [ref, ref_norm, ref.upper(), ref.lower(), ref_norm.upper()]
        if matched_gtfs_ref and matched_gtfs_ref not in ref_keys:
            ref_keys += [matched_gtfs_ref, matched_gtfs_ref.upper(), matched_gtfs_ref.lower()]
        for lk_ref in ref_keys:
            if (lk_ref, bucket) in _line_canonical_export:
                canon = _line_canonical_export[(lk_ref, bucket)]
                break

        # canon is a list of candidates (each a list of (stop_id, arr, dep) tuples)
        best_coords: list = []
        if canon:
            for candidate in canon:
                ccoords = []
                for stop_id, arr, dep in candidate:
                    c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                    if c and stop_near_bbox(c[0], c[1], bbox):
                        ccoords.append(list(c))
                if len(ccoords) > len(best_coords):
                    best_coords = ccoords

        # Geo-based fallback: triggers when (a) no canon found at all, (b) canon found
        # but its stops don't overlap this OSM feature's bbox, or (c) canonical stops
        # fail endpoint coverage — indicating the ref matched the wrong GTFS service
        # (e.g. SBB 'RE' for MGB 'RE41', or MGB 'R' for Gornergrat 'R48 (CC)').
        # For mountain-mode features, also search the "train" bucket since WAB/JB/MGB service
        # is carried as GTFS train type=2 routes under short_name "R".
        if not best_coords or not _covers_endpoints(osm_pts, best_coords):
            if mode == "ferry":
                # Ferry: collect ALL pier stops from any GTFS ferry route within the bbox,
                # deduped by position. OSM ref ≠ GTFS short_name so we can't ref-match.
                seen_pos: set = set()
                for (lk_ref, lk_bucket), lk_candidates in _line_canonical_export.items():
                    if lk_bucket != "ferry":
                        continue
                    for cand in lk_candidates:
                        for stop_id, _a, _d in cand:
                            c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                            if c and stop_near_bbox(c[0], c[1], bbox, margin=0.01):
                                key = (round(c[0], 4), round(c[1], 4))
                                if key not in seen_pos:
                                    seen_pos.add(key)
                                    best_coords.append(list(c))
            else:
                search_buckets = {bucket}
                if bucket == "mountain":
                    search_buckets.add("train")
                best_n = 1   # require at least 2 matching stops
                geo_best: list = []
                for (lk_ref, lk_bucket), lk_candidates in _line_canonical_export.items():
                    if lk_bucket not in search_buckets:
                        continue
                    for cand in lk_candidates:
                        ccoords = []
                        for stop_id, arr, dep in cand:
                            c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                            if c and stop_near_bbox(c[0], c[1], bbox):
                                ccoords.append(list(c))
                        if len(ccoords) > best_n:
                            best_n = len(ccoords)
                            geo_best = ccoords
                # Keep whichever result has more stops (geo may override a wrong
                # canonical match, or canonical may be kept if it was already better)
                if len(geo_best) > len(best_coords):
                    best_coords = geo_best

        if best_coords:
            line_stops_out[osm_id] = best_coords

    line_canonical_export = None  # free reference

    OUT_STOPS.write_text(json.dumps(line_stops_out))
    print(f"  Stop coords: {sum(len(v) for v in line_stops_out.values()):,} stops across {len(line_stops_out):,} lines → {OUT_STOPS}")

    print(f"\nResults:")
    print(f"  Drawn (matched):  {stats['matched']:,}")
    print(f"  Hidden (no GTFS): {stats['unmatched']:,}")
    print(f"  Excluded (coach): {stats['excluded']:,}")
    print(f"  Output:           {OUT}")

    mode_counts: dict = defaultdict(int)
    for f in features:
        mode_counts[f["properties"]["mode"]] += 1
    print("\nBy mode:")
    for m, c in sorted(mode_counts.items(), key=lambda x: -x[1]):
        print(f"  {m:<20} {c:>5}")

    scores = [f["properties"]["freq_score"] for f in features]
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
