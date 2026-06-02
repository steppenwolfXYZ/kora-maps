#!/usr/bin/env python3
"""
Diagnostic: list every GTFS trip group whose stops include the requested
station (rail UIC) or any stop whose name matches a search pattern (bus / tram
"Bern, Bahnhof" stops, which sit under different UICs from the rail station).
Prints terminus names and a ✓/✗ for whether the group ended up drawn.

Usage:
    python3 scripts/transit/scan_lines_at_stop.py 8507000              # exact UIC
    python3 scripts/transit/scan_lines_at_stop.py "Bern, Bahnhof"      # name match
    python3 scripts/transit/scan_lines_at_stop.py "Bern, Bahnhof" 10   # drill down

When a second argument (ref filter) is given, the script prints the full
ordered stop sequence for each matching trip group instead of the summary
table. Useful for verifying pfaedle's input shape (if the canonical stops are
wrong, pfaedle's shape will be wrong too).

Reuses 05_score_and_match.py's loaders + stream_stop_times so the grouping
result is identical to what the pipeline produced (no second guessing).
"""

import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

spec = importlib.util.spec_from_file_location("s05", HERE / "05_score_and_match.py")
_m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_m)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: scan_lines_at_stop.py <UIC | name-pattern> [<ref-filter>]")
    query = sys.argv[1]
    ref_filter = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"Loading GTFS (data/gtfs_routed/) …")
    stop_coords  = _m.load_stops()
    stop_meta    = _m.load_stop_meta()
    svc_dates    = _m.load_calendar_dates()
    route_lookup = _m.load_routes()
    trip_lookup  = _m.load_trips(route_lookup)
    trip_freqs   = _m.load_frequencies()
    print(f"  {len(trip_lookup):,} trips, {len(stop_coords):,} stops")

    # Resolve the query → set of target UIC base codes.
    target_uics: set = set()
    target_label = query
    if query.isdigit():
        target_uics.add(query)
        target_label = f"{query} ({stop_meta.get(query, ('?', ''))[0] or '?'})"
    else:
        ql = query.lower()
        for sid, (name, _parent) in stop_meta.items():
            if ql in name.lower():
                target_uics.add(sid.split(":")[0])
        if not target_uics:
            sys.exit(f"no stops matched pattern {query!r}")
        sample_names = sorted({stop_meta.get(u, ("?", ""))[0]
                               for u in list(target_uics)[:5]})
        target_label = (f"{len(target_uics)} UICs matching {query!r} — "
                        f"sample: {', '.join(sample_names)}")

    print(f"Streaming stop_times.txt → grouping…")
    line_freq, _line_speed, _line_canonical = _m.stream_stop_times(
        trip_lookup, stop_coords, svc_dates, trip_freqs, stop_meta)

    # Build a per-line_key service-window-days estimate: total distinct
    # service dates active on ANY trip of that line.
    line_active_days: dict = {}
    for tid, info in trip_lookup.items():
        lk = info["line_key"]
        sid = info["service_id"]
        dates = svc_dates.get(sid, set())
        if not dates:
            continue
        line_active_days.setdefault(lk, set()).update(dates)

    print(f"\nScanning trip groups visiting {target_label} …\n")

    # transit_lines.geojson — find which (line_key, agency_id, tg_id) drew.
    # agency_id is part of the key because trip_group_id is unique only within
    # a partition (line_key + agency_id), so different cities' same-numbered
    # lines share tg_id=0 etc.
    lines_path = ROOT / "data" / "transit" / "transit_lines.geojson"
    drawn_tgids: set = set()
    if lines_path.exists():
        gj = json.loads(lines_path.read_text())
        for f in gj["features"]:
            p = f["properties"]
            tgid = p.get("trip_group_id")
            ref  = p.get("ref", "")
            name = p.get("name", "")
            aid  = p.get("agency_id", "")
            bkt  = p.get("mode", "")
            # Map mode→bucket for matching against canonical line_key.
            bkt_map = {"train": "train", "tram": "tram", "metro": "metro",
                       "bus": "bus", "regional_bus": "bus",
                       "ferry": "ferry", "mountain": "mountain"}
            bucket = bkt_map.get(bkt, bkt)
            if tgid is not None:
                drawn_tgids.add((ref, name, bucket, aid, tgid))

    # Walk the canonical export and pick entries whose stops include the UIC.
    # When ref_filter is set, keep ALL entries (one per CanonEntry) for the
    # matching ref so the user sees every variant; otherwise dedupe per group.
    rows: list = []
    seen_keys: set = set()
    for (key, bucket), entries in _m._line_canonical_export.items():
        for e in entries:
            sn, ln, bkt = e.line_key
            if ref_filter is not None and sn != ref_filter:
                continue
            if ref_filter is None and (e.line_key, e.trip_group_id) in seen_keys:
                continue
            stop_ids = [s[0] for s in e.stops]
            if not any(sid.split(":")[0] in target_uics for sid in stop_ids):
                continue
            if ref_filter is None:
                seen_keys.add((e.line_key, e.trip_group_id))

            first_uic = stop_ids[0].split(":")[0]
            last_uic  = stop_ids[-1].split(":")[0]
            first_name = stop_meta.get(stop_ids[0],
                            stop_meta.get(first_uic, ("?", "")))[0] or "?"
            last_name  = stop_meta.get(stop_ids[-1],
                            stop_meta.get(last_uic, ("?", "")))[0] or "?"
            drawn = (sn, ln, bkt, e.agency_id, e.trip_group_id) in drawn_tgids
            rows.append({
                "short": sn, "long": ln, "bucket": bkt,
                "tg": e.trip_group_id, "agency": e.agency_id,
                "no_draw": e.no_draw,
                "n_stops": len(stop_ids),
                "stop_ids": stop_ids,
                "from": first_name, "to": last_name,
                "drawn": drawn,
                "dir_aware": e.dir_aware,
            })

    # Sort: bucket, then short_name (numeric where possible), then tg_id.
    def sort_key(r):
        sn = r["short"]
        try:
            sn_n = (0, int(sn))
        except ValueError:
            sn_n = (1, sn)
        return (r["bucket"], sn_n, r["tg"])
    rows.sort(key=sort_key)

    if not rows:
        msg = f"(no groups visit the target)"
        if ref_filter is not None:
            msg += f" with ref {ref_filter!r}"
        print(msg)
        return

    # Drill-down rendering: full ordered stop sequence per matching entry.
    if ref_filter is not None:
        for i, r in enumerate(rows):
            marker = "drawn" if r["drawn"] else "NOT drawn"
            flag = f" [{r['no_draw']}]" if r["no_draw"] else ""
            dir_flag = " dir_aware" if r["dir_aware"] else ""
            print(f"=== {r['short']} ({r['bucket']}) "
                  f"tg={r['tg']} agency={r['agency']} {marker}{flag}{dir_flag} ===")
            print(f"    {r['from']}  →  {r['to']}   ({r['n_stops']} stops)")
            for j, sid in enumerate(r["stop_ids"], 1):
                uic = sid.split(":")[0]
                name = (stop_meta.get(sid, stop_meta.get(uic, ("?", "")))[0]
                        or "?")
                print(f"    {j:3}. {name:<40} {sid}")
            if i < len(rows) - 1:
                print()
        print(f"\n{len(rows)} canonical entries for ref {ref_filter!r}")
        return

    # Render.
    w_sn = max(6, max(len(r["short"]) for r in rows))
    w_bk = max(8, max(len(r["bucket"]) for r in rows))
    w_from = max(18, max(len(r["from"]) for r in rows))
    w_to = max(18, max(len(r["to"]) for r in rows))

    header = (f"{'':2}  {'ref':<{w_sn}}  {'mode':<{w_bk}}  {'tg':>4}  "
              f"{'#st':>4}  {'from':<{w_from}}  →  {'to':<{w_to}}  "
              f"agency  flag")
    print(header)
    print("-" * len(header))
    drawn_count = 0
    for r in rows:
        marker = "✓ " if r["drawn"] else "✗ "
        if r["drawn"]:
            drawn_count += 1
        flag = r["no_draw"] or ""
        print(f"{marker}  {r['short']:<{w_sn}}  {r['bucket']:<{w_bk}}  "
              f"{r['tg']:>4}  {r['n_stops']:>4}  "
              f"{r['from']:<{w_from}}  →  {r['to']:<{w_to}}  "
              f"{r['agency']:<6}  {flag}")

    print(f"\n{drawn_count}/{len(rows)} groups drawn")


if __name__ == "__main__":
    main()
