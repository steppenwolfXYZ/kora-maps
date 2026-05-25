"""
Diagnostic: show GTFS lines that could not be matched to an OSM route.

Reads:
  data/transit/gtfs_no_osm.json      — entries from 05_score_and_match.py
  data/transit/transit_lines.geojson — drawn features

Usage:
  python3 scripts/transit/check_unmatched_gtfs.py [--mode train|bus|tram|ferry]
                                                   [--ch]      # only Switzerland/border area
                                                   [--foreign] # only outside Switzerland
                                                   [--all]     # include partially-matched too
                                                   [--limit N]
"""

import json
import argparse
from collections import defaultdict, Counter
from pathlib import Path

ROOT      = Path(__file__).parents[2]
NO_OSM    = ROOT / "data" / "transit" / "gtfs_no_osm.json"
LINES_OUT = ROOT / "data" / "transit" / "transit_lines.geojson"

# Rough bounding box for Switzerland + generous border area (~30 km margin)
CH_LON = (5.8, 10.7)
CH_LAT = (45.6, 48.0)


def in_ch(lon, lat):
    if lon is None or lat is None:
        return None  # unknown
    return CH_LON[0] <= lon <= CH_LON[1] and CH_LAT[0] <= lat <= CH_LAT[1]


def location_label(lon, lat):
    if lon is None:
        return "?"
    if in_ch(lon, lat):
        return f"{lat:.2f}N {lon:.2f}E"
    if lon < 5.8:
        return f"FR {lat:.1f}N {lon:.1f}E"
    if lon > 10.7:
        return f"AT/DE {lat:.1f}N {lon:.1f}E"
    if lat < 45.6:
        return f"IT {lat:.1f}N {lon:.1f}E"
    if lat > 48.0:
        return f"DE {lat:.1f}N {lon:.1f}E"
    return f"{lat:.2f}N {lon:.2f}E"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", help="Filter by bucket (train, bus, tram, ferry, ...)")
    ap.add_argument("--ch", action="store_true", help="Only show lines inside Switzerland/border area")
    ap.add_argument("--foreign", action="store_true", help="Only show lines outside Switzerland")
    ap.add_argument("--all", action="store_true", help="Also show partially-matched entries")
    ap.add_argument("--limit", type=int, default=80, help="Max rows per section (default 80)")
    args = ap.parse_args()

    no_osm = json.loads(NO_OSM.read_text())
    lines  = json.loads(LINES_OUT.read_text())

    # Build set of (ref, bucket) that were drawn
    matched: set = set()
    for f in lines["features"]:
        fid = f["properties"].get("feature_id", "")
        if fid.startswith("gtfs:"):
            parts = fid.split(":")
            matched.add((parts[1], parts[2]))

    # Group no-OSM entries by (ref, bucket), keeping centroid of first candidate
    groups = {}   # (ref, bucket) → {"count": n, "lon": x, "lat": y}
    for e in no_osm:
        key = (e["ref"], e["bucket"])
        if key not in groups:
            groups[key] = {"count": 0, "lon": e.get("lon"), "lat": e.get("lat")}
        groups[key]["count"] += 1

    fully   = [(r, b, g["count"], g["lon"], g["lat"])
               for (r, b), g in groups.items() if (r, b) not in matched]
    partial = [(r, b, g["count"], g["lon"], g["lat"])
               for (r, b), g in groups.items() if (r, b) in matched]

    def geo_filter(rows):
        if args.ch:
            return [x for x in rows if in_ch(x[3], x[4]) is True]
        if args.foreign:
            return [x for x in rows if in_ch(x[3], x[4]) is False]
        return rows

    if args.mode:
        fully   = [(r, b, n, lo, la) for r, b, n, lo, la in fully   if b == args.mode]
        partial = [(r, b, n, lo, la) for r, b, n, lo, la in partial if b == args.mode]

    fully   = geo_filter(fully)
    partial = geo_filter(partial)

    # ── Summary ─────────────────────────────────────────────────────────────────
    print(f"\n{'─'*62}")
    print(f"  GTFS lines with no OSM match  ({NO_OSM.name})")
    print(f"{'─'*62}")
    print(f"  Fully unmatched (no variant drew): {len(fully):,}")
    print(f"  Partially matched (≥1 variant drew): {len(partial):,}")
    print()

    ch_count      = sum(1 for *_, lo, la in fully if in_ch(lo, la) is True)
    foreign_count = sum(1 for *_, lo, la in fully if in_ch(lo, la) is False)
    unknown_count = sum(1 for *_, lo, la in fully if in_ch(lo, la) is None)
    print(f"  Fully unmatched location breakdown:")
    print(f"    Inside CH/border area: {ch_count:>5}")
    print(f"    Outside CH:            {foreign_count:>5}")
    print(f"    Unknown location:      {unknown_count:>5}")
    print()

    c = Counter(b for _, b, *_ in fully)
    print("  By bucket:")
    for b, n in sorted(c.items(), key=lambda x: -x[1]):
        print(f"    {b:<22} {n:>5}")

    # ── Fully unmatched table ────────────────────────────────────────────────────
    print(f"\n{'─'*62}")
    print(f"  Fully unmatched  (sorted by bucket, ref)")
    print(f"{'─'*62}")
    print(f"  {'bucket':<14}  {'ref':<16}  {'location':<22}  cands")
    print(f"  {'------':<14}  {'---':<16}  {'--------':<22}  -----")
    rows = sorted(fully, key=lambda x: (x[1], x[0]))
    for r, b, n, lo, la in rows[:args.limit]:
        print(f"  {b:<14}  {r:<16}  {location_label(lo, la):<22}  {n}")
    if len(rows) > args.limit:
        print(f"  … {len(rows) - args.limit} more (use --limit N or --ch/--foreign to filter)")

    # ── Partially matched ────────────────────────────────────────────────────────
    if args.all:
        print(f"\n{'─'*62}")
        print(f"  Partially matched (≥1 variant drew, rest missing)")
        print(f"{'─'*62}")
        print(f"  {'bucket':<14}  {'ref':<16}  {'location':<22}  cands")
        print(f"  {'------':<14}  {'---':<16}  {'--------':<22}  -----")
        rows_p = sorted(partial, key=lambda x: (x[1], x[0]))
        for r, b, n, lo, la in rows_p[:args.limit]:
            print(f"  {b:<14}  {r:<16}  {location_label(lo, la):<22}  {n}")
        if len(rows_p) > args.limit:
            print(f"  … {len(rows_p) - args.limit} more")

    print()


if __name__ == "__main__":
    main()
