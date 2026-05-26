#!/usr/bin/env python3
"""
Transit pipeline regression tracker.

Usage:
  python3 scripts/transit/diff_snapshot.py                   # diff vs saved baseline (capped)
  python3 scripts/transit/diff_snapshot.py --out report.txt  # write full diff to file
  python3 scripts/transit/diff_snapshot.py --save            # diff then save new baseline

On first run (no baseline): saves snapshot and exits.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

CAP = 10  # max entries shown per section in console output

GEOJSON_PATH = Path("data/transit/transit_lines.geojson")
LINE_STOPS_PATH = Path("data/transit/line_stops.json")
SNAPSHOT_PATH = Path("data/transit/snapshot.json")

MODE_ORDER = ["train", "tram", "metro", "bus", "regional_bus", "ferry", "mountain"]


def _mode_sort_key(entry):
    mode = entry.get("mode", "")
    try:
        mi = MODE_ORDER.index(mode)
    except ValueError:
        mi = len(MODE_ORDER)
    ref = entry.get("ref") or ""
    # Sort refs numerically where possible, else lexicographically
    try:
        ref_key = (0, int(ref))
    except (ValueError, TypeError):
        ref_key = (1, ref)
    return (mi, ref_key)


def build_snapshot():
    with open(GEOJSON_PATH) as f:
        geojson = json.load(f)
    with open(LINE_STOPS_PATH) as f:
        line_stops = json.load(f)

    lines = {}
    for feat in geojson["features"]:
        p = feat["properties"]
        osm_id = str(p["osm_id"])
        ls = line_stops.get(osm_id, {})
        stops = ls.get("stops", [])
        lines[osm_id] = {
            "ref": p.get("ref") or "",
            "name": p.get("name") or "",
            "mode": p.get("mode") or "",
            "from": p.get("from") or "",
            "to": p.get("to") or "",
            "gtfs_ref": ls.get("gtfs_ref") or "",
            "num_stops": len(stops),
        }
    return {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "lines": lines,
    }


def _label(entry, osm_id):
    ref = entry.get("ref") or osm_id
    fr = entry.get("from") or ""
    to = entry.get("to") or ""
    route = f"{fr} → {to}" if fr or to else entry.get("name") or ""
    return ref, route


def _build_diff(baseline, current):
    bl = baseline["lines"]
    cu = current["lines"]

    baseline_ids = set(bl.keys())
    current_ids = set(cu.keys())

    disappeared = sorted(
        [bl[oid] | {"_oid": oid} for oid in baseline_ids - current_ids],
        key=lambda e: _mode_sort_key(e),
    )
    added = sorted(
        [cu[oid] | {"_oid": oid} for oid in current_ids - baseline_ids],
        key=lambda e: _mode_sort_key(e),
    )

    common = baseline_ids & current_ids
    stop_changes = []
    for oid in common:
        before = bl[oid]["num_stops"]
        after = cu[oid]["num_stops"]
        if before != after:
            stop_changes.append(cu[oid] | {"_oid": oid, "_before": before, "_after": after})
    stop_changes.sort(key=lambda e: _mode_sort_key(e))

    bl_total = sum(v["num_stops"] for v in bl.values())
    cu_total = sum(v["num_stops"] for v in cu.values())

    return disappeared, added, stop_changes, len(bl), len(cu), bl_total, cu_total


def _format_disappeared(e):
    ref, route = _label(e, e["_oid"])
    stops_str = f"was {e['num_stops']} stops" if e["num_stops"] else "no stops"
    return f"  [{e['mode']:<12}] {ref:<6}  {route:<45}  osm:{e['_oid']:<12}  ({stops_str})"


def _format_added(e):
    ref, route = _label(e, e["_oid"])
    return f"  [{e['mode']:<12}] {ref:<6}  {route:<45}  osm:{e['_oid']:<12}  ({e['num_stops']} stops)"


def _format_stop_change(e):
    ref, route = _label(e, e["_oid"])
    diff = e["_after"] - e["_before"]
    arrow = "↑" if diff > 0 else "↓"
    return (
        f"  [{e['mode']:<12}] {ref:<6}  {route:<45}  "
        f"{e['_before']} → {e['_after']}  ({diff:+d})  {arrow}"
    )


def format_diff(baseline, current, cap=None):
    """Return diff as a list of lines. cap=None means no limit."""
    disappeared, added, stop_changes, bl_count, cu_count, bl_total, cu_total = _build_diff(baseline, current)

    out = [f"=== Transit diff vs baseline {baseline['created_at']} ==="]

    if not disappeared and not added and not stop_changes:
        out.append("No changes.")
    else:
        if disappeared:
            out.append(f"\nDISAPPEARED ({len(disappeared)}):")
            shown = disappeared[:cap] if cap else disappeared
            for e in shown:
                out.append(_format_disappeared(e))
            if cap and len(disappeared) > cap:
                out.append(f"  … and {len(disappeared) - cap} more")

        if added:
            out.append(f"\nADDED ({len(added)}):")
            shown = added[:cap] if cap else added
            for e in shown:
                out.append(_format_added(e))
            if cap and len(added) > cap:
                out.append(f"  … and {len(added) - cap} more")

        gained = [e for e in stop_changes if e["_after"] > e["_before"]]
        lost = [e for e in stop_changes if e["_after"] < e["_before"]]

        if gained:
            out.append(f"\nSTOPS GAINED ({len(gained)}):")
            shown = gained[:cap] if cap else gained
            for e in shown:
                out.append(_format_stop_change(e))
            if cap and len(gained) > cap:
                out.append(f"  … and {len(gained) - cap} more")

        if lost:
            out.append(f"\nSTOPS LOST ({len(lost)}):")
            shown = lost[:cap] if cap else lost
            for e in shown:
                out.append(_format_stop_change(e))
            if cap and len(lost) > cap:
                out.append(f"  … and {len(lost) - cap} more")

    line_diff = cu_count - bl_count
    stop_diff = cu_total - bl_total
    out.append(
        f"\nSummary: {bl_count} → {cu_count} lines ({line_diff:+d})   "
        f"{bl_total} → {cu_total} total stops ({stop_diff:+d})"
    )
    return out


def main():
    parser = argparse.ArgumentParser(description="Transit pipeline regression diff")
    parser.add_argument("--save", action="store_true", help="Save current state as new baseline after diffing")
    parser.add_argument("--out", metavar="FILE", help="Write full (uncapped) diff to FILE instead of console")
    args = parser.parse_args()

    for path in (GEOJSON_PATH, LINE_STOPS_PATH):
        if not path.exists():
            print(f"ERROR: {path} not found. Run rebuild_transit.sh first.", file=sys.stderr)
            sys.exit(1)

    current = build_snapshot()

    if not SNAPSHOT_PATH.exists():
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SNAPSHOT_PATH, "w") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
        print(f"No baseline found — snapshot saved ({len(current['lines'])} lines).")
        return

    with open(SNAPSHOT_PATH) as f:
        baseline = json.load(f)

    if args.out:
        lines = format_diff(baseline, current, cap=None)
        out_path = Path(args.out)
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Full diff written to {out_path} ({len(lines)} lines).")
    else:
        lines = format_diff(baseline, current, cap=CAP)
        print("\n".join(lines))
        print("\n(Run with --save to accept as new baseline)")
        print(f"(Run with --out FILE to write the full uncapped diff to a file)")

    if args.save:
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SNAPSHOT_PATH, "w") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)
        print(f"\nBaseline updated ({current['created_at']}).")


if __name__ == "__main__":
    main()
