"""GTFS feed loaders + mode classification.

Reads the pfaedle-routed feed at data/gtfs_routed/. Pure I/O and
mode/bucket classification; independent of the emission pipeline.
"""
import csv
import sys
from collections import defaultdict
from typing import Optional

from common import PROJECT_ROOT
from geometry import parse_time

from .frequency import _sample_dates

GTFS = PROJECT_ROOT / "data" / "gtfs_routed"

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
    "812",   # AAGR (Auto AG Rothenburg)
    "819",   # ARAG (Automobil Rottal AG)
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


# Coarse bucket → mode mapping used when the full context (short_name,
# length, route_type) isn't available. Bus collapses to regional_bus as the
# safe default; every other bucket maps to itself.
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
    """{stop_id: (lon, lat)}, plus one alias per station UIC so lookups
    keyed by the merged UIC (dwell, grouping centroids, direction
    endpoints) resolve to a representative coordinate. The UIC comes
    from the step-04 identity table — the SLOID scheme's stop_ids carry
    no parseable UIC (see sloid-stop-identity.md)."""
    from .stop_identity import load_identity
    identity = load_identity()
    coords = {}
    with open(GTFS / "stops.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sid = row["stop_id"]
            if sid.startswith("0000"):
                continue
            try:
                lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
            except ValueError:
                continue
            coords[sid] = (lon, lat)
            e = identity.get(sid)
            uic = e["uic"] if e else ""
            if uic and uic not in coords:
                coords[uic] = (lon, lat)
    return coords


def load_stop_meta() -> dict:
    """Return {stop_id: {"name": str, "parent": str, "platform_code": str}}.

    The official OTD GTFS feed prefixes parent_station values with `Parent`
    (e.g. `Parent8507000`); the prefix is stripped so downstream clustering
    and comparisons are format-agnostic. `platform_code` is the raw GTFS
    field (empty string when the feed omits it).
    """
    from .stop_identity import load_identity
    identity = load_identity()
    meta: dict = {}
    stops_txt = GTFS / "stops.txt"
    # No exists() guard: a missing stops.txt must fail loudly here rather
    # than let the pipeline run to completion on an empty stop table.
    with open(stops_txt, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sid = row["stop_id"]
            if sid.startswith("0000"):
                continue
            e = identity.get(sid)
            # platform_code: the identity table's track code, so sector
            # variants surface their track ("19"), never the sector
            # range ("19A-D") — rendering is track-granular per
            # sloid-stop-identity.md. Raw GTFS platform_code is the
            # fallback for stops the table doesn't know.
            entry = {
                "name": row.get("stop_name", ""),
                "parent": row.get("parent_station", "").removeprefix("Parent"),
                "uic": e["uic"] if e else "",
                "platform_code": (e["track"] if e else "")
                                 or (row.get("platform_code") or "").strip(),
            }
            meta[sid] = entry
            uic = entry["uic"]
            if uic and uic not in meta:
                meta[uic] = entry
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
