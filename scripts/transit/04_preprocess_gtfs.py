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
dropped trips' route identity and reason, and data/transit/gtfs_trip_splits.json
listing every original trip replaced by split-at-stop overrides.

Streaming assumption: stop_times.txt rows for one trip_id are contiguous.
Verified once on the current feed; the script raises if violated.

Trip splitting: `gtfs_trip_overrides` (action: split_at_stop) rewrites every
trip on a matched route into two new trips that share the original's metadata
but each cover only one segment of the stop sequence. Used for services where
two physically separate vehicles share one GTFS trip_id with a passenger
transfer in the middle (canonical case: Niesenbahn at Schwandegg). New trip
ids carry a `__1` / `__2` suffix; shape ids carry the same suffix so pfaedle
emits distinct shapes per leg.
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
SPLIT_DIAG_OUT = ROOT / "data" / "transit" / "gtfs_trip_splits.json"


def load_cfg() -> dict:
    return yaml.safe_load(CFG_PATH.read_text())


def in_bbox(lon: float, lat: float, bbox: dict) -> bool:
    return (bbox["min_lon"] <= lon <= bbox["max_lon"]
            and bbox["min_lat"] <= lat <= bbox["max_lat"])


# stop_id → UIC (didok column), filled by _load_sid_uic() in main().
# Since the 2026-06 SLOID migration the UIC is a column, not the stop_id
# prefix, so every config override that names a UIC must match through
# this map (see sloid-stop-identity.md).
_SID_UIC: dict = {}


def _load_sid_uic() -> None:
    with open(GTFS_IN / "stops.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            uic = (row.get("didok") or "").strip()
            if uic:
                _SID_UIC[row["stop_id"]] = uic


def _override_for(overrides: dict, row: dict):
    """Override lookup for a stops.txt row: exact stop_id, or the row's
    UIC (didok). UIC-keyed overrides move every row of the station —
    platforms and parent alike — which matches the legacy semantics
    where the bare-UIC row was the one trips referenced."""
    ov = overrides.get(row["stop_id"])
    if ov is not None:
        return ov
    uic = (row.get("didok") or "").strip()
    if uic:
        return overrides.get(uic)
    return None


def load_stop_overrides(cfg: dict) -> dict:
    """{stop_id: (lon, lat)} from config.gtfs_stop_overrides.

    Each configured stop_id also seeds the matching `Parent…` row so the
    location_type=1 station entry stays consistent with the platform entry.
    """
    out: dict = {}
    for entry in (cfg.get("gtfs_stop_overrides") or []):
        sid = str(entry.get("stop_id", "")).strip()
        if not sid:
            continue
        try:
            lon = float(entry["lon"])
            lat = float(entry["lat"])
        except (KeyError, ValueError, TypeError):
            continue
        out[sid] = (lon, lat)
        out[f"Parent{sid}"] = (lon, lat)
    return out


def load_stop_coords(overrides: dict) -> dict:
    """{stop_id: (lon, lat)}. Skips rows without coords. Applies overrides."""
    out = {}
    with open(GTFS_IN / "stops.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sid = row["stop_id"]
            ov = _override_for(overrides, row)
            if ov is not None:
                out[sid] = ov
                continue
            try:
                lon = float(row["stop_lon"])
                lat = float(row["stop_lat"])
            except (KeyError, ValueError):
                continue
            out[sid] = (lon, lat)
    return out


def write_filtered_stops(overrides: dict, waypoint_overrides: list) -> int:
    """Copy stops.txt to GTFS_OUT, replacing stop_lon/stop_lat for any row
    whose stop_id matches an override, and appending one synthetic `WPT:`
    stop row per insert_waypoint override so pfaedle can route through it.
    Returns the count of overridden rows.
    """
    src = GTFS_IN / "stops.txt"
    dst = GTFS_OUT / "stops.txt"
    n = 0
    with open(src, encoding="utf-8-sig", newline="") as fin, \
         open(dst, "w", encoding="utf-8", newline="") as fout:
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=reader.fieldnames,
                                quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in reader:
            ov = _override_for(overrides, row)
            if ov is not None:
                row["stop_lon"] = f"{ov[0]:.8f}"
                row["stop_lat"] = f"{ov[1]:.8f}"
                n += 1
            writer.writerow(row)
        for e in waypoint_overrides:
            row = {k: "" for k in reader.fieldnames}
            row["stop_id"] = e["wpt_sid"]
            if "stop_name" in row:
                row["stop_name"] = (f"WPT {e['route_short_name']} "
                                    f"after {e['after_stop_id']}")
            row["stop_lon"] = f"{e['lon']:.8f}"
            row["stop_lat"] = f"{e['lat']:.8f}"
            if "location_type" in row:
                row["location_type"] = "0"
            writer.writerow(row)
    return n


def load_trip_split_overrides(cfg: dict) -> list:
    """Normalized list of `gtfs_trip_overrides` entries with action
    `split_at_stop`. Entries missing required fields are skipped."""
    out: list = []
    for entry in (cfg.get("gtfs_trip_overrides") or []):
        action = (entry.get("action") or "").strip()
        if action != "split_at_stop":
            continue
        agency_id = str(entry.get("agency_id", "")).strip()
        rsn = str(entry.get("route_short_name", "")).strip()
        tsid = str(entry.get("transfer_stop_id", "")).strip()
        if not (agency_id and rsn and tsid):
            continue
        out.append({
            "agency_id": agency_id,
            "route_short_name": rsn,
            "transfer_stop_id": tsid,
            "reason": entry.get("reason", ""),
        })
    return out


def identify_split_routes(split_overrides: list) -> dict:
    """{route_id: transfer_stop_id} for every route_id whose
    (agency_id, route_short_name) matches an override entry."""
    if not split_overrides:
        return {}
    key_to_tsid = {
        (e["agency_id"], e["route_short_name"]): e["transfer_stop_id"]
        for e in split_overrides
    }
    out: dict = {}
    with open(GTFS_IN / "routes.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = (row.get("agency_id", "").strip(),
                   (row.get("route_short_name") or "").strip())
            if key in key_to_tsid:
                out[row["route_id"]] = key_to_tsid[key]
    return out


def _stop_matches_transfer(row_sid: str, transfer_sid: str) -> bool:
    """Stop_id match: exact; the row's UIC (didok, post-SLOID scheme);
    or — legacy scheme, when the override names a parent (no `:`
    suffix) — the row's stop_id prefix."""
    if row_sid == transfer_sid:
        return True
    if _SID_UIC.get(row_sid) == transfer_sid:
        return True
    if ":" not in transfer_sid and ":" in row_sid:
        return row_sid.split(":")[0] == transfer_sid
    return False


def load_waypoint_overrides(cfg: dict) -> list:
    """Normalized list of `gtfs_trip_overrides` entries with action
    `insert_waypoint` (see gtfs-trip-overrides concept). Entries missing
    required fields are skipped. Each entry carries a mutable
    `matched_trips` counter filled during the stop_times stream."""
    out: list = []
    for entry in (cfg.get("gtfs_trip_overrides") or []):
        action = (entry.get("action") or "").strip()
        if action != "insert_waypoint":
            continue
        agency_id = str(entry.get("agency_id", "")).strip()
        rsn = str(entry.get("route_short_name", "")).strip()
        after = str(entry.get("after_stop_id", "")).strip()
        before = str(entry.get("before_stop_id", "")).strip()
        wp = entry.get("waypoint") or []
        if not (agency_id and rsn and after and before and len(wp) == 2):
            continue
        out.append({
            "agency_id": agency_id,
            "route_short_name": rsn,
            "after_stop_id": after,
            "before_stop_id": before,
            "lon": float(wp[0]),
            "lat": float(wp[1]),
            "wpt_sid": f"WPT:{agency_id}:{rsn}:{after}",
            "matched_trips": 0,
            "reason": entry.get("reason", ""),
        })
    return out


def identify_waypoint_routes(waypoint_overrides: list) -> dict:
    """{route_id: [override entries]} for every route_id whose
    (agency_id, route_short_name) matches an insert_waypoint entry."""
    if not waypoint_overrides:
        return {}
    by_key: dict = defaultdict(list)
    for e in waypoint_overrides:
        by_key[(e["agency_id"], e["route_short_name"])].append(e)
    out: dict = defaultdict(list)
    with open(GTFS_IN / "routes.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = (row.get("agency_id", "").strip(),
                   (row.get("route_short_name") or "").strip())
            for e in by_key.get(key, []):
                out[row["route_id"]].append(e)
    return dict(out)


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
                             trips_excluded_by_route: set,
                             split_routes: dict,
                             waypoint_routes: dict,
                             trip_to_route: dict) -> tuple:
    """
    Streams stop_times.txt once, writes the filtered version to GTFS_OUT,
    and returns (foreign_terminus_trips, total_trips, kept_trips,
    n_time_repairs, split_map, split_warnings). Decides per-trip when the
    trip_id changes (relies on rows being contiguous per trip_id).

    For trips whose route is in `waypoint_routes`, a synthetic `WPT:` stop
    is inserted per matching insert_waypoint override — before the split
    handling, so both overrides compose on one route.

    For trips whose route is in `split_routes`, the trip is rewritten into two
    new trips with `__1` and `__2` suffixes on trip_id, split at the
    configured transfer stop. Failed splits (transfer stop not in sequence,
    or only at an endpoint) fall back to writing the trip unsplit and append
    a warning.
    """
    src = GTFS_IN / "stop_times.txt"
    dst = GTFS_OUT / "stop_times.txt"

    foreign_terminus = set()
    total_trips = 0
    kept_trips = 0
    n_time_repairs = 0
    seen_trips: set = set()
    split_map: dict = {}        # orig_tid -> [new_tid_1, new_tid_2]
    split_warnings: list = []

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
        seq_idx = header.index("stop_sequence") if "stop_sequence" in header else None
        dist_idx = (header.index("shape_dist_traveled")
                    if "shape_dist_traveled" in header else None)
        pickup_idx = (header.index("pickup_type")
                      if "pickup_type" in header else None)
        dropoff_idx = (header.index("drop_off_type")
                       if "drop_off_type" in header else None)

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
            rid = trip_to_route.get(cur_trip, "")
            rows = cur_rows
            wp_entries = waypoint_routes.get(rid)
            if wp_entries:
                rows = _insert_waypoint_rows(
                    rows, wp_entries, stop_idx, arr_idx, dep_idx,
                    seq_idx, dist_idx, pickup_idx, dropoff_idx)
            tsid = split_routes.get(rid)
            if tsid:
                split = _split_trip_rows(
                    cur_trip, rows, tsid,
                    trip_idx, stop_idx, seq_idx, dist_idx,
                    split_warnings, rid)
                if split is not None:
                    leg1_lines, leg2_lines, new_tid_1, new_tid_2 = split
                    for line in leg1_lines:
                        fout.write(line)
                    for line in leg2_lines:
                        fout.write(line)
                    split_map[cur_trip] = [new_tid_1, new_tid_2]
                    kept_trips += 2
                    return
            for line in rows:
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

    return (foreign_terminus, total_trips, kept_trips, n_time_repairs,
            split_map, split_warnings)


def _split_trip_rows(orig_tid: str, cur_rows: list, transfer_sid: str,
                      trip_idx: int, stop_idx: int,
                      seq_idx, dist_idx,
                      warnings_out: list, route_id: str):
    """Split `cur_rows` at the row whose stop_id matches `transfer_sid`.
    Returns (leg1_lines, leg2_lines, new_tid_1, new_tid_2) or None on failure
    (with a warning appended). The transfer-stop row appears as the last row
    of leg1 AND the first row of leg2."""
    parsed: list = []
    for line in cur_rows:
        if '"' in line:
            row = next(csv.reader([line]))
            quoted = True
        else:
            row = line.rstrip("\n\r").split(",")
            quoted = False
        parsed.append((row, quoted))

    transfer_pos = None
    for i, (row, _) in enumerate(parsed):
        if _stop_matches_transfer(row[stop_idx], transfer_sid):
            transfer_pos = i
            break
    if transfer_pos is None:
        warnings_out.append({"trip_id": orig_tid, "route_id": route_id,
                             "reason": "transfer_not_in_sequence"})
        return None
    if transfer_pos == 0 or transfer_pos == len(parsed) - 1:
        warnings_out.append({"trip_id": orig_tid, "route_id": route_id,
                             "reason": "transfer_at_endpoint"})
        return None

    new_tid_1 = f"{orig_tid}__1"
    new_tid_2 = f"{orig_tid}__2"

    def emit(parsed_slice, new_tid):
        out_lines = []
        for new_seq, (row, quoted) in enumerate(parsed_slice, 1):
            r = list(row)
            r[trip_idx] = new_tid
            if seq_idx is not None:
                r[seq_idx] = str(new_seq)
            if dist_idx is not None:
                r[dist_idx] = ""
            needs_quote = quoted or any(
                ("," in f or '"' in f or "\n" in f) for f in r)
            if needs_quote:
                out_lines.append(",".join(_csv_escape(f) for f in r) + "\n")
            else:
                out_lines.append(",".join(r) + "\n")
        return out_lines

    leg1 = emit(parsed[:transfer_pos + 1], new_tid_1)
    leg2 = emit(parsed[transfer_pos:],     new_tid_2)
    return leg1, leg2, new_tid_1, new_tid_2


def _insert_waypoint_rows(cur_rows: list, entries: list,
                           stop_idx: int, arr_idx: int, dep_idx: int,
                           seq_idx, dist_idx, pickup_idx, dropoff_idx):
    """Insert one synthetic `WPT:` stop row per matching override entry into
    a trip's rows, between `after_stop_id` and `before_stop_id` where they
    appear consecutively (gtfs-trip-overrides concept, insert_waypoint).

    The synthetic row is a copy of the after-stop's row with the waypoint
    stop_id, arrival = departure = the after-stop's departure (keeps times
    non-decreasing), no pickup/drop-off, and no shape_dist. Trips without
    the ordered pair are returned unchanged — short workings and variants
    are expected, so no per-trip warning. On insertion, stop_sequence is
    renumbered 1..n. Increments each applied entry's `matched_trips`."""
    parsed = []
    for line in cur_rows:
        if '"' in line:
            parsed.append((next(csv.reader([line])), True))
        else:
            parsed.append((line.rstrip("\n\r").split(","), False))

    inserted = False
    for e in entries:
        for i in range(len(parsed) - 1):
            if (_stop_matches_transfer(parsed[i][0][stop_idx],
                                       e["after_stop_id"])
                    and _stop_matches_transfer(parsed[i + 1][0][stop_idx],
                                               e["before_stop_id"])):
                r = list(parsed[i][0])
                r[stop_idx] = e["wpt_sid"]
                r[arr_idx] = parsed[i][0][dep_idx]
                r[dep_idx] = parsed[i][0][dep_idx]
                if dist_idx is not None:
                    r[dist_idx] = ""
                if pickup_idx is not None:
                    r[pickup_idx] = "1"
                if dropoff_idx is not None:
                    r[dropoff_idx] = "1"
                parsed.insert(i + 1, (r, parsed[i][1]))
                e["matched_trips"] += 1
                inserted = True
                break
    if not inserted:
        return cur_rows

    out = []
    for new_seq, (row, quoted) in enumerate(parsed, 1):
        r = list(row)
        if seq_idx is not None:
            r[seq_idx] = str(new_seq)
        needs_quote = quoted or any(
            ("," in f or '"' in f or "\n" in f) for f in r)
        if needs_quote:
            out.append(",".join(_csv_escape(f) for f in r) + "\n")
        else:
            out.append(",".join(r) + "\n")
    return out


def _csv_escape(field: str) -> str:
    """Minimal CSV field escaping for stop_times rewrite."""
    if any(c in field for c in (",", '"', "\n")):
        return '"' + field.replace('"', '""') + '"'
    return field


def write_filtered_trips(dropped_trip_ids: set, split_map: dict) -> tuple:
    """Writes trips.txt and returns (kept_route_ids, kept_trip_count).

    For trip_ids in `split_map`, writes two rows with the suffixed trip_ids
    and matching `__1` / `__2` suffixes on shape_id (so pfaedle emits distinct
    shapes per leg)."""
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
        shape_idx = header.index("shape_id") if "shape_id" in header else None
        for row in reader:
            tid = row[tid_idx]
            if tid in dropped_trip_ids:
                continue
            if tid in split_map:
                orig_shape = row[shape_idx] if shape_idx is not None else ""
                for leg_no, new_tid in enumerate(split_map[tid], 1):
                    new_row = list(row)
                    new_row[tid_idx] = new_tid
                    if shape_idx is not None and orig_shape:
                        new_row[shape_idx] = f"{orig_shape}__{leg_no}"
                    writer.writerow(new_row)
                    kept += 1
                kept_route_ids.add(row[rid_idx])
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


def write_filtered_frequencies(dropped_trip_ids: set, split_map: dict) -> tuple:
    """Copy frequencies.txt, skipping rows for dropped trips and duplicating
    rows for split trips so each new leg inherits the original headway."""
    src = GTFS_IN / "frequencies.txt"
    dst = GTFS_OUT / "frequencies.txt"
    if not src.exists():
        return 0, 0
    total = kept = 0
    with open(src, encoding="utf-8-sig", newline="") as fin, \
         open(dst, "w", encoding="utf-8", newline="") as fout:
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            total += 1
            tid = row.get("trip_id", "")
            if tid in dropped_trip_ids:
                continue
            if tid in split_map:
                for new_tid in split_map[tid]:
                    new_row = dict(row)
                    new_row["trip_id"] = new_tid
                    writer.writerow(new_row)
                    kept += 1
                continue
            writer.writerow(row)
            kept += 1
    return total, kept


def write_filtered_transfers(kept_route_ids: set, dropped_trip_ids: set) -> tuple:
    """Copy transfers.txt but drop rows that reference a route or trip we
    filtered out. pfaedle validates the whole feed at load time and refuses to
    proceed when it sees a transfer pointing at an unknown route_id.
    """
    src = GTFS_IN / "transfers.txt"
    dst = GTFS_OUT / "transfers.txt"
    if not src.exists():
        return 0, 0
    total = kept = 0
    with open(src, encoding="utf-8-sig", newline="") as fin, \
         open(dst, "w", encoding="utf-8", newline="") as fout:
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            total += 1
            from_route = (row.get("from_route_id") or "").strip()
            to_route   = (row.get("to_route_id")   or "").strip()
            from_trip  = (row.get("from_trip_id")  or "").strip()
            to_trip    = (row.get("to_trip_id")    or "").strip()
            if from_route and from_route not in kept_route_ids: continue
            if to_route   and to_route   not in kept_route_ids: continue
            if from_trip  and from_trip  in dropped_trip_ids:   continue
            if to_trip    and to_trip    in dropped_trip_ids:   continue
            writer.writerow(row)
            kept += 1
    return total, kept


def main() -> None:
    if not GTFS_IN.exists():
        sys.exit(f"missing {GTFS_IN} — run 01_download_gtfs.py first")

    cfg = load_cfg()
    bbox = cfg["osm_bbox"]
    excluded_tokens = cfg.get("excluded_agencies", [])

    GTFS_OUT.mkdir(parents=True, exist_ok=True)
    DIAG_OUT.parent.mkdir(parents=True, exist_ok=True)

    stop_overrides = load_stop_overrides(cfg)
    if stop_overrides:
        # Each entry seeds itself + its Parent… mirror, so divide by 2 for display.
        print(f"Loaded {len(stop_overrides)//2} GTFS stop coordinate override(s)")

    split_overrides = load_trip_split_overrides(cfg)
    if split_overrides:
        print(f"Loaded {len(split_overrides)} GTFS trip split override(s)")
        for e in split_overrides:
            print(f"  • agency={e['agency_id']} route={e['route_short_name']} "
                  f"→ split at stop {e['transfer_stop_id']}")
    split_routes = identify_split_routes(split_overrides)
    if split_overrides:
        print(f"  matched {len(split_routes):,} route_id(s) in routes.txt")

    waypoint_overrides = load_waypoint_overrides(cfg)
    if waypoint_overrides:
        print(f"Loaded {len(waypoint_overrides)} GTFS waypoint override(s)")
        for e in waypoint_overrides:
            print(f"  • agency={e['agency_id']} route={e['route_short_name']} "
                  f"→ waypoint between {e['after_stop_id']} and "
                  f"{e['before_stop_id']}")
    waypoint_routes = identify_waypoint_routes(waypoint_overrides)
    if waypoint_overrides:
        print(f"  matched {len(waypoint_routes):,} route_id(s) in routes.txt")

    print(f"Loading stops…")
    _load_sid_uic()
    stop_coords = load_stop_coords(stop_overrides)
    print(f"  {len(stop_coords):,} stops with coords "
          f"({len(_SID_UIC):,} with a UIC)")

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

    print(f"Streaming stop_times.txt → filter foreign-terminus trips"
          + (" + split overridden routes" if split_routes else "")
          + (" + insert waypoints" if waypoint_routes else "") + "…")
    (foreign_terminus, total_trips, kept_trips, n_time_repairs,
     split_map, split_warnings) = stream_filter_stop_times(
        stop_coords, bbox, trips_excluded_by_route,
        split_routes, waypoint_routes, trip_to_route)
    print(f"  {total_trips:,} trips scanned, "
          f"{len(foreign_terminus):,} foreign-terminus, "
          f"{kept_trips:,} kept (post-split count)")
    if n_time_repairs:
        print(f"  Repaired {n_time_repairs} rows with arrival_time > departure_time "
              "(clamped dep = arr)")
    if split_map:
        print(f"  Split {len(split_map):,} trip(s) → {len(split_map)*2:,} legs")
    for e in waypoint_overrides:
        print(f"  Waypoint {e['wpt_sid']}: inserted into "
              f"{e['matched_trips']:,} trip(s)")
    if split_warnings:
        by_reason: dict = defaultdict(int)
        for w in split_warnings:
            by_reason[w["reason"]] += 1
        print(f"  Warning: {len(split_warnings):,} trip(s) on split routes "
              "could not be split:")
        for reason, n in sorted(by_reason.items()):
            print(f"    {reason}: {n}")

    dropped = trips_excluded_by_route | foreign_terminus
    # Original trip_ids that were replaced by split legs no longer exist in
    # trips.txt or stop_times.txt; treat them like dropped trips for cross-file
    # reference cleanup.
    removed_or_split = dropped | set(split_map.keys())

    print(f"Writing filtered trips.txt …")
    kept_route_ids, n_trips = write_filtered_trips(dropped, split_map)
    print(f"  {n_trips:,} trips, {len(kept_route_ids):,} distinct routes")

    print(f"Writing filtered routes.txt …")
    kept_agency_ids, n_routes = write_filtered_routes(kept_route_ids)
    print(f"  {n_routes:,} routes, {len(kept_agency_ids):,} distinct agencies")

    print(f"Writing filtered agency.txt …")
    n_agencies = write_filtered_agency(kept_agency_ids)
    print(f"  {n_agencies:,} agencies")

    print(f"Writing filtered stops.txt …")
    n_overridden = write_filtered_stops(stop_overrides, waypoint_overrides)
    print(f"  {n_overridden:,} stop rows overridden"
          + (f", {len(waypoint_overrides)} waypoint stop(s) appended"
             if waypoint_overrides else ""))

    print(f"Writing stop identity table…")
    from gtfs.stop_identity import build_identity, write_identity, IDENTITY_PATH
    with open(GTFS_OUT / "stops.txt", encoding="utf-8-sig", newline="") as f:
        identity = build_identity(csv.DictReader(f))
    write_identity(identity)
    n_uic = sum(1 for e in identity.values() if e["uic"])
    n_sector = sum(1 for e in identity.values() if e["sector"])
    print(f"  {len(identity):,} stops ({n_uic:,} with UIC, "
          f"{n_sector:,} sector variants) → {IDENTITY_PATH.name}")

    print(f"Copying remaining GTFS files…")
    for name in ("calendar.txt", "calendar_dates.txt", "feed_info.txt"):
        copy_verbatim(name)

    print(f"Filtering frequencies.txt …")
    n_fq_total, n_fq_kept = write_filtered_frequencies(dropped, split_map)
    if n_fq_total:
        print(f"  {n_fq_kept:,} kept of {n_fq_total:,}")

    print(f"Filtering transfers.txt …")
    n_xf_total, n_xf_kept = write_filtered_transfers(kept_route_ids,
                                                     removed_or_split)
    print(f"  {n_xf_kept:,} kept of {n_xf_total:,} "
          f"(dropped rows referencing excluded routes/trips)")

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

    # Diagnostic: trip split map (original → legs) + any unsplit warnings,
    # plus the per-entry waypoint insertion counts.
    split_diag = {
        "splits": [
            {"original_trip_id": orig,
             "route_id": trip_to_route.get(orig, ""),
             "split_trip_ids": legs}
            for orig, legs in sorted(split_map.items())
        ],
        "warnings": split_warnings,
        "waypoints": [
            {"agency_id": e["agency_id"],
             "route_short_name": e["route_short_name"],
             "after_stop_id": e["after_stop_id"],
             "before_stop_id": e["before_stop_id"],
             "wpt_sid": e["wpt_sid"],
             "matched_trips": e["matched_trips"],
             "reason": e["reason"]}
            for e in waypoint_overrides
        ],
    }
    SPLIT_DIAG_OUT.write_text(json.dumps(split_diag, ensure_ascii=False))
    print(f"  Diagnostic: {len(split_map):,} split trip(s) → {SPLIT_DIAG_OUT}")

    print(f"\nDone. Filtered feed at {GTFS_OUT}")


if __name__ == "__main__":
    main()
