#!/usr/bin/env python3
"""
Step 04 — Preprocess the GTFS feed for pfaedle.

Drops trips that pfaedle should not route:
  • Trips belonging to excluded agencies (long-distance coaches).
    Match is case-insensitive substring on agency_name vs config
    `excluded_agencies`.
  • Trips whose route_short_name begins with "EV" (Bahnersatz /
    rail-replacement buses). The MVP map shows general connections,
    not construction-period substitutes; a future daily-updating
    variant will reintroduce them.
  • Trips with any stop outside the bbox declared in
    config `osm_bbox`. (Foreign-terminus trips.)

Writes a filtered GTFS folder at `data/gtfs_filtered/`. Files unaffected by
the filter (calendar, feed_info, transfers, frequencies) are copied verbatim.
Stop coverage is preserved — we do not prune unreferenced stops because
pfaedle does not care, and downstream stages still need full stop metadata.

Diagnostic side-effect: writes data/transit/gtfs_filtered.json listing the
dropped trips' route identity and reason.

Streaming assumption: stop_times.txt rows for one trip_id are contiguous.
Verified once on the current feed; the script raises if violated.
"""

import csv
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GTFS_IN = ROOT / "data" / "gtfs"
GTFS_OUT = ROOT / "data" / "gtfs_filtered"
CFG_PATH = ROOT / "scripts" / "transit" / "config.yaml"
DIAG_OUT = ROOT / "data" / "transit" / "gtfs_filtered.json"


def load_cfg() -> dict:
    return yaml.safe_load(CFG_PATH.read_text())


def in_bbox(lon: float, lat: float, bbox: dict) -> bool:
    return (bbox["min_lon"] <= lon <= bbox["max_lon"]
            and bbox["min_lat"] <= lat <= bbox["max_lat"])


def load_stop_coords() -> dict:
    """{stop_id: (lon, lat)}. Skips rows without coords."""
    out = {}
    with open(GTFS_IN / "stops.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                lon = float(row["stop_lon"])
                lat = float(row["stop_lat"])
            except (KeyError, ValueError):
                continue
            out[row["stop_id"]] = (lon, lat)
    return out


def identify_excluded_agencies(excluded_tokens: list) -> set:
    """{agency_id} for agencies whose lowercased name contains any token."""
    tokens = [t.lower() for t in excluded_tokens]
    out = set()
    with open(GTFS_IN / "agency.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("agency_name") or "").lower()
            if any(tok in name for tok in tokens):
                out.add(row["agency_id"])
    return out


def identify_excluded_routes(excluded_agencies: set) -> tuple:
    """
    Returns (excluded_route_ids, route_to_agency, route_drop_reason).
    A route is excluded if its agency is in `excluded_agencies` OR its
    `route_short_name` begins with "EV" (Bahnersatz / rail-replacement).
    Agency takes precedence in the reason map when both apply.
    """
    excluded = set()
    route_to_agency = {}
    route_drop_reason: dict = {}
    with open(GTFS_IN / "routes.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rid = row["route_id"]
            aid = row.get("agency_id", "")
            short = (row.get("route_short_name") or "").strip().upper()
            route_to_agency[rid] = aid
            if aid in excluded_agencies:
                excluded.add(rid)
                route_drop_reason[rid] = "agency"
            elif short.startswith("EV"):
                excluded.add(rid)
                route_drop_reason[rid] = "ev_route"
    return excluded, route_to_agency, route_drop_reason


def load_trips_index(excluded_route_ids: set) -> tuple:
    """
    Returns (trip_to_route, trips_excluded_by_route).
    Reads trips.txt once.
    """
    trip_to_route = {}
    excl = set()
    with open(GTFS_IN / "trips.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            tid = row["trip_id"]
            rid = row["route_id"]
            trip_to_route[tid] = rid
            if rid in excluded_route_ids:
                excl.add(tid)
    return trip_to_route, excl


def stream_filter_stop_times(stop_coords: dict, bbox: dict,
                             trips_excluded_by_route: set) -> tuple:
    """
    Streams stop_times.txt once, writes the filtered version to GTFS_OUT,
    and returns (foreign_terminus_trips, total_trips, kept_trips).
    Decides per-trip when the trip_id changes (relies on rows being
    contiguous per trip_id).
    """
    src = GTFS_IN / "stop_times.txt"
    dst = GTFS_OUT / "stop_times.txt"

    foreign_terminus = set()
    total_trips = 0
    kept_trips = 0
    n_time_repairs = 0
    seen_trips: set = set()

    with open(src, encoding="utf-8-sig", newline="") as fin, \
         open(dst, "w", encoding="utf-8", newline="") as fout:

        header_line = fin.readline()
        fout.write(header_line)
        header = header_line.rstrip("\n\r").split(",")
        try:
            trip_idx = header.index("trip_id")
            stop_idx = header.index("stop_id")
            arr_idx  = header.index("arrival_time")
            dep_idx  = header.index("departure_time")
        except ValueError:
            sys.exit("stop_times.txt missing required columns")

        cur_trip = None
        cur_rows: list = []
        cur_any_foreign = False
        cur_excluded = False

        def flush():
            nonlocal kept_trips
            if cur_trip is None:
                return
            if cur_excluded or cur_any_foreign:
                return
            for line in cur_rows:
                fout.write(line)
            kept_trips += 1

        for line in fin:
            # Cheap split: respect commas inside quoted stop_headsign etc.
            if '"' in line:
                row = next(csv.reader([line]))
                quoted = True
            else:
                row = line.rstrip("\n\r").split(",")
                quoted = False
            tid = row[trip_idx]
            sid = row[stop_idx]
            arr = row[arr_idx]
            dep = row[dep_idx]

            # Repair: pfaedle (and the GTFS spec) reject rows where the vehicle
            # appears to depart before it arrives. The SBB feed has a handful
            # of single-second/single-minute glitches. Clamp dep = arr.
            if arr and dep and arr > dep:
                row[dep_idx] = arr
                if quoted:
                    line = ",".join(_csv_escape(f) for f in row) + "\n"
                else:
                    line = ",".join(row)
                    if not line.endswith("\n"):
                        line += "\n"
                n_time_repairs += 1

            if tid != cur_trip:
                flush()
                if cur_trip is not None:
                    total_trips += 1
                if tid in seen_trips:
                    sys.exit(f"stop_times.txt: trip_id {tid!r} appears "
                             "non-contiguously — preprocessing assumes "
                             "contiguous trip blocks")
                cur_trip = tid
                seen_trips.add(tid)
                cur_rows = []
                cur_any_foreign = False
                cur_excluded = tid in trips_excluded_by_route

            cur_rows.append(line)

            if cur_excluded:
                continue
            coords = stop_coords.get(sid)
            if coords is None:
                # No coords — treat as foreign to be safe.
                cur_any_foreign = True
                foreign_terminus.add(tid)
                continue
            if not in_bbox(coords[0], coords[1], bbox):
                cur_any_foreign = True
                foreign_terminus.add(tid)

        # Final trip
        flush()
        if cur_trip is not None:
            total_trips += 1

    return foreign_terminus, total_trips, kept_trips, n_time_repairs


def _csv_escape(field: str) -> str:
    """Minimal CSV field escaping for stop_times rewrite."""
    if any(c in field for c in (",", '"', "\n")):
        return '"' + field.replace('"', '""') + '"'
    return field


def write_filtered_trips(dropped_trip_ids: set) -> tuple:
    """Writes trips.txt and returns (kept_route_ids, kept_trip_count)."""
    src = GTFS_IN / "trips.txt"
    dst = GTFS_OUT / "trips.txt"
    kept_route_ids: set = set()
    kept = 0
    with open(src, encoding="utf-8-sig", newline="") as fin, \
         open(dst, "w", encoding="utf-8", newline="") as fout:
        reader = csv.reader(fin)
        writer = csv.writer(fout)
        header = next(reader)
        writer.writerow(header)
        tid_idx = header.index("trip_id")
        rid_idx = header.index("route_id")
        for row in reader:
            if row[tid_idx] in dropped_trip_ids:
                continue
            writer.writerow(row)
            kept_route_ids.add(row[rid_idx])
            kept += 1
    return kept_route_ids, kept


def write_filtered_routes(kept_route_ids: set) -> tuple:
    """Writes routes.txt restricted to kept_route_ids."""
    src = GTFS_IN / "routes.txt"
    dst = GTFS_OUT / "routes.txt"
    kept_agency_ids: set = set()
    kept = 0
    with open(src, encoding="utf-8-sig", newline="") as fin, \
         open(dst, "w", encoding="utf-8", newline="") as fout:
        reader = csv.reader(fin)
        writer = csv.writer(fout)
        header = next(reader)
        writer.writerow(header)
        rid_idx = header.index("route_id")
        aid_idx = header.index("agency_id")
        for row in reader:
            if row[rid_idx] not in kept_route_ids:
                continue
            writer.writerow(row)
            kept_agency_ids.add(row[aid_idx])
            kept += 1
    return kept_agency_ids, kept


def write_filtered_agency(kept_agency_ids: set) -> int:
    src = GTFS_IN / "agency.txt"
    dst = GTFS_OUT / "agency.txt"
    kept = 0
    with open(src, encoding="utf-8-sig", newline="") as fin, \
         open(dst, "w", encoding="utf-8", newline="") as fout:
        reader = csv.reader(fin)
        writer = csv.writer(fout)
        header = next(reader)
        writer.writerow(header)
        aid_idx = header.index("agency_id")
        for row in reader:
            if row[aid_idx] not in kept_agency_ids:
                continue
            writer.writerow(row)
            kept += 1
    return kept


def copy_verbatim(name: str) -> None:
    src = GTFS_IN / name
    if not src.exists():
        return
    shutil.copyfile(src, GTFS_OUT / name)


def main() -> None:
    if not GTFS_IN.exists():
        sys.exit(f"missing {GTFS_IN} — run 01_download_gtfs.py first")

    cfg = load_cfg()
    bbox = cfg["osm_bbox"]
    excluded_tokens = cfg.get("excluded_agencies", [])

    GTFS_OUT.mkdir(parents=True, exist_ok=True)
    DIAG_OUT.parent.mkdir(parents=True, exist_ok=True)

    print(f"Loading stops…")
    stop_coords = load_stop_coords()
    print(f"  {len(stop_coords):,} stops with coords")

    print(f"Identifying excluded agencies (tokens: {excluded_tokens})…")
    excluded_agencies = identify_excluded_agencies(excluded_tokens)
    print(f"  {len(excluded_agencies):,} agencies matched")

    print(f"Identifying excluded routes…")
    excluded_route_ids, route_to_agency, route_drop_reason = \
        identify_excluded_routes(excluded_agencies)
    n_agency = sum(1 for r in route_drop_reason.values() if r == "agency")
    n_ev = sum(1 for r in route_drop_reason.values() if r == "ev_route")
    print(f"  {len(excluded_route_ids):,} routes excluded "
          f"({n_agency:,} via excluded agencies, "
          f"{n_ev:,} via EV route_short_name)")

    print(f"Indexing trips…")
    trip_to_route, trips_excluded_by_route = load_trips_index(excluded_route_ids)
    print(f"  {len(trip_to_route):,} trips total, "
          f"{len(trips_excluded_by_route):,} via excluded routes")

    print(f"Streaming stop_times.txt → filter foreign-terminus trips…")
    foreign_terminus, total_trips, kept_trips, n_time_repairs = stream_filter_stop_times(
        stop_coords, bbox, trips_excluded_by_route)
    print(f"  {total_trips:,} trips scanned, "
          f"{len(foreign_terminus):,} foreign-terminus, "
          f"{kept_trips:,} kept")
    if n_time_repairs:
        print(f"  Repaired {n_time_repairs} rows with arrival_time > departure_time "
              "(clamped dep = arr)")

    dropped = trips_excluded_by_route | foreign_terminus
    print(f"Writing filtered trips.txt …")
    kept_route_ids, n_trips = write_filtered_trips(dropped)
    print(f"  {n_trips:,} trips, {len(kept_route_ids):,} distinct routes")

    print(f"Writing filtered routes.txt …")
    kept_agency_ids, n_routes = write_filtered_routes(kept_route_ids)
    print(f"  {n_routes:,} routes, {len(kept_agency_ids):,} distinct agencies")

    print(f"Writing filtered agency.txt …")
    n_agencies = write_filtered_agency(kept_agency_ids)
    print(f"  {n_agencies:,} agencies")

    print(f"Copying remaining GTFS files…")
    for name in ("stops.txt", "calendar.txt", "calendar_dates.txt",
                 "feed_info.txt", "transfers.txt", "frequencies.txt"):
        copy_verbatim(name)

    # Diagnostic: summarize dropped trips by route+reason.
    diag: dict = defaultdict(
        lambda: {"by_agency": 0, "by_ev_route": 0, "foreign_terminus": 0})
    for tid in trips_excluded_by_route:
        rid = trip_to_route.get(tid, "?")
        reason = route_drop_reason.get(rid, "agency")
        if reason == "ev_route":
            diag[rid]["by_ev_route"] += 1
        else:
            diag[rid]["by_agency"] += 1
    for tid in foreign_terminus:
        rid = trip_to_route.get(tid, "?")
        diag[rid]["foreign_terminus"] += 1
    summary = [
        {"route_id": rid, "agency_id": route_to_agency.get(rid, ""), **counts}
        for rid, counts in sorted(diag.items())
    ]
    DIAG_OUT.write_text(json.dumps(summary, ensure_ascii=False))
    print(f"  Diagnostic: {len(summary):,} routes affected → {DIAG_OUT}")

    print(f"\nDone. Filtered feed at {GTFS_OUT}")


if __name__ == "__main__":
    main()
