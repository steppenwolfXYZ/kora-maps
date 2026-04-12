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
  intercity      — IC, IR, EC, TGV, ICE, RJ, EN (red, thick)
  train          — S-Bahn, RegioExpress, RE, R, TER (red, thinner)
  tram           — trams, light rail (reddish purple)
  metro          — underground (green)
  bus            — city buses, avg speed <30 km/h (blue)
  regional_bus   — PostBus/regional, avg speed ≥30 km/h (turquoise)
  ferry          — boats (blue)
  mountain       — funicular, gondola, cable car (yellow)

  Long-distance coaches (Flixbus etc.) → excluded entirely.
  Trolleybuses → treated as bus.

Frequency scoring:
  score = min(1.0, best_headway / actual_headway)
  Best headways: intercity=30min, train=12min, tram=7min, metro=5min,
                 bus=6min, regional_bus=30min, ferry=45min, mountain=60min
  Malus applied for sparse evening/weekend service.
"""

import csv
import json
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
MALUS_LOW_EVENING = 0.08   # evening headway > LOW_EVE threshold
MALUS_NO_EVENING  = 0.18   # no evening service
MALUS_LOW_WEEKEND = 0.06   # weekend headway > LOW_WE threshold
MALUS_NO_WEEKEND  = 0.14   # no weekend service

# "Low service" evening/weekend headway thresholds per mode
LOW_EVE_HEADWAY = {
    "intercity": 60, "train": 30, "tram": 20, "metro": 15,
    "bus": 20, "regional_bus": 60, "ferry": 90, "mountain": 120,
}
LOW_WE_HEADWAY = {
    "intercity": 90, "train": 60, "tram": 30, "metro": 20,
    "bus": 30, "regional_bus": 90, "ferry": 120, "mountain": 120,
}

# Best headway per mode (minutes) — at this headway, core_score = 1.0
BEST_HEADWAY = {
    "intercity":    30,
    "train":        12,
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
        ref_upper = ref.upper()
        if any(x in ref_upper for x in ("IC", "IR", "EC", "TGV", "ICE", "RJ", "EN")):
            return "intercity"
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
    "intercity":    0,    # red
    "train":        0,    # red
    "tram":       290,    # purple shifted towards red (was 270)
    "metro":      120,    # green
    "bus":        220,    # blue
    "regional_bus": 180,  # turquoise
    "ferry":      220,    # blue (same as bus; no geographic overlap)
    "mountain":   320,    # deep pink / magenta
}

def freq_to_color(mode: str, freq_score: float) -> str:
    """Convert mode + frequency score (0–1) to hex color via HSL."""
    if mode == "mountain":
        # Mountain lines: fixed light yellow, no frequency variance
        return "#ffe566"
    hue = MODE_HUE.get(mode, 220) / 360.0
    # Low freq → light + desaturated.  High freq → dark + vivid.
    s = 0.20 + freq_score * 0.72   # 20% → 92%
    l = 0.77 - freq_score * 0.50   # 77% → 27%  (midpoint between original and current)
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
    svc_dates: dict = defaultdict(set)
    with open(GTFS / "calendar_dates.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["exception_type"] == "1":
                svc_dates[row["service_id"]].add(row["date"])
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
        if n > line_canonical.get(line_key, {}).get("stop_count", 0):
            line_canonical[line_key] = {
                "stop_count": n,
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
        existing = line_canonical_geo.get(geo_key)
        if existing is None or n > existing["stop_count"]:
            line_canonical_geo[geo_key] = {
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
    _line_canonical_export.clear()
    for (line_key, _gb), canon in line_canonical_geo.items():
        short_name, long_name, bucket = line_key
        _line_canonical_export[(short_name, bucket)].append(canon["stops"])
        long_norm = long_name.replace(" ", "")
        if long_norm and long_norm != short_name:
            _line_canonical_export[(long_norm, bucket)].append(canon["stops"])

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
    if eve_trips >= 2:
        eve_hw = EVENING_MINUTES / eve_trips
        if eve_hw > low_eve:
            core_score -= MALUS_LOW_EVENING
    elif eve_trips == 0:
        core_score -= MALUS_NO_EVENING

    # Weekend malus
    low_we = LOW_WE_HEADWAY.get(mode, 60)
    if we_trips >= 2:
        we_hw = WEEKEND_MINUTES / we_trips
        if we_hw > low_we:
            core_score -= MALUS_LOW_WEEKEND
    elif we_trips == 0:
        core_score -= MALUS_NO_WEEKEND

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


def speed_to_width_base(speed_kmh, mode) -> float:
    if mode == "intercity":    return 4.0
    if mode == "mountain":     return 1.0   # narrow — mountain lines are accent lines
    if mode == "regional_bus":
        # Regional buses are thicker than city buses at the same speed
        if speed_kmh is None:  return 2.0
        if speed_kmh >= 60:    return 3.0
        if speed_kmh >= 40:    return 2.5
        return 2.0
    if speed_kmh is None:      return 1.5
    if speed_kmh >= 80:        return 3.5
    if speed_kmh >= 40:        return 2.5
    if speed_kmh >= 20:        return 2.0
    return 1.5


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
        "intercity": "train", "train": "train",
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

        # OSM route bbox (used for geographic checks below)
        geom = feat["geometry"]
        osm_pts = ([c for seg in geom["coordinates"] for c in seg]
                   if geom["type"] == "MultiLineString" else geom["coordinates"])
        osm_bbox = line_bbox(osm_pts)

        # Cross-bucket fallback: some OSM 'train' routes are GTFS type 6/7 (cog/mountain railway).
        # Apply only when the matched GTFS canonical stops actually overlap the OSM route's bbox,
        # to avoid false matches where different Swiss railways coincidentally share the same ref
        # (e.g. FUN 311 = Stanserhornbahn/VerAlp matching OSM ref=311 in Bernese Oberland).
        if gtfs is None and bucket == "train":
            for k in [("mountain", ref), ("mountain", ref_norm), ("mountain", ref.upper())]:
                candidate_gtfs = gtfs_index.get(k)
                if not candidate_gtfs:
                    continue
                # Geographic check: look for a canonical trip with stops near the OSM bbox
                geo_ok = False
                for lk in [(ref, "mountain"), (ref_norm, "mountain")]:
                    for cand_stops in _line_canonical_export.get(lk, []):
                        n_inside = sum(1 for sid, arr, dep in cand_stops
                                       if (c := stop_coords.get(sid) or stop_coords.get(sid.split(":")[0]))
                                       and stop_near_bbox(c[0], c[1], osm_bbox))
                        if n_inside >= 2:
                            geo_ok = True
                            break
                    if geo_ok:
                        break
                if geo_ok:
                    gtfs = candidate_gtfs
                    mode = "mountain"
                    bucket = "mountain"
                    break

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

        # Refine bus → regional_bus based on urban land fraction.
        # If ≥50% of the route passes through urban landuse (residential/commercial/industrial),
        # it's a city bus; otherwise regional.  Falls back to length threshold when urban_fraction
        # was not computed (non-bus routes or old routes.geojson without the property).
        if mode == "bus":
            urban_frac = props.get("urban_fraction")
            if urban_frac is not None:
                if urban_frac < 0.5:
                    mode = "regional_bus"
            else:
                line_length_km = props.get("raw_length_km", props.get("length_km", 0))
                if line_length_km >= REGIONAL_BUS_MIN_LENGTH:
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
            # Pick whichever gives a higher core_wd count
            raw_freq = corr_raw if (corr_raw and corr_raw["core_wd"] > own_raw["core_wd"]) \
                       else own_raw
            freq_score = compute_freq_score(raw_freq, mode)
            stats["matched"] += 1
        elif mode == "mountain":
            # Exclude ski infrastructure incorrectly tagged as transit routes
            MOUNTAIN_EXCLUDE_KEYWORDS = {"skipiste", "skis ", "piste ", "skipisten"}
            name_lower = osm_name.lower()
            if any(kw in name_lower for kw in MOUNTAIN_EXCLUDE_KEYWORDS):
                stats["unmatched"] += 1
                continue
            # Unmatched mountain route (no GTFS name match but OSM name clearly indicates
            # a mountain railway) → show with default score rather than hiding
            freq_score = 0.6
            stats["matched"] += 1
        else:
            freq_score = None   # unmatched → skip (don't draw)
            stats["unmatched"] += 1

        if freq_score is None:
            continue

        # Mountain railways are always worth showing; clamp to a visible minimum
        if mode == "mountain" and freq_score < 0.4:
            freq_score = 0.4

        color      = freq_to_color(mode, freq_score)
        width_base = speed_to_width_base(speed_kmh, mode)

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

    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}))

    # Save stop coordinates per line (osm_id → [[lon,lat], ...]) for stop dot rendering
    line_stops_out = {}
    for feat in features:
        osm_id = str(feat["properties"]["osm_id"])
        ref    = feat["properties"]["ref"]
        mode   = feat["properties"]["mode"]
        bucket = MODE_TO_BUCKET.get(mode, "bus")
        ref_norm = ref.replace(" ", "")
        gtfs   = gtfs_index.get((bucket, ref))
        if gtfs is None:
            for k in [(bucket, ref_norm),
                      (bucket, ref.upper()),
                      (bucket, ref.lower())]:
                gtfs = gtfs_index.get(k)
                if gtfs: break
        if gtfs is None:
            gtfs = gtfs_long_index.get((bucket, ref_norm)) or \
                   gtfs_long_index.get((bucket, ref_norm.upper()))
        if gtfs is None:
            continue
        # Reconstruct stop coords from canonical trip
        canon = None
        for lk in [(ref, bucket), (ref_norm, bucket),
                   (ref.upper(), bucket), (ref.lower(), bucket),
                   (ref_norm.upper(), bucket)]:
            if lk in _line_canonical_export:
                canon = _line_canonical_export[lk]
                break
        if not canon:
            continue
        # Compute OSM line bbox to filter out wrong-city GTFS stops
        geom = feat["geometry"]
        if geom["type"] == "MultiLineString":
            osm_pts = [c for seg in geom["coordinates"] for c in seg]
        else:
            osm_pts = geom["coordinates"]
        bbox = line_bbox(osm_pts)
        # canon is a list of candidates (each a list of (stop_id, arr, dep) tuples)
        candidates = canon
        best_coords: list = []
        for candidate in candidates:
            ccoords = []
            for stop_id, arr, dep in candidate:
                c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                if c and stop_near_bbox(c[0], c[1], bbox):
                    ccoords.append(list(c))
            if len(ccoords) > len(best_coords):
                best_coords = ccoords
        coords = best_coords
        if coords:
            line_stops_out[osm_id] = coords

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
