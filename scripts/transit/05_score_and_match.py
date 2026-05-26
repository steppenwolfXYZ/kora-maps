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
from typing import Optional

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


_line_canonical_export: dict = defaultdict(list)  # (short_name|long_norm, bucket) → [(line_key, stop_list, direction_aware), ...]
_canonical_density: dict = {}  # (line_key, geo_bucket) → stops/km from largest ordered variant for that cell

# Coarse geo-grid for canonical trip bucketing: ~0.5° ≈ 40 km per cell
GEO_BUCKET_DEG = 0.5

# Maps GTFS bucket name to mode approximation used in compute_freq_score.
# "bus" bucket is approximated as "regional_bus" (lower maluses) — intentionally
# conservative; a city bus with sparse service might survive the filter here even
# though it would be dropped at draw time. Mountain bucket is exempt.
_BUCKET_MODE_APPROX = {
    "train": "train", "tram": "tram", "metro": "metro",
    "ferry": "ferry", "bus": "regional_bus", "regional_bus": "regional_bus",
}


def stream_stop_times(trips, stop_coords, svc_dates, trip_frequencies):
    """One streaming pass → raw trip counts + speed per line."""
    global _line_canonical_export
    print("  Streaming stop_times.txt (~1–2 min)...")

    # Raw trip counts per line: {line_key: {core_wd, eve_wd, we}}
    line_freq: dict = defaultdict(lambda: {"core_wd": 0, "eve_wd": 0, "we": 0})

    # Canonical trip (most stops) per line for speed/pair-freq computation
    line_canonical: dict = {}

    # All unique stop sets per (line_key, geo_bucket), sorted by stop count desc.
    # Preserves minority stop sets (e.g. BOB full Grindelwald service with 0 active
    # sample days alongside the frequent short Terminal Express variant).
    line_canonical_geo_stops: dict = {}  # (line_key, geo_bucket) → [{"stop_count", "stops"}, …] sorted desc
    line_variant_counts: dict = defaultdict(lambda: defaultdict(int))  # (line_key, geo_bucket) → {frozenset(stop_ids) → trip_count}
    line_variant_sequences: dict = {}  # (geo_key, frozenset) → ordered [(sid, arr, dep), ...] for one representative trip

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
        variant = frozenset(s[1] for s in stops)
        line_variant_counts[geo_key][variant] += max(1, len(active_dates))
        if (geo_key, variant) not in line_variant_sequences:
            line_variant_sequences[(geo_key, variant)] = [(s[1], s[2], s[3]) for s in stops]

        # Stop-display canonicals: keep all unique stop sets per cell, sorted by stop count desc.
        # Multiple stop sets arise when different services share a line_key + geo_bucket
        # (e.g. Maienfeld Bus 14 with 5 stops and Feldkirch Bus 14 with 30 stops both land
        # in the same 0.5° cell).  The sanity-check loop in stop assignment will iterate them
        # in order and accept the first one whose stops align with the OSM geometry.
        new_stops = [(s[1], s[2], s[3]) for s in stops]
        new_sid_set = frozenset(s[0] for s in new_stops)
        existing_list = line_canonical_geo_stops.get(geo_key)
        if existing_list is None:
            line_canonical_geo_stops[geo_key] = [{"stop_count": n, "stops": new_stops}]
        elif not any(frozenset(s[0] for s in e["stops"]) == new_sid_set for e in existing_list):
            existing_list.append({"stop_count": n, "stops": new_stops})
            existing_list.sort(key=lambda e: -e["stop_count"])

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

    # Remove rare stop sets (< 10% of trips) from both source dicts so that garage runs
    # and other infrequent variants never surface as stop candidates in section 1 or 2.
    # If no variant clears 10% (many roughly-equal stopping patterns), fall back to 5%.
    # If still nothing clears 5%, keep all variants rather than discarding everything.
    for geo_key, variant_counts in list(line_variant_counts.items()):
        total = sum(variant_counts.values())
        for pct in (0.10, 0.05):
            threshold = max(1, total * pct)
            filtered = {v: c for v, c in variant_counts.items() if c >= threshold}
            if filtered:
                line_variant_counts[geo_key] = filtered
                break

    for geo_key, canons in line_canonical_geo_stops.items():
        variant_counts = line_variant_counts.get(geo_key, {})
        line_canonical_geo_stops[geo_key] = [
            c for c in canons
            if frozenset(s[0] for s in c["stops"]) in variant_counts
        ]

    # Exclude line_keys that would not be drawn (freq_score == 0.0) from all three
    # source dicts before sections 1 and 2 build _line_canonical_export.  This prevents
    # zero/near-zero-service lines (e.g. EXT Extrazug) from entering the geo-fallback pool
    # and contaminating stop assignment for other lines.  "bus" bucket uses "regional_bus"
    # as mode approximation — intentionally conservative; see transit.md for the known edge case.
    _zero_freq = {"core_wd": 0, "eve_wd": 0, "we": 0}
    zero_service_keys = {
        geo_key[0] for geo_key in line_canonical_geo_stops
        if geo_key[0][2] != "mountain"
        # CC short_name = rack/cog railway (WAB, JB, SPB, BRB, GGB, PB, RB…).
        # These are seasonal (June–Oct) and will have zero service on April sample dates.
        # Keep them in the pool so mountain-mode geo fallback can find their stop sequences.
        and not (geo_key[0][0] == "CC" and geo_key[0][2] == "train")
        and compute_freq_score(
            line_freq.get(geo_key[0], _zero_freq),
            _BUCKET_MODE_APPROX.get(geo_key[0][2], "regional_bus"),
        ) == 0.0
    }
    for geo_key in list(line_canonical_geo_stops.keys()):
        if geo_key[0] in zero_service_keys:
            del line_canonical_geo_stops[geo_key]
            line_variant_counts.pop(geo_key, None)

    # Precompute true stop density (stops/km) for each (line_key, geo_bucket) from its
    # largest ordered variant. Keyed by (line_key, geo_bucket) so that different routes
    # sharing the same short_name but in different cities (e.g. Fribourg 182 vs Julierpass 182)
    # keep their own density and don't contaminate each other.
    _canonical_density.clear()
    for (lk, gb), canons in line_canonical_geo_stops.items():
        stops = canons[0]["stops"]  # largest ordered variant (sorted desc by stop_count)
        if len(stops) < 2:
            continue
        coords = []
        for sid, _a, _d in stops:
            c = stop_coords.get(sid) or stop_coords.get(sid.split(":")[0])
            if c:
                coords.append(c)
        if len(coords) < 2:
            continue
        span = sum(
            haversine_km(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
            for i in range(len(coords) - 1)
        )
        if span > 0:
            _canonical_density[(lk, gb)] = len(coords) / span

    # Build canonical export: all unique stop sets per geographic cell per line_key.
    # This gives separate candidates for "S6 Bern" and "S6 Zürich" even though they
    # share the same GTFS line_key = ("S6", "S 6", "train").  It also preserves minority
    # services that share a line_key+cell with a larger route (e.g. Maienfeld Bus 14 with
    # 5 stops alongside Feldkirch Bus 14 with 30 stops in the same 0.5° cell) so the
    # stop-assignment sanity check can iterate all candidates and pick the right one.
    _line_canonical_export.clear()
    for (line_key, _gb), canons in line_canonical_geo_stops.items():
        short_name, long_name, bucket = line_key
        long_norm = long_name.replace(" ", "")
        for canon in canons:
            _line_canonical_export[(short_name, bucket)].append((line_key, canon["stops"], False))
            if long_norm and long_norm != short_name:
                _line_canonical_export[(long_norm, bucket)].append((line_key, canon["stops"], False))

    # Filtered union candidate: union of stops from variants that represent ≥10% of trips
    # for this (line, geo_bucket). Prevents rare detour/construction trips from leaking
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
    for (line_key, geo_bucket), variant_counts in line_variant_counts.items():
        short_name, long_name, bucket = line_key
        qualifying = list(variant_counts.items())
        if not qualifying:
            continue
        long_norm = long_name.replace(" ", "")
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
            # All qualifying variants nest under one master route → safe union
            _line_canonical_export[(short_name, bucket)].append((line_key, union_cand, False))
            if long_norm and long_norm != short_name:
                _line_canonical_export[(long_norm, bucket)].append((line_key, union_cand, False))
        else:
            # Genuinely different stops per direction — emit each qualifying variant as its own
            # direction-aware ordered candidate so the assignment loop picks the matching one.
            for v, _ in qualifying:
                geo_key = (line_key, geo_bucket)
                var_stops = line_variant_sequences.get((geo_key, v))
                if not var_stops:
                    continue
                _line_canonical_export[(short_name, bucket)].append((line_key, var_stops, True))
                if long_norm and long_norm != short_name:
                    _line_canonical_export[(long_norm, bucket)].append((line_key, var_stops, True))

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


def build_sub_bboxes(pts: list, segment_km: float = 40.0) -> list:
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

def _count_endpoints_covered(osm_pts: list, stops: list, threshold_km: float = GEO_SORT_ENDPOINT_KM) -> int:
    """Return how many OSM endpoints (0, 1, or 2) have a stop within threshold_km.
    Default (0.5 km) is used to rank geo-fallback candidates.
    Called with ENDPOINT_THRESHOLD_KM (5 km) as the canonical-stop gate."""
    if not stops or len(osm_pts) < 2:
        return 2  # can't determine — don't penalise
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
    Check 2 — GTFS stops → OSM geometry: are 3/5 evenly-spaced GTFS stops within 200 m of the OSM line?
    Check 3 — OSM stops → GTFS stops: are 3/5 evenly-spaced OSM stop nodes within 200 m of any GTFS stop?
    """
    if len(ccoords) < 2 or len(osm_pts) < 2:
        return False

    # Check 1: OSM stop names vs GTFS candidate stop names — O(N_osm + N_gtfs), pure string ops
    # For each OSM stop node (name extracted by 04_extract_osm.py), checks whether that
    # normalised name appears in the GTFS candidate's stop name set (whole-token equality,
    # not substring). Requires at least 1/3 of OSM stop nodes (minimum 2) to match.
    # Individual comparisons are skipped when either side is < 2 chars after normalisation.
    gtfs_names = set()
    for s in ccoords:
        sid = s[2] if len(s) > 2 else None
        if not sid:
            continue
        sname = _norm_stop_name(stop_meta.get(sid, ("", ""))[0])
        if sname and len(sname) >= 2:
            gtfs_names.add(sname)
    threshold = max(2, len(osm_stop_nodes) // 3)
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

    # Proximity check: 3/5 evenly-spaced GTFS stops within 100 m of OSM polyline.
    if density_ok:
        step2 = max(1, len(ccoords) // 5)
        sampled_gtfs = ccoords[::step2][:5]
        close2 = sum(
            1 for s in sampled_gtfs
            if _min_dist_to_polyline_km(s[0], s[1], osm_pts) <= 0.1
        )
        if close2 * 5 >= len(sampled_gtfs) * 3:  # ≥ 3/5
            return True

    # Check 3: OSM stops → GTFS stops — O(5 × N_gtfs_stops)
    # Sample 5 evenly-spaced OSM stop nodes; require 3/5 within 200 m of any GTFS stop.
    if osm_stop_nodes and len(osm_stop_nodes) >= 2:
        step3 = max(1, len(osm_stop_nodes) // 5)
        sampled_osm = osm_stop_nodes[::step3][:5]
        close3 = sum(
            1 for p in sampled_osm
            if any(haversine_km(p[0], p[1], s[0], s[1]) <= 0.2 for s in ccoords)
        )
        if close3 * 5 >= len(sampled_osm) * 3:  # ≥ 3/5
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
) -> tuple[list, bool]:
    """Look up canonical stops for an OSM line and apply the name-fallback sanity check.

    Returns (best_coords, used_name_fallback, canon_gtfs_ref).  best_coords is empty if
    no canonical was found or if a name-fallback canonical failed the geo sanity check
    (Trigger 1).  canon_gtfs_ref is the GTFS short_name key that produced the result
    (None if nothing was found).
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
    best_line_key = None
    if canon:
        for (canon_line_key, candidate, dir_aware) in canon:
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
                c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                if c and any(stop_near_bbox(c[0], c[1], sb) for sb in sub_bboxes):
                    ccoords.append([c[0], c[1], stop_id])
            if len(ccoords) > len(best_coords):
                best_coords = ccoords
                best_line_key = canon_line_key

    # Trigger 1: if canonical came from a name fallback, sanity-check before trusting it.
    if best_coords and used_name_fallback:
        if best_line_key and best_coords:
            _best_gb = (int(best_coords[0][0] / GEO_BUCKET_DEG), int(best_coords[0][1] / GEO_BUCKET_DEG))
            _full_density = _canonical_density.get((best_line_key, _best_gb), 0.0)
        else:
            _full_density = 0.0
        if not _passes_geo_sanity(osm_pts, best_coords, stop_meta, osm_from, osm_to, osm_stop_nodes, osm_line_km,
                                   cand_full_density=_full_density, skip_upper_density=skip_upper_density):
            best_coords = []

    return best_coords, used_name_fallback


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


# ── OSM → GTFS matching ───────────────────────────────────────────────────────

def find_best_gtfs_candidate(ref, bucket, osm_bbox, stop_coords, line_freq, line_speed,
                              osm_name=""):
    """
    Match an OSM route to a specific GTFS line using geographic stop overlap as a
    tiebreaker.  Tries all ref variants (exact, normalised, name-prefix, alpha-prefix)
    and scores each GTFS candidate by the fraction of its canonical stops that fall
    inside the OSM route bbox.  The highest-scoring candidate wins; geo is a
    tiebreaker, NOT a gate — a candidate with score=0 still wins if it is the only
    one for this ref.

    Returns (line_key, raw_freq, speed_kmh, canon_stops), or None if no candidates
    exist in _line_canonical_export for any ref variant (caller falls back to gtfs_index).
    """
    ref_norm = ref.replace(" ", "")

    ref_variants: dict = dict.fromkeys([ref, ref_norm, ref.upper(), ref.lower(), ref_norm.upper()])

    # Name-prefix fallback: "R 311: Interlaken..." → try "R", "311"
    for token in osm_name.split(":")[0].strip().split():
        if token != ref and len(token) <= 6:
            ref_variants[token] = None
            ref_variants[token.upper()] = None

    # RE{n} ↔ R{n}: MGB trains appear as 'R 41'/'R 42' in GTFS long_name but 'RE41'/'RE42' in OSM
    r_ref = _re_to_r_ref(ref_norm)
    if r_ref:
        ref_variants[r_ref] = None

    # Alpha-prefix fallback: "R43" → "R"
    m = re.match(r'^([A-Za-z ]+)\d', ref)
    if m:
        alpha = m.group(1).strip()
        if alpha and alpha != ref:
            ref_variants[alpha] = None
            ref_variants[alpha.upper()] = None

    seen_line_keys: set = set()
    best_score = -1
    best_line_key = None
    best_stops = None

    for rv in ref_variants:
        for line_key, stops, _da in _line_canonical_export.get((rv, bucket), []):
            if line_key in seen_line_keys:
                continue
            seen_line_keys.add(line_key)
            n_inside = sum(
                1 for sid, _a, _d in stops
                if (c := stop_coords.get(sid) or stop_coords.get(sid.split(":")[0]))
                and stop_near_bbox(c[0], c[1], osm_bbox, margin=0.05)
            )
            score = n_inside / max(len(stops), 1)
            if score > best_score:
                best_score = score
                best_line_key = line_key
                best_stops = stops

    if best_line_key is None:
        return None   # no candidates at all — caller falls back to gtfs_index

    raw_freq = dict(line_freq.get(best_line_key, {"core_wd": 0, "eve_wd": 0, "we": 0}))
    speed_kmh = line_speed.get(best_line_key)
    return best_line_key, raw_freq, speed_kmh, best_stops


def _group_reassign_stops(
    group_osm_ids: list,
    gtfs_r: str,
    bucket: str,
    geom_by_id: dict,
    stop_coords: dict,
) -> dict:
    """Reassign stops for a group of OSM relations sharing the same gtfs_ref.

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

    all_stops: list = []  # [(stop_id, lon, lat)]
    seen_sids: set = set()
    for ref_variant in [gtfs_r, gtfs_r.upper(), gtfs_r.lower()]:
        for (_, cand, _da) in _line_canonical_export.get((ref_variant, bucket), []):
            for stop_id, _arr, _dep in cand:
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
            for (_, _cand_stops, _da) in _line_canonical_export.get((_ref, "mountain"), []):
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
    dropped_details: list = []       # routes dropped in main loop (no_gtfs / zero_freq)
    matched_gtfs_line_keys: set = set()  # line_keys successfully matched to a drawn feature

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
        network   = props.get("network", "")
        length_km = props.get("length_km", 0)

        # Skip non-transit
        if route_tag in ("fitness_trail", "hiking", "cycling", "foot"):
            continue

        mode = osm_to_mode(route_tag, ref, operator, length_km, network)
        if mode is None:
            stats["excluded"] += 1
            continue

        # Forchbahn (FB): OSM ref="S18", GTFS short_name="18" (type=0, tram).
        # Remap ref so all GTFS lookups and stop assignment find the right entry.
        if operator.lower() == "fb":
            ref = ref.lstrip("S") or ref

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
        ref_norm = ref.replace(" ", "")

        # OSM route bbox
        geom = feat["geometry"]
        osm_pts = ([c for seg in geom["coordinates"] for c in seg]
                   if geom["type"] == "MultiLineString" else geom["coordinates"])
        osm_bbox = line_bbox(osm_pts)
        osm_line_km = sum(
            haversine_km(osm_pts[i][0], osm_pts[i][1], osm_pts[i+1][0], osm_pts[i+1][1])
            for i in range(len(osm_pts) - 1)
        ) if len(osm_pts) >= 2 else 0.0

        # Primary match: geo-scored candidate selection (all modes).
        # Picks the specific GTFS line whose canonical stops best overlap this OSM route.
        # Geo is a tiebreaker — a single candidate wins even with score=0.
        gtfs = None
        matched_line_key = None
        matched_canon_stops = None
        gtfs_match = find_best_gtfs_candidate(
            ref, bucket, osm_bbox, stop_coords, line_freq, line_speed,
            osm_name=props.get("name", ""))
        if gtfs_match:
            matched_line_key, gtfs_raw_freq, gtfs_speed, matched_canon_stops = gtfs_match
            if sum(gtfs_raw_freq.values()) > 0:
                gtfs = {"raw_freq": gtfs_raw_freq, "speed_kmh": gtfs_speed}

        if gtfs is None:
            # No candidates in _line_canonical_export, OR geo match had zero service on
            # sample dates (wrong line_key picked) — fall back to aggregated index.
            # Preserves old behaviour for lines whose canonical trips have no resolvable
            # stop coordinates (geo_bucket could not be determined during streaming).
            gtfs = gtfs_index.get((bucket, ref))
            if gtfs is None:
                for k in [(bucket, ref_norm), (bucket, ref.upper()), (bucket, ref.lower())]:
                    gtfs = gtfs_index.get(k)
                    if gtfs: break
            if gtfs is None:
                gtfs = (gtfs_long_index.get((bucket, ref_norm)) or
                        gtfs_long_index.get((bucket, ref_norm.upper())))
            if gtfs is None:
                r_norm = _re_to_r_ref(ref_norm)
                if r_norm:
                    gtfs = (gtfs_long_index.get((bucket, r_norm)) or
                            gtfs_long_index.get((bucket, r_norm.upper())))
            if gtfs is None:
                osm_name_prefix = props.get("name", "").split(":")[0].strip()
                for token in osm_name_prefix.split():
                    if token != ref and len(token) <= 6:
                        gtfs = gtfs_index.get((bucket, token)) or gtfs_index.get((bucket, token.upper()))
                        if gtfs: break
            if gtfs is None:
                m = re.match(r'^([A-Za-z ]+)\d', ref)
                if m:
                    alpha = m.group(1).strip()
                    if alpha and alpha != ref:
                        gtfs = gtfs_index.get((bucket, alpha)) or gtfs_index.get((bucket, alpha.upper()))

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

        # Operator-based override for rack/cog railways that are type=2 (rail) in GTFS
        # but are tourist mountain railways in reality. Keyed by OSM operator string.
        # WAB and JB are rack railways to Kleine Scheidegg / Jungfraujoch.
        # BRB (Brienz Rothorn Bahn) is a steam rack railway, also type=2 in GTFS.
        # SPB (Schynige Platte Bahn) uses the full name in OSM; "BOB" (valley train) does not.
        MOUNTAIN_RAIL_OPERATORS = {
            "WAB",                    # Wengernalpbahn — Lauterbrunnen/Grindelwald→Kleine Scheidegg
            "JB",                     # Jungfraubahn — Kleine Scheidegg→Jungfraujoch
            "BRB",                    # Brienz Rothorn Bahn
            "Berner Oberland-Bahnen", # Schynige Platte Bahn (BOB valley trains use "BOB")
            "Gornergratbahn",         # GGB — Zermatt→Gornergrat (shows as train without this)
            "PILATUS-BAHNEN AG",      # Pilatusbahn — Alpnachstad→Pilatus Kulm
            "RB",                     # Rigi Bahnen — Arth-Rigi-Bahn / Vitznau-Rigi-Bahn
            "MG",                     # Ferrovia Monte Generoso
            "Dampfbahn Furka-Bergstrecke",  # DFB — Realp→Oberwald (seasonal steam)
        }
        if mode == "train" and operator in MOUNTAIN_RAIL_OPERATORS:
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
            # Use the geo-matched canonical stops when available; otherwise fall back
            # to the first candidate for this ref (any stop sequence will do for corridor).
            if matched_canon_stops:
                corr_canon = matched_canon_stops
            else:
                corr_canon = None
                for lk in [(ref, bucket), (ref_norm, bucket),
                           (ref_norm.upper(), bucket), (ref.lower(), bucket)]:
                    candidates = _line_canonical_export.get(lk)
                    if candidates:
                        corr_canon = candidates[0][1]   # (line_key, stops, dir_aware) → stops
                        break
            corr_raw = corridor_freq(corr_canon, pair_freq) if corr_canon else None
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
            # Reached only for OSM train routes overridden to mountain via
            # MOUNTAIN_RAIL_OPERATORS (WAB, JB, BRB, SPB, GGB, PB, RB, MG, DFB).
            # No GTFS mountain bucket match exists for these type=2 rack railways.
            freq_score = 0.6
            stats["matched"] += 1
        else:
            freq_score = None   # unmatched → skip (don't draw)
            stats["unmatched"] += 1

        # Skip routes with no service on sample dates (freq_score == 0.0).
        # Mountain mode is exempt: seasonal railways may not run on our specific
        # sample date but are still worth showing (they get clamped to 0.4 below).
        if freq_score is None or (freq_score == 0.0 and mode != "mountain"):
            dropped_details.append({
                "osm_id":           str(props.get("osm_id", "")),
                "ref":              ref,
                "name":             props.get("name", ""),
                "mode":             mode,
                "operator":         operator,
                "matched_line_key": list(matched_line_key) if matched_line_key else None,
                "freq_score":       freq_score,
                "reason":           "no_gtfs" if freq_score is None else "zero_freq",
            })
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
                "line_km":    round(osm_line_km, 1),
                "gtfs_matched": True,
                "from":       props.get("from", ""),
                "to":         props.get("to", ""),
                "stop_nodes": props.get("stop_nodes", []),
            },
        })
        if matched_line_key:
            matched_gtfs_line_keys.add(matched_line_key)

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
        for (mtn_line_key, stop_list, _da) in stop_list_candidates:
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

    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}))

    # Save stop coordinates per line (osm_id → [[lon,lat], ...]) for stop dot rendering
    line_stops_out = {}
    excluded_osm_ids: set = set()   # osm_ids where geo sanity check rejected all candidates
    excluded_details: list = []     # metadata for sanity_excluded.json sidecar
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
        if gtfs is None:
            r_norm = _re_to_r_ref(ref_norm)
            if r_norm:
                cand = (gtfs_long_index.get((bucket, r_norm)) or
                        gtfs_long_index.get((bucket, r_norm.upper())))
                if cand:
                    gtfs = cand
                    matched_gtfs_ref = r_norm
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
                    for (_, cand, _da) in lk_candidates:
                        for stop_id, _a, _d in cand:
                            c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                            if c and stop_near_bbox(c[0], c[1], bbox, margin=0.01):
                                key = (round(c[0], 4), round(c[1], 4))
                                if key not in seen_pos:
                                    seen_pos.add(key)
                                    pier_coords.append([c[0], c[1], stop_id])
                if pier_coords:
                    line_stops_out[osm_id] = {"gtfs_ref": ref, "osm_ref": ref, "stops": pier_coords, "_bucket": "ferry"}
                continue
            elif mode != "mountain":
                continue
            # Mountain rack railways (WAB, JB, BRB) have no GTFS mountain-bucket entry
            # (they are type=2 rail in GTFS). Fall through to the geo-based fallback
            # below, which already searches the "train" bucket for mountain-mode lines.

        # Compute OSM line bbox (needed for stop filtering and geo fallback)
        geom = feat["geometry"]
        if geom["type"] == "MultiLineString":
            osm_pts = [c for seg in geom["coordinates"] for c in seg]
        else:
            osm_pts = geom["coordinates"]
        bbox = line_bbox(osm_pts)
        sub_bboxes = build_sub_bboxes(osm_pts)   # corridor-aware stop filter

        # Precompute OSM direction for direction-aware candidate filtering.
        # Only meaningful when start and end are well-separated (non-circular routes).
        osm_start = osm_pts[0]
        osm_end   = osm_pts[-1]
        osm_span_km = haversine_km(osm_start[0], osm_start[1], osm_end[0], osm_end[1])

        # Extract OSM terminal tags and stop nodes — needed for sanity checks below.
        osm_from       = feat["properties"].get("from", "")
        osm_to         = feat["properties"].get("to", "")
        osm_stop_nodes = feat["properties"].get("stop_nodes", [])
        osm_line_km    = feat["properties"].get("line_km", 0.0)

        # Reconstruct stop coords from canonical trip, with name-fallback sanity check.
        # Logic is in _lookup_canonical_stops() so the diagnostic script can share it.
        best_coords, used_name_fallback = _lookup_canonical_stops(
            ref, ref_norm, matched_gtfs_ref, bucket,
            osm_pts, osm_span_km, osm_from, osm_to,
            stop_coords, stop_meta, sub_bboxes, osm_stop_nodes, osm_line_km,
            skip_upper_density=(mode == "regional_bus"),
        )
        geo_best_ref: Optional[str] = None

        # Geo-based fallback: triggers when (a) no canon found at all, (b) 0 of 2 endpoints
        # covered at 5 km — canonical resolved to wrong GTFS service (e.g. SBB 'RE' for MGB
        # 'RE41'), or (c) 1 of 2 endpoints covered and sanity check fails — partial match.
        # For mountain-mode features, also search the "train" bucket since WAB/JB/MGB service
        # is carried as GTFS train type=2 routes under short_name "R".
        ep_count = _count_endpoints_covered(osm_pts, best_coords, ENDPOINT_THRESHOLD_KM) if best_coords else 0
        needs_fallback = not best_coords or ep_count == 0
        if not needs_fallback and ep_count == 1:
            if not _passes_geo_sanity(osm_pts, best_coords, stop_meta, osm_from, osm_to, osm_stop_nodes, osm_line_km,
                                       skip_upper_density=(mode == "regional_bus")):
                needs_fallback = True
        if needs_fallback:
            if mode == "ferry":
                # Ferry: collect ALL pier stops from any GTFS ferry route within the bbox,
                # deduped by position. OSM ref ≠ GTFS short_name so we can't ref-match.
                seen_pos: set = set()
                for (lk_ref, lk_bucket), lk_candidates in _line_canonical_export.items():
                    if lk_bucket != "ferry":
                        continue
                    for (_, cand, _da) in lk_candidates:
                        for stop_id, _a, _d in cand:
                            c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                            if c and stop_near_bbox(c[0], c[1], bbox, margin=0.01):
                                key = (round(c[0], 4), round(c[1], 4))
                                if key not in seen_pos:
                                    seen_pos.add(key)
                                    best_coords.append([c[0], c[1], stop_id])
            else:
                search_buckets = {bucket}
                if bucket == "mountain":
                    search_buckets.add("train")
                # Collect all scored candidates, then pick the highest-scoring one
                # that passes the geo sanity checks.  Sorting first means we check
                # the most-likely-correct candidates first and exit early.
                geo_candidates: list = []  # (score, ccoords, lk_ref, full_density)
                for (lk_ref, lk_bucket), lk_candidates in _line_canonical_export.items():
                    if lk_bucket not in search_buckets:
                        continue
                    for (line_key, cand, _da) in lk_candidates:
                        if not cand:
                            continue
                        ccoords = []
                        for stop_id, _arr, _dep in cand:
                            c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                            if c and any(stop_near_bbox(c[0], c[1], sb) for sb in sub_bboxes):
                                ccoords.append([c[0], c[1], stop_id])
                        if len(ccoords) < 2:
                            continue
                        score = len(ccoords) / len(cand)
                        if score < 0.5:
                            continue
                        _gb = (int(ccoords[0][0] / GEO_BUCKET_DEG), int(ccoords[0][1] / GEO_BUCKET_DEG))
                        full_density = _canonical_density.get((line_key, _gb), 0.0)
                        geo_candidates.append((score, ccoords, lk_ref, full_density))

                # Sort: bbox score first, then endpoint coverage (0/1/2 at 500m threshold),
                # then absolute stop count. Equal-score full-corridor routes beat partial ones.
                geo_candidates.sort(
                    key=lambda x: (-x[0], -_count_endpoints_covered(osm_pts, x[1]), -len(x[1]))
                )
                geo_best: list = []
                for _score, _ccoords, _lk_ref, _full_density in geo_candidates[:50]:
                    if _passes_geo_sanity(osm_pts, _ccoords, stop_meta, osm_from, osm_to, osm_stop_nodes, osm_line_km,
                                          cand_full_density=_full_density, skip_upper_density=(mode == "regional_bus")):
                        geo_best = _ccoords
                        geo_best_ref = _lk_ref
                        break
                if geo_best:
                    best_coords = geo_best
                elif mode == "mountain":
                    # Mountain rack railways (WAB, JB, BRB, SPB, GGB, PB, RB, MG, DFB) are
                    # seasonal — their GTFS CC/type=2 entries may be pruned from
                    # _line_canonical_export by the low-service filter when sample dates fall
                    # outside the operating season.  Fall back to a terminal-name stop lookup:
                    # search stop_meta for stops whose normalised name matches the OSM from/to
                    # tag and whose coordinates are within this route's bbox.
                    norm_from = _norm_stop_name(osm_from)
                    norm_to   = _norm_stop_name(osm_to)
                    term_stops: list = []
                    seen_term_coords: set = set()
                    for _sid, (_sname, _parent) in stop_meta.items():
                        _norm_sname = _norm_stop_name(_sname)
                        if not _norm_sname:
                            continue
                        if not (
                            (norm_from and len(norm_from) >= 4 and _norm_sname == norm_from) or
                            (norm_to   and len(norm_to)   >= 4 and _norm_sname == norm_to)
                        ):
                            continue
                        _tc = stop_coords.get(_sid) or stop_coords.get(_sid.split(":")[0])
                        if not _tc or not stop_near_bbox(_tc[0], _tc[1], bbox, margin=0.05):
                            continue
                        _key = (round(_tc[0], 3), round(_tc[1], 3))
                        if _key in seen_term_coords:
                            continue
                        seen_term_coords.add(_key)
                        term_stops.append([_tc[0], _tc[1], _sid])
                    if len(term_stops) >= 2:
                        best_coords = term_stops
                    else:
                        best_coords = []
                        excluded_osm_ids.add(osm_id)
                        excluded_details.append({
                            "osm_id": osm_id,
                            "ref":    feat["properties"]["ref"],
                            "mode":   feat["properties"]["mode"],
                            "name":   feat["properties"].get("name", ""),
                        })
                else:
                    # No geo candidate passed sanity. The geo-fallback only triggers when
                    # best_coords is already suspect (empty or failed endpoint coverage).
                    # If geo finds nothing valid either, discard rather than keep wrong stops.
                    best_coords = []
                    excluded_osm_ids.add(osm_id)
                    excluded_details.append({
                        "osm_id": osm_id,
                        "ref":    feat["properties"]["ref"],
                        "mode":   feat["properties"]["mode"],
                        "name":   feat["properties"].get("name", ""),
                    })

        if best_coords:
            gtfs_ref = geo_best_ref or matched_gtfs_ref or ref
            line_stops_out[osm_id] = {"gtfs_ref": gtfs_ref, "osm_ref": ref, "stops": best_coords, "_bucket": bucket}

    line_canonical_export = None  # free reference

    # Dedup: if a gtfs_ref group has any direct-ref match (osm_ref ≈ gtfs_ref after
    # normalization), remove all fallback-matched entries (osm_ref ≠ gtfs_ref) so the
    # same GTFS line doesn't appear twice under different OSM route refs.
    def _refs_match(osm_ref: str, gtfs_ref: str) -> bool:
        norm = lambda s: s.replace(" ", "").lower()
        return norm(osm_ref) == norm(gtfs_ref)

    by_gtfs_ref: dict = defaultdict(list)
    for osm_id, entry in line_stops_out.items():
        if entry.get("gtfs_ref"):
            by_gtfs_ref[entry["gtfs_ref"]].append(osm_id)

    # Quick lookup for dedup logging: osm_id → {ref, name, mode, operator}
    feat_props_by_id = {
        str(f["properties"]["osm_id"]): f["properties"]
        for f in features if f["properties"].get("osm_id")
    }

    dedup_removed: set = set()
    for gtfs_r, osm_ids in by_gtfs_ref.items():
        direct = [oid for oid in osm_ids if _refs_match(line_stops_out[oid]["osm_ref"], gtfs_r)]
        if direct:
            fallback = [oid for oid in osm_ids if not _refs_match(line_stops_out[oid]["osm_ref"], gtfs_r)]
            dedup_removed.update(fallback)

    for oid in dedup_removed:
        fp = feat_props_by_id.get(oid, {})
        dropped_details.append({
            "osm_id":           oid,
            "ref":              fp.get("ref", line_stops_out[oid].get("osm_ref", "")),
            "name":             fp.get("name", ""),
            "mode":             fp.get("mode", ""),
            "operator":         fp.get("operator", ""),
            "matched_line_key": None,
            "freq_score":       fp.get("freq_score"),
            "reason":           "dedup",
            "gtfs_ref":         line_stops_out[oid].get("gtfs_ref", ""),
        })
        del line_stops_out[oid]

    if dedup_removed:
        print(f"  Dedup-removed:  {len(dedup_removed)} fallback-matched lines superseded by direct-ref match")

    # Group-level stop reassignment: use combined geometry of all OSM relations for a
    # gtfs_ref so stops are included if near ANY relation in the group, then placed on
    # the closest one. Fixes variant bleeding (e.g. Glattbrugg leaking into Hardbrücke).
    geom_by_id_grp: dict = {}
    for feat in features:
        oid = str(feat["properties"]["osm_id"])
        geom = feat["geometry"]
        geom_by_id_grp[oid] = (
            [c for seg in geom["coordinates"] for c in seg]
            if geom["type"] == "MultiLineString"
            else geom["coordinates"]
        )

    by_ref_bucket: dict = defaultdict(list)
    for osm_id, entry in line_stops_out.items():
        gtfs_r = entry.get("gtfs_ref")
        bkt = entry.get("_bucket")
        if gtfs_r and bkt:
            by_ref_bucket[(gtfs_r, bkt)].append(osm_id)

    n_grp = 0
    for (gtfs_r, bkt), osm_ids in by_ref_bucket.items():
        if len(osm_ids) < 2:
            continue
        if not any((v, bkt) in _line_canonical_export for v in [gtfs_r, gtfs_r.upper(), gtfs_r.lower()]):
            continue
        new_asgn = _group_reassign_stops(osm_ids, gtfs_r, bkt, geom_by_id_grp, stop_coords)
        for oid in osm_ids:
            new_stops = new_asgn.get(oid, [])
            if new_stops:
                line_stops_out[oid]["stops"] = new_stops
        n_grp += 1

    if n_grp:
        print(f"  Group reassignment: {n_grp} groups processed")

    for entry in line_stops_out.values():
        entry.pop("_bucket", None)

    OUT_STOPS.write_text(json.dumps(line_stops_out))
    print(f"  Stop coords: {sum(len(v['stops']) for v in line_stops_out.values()):,} stops across {len(line_stops_out):,} lines → {OUT_STOPS}")

    OUT_EXCLUDED.write_text(json.dumps(excluded_details, ensure_ascii=False))
    print(f"  Sanity log:  {len(excluded_details)} excluded lines → {OUT_EXCLUDED}")

    # Dropped routes log (no_gtfs, zero_freq, dedup)
    OUT_DROPPED.write_text(json.dumps(dropped_details, ensure_ascii=False))
    print(f"  Dropped log: {len(dropped_details)} dropped lines → {OUT_DROPPED}")

    # GTFS lines never matched to any drawn OSM route
    all_gtfs_line_keys: set = set()
    for candidates in _line_canonical_export.values():
        for (lk, _stops, _da) in candidates:
            all_gtfs_line_keys.add(lk)
    unmatched_lks = all_gtfs_line_keys - matched_gtfs_line_keys
    gtfs_unmatched_out = []
    for lk in sorted(unmatched_lks, key=lambda x: (x[2], x[0], x[1])):
        short_name, long_name, bucket = lk
        freq = line_freq.get(lk, {"core_wd": 0, "eve_wd": 0, "we": 0})
        mode_approx = _BUCKET_MODE_APPROX.get(bucket, "regional_bus")
        fs = compute_freq_score(freq, mode_approx)
        if fs == 0.0:
            continue   # zero-service lines: not interesting for unmatched review
        gtfs_unmatched_out.append({
            "short_name":  short_name,
            "long_name":   long_name,
            "bucket":      bucket,
            "freq_score":  round(fs, 3),
            "total_trips": sum(freq.values()),
        })
    OUT_GTFS_UNMATCHED.write_text(json.dumps(gtfs_unmatched_out, ensure_ascii=False))
    print(f"  GTFS unmatched: {len(gtfs_unmatched_out)} lines with service but no OSM match → {OUT_GTFS_UNMATCHED}")

    # Remove lines whose geo sanity check rejected all candidates — they have no valid
    # GTFS-backed stops and must not be drawn.  Also remove dedup-eliminated lines.
    excluded_osm_ids |= dedup_removed
    if excluded_osm_ids:
        before = len(features)
        features = [f for f in features
                    if str(f["properties"]["osm_id"]) not in excluded_osm_ids]
        n_sanity_excluded = before - len(features)
        OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        print(f"  Sanity-excluded:  {n_sanity_excluded} lines removed from output")

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
