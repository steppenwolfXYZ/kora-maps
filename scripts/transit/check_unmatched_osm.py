"""
Diagnostic: show OSM route relations that exist but were not drawn.

Reads:
  data/osm/routes.geojson          — all OSM transit routes (from 04_extract_osm.py)
  data/transit/transit_lines.geojson — drawn features (from 05_score_and_match.py)

An OSM route is "unmatched" when its osm_id does not appear in any drawn feature.
This is the GTFS-first complement to check_unmatched_gtfs.py:
  - check_unmatched_gtfs.py  → GTFS lines with no OSM geometry
  - check_unmatched_osm.py   → OSM routes that no GTFS line claimed

Usage:
  python3 scripts/transit/check_unmatched_osm.py [--mode train|bus|tram|ferry|...]
                                                  [--limit N]
                                                  [--matched]   # show matched instead
"""

import json
import argparse
from collections import Counter, defaultdict
from pathlib import Path

ROOT      = Path(__file__).parents[2]
OSM_IN    = ROOT / "data" / "osm" / "routes.geojson"
LINES_OUT = ROOT / "data" / "transit" / "transit_lines.geojson"

# Modes that are intentionally not drawn (mountain is drawn via GTFS-first loop
# and may share osm_id with train entries; hiking/cycling are non-transit)
NON_TRANSIT = {"fitness_trail", "hiking", "cycling", "foot"}


def osm_mode_bucket(route_tag, ref="", operator=""):
    """Rough bucket for display — mirrors the logic in osm_to_mode() without importing it."""
    if route_tag in NON_TRANSIT:
        return None
    if route_tag in ("train", "rail", "light_rail", "narrow_gauge", "subway",
                     "monorail", "tram", "bus", "trolleybus", "ferry",
                     "funicular", "cable_car", "gondola", "aerialway"):
        return route_tag
    return route_tag or "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", help="Filter by OSM route tag (train, bus, tram, ferry, ...)")
    ap.add_argument("--limit", type=int, default=80, help="Max rows per section (default 80)")
    ap.add_argument("--matched", action="store_true", help="Show matched routes instead of unmatched")
    args = ap.parse_args()

    osm_feats = json.loads(OSM_IN.read_text())["features"]
    lines     = json.loads(LINES_OUT.read_text())["features"]

    # osm_ids that appear in drawn features
    drawn_oids: set = set()
    for f in lines:
        oid = f["properties"].get("osm_id")
        if oid is not None:
            drawn_oids.add(str(oid))

    # Partition OSM routes
    unmatched = []
    matched   = []
    skipped   = 0

    for f in osm_feats:
        p         = f["properties"]
        route_tag = p.get("route", "")
        if route_tag in NON_TRANSIT:
            skipped += 1
            continue
        ref      = p.get("ref", "").strip()
        operator = p.get("operator", "")
        oid      = str(p.get("osm_id", ""))
        name     = p.get("name", "")
        km       = p.get("length_km", 0)

        bucket = osm_mode_bucket(route_tag, ref, operator)
        if args.mode and bucket != args.mode:
            continue

        row = (bucket, ref, operator, oid, name, km)
        if oid in drawn_oids:
            matched.append(row)
        else:
            unmatched.append(row)

    target = matched if args.matched else unmatched
    label  = "Matched" if args.matched else "Unmatched"

    # ── Summary ─────────────────────────────────────────────────────────────────
    total_transit = len(matched) + len(unmatched)
    print(f"\n{'─'*60}")
    print(f"  OSM transit routes: {total_transit:,} total  "
          f"({len(matched):,} drawn, {len(unmatched):,} not drawn)")
    if skipped:
        print(f"  ({skipped:,} non-transit routes ignored)")
    print(f"{'─'*60}")

    c = Counter(b for b, *_ in unmatched)
    print("  Not drawn, by route tag:")
    for tag, n in sorted(c.items(), key=lambda x: -x[1]):
        print(f"    {tag:<22} {n:>5}")

    # ── Table ────────────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  {label} OSM routes (sorted by route tag, then ref)")
    print(f"{'─'*60}")
    print(f"  {'route':<14}  {'ref':<12}  {'operator':<22}  {'osm_id':<12}  name")
    print(f"  {'-----':<14}  {'---':<12}  {'--------':<22}  {'------':<12}  ----")

    rows = sorted(target, key=lambda x: (x[0], x[1]))
    for bucket, ref, operator, oid, name, km in rows[:args.limit]:
        disp_name = name[:30] if name else ""
        print(f"  {bucket:<14}  {ref:<12}  {operator:<22}  {oid:<12}  {disp_name}")

    if len(rows) > args.limit:
        print(f"  … {len(rows) - args.limit} more (use --limit N to see more)")

    print()


if __name__ == "__main__":
    main()
