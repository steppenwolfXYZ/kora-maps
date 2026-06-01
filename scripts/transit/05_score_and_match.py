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
from typing import Optional, NamedTuple

ROOT = Path(__file__).resolve().parents[2]
GTFS = ROOT / "data" / "gtfs"
OSM_ROUTES = ROOT / "data" / "osm" / "routes.geojson"
OUT = ROOT / "data" / "transit" / "transit_lines.geojson"
OUT_STOPS = ROOT / "data" / "transit" / "line_stops.json"
OUT_EXCLUDED = ROOT / "data" / "transit" / "sanity_excluded.json"
OUT_DROPPED = ROOT / "data" / "transit" / "main_loop_dropped.json"
OUT_GTFS_UNMATCHED = ROOT / "data" / "transit" / "gtfs_unmatched.json"

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

# Off-peak malus factors (multiplicative): low service = 10%, no service = 20% reduction.
MALUS_LOW = 0.10
MALUS_NO  = 0.20

# Minimum freq_score required to draw a line (all modes; mountain exempt).
MIN_FREQ_SCORE = 0.075

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

# Operators/networks to exclude entirely (long-distance commercial coaches).
# Checked against both the OSM `operator` tag and the `network` tag (lowercase).
# Flixbus subcontractors use their own company name in `operator` but "Flixbus" in `network`.
EXCLUDED_OPERATORS = {"flixbus", "flixcoach", "eurolines", "deinbus", "megabus", "ic bus",
                      "blablacar bus", "blablabus", "ouibus"}

# OSM route length threshold for bus → regional_bus classification.
# Using OSM line length (already computed, per-route, no GTFS cross-city confusion):
#   city buses typically < 12 km total; regional PostBus/rural typically > 12 km.
REGIONAL_BUS_MIN_LENGTH = 12.0   # km

# ── Mode classification ───────────────────────────────────────────────────────

def osm_to_mode(route_tag: str, ref: str, operator: str, length_km: float,
                network: str = ""):
    """Return mode string, or None to exclude this route entirely."""
    r = route_tag.lower()
    op = operator.lower()
    net = network.lower()

    # Exclude long-distance coaches — check both operator and network tags since
    # Flixbus subcontractors appear under their own name in operator but "Flixbus" in network.
    if any(x in op for x in EXCLUDED_OPERATORS) or any(x in net for x in EXCLUDED_OPERATORS):
        return None
    # Also exclude very long bus routes without a known operator (likely Flixbus variants)
    if r in ("bus", "coach") and length_km > 200:
        return None

    if r == "railway":
        return None   # OSM infrastructure track sections, not passenger services
    # Forchbahn (FB) is tagged light_rail in OSM but is a tram in GTFS (type=0, short_name="18")
    if r == "light_rail" and op == "fb":
        return "tram"
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


# ── Service area filter ───────────────────────────────────────────────────────
# Prefix-85 covers Switzerland + Liechtenstein. Some prefix-85 IDs are
# physically in Italy/Germany (SBB-operated border stations) — exclude them.
# A small set of non-85 IDs are foreign stations Swiss operators serve — include them.
_SERVICE_AREA_EXCLUDE: frozenset = frozenset({
    # Simplon south ramp (Italy)
    "8501952", "8501951", "8501950",
    # Bernina line Italy end
    "8509369", "8581990",
    # Lago Maggiore Italian shore
    "8505874", "8505861", "8505862",
    # Val Vigezzo / Ossola valley (SSIF/Centovalli Italy section)
    "8505599", "8505597", "8505588", "8505580", "8505590", "8505584",
    "8505578", "8505593", "8505594", "8505585", "8505589", "8505581",
    # German enclaves surrounded by Swiss territory
    "8503420", "8503421",
})
_SERVICE_AREA_INCLUDE: frozenset = frozenset({
    # Konstanz and surrounds (Thurbo/SBB cross-border DE)
    "8014586", "8014587", "8014481", "8014491",
    # Pougny-Chancy (Geneva area, French prefix)
    "8774538",
    # Delle (Jura border, French prefix)
    "8718444",
})

def is_in_service_area(stop_id: str) -> bool:
    sid = stop_id.split(":")[0]
    if sid in _SERVICE_AREA_INCLUDE:
        return True
    if sid in _SERVICE_AREA_EXCLUDE:
        return False
    return sid.startswith("85")


# ── Geometry helpers ──────────────────────────────────────────────────────────
def haversine_km(lon1, lat1, lon2, lat2) -> float:
    R = 6371
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi, dlam = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlam / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _min_dist_to_polyline_km(px: float, py: float, pts: list) -> float:
    """Minimum haversine distance (km) from point to the nearest vertex in pts."""
    min_d = float("inf")
    for p in pts:
        d = haversine_km(px, py, p[0], p[1])
        if d < min_d:
            min_d = d
            if min_d < 0.1:  # < 100 m — close enough, stop scanning
                break
    return min_d

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
            if sid.startswith("0000"):  # pseudo-stops (tunnel/track-section markers, not passenger stops)
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
    """Return {stop_id: (stop_name, parent_station)} for debug annotation."""
    meta = {}
    with open(GTFS / "stops.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sid = row["stop_id"]
            if sid.startswith("0000"):  # pseudo-stops (tunnel/track-section markers, not passenger stops)
                continue
            meta[sid] = (row.get("stop_name", ""), row.get("parent_station", ""))
            base = sid.split(":")[0]
            if base not in meta:
                meta[base] = meta[sid]
    return meta


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
                "agency_id": row.get("agency_id", ""),
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
                "agency_id": r.get("agency_id", ""),
            }
    return trips


class CanonEntry(NamedTuple):
    line_key: tuple        # (short_name, long_name, bucket)
    stops: list            # [(stop_id, arr, dep), ...]
    dir_aware: bool        # True when variants have genuinely different stop sets
    agency_id: str
    no_draw: Optional[str] # None = drawable; "low_frequency" = freq < MIN_FREQ_SCORE
    trip_group_id: int     # connected-component id within (long_norm, agency_id, bucket) partition

_line_canonical_export: dict = defaultdict(list)  # (short_name|long_norm, bucket) → [CanonEntry, ...]

# Maps GTFS bucket name to mode approximation used in compute_freq_score.
# "bus" bucket is approximated as "regional_bus" (lower maluses) — intentionally
# conservative; a city bus with sparse service might survive the filter here even
# though it would be dropped at draw time. Mountain bucket is exempt.
_BUCKET_MODE_APPROX = {
    "train": "train", "tram": "tram", "metro": "metro",
    "ferry": "ferry", "bus": "regional_bus", "regional_bus": "regional_bus",
}


def stream_stop_times(trips, stop_coords, svc_dates, trip_frequencies, stop_meta):
    """One streaming pass → raw trip counts + speed per line.

    Partitions trips by (long_name_norm or short_name fallback, agency_id, bucket)
    and within each partition merges trips that share ≥2 stops (using parent_station
    or base UIC as merged stop identity) into one connected component. Each component
    is one physical line (the trip_group). Canonical exports are then keyed by
    (line_key, trip_group_id) so e.g. SBB S3 in Zürich / Basel / Luzern stay separate.
    """
    global _line_canonical_export

    # Build stop-merge map: parent_station when non-empty, else the part of stop_id
    # before the first colon (base UIC). Collapses platforms of the same station.
    stop_merge: dict = {}
    for sid, (_name, parent) in stop_meta.items():
        stop_merge[sid] = parent if parent else sid.split(":")[0]

    print("  Streaming stop_times.txt (~1–2 min)...")

    # Raw trip counts per line: {line_key: {core_wd, eve_wd, we}}
    line_freq: dict = defaultdict(lambda: {"core_wd": 0, "eve_wd": 0, "we": 0})

    # Canonical trip (most stops) per line for speed/pair-freq computation
    line_canonical: dict = {}

    # Per-trip buffer for the post-stream trip-group phase. Carries everything the
    # grouping and per-group variant accumulators need so we don't re-stream.
    #   trip_id → (line_key, agency_id, weight, raw_variant_frozenset,
    #              merged_stop_frozenset, sequence_list)
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
    # Partition trips by (long_name_norm or short_name fallback, agency_id, bucket).
    # Within each partition, run union-find over distinct merged-stop patterns:
    # two patterns are connected iff they share ≥2 merged stop identities. Each
    # connected component is one physical line (trip_group).
    print("  Partitioning trips and computing trip-groups...")

    partition_trips: dict = defaultdict(list)
    for tid, (lk, aid, _w, _rv, _ms, _seq) in trip_buf.items():
        sn, ln, bkt = lk
        ln_norm = ln.replace(" ", "").lower()
        partition_str = ln_norm if ln_norm else sn.replace(" ", "").lower()
        partition_trips[(partition_str, aid, bkt)].append(tid)

    trip_group: dict = {}   # trip_id → trip_group_id (unique within its partition)
    n_groups_total = 0
    for tids in partition_trips.values():
        # Deduplicate trips to merged-stop patterns first; union-find runs on patterns,
        # which is O(P²) with set-intersection — tractable even for big partitions
        # because trips with the same stop set collapse to one pattern.
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
    # These replace the (line_key, geo_bucket)-keyed dicts. Same structure, new key.
    line_canonical_tg_stops: dict = {}   # tg_key → [{"stop_count","stops"}, …] desc
    line_variant_counts: dict = defaultdict(lambda: defaultdict(int))  # tg_key → {raw_variant → weighted_count}
    line_variant_sequences: dict = {}    # (tg_key, raw_variant) → representative sequence
    line_canonical_tg_agency: dict = {}  # tg_key → agency_id (first seen wins)

    for tid, (lk, aid, weight, raw_variant, _ms, sequence) in trip_buf.items():
        tg = trip_group.get(tid)
        if tg is None:
            continue
        tg_key = (lk, tg)
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

    # Remove rare stop sets (< 10% of trips) from both source dicts so that garage runs
    # and other infrequent variants never surface as stop candidates in section 1 or 2.
    # If no variant clears 10% (many roughly-equal stopping patterns), fall back to 5%.
    # If still nothing clears 5%, keep all variants rather than discarding everything.
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

    # Compute the set of line_keys whose frequency is below MIN_FREQ_SCORE.
    # These are flagged no_draw="low_frequency" on their CanonEntry objects rather than
    # excluded from the pool entirely — the 4-loop can still settle on them and the draw
    # gate decides after matching rather than before.
    # "bus" bucket approximated as "regional_bus" — intentionally conservative (see transit.md).
    # Mountain bucket and CC/train (seasonal rack railways) are exempt.
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

    # Build canonical export: all unique stop sets per (line_key, trip_group_id).
    # This gives separate candidates for "S3 Zürich", "S3 Basel" and "S3 Luzern" even
    # though they share the same GTFS line_key = ("S3", "S 3", "train"). It also
    # preserves minority services within a single trip group (e.g. Maienfeld Bus 14
    # with 5 stops vs. Feldkirch Bus 14 with 30 stops, if they share a trunk that
    # connects them — though typically distinct corridors land in distinct groups).
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

    # Filtered union candidate: union of stops from variants that represent ≥10% of trips
    # for this (line, trip_group). Prevents rare detour/construction trips from leaking
    # their stops into the main stop list (e.g. Tram 9 Hasler at 1.8% of trips).
    #
    # When qualifying variants are genuinely divergent (different intermediate stops per
    # direction, e.g. bus 17 outbound via Kaufmännischer Verband vs inbound via Brunnhof),
    # we emit each as a separate direction-aware candidate so the assignment loop can pick
    # the one matching the OSM line's direction.
    #
    # Detection: a variant is "maximal" if no other qualifying variant is a proper superset
    # of it.  If only one maximal exists, all others are short-turn/partial trips of the same
    # route (e.g. S44 Fribourg→Bern ⊆ full Fribourg→Biel) → safe to union.  If two or more
    # maximals exist, the routes genuinely fork → per-variant with direction filter.
    for (line_key, tg_id), variant_counts in line_variant_counts.items():
        short_name, long_name, bucket = line_key
        qualifying = list(variant_counts.items())
        if not qualifying:
            continue
        long_norm = long_name.replace(" ", "")
        agency_id = line_canonical_tg_agency.get((line_key, tg_id), "")
        no_draw = "low_frequency" if line_key in low_freq_keys else None
        unique_stop_sets = {v for v, _ in qualifying}
        union_sids: set = set()
        for v in unique_stop_sets:
            union_sids.update(v)
        union_cand = [(sid, 0, 0) for sid in union_sids]
        maximal_variants = [
            v for v in unique_stop_sets
            if not any(v < other for other in unique_stop_sets)
        ]
        is_truly_divergent = len(maximal_variants) > 1
        if not is_truly_divergent:
            _line_canonical_export[(short_name, bucket)].append(
                CanonEntry(line_key, union_cand, False, agency_id, no_draw, tg_id))
            if long_norm and long_norm != short_name:
                _line_canonical_export[(long_norm, bucket)].append(
                    CanonEntry(line_key, union_cand, False, agency_id, no_draw, tg_id))
        else:
            for v, _ in qualifying:
                tg_key = (line_key, tg_id)
                var_stops = line_variant_sequences.get((tg_key, v))
                if not var_stops:
                    continue
                _line_canonical_export[(short_name, bucket)].append(
                    CanonEntry(line_key, var_stops, True, agency_id, no_draw, tg_id))
                if long_norm and long_norm != short_name:
                    _line_canonical_export[(long_norm, bucket)].append(
                        CanonEntry(line_key, var_stops, True, agency_id, no_draw, tg_id))

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
    Off-peak malus applied multiplicatively: low service −10%, no service −20% per dimension.
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
    if eve_trips >= 2:
        eve_factor = MALUS_LOW if EVENING_MINUTES / eve_trips > low_eve else 0.0
    elif eve_trips == 0:
        eve_factor = MALUS_NO
    else:
        eve_factor = 0.0

    # Weekend malus
    low_we = LOW_WE_HEADWAY.get(mode, 60)
    if we_trips >= 2:
        we_factor = MALUS_LOW if WEEKEND_MINUTES / we_trips > low_we else 0.0
    elif we_trips == 0:
        we_factor = MALUS_NO
    else:
        we_factor = 0.0

    final = core_score * (1 - eve_factor) * (1 - we_factor)
    return round(max(0.0, min(1.0, final)), 3)


def line_bbox(coords):
    """Return (min_lon, min_lat, max_lon, max_lat) for a list of [lon, lat] points."""
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lons), min(lats), max(lons), max(lats)


def stop_near_bbox(lon, lat, bbox, margin=0.02):
    """True if (lon, lat) is within bbox expanded by margin degrees (~2km at CH latitude)."""
    return (bbox[0] - margin <= lon <= bbox[2] + margin and
            bbox[1] - margin <= lat <= bbox[3] + margin)


def build_sub_bboxes(pts: list, segment_km: float = 20.0) -> list:
    """
    Split a polyline into sub-bboxes of at most segment_km each.
    Returns a list of (min_lon, min_lat, max_lon, max_lat) tuples.
    Consecutive segments share their boundary point so no stretch is uncovered.

    Using sub-bboxes instead of a single full bbox prevents long-distance lines
    (e.g. Glacier Express, full bbox ≈ all of eastern Switzerland) from absorbing
    stops of unrelated regional lines that happen to lie entirely inside the
    large bounding box but are 30–40 km from the actual geometry.
    """
    if not pts:
        return []
    sub_bboxes = []
    seg_pts = [pts[0]]
    seg_dist = 0.0
    for i in range(1, len(pts)):
        seg_dist += haversine_km(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
        seg_pts.append(pts[i])
        if seg_dist >= segment_km:
            sub_bboxes.append(line_bbox(seg_pts))
            seg_pts = [pts[i]]   # next segment starts from the current point
            seg_dist = 0.0
    if seg_pts:
        sub_bboxes.append(line_bbox(seg_pts))
    return sub_bboxes


ENDPOINT_THRESHOLD_KM = 5.0
GEO_SORT_ENDPOINT_KM = 0.5  # tighter threshold for geo-fallback candidate ranking only

GENERIC_GTFS_PREFIXES = frozenset({
    "S", "R", "RE", "IR", "IC", "EC", "ICE", "TGV", "RB",
    "N", "SN", "NJ", "RJX", "TER", "EV", "EXT", "PE",
})

def _count_endpoints_covered(osm_pts: list, stops: list, threshold_km: float = GEO_SORT_ENDPOINT_KM,
                              osm_stop_nodes: list = [], osm_segs: list = None) -> int:
    """Return how many OSM endpoints (0, 1, or 2) have a stop within threshold_km.
    Default (0.5 km) is used to rank geo-fallback candidates.
    Called with ENDPOINT_THRESHOLD_KM (5 km) as the canonical-stop gate.
    Priority: (1) osm_stop_nodes[0]/[-1] — actual passenger stop positions, most
    accurate; (2) all segment start/end points for MultiLineString geometries —
    correct for Y-shapes and avoids relying on flattened-array order; (3) osm_pts[0]
    and osm_pts[-1] as final fallback."""
    if not stops or len(osm_pts) < 2:
        return 2  # can't determine — don't penalise
    if len(osm_stop_nodes) >= 2:
        start = osm_stop_nodes[0][:2]
        end   = osm_stop_nodes[-1][:2]
        near_start = any(haversine_km(s[0], s[1], start[0], start[1]) <= threshold_km for s in stops)
        near_end   = any(haversine_km(s[0], s[1], end[0],   end[1])   <= threshold_km for s in stops)
    elif osm_segs and len(osm_segs) > 1:
        # MultiLineString: check GTFS first/last stop against all segment endpoints.
        all_eps = [ep for seg in osm_segs for ep in (seg[0], seg[-1])]
        near_start = any(haversine_km(stops[0][0], stops[0][1], ep[0], ep[1]) <= threshold_km
                         for ep in all_eps)
        near_end   = any(haversine_km(stops[-1][0], stops[-1][1], ep[0], ep[1]) <= threshold_km
                         for ep in all_eps)
    else:
        start, end = osm_pts[0], osm_pts[-1]
        near_start = any(haversine_km(s[0], s[1], start[0], start[1]) <= threshold_km for s in stops)
        near_end   = any(haversine_km(s[0], s[1], end[0],   end[1])   <= threshold_km for s in stops)
    return int(near_start) + int(near_end)


def _re_to_r_ref(ref_norm: str) -> Optional[str]:
    """RE{n} ↔ R{n}: MGB trains use 'R 41' as GTFS long_name but 'RE41' in OSM."""
    m = re.match(r'^RE(\d+)$', ref_norm, re.IGNORECASE)
    return ('R' + m.group(1)) if m else None


def _norm_stop_name(name: str) -> str:
    """Normalise a stop name for name comparison (lowercase, strip station suffixes)."""
    name = name.lower().strip()
    name = re.sub(r',\s*(bahnhof|bhf|hbf|hb|bf|gare|station)\s*$', '', name)
    name = re.sub(r'\s+(bahnhof|hbf|hb|bhf|bf|gare|station)\s*$', '', name)
    name = re.sub(r'\s*\(.*?\)\s*$', '', name)  # strip trailing "(Hbf)" etc.
    return name.strip()


def _passes_geo_sanity(
    osm_pts: list,
    ccoords: list,
    stop_meta: dict,
    osm_from: str = "",
    osm_to: str = "",
    osm_stop_nodes: list = [],
    osm_line_km: float = 0.0,
    cand_full_density: float = 0.0,
    skip_upper_density: bool = False,
) -> bool:
    """Return True if a geo-fallback candidate is a plausible match for the OSM line.

    Checks are ordered cheapest-first; returns True on the first passing check so
    later (slower) checks are skipped as soon as one piece of evidence is found.

    Check 1 — OSM stop names vs GTFS stop names: do enough OSM stop node names appear in the GTFS candidate?
    Check 2 — GTFS stops → OSM geometry: are 4/5 evenly-spaced GTFS stops within 200 m of the OSM line?
    Check 3 — OSM stops → GTFS stops: are 4/5 evenly-spaced OSM stop nodes within 200 m of any GTFS stop?
    """
    if len(ccoords) < 2 or len(osm_pts) < 2:
        return False

    # Check 1: OSM stop names vs GTFS candidate stop names — O(N_osm + N_gtfs), pure string ops
    # For each OSM stop node (name extracted by 04_extract_osm.py), checks whether that
    # normalised name appears in the GTFS candidate's stop name set (whole-token equality,
    # not substring). Requires 90% of OSM stop nodes (minimum 2) to match — a partial
    # corridor match (e.g. R2 Landquart–Davos matching RE4 Landquart–Scuol) scores ~70%
    # and is rejected, while a correct full-route match typically scores 100%.
    # Individual comparisons are skipped when either side is < 2 chars after normalisation.
    gtfs_names = set()
    for s in ccoords:
        sid = s[2] if len(s) > 2 else None
        if not sid:
            continue
        sname = _norm_stop_name(stop_meta.get(sid, ("", ""))[0])
        if sname and len(sname) >= 2:
            gtfs_names.add(sname)
    threshold = max(2, round(len(osm_stop_nodes) * 0.9))
    matches = 0
    for node in osm_stop_nodes:
        nname = _norm_stop_name(node[2] if len(node) > 2 else "")
        if not nname or len(nname) < 2:
            continue
        if nname in gtfs_names:
            matches += 1
    if matches >= threshold:
        return True

    # Check 2: density gate + GTFS stops → OSM geometry proximity
    # Density gate (cheap, runs first): if OSM has stop nodes, compare stops/km.
    # A candidate with more than 2× or less than 0.5× the OSM stop density is
    # almost certainly a wrong route (e.g. dense regional S-Bahn matching sparse EC).
    density_ok = True
    if len(osm_stop_nodes) >= 2 and osm_line_km > 0:
        osm_density = len(osm_stop_nodes) / osm_line_km
        if cand_full_density > 0:
            # Use precomputed full-trip density — avoids the bbox-filtered span
            # making a long-distance train look dense when only its short overlap
            # with the OSM route is measured.
            ratio = cand_full_density / osm_density if osm_density > 0 else 1.0
        else:
            # Fallback: compute from bbox-filtered ccoords (union candidates have no span).
            cand_span_km = sum(
                haversine_km(ccoords[i][0], ccoords[i][1], ccoords[i+1][0], ccoords[i+1][1])
                for i in range(len(ccoords) - 1)
            )
            ratio = (len(ccoords) / cand_span_km) / osm_density if cand_span_km > 0 and osm_density > 0 else 1.0
        # Regional buses: GTFS maps every stop, OSM often only maps interchanges.
        # Only apply the lower bound (candidate too sparse = wrong mode); the upper
        # bound would incorrectly reject a dense PostAuto route against a sparse OSM relation.
        density_ok = (ratio >= 0.5) if skip_upper_density else (0.5 <= ratio <= 2.0)

    # Proximity check: 4/5 evenly-spaced GTFS stops within 100 m of OSM polyline.
    if density_ok:
        _k2 = min(5, len(ccoords))
        sampled_gtfs = [ccoords[round(i * (len(ccoords) - 1) / max(1, _k2 - 1))] for i in range(_k2)]
        close2 = sum(
            1 for s in sampled_gtfs
            if _min_dist_to_polyline_km(s[0], s[1], osm_pts) <= 0.1
        )
        if close2 * 5 >= len(sampled_gtfs) * 4:  # ≥ 4/5
            return True

    # Check 3: OSM stops → GTFS stops — O(6 × N_gtfs_stops)
    # Sample 6 evenly-spaced OSM stop nodes (always including first and last);
    # require 5/6 within 200 m of any GTFS stop.
    if osm_stop_nodes and len(osm_stop_nodes) >= 2:
        _k3 = min(6, len(osm_stop_nodes))
        sampled_osm = [osm_stop_nodes[round(i * (len(osm_stop_nodes) - 1) / max(1, _k3 - 1))] for i in range(_k3)]
        close3 = sum(
            1 for p in sampled_osm
            if any(haversine_km(p[0], p[1], s[0], s[1]) <= 0.2 for s in ccoords)
        )
        if close3 * 6 >= len(sampled_osm) * 5:  # ≥ 5/6
            return True

    return False


def _lookup_canonical_stops(
    ref: str,
    ref_norm: str,
    matched_gtfs_ref: Optional[str],
    bucket: str,
    osm_pts: list,
    osm_span_km: float,
    osm_from: str,
    osm_to: str,
    stop_coords: dict,
    stop_meta: dict,
    sub_bboxes: list,
    osm_stop_nodes: list = [],
    osm_line_km: float = 0.0,
    skip_upper_density: bool = False,
) -> tuple[list, bool, Optional[tuple]]:
    """Look up canonical stops for an OSM line and apply the name-fallback sanity check.

    Returns (best_coords, used_name_fallback, best_line_key_full).  best_coords is empty if
    no canonical was found or if a name-fallback canonical failed the geo sanity check
    (Trigger 1).  best_line_key_full is (short_name, long_name, bucket, agency_id, trip_group_id)
    of the winning candidate (None if nothing was found).
    Used by both the main pipeline and the diagnostic script so they share identical logic.
    """
    osm_start = osm_pts[0]
    osm_end   = osm_pts[-1]

    exact_ref_keys = [ref, ref_norm, ref.upper(), ref.lower(), ref_norm.upper()]
    fallback_ref_keys: list = []
    if matched_gtfs_ref and matched_gtfs_ref not in exact_ref_keys:
        fallback_ref_keys = [matched_gtfs_ref, matched_gtfs_ref.upper(), matched_gtfs_ref.lower()]

    used_name_fallback = False
    canon = None
    canon_gtfs_ref: Optional[str] = None
    for i, lk_ref in enumerate(exact_ref_keys + fallback_ref_keys):
        if (lk_ref, bucket) in _line_canonical_export:
            canon = _line_canonical_export[(lk_ref, bucket)]
            used_name_fallback = (i >= len(exact_ref_keys))
            canon_gtfs_ref = lk_ref
            break

    best_coords: list = []
    best_candidate: list = []
    best_line_key_full: Optional[tuple] = None
    if canon:
        for entry in canon:
            canon_line_key, candidate, dir_aware, agency_id, tg_id = (
                entry.line_key, entry.stops, entry.dir_aware, entry.agency_id, entry.trip_group_id
            )
            if dir_aware and osm_span_km >= 1.0 and candidate:
                first_sid = candidate[0][0]
                first_c = stop_coords.get(first_sid) or stop_coords.get(first_sid.split(":")[0])
                if first_c:
                    d_to_start = haversine_km(first_c[0], first_c[1], osm_start[0], osm_start[1])
                    d_to_end   = haversine_km(first_c[0], first_c[1], osm_end[0],   osm_end[1])
                    if d_to_end < d_to_start * 0.5:
                        continue  # candidate runs reverse of this OSM line
            ccoords = []
            for stop_id, _arr, _dep in candidate:
                if not is_in_service_area(stop_id):
                    continue
                c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                if c and any(stop_near_bbox(c[0], c[1], sb) for sb in sub_bboxes):
                    ccoords.append([c[0], c[1], stop_id])
            if len(ccoords) > len(best_coords):
                best_coords = ccoords
                best_candidate = candidate
                best_line_key_full = (*canon_line_key, agency_id, tg_id)

    # Trigger 1: if canonical came from a name fallback, sanity-check before trusting it.
    if best_coords and used_name_fallback:
        _cand_coords = [c for sid, *_ in best_candidate
                        if is_in_service_area(sid)
                        and (c := stop_coords.get(sid) or stop_coords.get(sid.split(":")[0]))]
        _span = sum(haversine_km(_cand_coords[i][0], _cand_coords[i][1],
                                  _cand_coords[i+1][0], _cand_coords[i+1][1])
                    for i in range(len(_cand_coords) - 1))
        _full_density = len(_cand_coords) / _span if _span > 0 else 0.0
        if not _passes_geo_sanity(osm_pts, best_coords, stop_meta, osm_from, osm_to, osm_stop_nodes, osm_line_km,
                                   cand_full_density=_full_density, skip_upper_density=skip_upper_density):
            best_coords = []
            best_line_key_full = None

    return best_coords, used_name_fallback, best_line_key_full


def _loop_keys(loop_level: int, ref: str, ref_norm: str, osm_name: str) -> list:
    """Return _line_canonical_export key list for loops 1, 2, or 3 (loop 4 uses all)."""
    is_gen = lambda k: k.upper() in GENERIC_GTFS_PREFIXES

    if loop_level == 1:
        # Long_norm first (= ref_norm), then case variants. Generics excluded.
        keys = [ref_norm, ref_norm.upper(), ref, ref.upper(), ref.lower()]
        return [k for k in dict.fromkeys(keys) if not is_gen(k)]

    if loop_level == 2:
        # String tricks (RE↔R, name-prefix, alpha-prefix). Generics excluded.
        keys = []
        r_ref = _re_to_r_ref(ref_norm)
        if r_ref:
            for k in [r_ref, r_ref.upper()]:
                if not is_gen(k):
                    keys.append(k)
        # Name-prefix: try normalized full segment before ":", then individual tokens.
        segment = osm_name.split(":")[0].strip()
        seg_norm = segment.replace(" ", "")
        if seg_norm and seg_norm != ref_norm and len(seg_norm) <= 12 and not is_gen(seg_norm):
            keys.extend([seg_norm, seg_norm.upper()])
        for token in segment.split():
            if token == ref or len(token) > 6:
                continue
            if not is_gen(token):
                keys.extend([token, token.upper()])
        # Alpha-prefix (e.g. "RE4" → "RE") — skip if generic; Loop 3 handles those.
        m = re.match(r'^([A-Za-z ]+)\d', ref)
        if m:
            alpha = m.group(1).strip()
            if alpha and alpha != ref and not is_gen(alpha):
                keys.extend([alpha, alpha.upper()])
        return list(dict.fromkeys(keys))

    if loop_level == 3:
        # Only generic-prefix keys (deferred from loops 1 and 2).
        keys = []
        for k in [ref, ref_norm, ref.upper(), ref.lower()]:
            if is_gen(k):
                keys.append(k)
        m = re.match(r'^([A-Za-z ]+)\d', ref)
        if m:
            alpha = m.group(1).strip()
            if alpha and alpha != ref and is_gen(alpha):
                keys.extend([alpha, alpha.upper()])
        for token in osm_name.split(":")[0].strip().split():
            if token == ref or len(token) > 6:
                continue
            if is_gen(token):
                keys.extend([token, token.upper()])
        return list(dict.fromkeys(keys))

    return []  # loop 4 uses all of _line_canonical_export


def _stop_candidates(
    keys: list,
    bucket: str,
    sub_bboxes: list,
    osm_pts: list,
    osm_stop_nodes: list,
    osm_segs,
    stop_coords: dict,
    osm_span_km: float,
) -> list:
    """Collect scored stop candidates from _line_canonical_export for the given keys.

    Returns list sorted by (-bbox_score, -ep_0.5km, -n_stops).
    Each element: (bbox_score, ep_0_5km, ccoords, full_density, line_key_full, lk_ref, no_draw)
    """
    result = []
    osm_start = osm_pts[0]
    osm_end   = osm_pts[-1]
    for lk_ref in keys:
        for entry in _line_canonical_export.get((lk_ref, bucket), []):
            line_key, cand, dir_aware, agency_id, no_draw, tg_id = (
                entry.line_key, entry.stops, entry.dir_aware,
                entry.agency_id, entry.no_draw, entry.trip_group_id,
            )
            if not cand:
                continue
            if dir_aware and osm_span_km >= 1.0:
                fc0 = stop_coords.get(cand[0][0]) or stop_coords.get(cand[0][0].split(":")[0])
                if fc0:
                    d_s = haversine_km(fc0[0], fc0[1], osm_start[0], osm_start[1])
                    d_e = haversine_km(fc0[0], fc0[1], osm_end[0],   osm_end[1])
                    if d_e < d_s * 0.5:
                        continue
            ccoords: list = []
            for stop_id, _a, _d in cand:
                if not is_in_service_area(stop_id):
                    continue
                c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                if c and any(stop_near_bbox(c[0], c[1], sb) for sb in sub_bboxes):
                    ccoords.append([c[0], c[1], stop_id])
            if len(ccoords) < 2:
                continue
            fc = [c for sid, *_ in cand
                  if is_in_service_area(sid)
                  and (c := stop_coords.get(sid) or stop_coords.get(sid.split(":")[0]))]
            if not fc:
                continue
            bbox_score = len(ccoords) / len(fc)
            sp = sum(haversine_km(fc[i][0], fc[i][1], fc[i+1][0], fc[i+1][1])
                     for i in range(len(fc) - 1))
            full_density = len(fc) / sp if sp > 0 else 0.0
            ep_0_5 = _count_endpoints_covered(
                osm_pts, ccoords, GEO_SORT_ENDPOINT_KM, osm_stop_nodes, osm_segs
            )
            sn, ln, bkt = line_key
            result.append((bbox_score, ep_0_5, ccoords, full_density, (sn, ln, bkt, agency_id, tg_id), lk_ref, no_draw))
    result.sort(key=lambda x: (-x[0], -x[1], -len(x[2])))
    return result


def _try_assign(
    candidates: list,
    cap: Optional[int],
    loop_level: int,
    osm_pts: list,
    osm_stop_nodes: list,
    osm_segs,
    osm_line_km: float,
    osm_from: str,
    osm_to: str,
    stop_meta: dict,
    skip_upper_density: bool,
) -> Optional[dict]:
    """Try candidates in ranking order. Return the first settled entry dict or None.

    Two passes: drawable candidates first, no_draw candidates as fallback.
    This ensures a drawable GTFS match at any rank beats a no_draw match.
    """
    pool = candidates[:cap] if cap else candidates
    drawable = [c for c in pool if c[6] is None]
    no_draw_pool = [c for c in pool if c[6] is not None]
    for subset in (drawable, no_draw_pool):
        for bbox_score, ep_0_5, ccoords, full_density, line_key_full, lk_ref, no_draw in subset:
            ep_5 = _count_endpoints_covered(
                osm_pts, ccoords, ENDPOINT_THRESHOLD_KM, osm_stop_nodes, osm_segs
            )
            if ep_5 == 0:
                continue
            skip_sanity = (loop_level == 1 and ep_5 == 2) or (loop_level == 2 and ep_0_5 == 2)
            if not skip_sanity:
                if not _passes_geo_sanity(
                    osm_pts, ccoords, stop_meta, osm_from, osm_to, osm_stop_nodes, osm_line_km,
                    cand_full_density=full_density, skip_upper_density=skip_upper_density,
                ):
                    continue
            sn, ln, bkt, aid, tg_id = line_key_full
            return {"stops": ccoords, "_line_key_full": line_key_full, "_bucket": bkt, "_no_draw": no_draw}
    return None


def _run_stop_loop(
    loop_level: int,
    osm_ids: list,
    route_info: dict,
    stop_coords: dict,
    stop_meta: dict,
) -> tuple:
    """One batch stop-assignment pass (loop_level 1–4).

    Returns (settled, remaining_ids, excl_ids, excl_details):
      settled       – {osm_id: entry dict}
      remaining_ids – [osm_id, ...]  returned to pool for the next loop
      excl_ids      – [osm_id, ...]  loop-4 only: exhausted all candidates
      excl_details  – [{...}, ...]   metadata for sanity_excluded.json
    """
    settled: dict = {}
    remaining: list = []
    excl_ids: list = []
    excl_dets: list = []
    cap = 50 if loop_level >= 3 else None
    n_total = len(osm_ids)

    for idx, osm_id in enumerate(osm_ids):
        if loop_level == 4 and (idx % 50 == 0 or idx == n_total - 1):
            pct = (idx + 1) * 100 // n_total if n_total else 100
            print(
                f"\r  Loop 4 geo-fallback: {idx+1}/{n_total} "
                f"[settled {len(settled)}, excl {len(excl_ids)}] {pct}%    ",
                end="", flush=True,
            )
        info      = route_info[osm_id]
        ref       = info["ref"]
        ref_norm  = info["ref_norm"]
        bucket    = info["bucket"]
        mode      = info["mode"]
        osm_pts   = info["osm_pts"]
        sub_bboxes= info["sub_bboxes"]
        osm_sn    = info["osm_stop_nodes"]
        osm_segs  = info["_osm_segs"]
        osm_lkm   = info["osm_line_km"]
        osm_from  = info["osm_from"]
        osm_to    = info["osm_to"]
        osm_name  = info["osm_name"]
        osm_span  = info["osm_span_km"]
        s_upper   = info["skip_upper_density"]
        feat      = info["feat"]

        if loop_level <= 3:
            keys = _loop_keys(loop_level, ref, ref_norm, osm_name)
            if not keys:
                remaining.append(osm_id)
                continue
            candidates = _stop_candidates(
                keys, bucket, sub_bboxes, osm_pts, osm_sn, osm_segs, stop_coords, osm_span,
            )
        else:
            # Loop 4: geo-fallback — score all candidates in bucket.
            # Phase 1: collect by bbox-score only (cheap); cap before computing
            # ep_0_5 and full_density (both O(n_stops) haversine) to avoid
            # O(n_routes × n_all_gtfs_candidates × n_stops) blowup.
            search_buckets = {bucket}
            if bucket == "mountain":
                search_buckets.add("train")
            # Overall OSM bbox expanded by ~100 km for cheap candidate pre-filter.
            _m = 0.9
            osm_bbox = (
                min(p[0] for p in osm_pts) - _m,
                min(p[1] for p in osm_pts) - _m,
                max(p[0] for p in osm_pts) + _m,
                max(p[1] for p in osm_pts) + _m,
            )
            raw: list = []
            for (lk_ref, lk_bucket), lk_cands in _line_canonical_export.items():
                if lk_bucket not in search_buckets:
                    continue
                for entry in lk_cands:
                    line_key, cand, agency_id, tg_id = (
                        entry.line_key, entry.stops, entry.agency_id, entry.trip_group_id,
                    )
                    if not cand:
                        continue
                    # Coarse pre-filter: skip if first in-service-area stop is outside expanded bbox.
                    first_c = None
                    for sid, *_ in cand:
                        if is_in_service_area(sid):
                            first_c = stop_coords.get(sid) or stop_coords.get(sid.split(":")[0])
                            if first_c:
                                break
                    if first_c and not (osm_bbox[0] <= first_c[0] <= osm_bbox[2] and
                                        osm_bbox[1] <= first_c[1] <= osm_bbox[3]):
                        continue
                    ccoords: list = []
                    for stop_id, _a, _d in cand:
                        if not is_in_service_area(stop_id):
                            continue
                        c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                        if c and any(stop_near_bbox(c[0], c[1], sb) for sb in sub_bboxes):
                            ccoords.append([c[0], c[1], stop_id])
                    if len(ccoords) < 2:
                        continue
                    score = len(ccoords) / len(cand)
                    if score < 0.5:
                        continue
                    raw.append((score, ccoords, cand, line_key, agency_id, tg_id, lk_ref, entry.no_draw))
            raw.sort(key=lambda x: (-x[0], -len(x[1])))
            # Phase 2: compute ep_0_5 and full_density only for the top-cap candidates.
            candidates = []
            for score, ccoords, cand, line_key, agency_id, tg_id, lk_ref, no_draw in raw[:cap]:
                fc = [c for sid, *_ in cand
                      if is_in_service_area(sid)
                      and (c := stop_coords.get(sid) or stop_coords.get(sid.split(":")[0]))]
                sp = sum(haversine_km(fc[i][0], fc[i][1], fc[i+1][0], fc[i+1][1])
                         for i in range(len(fc) - 1))
                full_density = len(fc) / sp if sp > 0 else 0.0
                ep_0_5 = _count_endpoints_covered(
                    osm_pts, ccoords, GEO_SORT_ENDPOINT_KM, osm_sn, osm_segs
                )
                sn, ln, bkt = line_key
                candidates.append(
                    (score, ep_0_5, ccoords, full_density, (sn, ln, bkt, agency_id, tg_id), lk_ref, no_draw)
                )
            candidates.sort(key=lambda x: (-x[0], -x[1], -len(x[2])))

        result = _try_assign(
            candidates, cap, loop_level,
            osm_pts, osm_sn, osm_segs, osm_lkm, osm_from, osm_to, stop_meta, s_upper,
        )

        if result:
            result["osm_ref"] = ref
            settled[osm_id] = result
        elif loop_level == 4:
            # Mountain rack railway terminal-name fallback when geo also fails.
            if mode == "mountain":
                norm_from = _norm_stop_name(osm_from)
                norm_to   = _norm_stop_name(osm_to)
                bbox = line_bbox(osm_pts)
                term_stops: list = []
                seen_tc: set = set()
                for _sid, (_sname, _parent) in stop_meta.items():
                    _ns = _norm_stop_name(_sname)
                    if not _ns:
                        continue
                    if not (
                        (norm_from and len(norm_from) >= 4 and _ns == norm_from) or
                        (norm_to   and len(norm_to)   >= 4 and _ns == norm_to)
                    ):
                        continue
                    _tc = stop_coords.get(_sid) or stop_coords.get(_sid.split(":")[0])
                    if not _tc or not stop_near_bbox(_tc[0], _tc[1], bbox, margin=0.05):
                        continue
                    _key = (round(_tc[0], 3), round(_tc[1], 3))
                    if _key in seen_tc:
                        continue
                    seen_tc.add(_key)
                    term_stops.append([_tc[0], _tc[1], _sid])
                if len(term_stops) >= 2:
                    settled[osm_id] = {
                        "osm_ref": ref, "stops": term_stops,
                        "_line_key_full": (ref, ref, bucket, "", 0), "_bucket": bucket, "_no_draw": None,
                    }
                    continue
            excl_ids.append(osm_id)
            excl_dets.append({
                "osm_id": osm_id,
                "ref":    ref,
                "mode":   mode,
                "name":   osm_name,
            })
        else:
            remaining.append(osm_id)

    if loop_level == 4:
        print("\r" + " " * 72 + "\r", end="", flush=True)  # erase progress line
    return settled, remaining, excl_ids, excl_dets


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


def _group_reassign_stops(
    group_osm_ids: list,
    line_key_full: tuple,
    bucket: str,
    geom_by_id: dict,
    stop_coords: dict,
) -> dict:
    """Reassign stops for a group of OSM relations sharing the same _line_key_full.

    Inclusion: a GTFS stop qualifies if it is near ANY relation in the group
    (uses each relation's sub-bboxes). Placement: the stop is assigned to the
    closest relation by polyline distance, plus any relation within
    max(d_min + 50 m, d_min * 1.1) — so shared stations appear on all routes
    that genuinely pass through them.
    Returns {osm_id: [[lon, lat, stop_id], ...]} only for relations that
    receive at least one stop.
    """
    id_geom_pairs = [
        (oid, geom_by_id[oid])
        for oid in group_osm_ids
        if oid in geom_by_id and len(geom_by_id[oid]) >= 2
    ]
    if not id_geom_pairs:
        return {}

    sub_bboxes_by_id = {oid: build_sub_bboxes(geom) for oid, geom in id_geom_pairs}

    sn, ln, bkt, aid, tg_id = line_key_full
    ln_norm = ln.replace(" ", "")
    target_line_key = (sn, ln, bkt)
    all_stops: list = []  # [(stop_id, lon, lat)]
    seen_sids: set = set()
    for ref_variant in [sn, ln_norm, sn.upper(), sn.lower()]:
        for entry in _line_canonical_export.get((ref_variant, bucket), []):
            if entry.line_key != target_line_key or entry.trip_group_id != tg_id:
                continue
            for stop_id, _arr, _dep in entry.stops:
                if stop_id in seen_sids:
                    continue
                seen_sids.add(stop_id)
                c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                if c:
                    all_stops.append((stop_id, c[0], c[1]))

    if not all_stops:
        return {}

    result: dict = defaultdict(dict)  # {oid: {stop_id: [lon, lat, stop_id]}}
    for stop_id, lon, lat in all_stops:
        near_rels = [
            (oid, geom) for oid, geom in id_geom_pairs
            if any(stop_near_bbox(lon, lat, sb) for sb in sub_bboxes_by_id[oid])
        ]
        if not near_rels:
            continue
        dists = [(oid, min(haversine_km(lon, lat, p[0], p[1]) for p in geom)) for oid, geom in near_rels]
        d_min = min(d for _, d in dists)
        threshold = max(d_min + 0.05, d_min * 1.1)
        for oid, d in dists:
            if d <= threshold:
                result[oid][stop_id] = [lon, lat, stop_id]

    return {oid: list(stops.values()) for oid, stops in result.items()}


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)

    print("Loading GTFS data...")
    stop_coords  = load_stops()
    stop_meta    = load_stop_meta()
    svc_dates    = load_calendar_dates()
    route_lookup = load_routes()
    trip_lookup  = load_trips(route_lookup)
    print(f"  {len(stop_coords):,} stop entries, {len(svc_dates):,} service IDs, "
          f"{len(trip_lookup):,} trips")

    trip_frequencies = load_frequencies()
    print(f"  {sum(len(v) for v in trip_frequencies.values()):,} frequency entries for {len(trip_frequencies):,} trips")
    line_freq, line_speed, line_canonical = stream_stop_times(trip_lookup, stop_coords, svc_dates, trip_frequencies, stop_meta)

    # TEMP: drop no_draw entries from the candidate pool before matching.
    # The flag-instead-of-remove design (keep low-freq lines in _line_canonical_export
    # with no_draw="low_frequency") was meant to prevent OSM relations from falling
    # through to the geo fallback and matching the wrong line. But when a low-freq
    # entry shares a physical route with a high-freq sibling stored under a different
    # short_name (e.g. IR-LIX alongside PE-LIX for the Brünig line), the OSM relation
    # settles on the no_draw sibling in an early loop and never gets to see the
    # drawable one. To unblock those cases, revert to pre-filter behavior here.
    # Remove this block (and the no_draw filter logic in stream_stop_times) to restore
    # the original behavior.
    _removed_lk = 0
    for _key in list(_line_canonical_export.keys()):
        _kept = [e for e in _line_canonical_export[_key] if e.no_draw is None]
        if not _kept:
            del _line_canonical_export[_key]
            _removed_lk += 1
        elif len(_kept) != len(_line_canonical_export[_key]):
            _line_canonical_export[_key] = _kept
    print(f"  TEMP no_draw filter: removed {_removed_lk} empty key(s) from _line_canonical_export")

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
            for _ce in _line_canonical_export.get((_ref, "mountain"), []):
                if any(
                    (_sc := stop_coords.get(_sid) or stop_coords.get(_sid.split(":")[0]))
                    and stop_near_bbox(_sc[0], _sc[1], _osm_bbox_chk)
                    for _sid, _arr, _dep in _ce.stops
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

    print("\nPreprocessing OSM routes...")
    features = []   # mountain features built by GTFS-first loop; non-mountain added after 4-loop
    stats = defaultdict(int)
    dropped_details: list = []
    matched_gtfs_line_keys: set = set()

    MODE_TO_BUCKET = {
        "train": "train",
        "tram": "tram", "metro": "metro",
        "bus": "bus", "regional_bus": "bus",
        "ferry": "ferry", "mountain": "mountain",
    }

    # Operator-based override: rack/cog railways type=2 (train) in GTFS but tourist mountain
    # in reality. Stay in 4-loop pool (bucket=mountain, searched alongside train bucket).
    MOUNTAIN_RAIL_OPERATORS = {
        "WAB",                    # Wengernalpbahn
        "JB",                     # Jungfraubahn
        "BRB",                    # Brienz Rothorn Bahn
        "Berner Oberland-Bahnen", # Schynige Platte Bahn
        "Gornergratbahn",         # GGB
        "PILATUS-BAHNEN AG",      # Pilatusbahn
        "RB",                     # Rigi Bahnen
        "MG",                     # Ferrovia Monte Generoso
        "Dampfbahn Furka-Bergstrecke",
    }

    route_info: dict = {}  # osm_id → classification + geometry (no GTFS data yet)

    for feat in osm_routes:
        props = feat["properties"]
        route_tag = props.get("route", "")
        ref       = props.get("ref", "").strip()
        operator  = props.get("operator", "")
        network   = props.get("network", "")
        length_km = props.get("length_km", 0)

        # Skip non-transit
        if route_tag in ("fitness_trail", "hiking", "cycling", "foot"):
            continue

        mode = osm_to_mode(route_tag, ref, operator, length_km, network)
        if mode is None:
            stats["excluded"] += 1
            continue

        # Forchbahn ref remap: OSM ref="S18" → GTFS short_name="18"
        if operator.lower() == "fb":
            ref = ref.lstrip("S") or ref

        # Mountain routes: diverted to GTFS-first loop, excluded from 4-loop pool
        if mode == "mountain":
            continue
        if mode == "train" and ref in osm_train_refs_in_mountain_gtfs:
            continue

        # TER exclusion: cross-border/French-domestic services
        if ref.upper().startswith("TER"):
            stats["excluded"] += 1
            continue

        # Mountain operator override: stays in 4-loop pool as bucket=mountain
        is_mountain_operator = (mode == "train" and operator in MOUNTAIN_RAIL_OPERATORS)
        if is_mountain_operator:
            mode = "mountain"

        # Bus → regional_bus refinement (pure OSM-tag logic, no GTFS)
        if mode == "bus":
            ref_upper = ref.strip().upper()
            digits_only = "".join(c for c in ref if c.isdigit())
            n_digits = len(digits_only)
            op_lower = operator.lower()
            net_lower = props.get("network", "").lower()
            is_regional_2digit_net = (
                "sti" in op_lower or "chur" in op_lower or "transreno" in net_lower
                or "pag" in op_lower or "postauto" in op_lower
            )
            if ref_upper == "EV":
                mode = "regional_bus"
            elif digits_only:
                if n_digits >= 3:
                    mode = "regional_bus"
                elif is_regional_2digit_net and n_digits == 2:
                    mode = "regional_bus"
            else:
                line_length_km = props.get("raw_length_km", props.get("length_km", 0))
                if line_length_km >= 10.0:
                    mode = "regional_bus"

        bucket = MODE_TO_BUCKET.get(mode, "bus")
        ref_norm = ref.replace(" ", "")
        geom = feat["geometry"]
        if geom["type"] == "MultiLineString":
            osm_pts  = [c for seg in geom["coordinates"] for c in seg]
            osm_segs = geom["coordinates"]
        else:
            osm_pts  = geom["coordinates"]
            osm_segs = None
        if not osm_pts:
            continue
        osm_line_km = float(props.get("raw_length_km") or props.get("length_km") or 0.0)

        route_info[str(props.get("osm_id", ""))] = {
            "feat": feat, "mode": mode, "bucket": bucket,
            "ref": ref, "ref_norm": ref_norm,
            "osm_pts": osm_pts, "sub_bboxes": build_sub_bboxes(osm_pts), "_osm_segs": osm_segs,
            "osm_span_km": haversine_km(
                osm_pts[0][0], osm_pts[0][1], osm_pts[-1][0], osm_pts[-1][1]
            ),
            "osm_from":       props.get("from", ""),
            "osm_to":         props.get("to", ""),
            "osm_stop_nodes": props.get("stop_nodes", []),
            "osm_line_km":    osm_line_km,
            "skip_upper_density": (mode == "regional_bus"),
            "osm_name":       props.get("name", ""),
            "operator":       operator,
            "is_mountain_operator": is_mountain_operator,
        }

    n_ferry = sum(1 for info in route_info.values() if info["mode"] == "ferry")
    print(f"  {len(route_info):,} routes preprocessed ({n_ferry} ferries, "
          f"{stats['excluded']:,} hard-excluded)")

    # ── GTFS-first mountain processing ──────────────────────────────────────────
    # Every cable car / gondola / funicular in the timetable (GTFS route type 5/6/7)
    # gets a line on the map.  Use OSM route geometry when a matching relation exists
    # (matched by GTFS short_name == OSM ref); otherwise draw a straight-line segment
    # between the canonical GTFS stop coordinates.
    print("\nGTFS-first mountain processing...")
    n_gtfs_mountain = 0
    n_osm_shape = 0
    n_straight_line = 0
    mountain_added_bboxes: dict = defaultdict(list)

    for (ref, bucket), stop_list_candidates in _line_canonical_export.items():
        if bucket != "mountain":
            continue

        gtfs_entry = gtfs_index.get(("mountain", ref))
        if gtfs_entry is None:
            continue

        raw_freq   = gtfs_entry["raw_freq"]
        speed_kmh  = gtfs_entry["speed_kmh"]
        freq_score = compute_freq_score(raw_freq, "mountain")
        freq_score = max(freq_score, 0.4)

        for entry in stop_list_candidates:
            mtn_line_key = entry.line_key
            stop_list = entry.stops
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
            matched_gtfs_line_keys.add(mtn_line_key)
            n_gtfs_mountain += 1
            stats["matched"] += 1

    print(f"  {n_gtfs_mountain} mountain lines: {n_osm_shape} with OSM shape, "
          f"{n_straight_line} straight-line fallback")

    print("Stop assignment (4-loop batch)...")
    # ── Stop assignment: 4 sequential batch loops ─────────────────────────────
    # Each loop processes all routes not yet settled. A route is settled once it
    # receives a match that passes all required checks; it is not revisited.
    # Loop 1: simple string (long_norm/short_name, no generics, no tricks)
    # Loop 2: string tricks (RE↔R, name-prefix, alpha-prefix; generics excluded)
    # Loop 3: generic-prefix keys (S, R, RE, IC, …) with unconditional sanity check
    # Loop 4: full geo-fallback over all candidates in bucket
    line_stops_out = {}
    excluded_osm_ids: set = set()
    excluded_details: list = []

    # Ferry: collect pier stops from all GTFS ferry routes within the OSM route bbox.
    # OSM ferry refs rarely match GTFS short_names directly, so geo collection is used.
    for osm_id, info in route_info.items():
        if info["mode"] != "ferry":
            continue
        ref  = info["ref"]
        bbox = line_bbox(info["osm_pts"])
        seen_pos: set = set()
        pier_coords: list = []
        for (lk_ref, lk_bucket), lk_candidates in _line_canonical_export.items():
            if lk_bucket != "ferry":
                continue
            for entry in lk_candidates:
                for stop_id, _a, _d in entry.stops:
                    c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                    if c and stop_near_bbox(c[0], c[1], bbox, margin=0.01):
                        key = (round(c[0], 4), round(c[1], 4))
                        if key not in seen_pos:
                            seen_pos.add(key)
                            pier_coords.append([c[0], c[1], stop_id])
        if pier_coords:
            line_stops_out[osm_id] = {
                "osm_ref": ref, "stops": pier_coords,
                "_bucket": "ferry", "_line_key_full": (ref, ref, "ferry", "", 0), "_no_draw": None,
            }

    # 4-loop stop assignment (ferries bypass — already in line_stops_out above)
    pool = [oid for oid, info in route_info.items() if info["mode"] != "ferry"]
    for loop_level in (1, 2, 3, 4):
        settled, pool, excl_ids, excl_dets = _run_stop_loop(
            loop_level, pool, route_info, stop_coords, stop_meta
        )
        for osm_id, entry in settled.items():
            line_stops_out[osm_id] = entry
        excluded_osm_ids.update(excl_ids)
        excluded_details.extend(excl_dets)
        verb = ("string", "tricks", "generic", "geo-fallback")[loop_level - 1]
        print(f"  Loop {loop_level} ({verb:12}): settled {len(settled):4}, remaining {len(pool)}")

    # Dedup: within each _line_key_full group, if any OSM entry has a direct ref match
    # (norm(osm_ref) == norm(short_name or long_name), not a generic prefix), remove all
    # fallback-matched entries so the same GTFS line doesn't appear twice under different refs.
    def _is_direct_match(osm_ref: str, short_name: str, long_name: str) -> bool:
        norm = lambda s: s.replace(" ", "").lower()
        rn = norm(osm_ref)
        if rn == norm(short_name) and short_name.upper() not in GENERIC_GTFS_PREFIXES:
            return True
        if rn == norm(long_name) and long_name.upper() not in GENERIC_GTFS_PREFIXES:
            return True
        return False

    by_line_key_full: dict = defaultdict(list)
    for osm_id, entry in line_stops_out.items():
        lkf = entry.get("_line_key_full")
        if lkf:
            by_line_key_full[lkf].append(osm_id)

    dedup_removed: set = set()
    for lkf, osm_ids in by_line_key_full.items():
        sn, ln = lkf[0], lkf[1]
        direct = [oid for oid in osm_ids
                  if _is_direct_match(line_stops_out[oid]["osm_ref"], sn, ln)]
        if direct:
            fallback = [oid for oid in osm_ids
                        if not _is_direct_match(line_stops_out[oid]["osm_ref"], sn, ln)]
            dedup_removed.update(fallback)

    for oid in dedup_removed:
        ri = route_info.get(oid, {})
        fp = ri.get("feat", {}).get("properties", {}) if ri else {}
        dropped_details.append({
            "osm_id":   oid,
            "ref":      line_stops_out[oid].get("osm_ref", ""),
            "name":     fp.get("name", ""),
            "mode":     ri.get("mode", ""),
            "operator": ri.get("operator", ""),
            "reason":   "dedup",
        })
        del line_stops_out[oid]

    if dedup_removed:
        print(f"  Dedup-removed:  {len(dedup_removed)} fallback-matched lines superseded by direct-ref match")

    # Group-level stop reassignment: use combined geometry of all OSM relations for a
    # _line_key_full group so stops are included if near ANY relation, then placed on
    # the closest one. Fixes variant bleeding (e.g. Glattbrugg leaking into Hardbrücke).
    geom_by_id_grp: dict = {oid: info["osm_pts"] for oid, info in route_info.items()}

    by_lkf_grp: dict = defaultdict(list)
    for osm_id, entry in line_stops_out.items():
        lkf = entry.get("_line_key_full")
        bkt = entry.get("_bucket")
        if lkf and bkt and bkt != "ferry":
            by_lkf_grp[lkf].append(osm_id)

    n_grp = 0
    for lkf, osm_ids in by_lkf_grp.items():
        if len(osm_ids) < 2:
            continue
        sn, ln, bkt, aid, tg_id = lkf
        ln_norm = ln.replace(" ", "")
        if not any((v, bkt) in _line_canonical_export for v in [sn, ln_norm, sn.upper(), sn.lower()]):
            continue
        new_asgn = _group_reassign_stops(osm_ids, lkf, bkt, geom_by_id_grp, stop_coords)
        for oid in osm_ids:
            new_stops = new_asgn.get(oid, [])
            if new_stops:
                line_stops_out[oid]["stops"] = new_stops
        n_grp += 1

    if n_grp:
        print(f"  Group reassignment: {n_grp} groups processed")

    # ── Post-4-loop draw gate ─────────────────────────────────────────────────
    # Apply the no_draw flag, look up freq/speed from gtfs_index via _line_key_full,
    # compute visual properties, and build non-mountain features.
    print("\nPost-loop draw gate...")
    no_draw_excluded = 0

    for osm_id in list(line_stops_out.keys()):
        entry    = line_stops_out[osm_id]
        no_draw  = entry.get("_no_draw")
        lkf      = entry.get("_line_key_full")
        bkt      = entry.get("_bucket")
        info     = route_info.get(osm_id, {})
        fp       = info.get("feat", {}).get("properties", {}) if info else {}

        if no_draw:
            dropped_details.append({
                "osm_id":           osm_id,
                "ref":              entry.get("osm_ref", ""),
                "name":             fp.get("name", ""),
                "mode":             info.get("mode", ""),
                "operator":         info.get("operator", ""),
                "matched_line_key": list(lkf[:3]) if lkf else None,
                "no_draw_reason":   no_draw,
                "reason":           "no_draw",
            })
            del line_stops_out[osm_id]
            no_draw_excluded += 1
            continue

        # Track matched GTFS line_key for gtfs_unmatched.json
        if lkf:
            matched_gtfs_line_keys.add(lkf[:3])

        if bkt == "ferry":
            continue  # ferry features built in the ferry block below

        if not info:
            continue

        mode     = info["mode"]
        operator = info["operator"]
        osm_line_km = info["osm_line_km"]
        ref      = info["ref"]

        # Mountain operator display override
        if info.get("is_mountain_operator"):
            mode = "mountain"

        # Freq/speed lookup via _line_key_full
        gtfs = None
        if lkf:
            sn, ln, lk_bkt, aid, _tg = lkf
            ln_norm = ln.replace(" ", "")
            gtfs = (gtfs_long_index.get((lk_bkt, ln_norm)) or gtfs_index.get((lk_bkt, sn)))

        if gtfs is None:
            if mode == "mountain":
                # MOUNTAIN_RAIL_OPERATORS with no GTFS train match: use visible default
                freq_score = 0.6
                speed_kmh  = None
            else:
                stats["unmatched"] += 1
                dropped_details.append({
                    "osm_id":           osm_id,
                    "ref":              ref,
                    "name":             fp.get("name", ""),
                    "mode":             mode,
                    "operator":         operator,
                    "matched_line_key": None,
                    "reason":           "no_gtfs",
                })
                del line_stops_out[osm_id]
                continue
        else:
            own_raw   = gtfs["raw_freq"]
            speed_kmh = gtfs["speed_kmh"]
            # Corridor frequency boost using settled stop positions
            settled_stops = entry.get("stops", [])
            corr_stops = [(s[2], 0, 0) for s in settled_stops if len(s) > 2]
            corr_raw = corridor_freq(corr_stops, pair_freq) if corr_stops else None
            if corr_raw and own_raw["core_wd"] > 0 and corr_raw["core_wd"] > own_raw["core_wd"]:
                raw_freq = corr_raw
            else:
                raw_freq = own_raw
            freq_score = compute_freq_score(raw_freq, mode)
            stats["matched"] += 1

        if mode == "mountain" and freq_score < 0.4:
            freq_score = 0.4

        color      = speed_to_color(mode, speed_kmh)
        width_base = freq_to_width_base(freq_score, mode)

        features.append({
            "type": "Feature",
            "geometry": info["feat"]["geometry"],
            "properties": {
                "osm_id":      fp.get("osm_id"),
                "ref":         ref,
                "name":        fp.get("name", ""),
                "operator":    operator,
                "mode":        mode,
                "freq_score":  freq_score,
                "speed_kmh":   speed_kmh,
                "color":       color,
                "width_base":  width_base,
                "line_km":     round(osm_line_km, 1),
                "gtfs_matched": True,
                "from":        fp.get("from", ""),
                "to":          fp.get("to", ""),
                "stop_nodes":  fp.get("stop_nodes", []),
            },
        })

    # Ferry features: freq/speed from gtfs_index via OSM ref + geo fallback
    for osm_id, info in route_info.items():
        if info["mode"] != "ferry":
            continue
        if osm_id not in line_stops_out:
            continue  # no pier stops found
        ref      = info["ref"]
        ref_norm = ref.replace(" ", "")
        fp       = info["feat"]["properties"]
        gtfs = (gtfs_long_index.get(("ferry", ref_norm))
                or gtfs_index.get(("ferry", ref))
                or gtfs_index.get(("ferry", ref_norm)))
        if gtfs is None:
            osm_bbox = line_bbox(info["osm_pts"])
            best_n = 1
            for gtfs_entry, cand_stops in ferry_geo_index:
                n_inside = sum(1 for sid, _a, _d in cand_stops
                               if (c := stop_coords.get(sid) or stop_coords.get(sid.split(":")[0]))
                               and stop_near_bbox(c[0], c[1], osm_bbox, margin=0.05))
                if n_inside > best_n:
                    best_n = n_inside
                    gtfs = gtfs_entry
        raw_freq   = gtfs["raw_freq"]   if gtfs else {"core_wd": 0, "eve_wd": 0, "we": 0}
        speed_kmh  = gtfs["speed_kmh"]  if gtfs else None
        freq_score = compute_freq_score(raw_freq, "ferry")
        stats["matched"] += 1
        color      = speed_to_color("ferry", speed_kmh)
        width_base = freq_to_width_base(freq_score, "ferry")
        features.append({
            "type": "Feature",
            "geometry": info["feat"]["geometry"],
            "properties": {
                "osm_id":      fp.get("osm_id"),
                "ref":         ref,
                "name":        fp.get("name", ""),
                "operator":    info["operator"],
                "mode":        "ferry",
                "freq_score":  freq_score,
                "speed_kmh":   speed_kmh,
                "color":       color,
                "width_base":  width_base,
                "line_km":     round(info["osm_line_km"], 1),
                "gtfs_matched": True,
                "from":        fp.get("from", ""),
                "to":          fp.get("to", ""),
                "stop_nodes":  fp.get("stop_nodes", []),
            },
        })

    if no_draw_excluded:
        print(f"  no_draw filtered: {no_draw_excluded} routes excluded (low-frequency GTFS match)")

    # Strip internal fields before writing line_stops.json
    for entry in line_stops_out.values():
        entry.pop("_bucket", None)
        entry.pop("_line_key_full", None)
        entry.pop("_no_draw", None)

    OUT_STOPS.write_text(json.dumps(line_stops_out))
    print(f"  Stop coords: {sum(len(v['stops']) for v in line_stops_out.values()):,} stops across {len(line_stops_out):,} lines → {OUT_STOPS}")

    OUT_EXCLUDED.write_text(json.dumps(excluded_details, ensure_ascii=False))
    print(f"  Sanity log:  {len(excluded_details)} excluded lines → {OUT_EXCLUDED}")

    OUT_DROPPED.write_text(json.dumps(dropped_details, ensure_ascii=False))
    print(f"  Dropped log: {len(dropped_details)} dropped lines → {OUT_DROPPED}")

    # GTFS lines never matched to any drawn OSM route
    all_gtfs_line_keys: set = set()
    for candidates in _line_canonical_export.values():
        for entry in candidates:
            all_gtfs_line_keys.add(entry.line_key)
    unmatched_lks = all_gtfs_line_keys - matched_gtfs_line_keys
    gtfs_unmatched_out = []
    for lk in sorted(unmatched_lks, key=lambda x: (x[2], x[0], x[1])):
        short_name, long_name, bucket = lk
        freq = line_freq.get(lk, {"core_wd": 0, "eve_wd": 0, "we": 0})
        mode_approx = _BUCKET_MODE_APPROX.get(bucket, "regional_bus")
        fs = compute_freq_score(freq, mode_approx)
        if fs < MIN_FREQ_SCORE:
            continue
        gtfs_unmatched_out.append({
            "short_name":  short_name,
            "long_name":   long_name,
            "bucket":      bucket,
            "freq_score":  round(fs, 3),
            "total_trips": sum(freq.values()),
        })
    OUT_GTFS_UNMATCHED.write_text(json.dumps(gtfs_unmatched_out, ensure_ascii=False))
    print(f"  GTFS unmatched: {len(gtfs_unmatched_out)} lines with service but no OSM match → {OUT_GTFS_UNMATCHED}")

    # Deduplicate mountain features, write final transit_lines.geojson
    features = deduplicate_mountain(features)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}))

    print(f"\nResults:")
    print(f"  Drawn (matched):      {stats['matched']:,}")
    print(f"  Hidden (no GTFS):     {stats['unmatched']:,}")
    print(f"  No draw (low-freq):   {no_draw_excluded:,}")
    print(f"  Excluded (coach/TER): {stats['excluded']:,}")
    print(f"  Output:               {OUT}")

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
