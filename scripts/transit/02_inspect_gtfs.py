#!/usr/bin/env python3
"""
Inspect the downloaded Swiss GTFS feed.
Prints row counts, route type breakdown, agency list, and sample rows.
Run after 01_download_gtfs.py.
"""

import csv
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data" / "gtfs"

# GTFS route_type codes → human label
ROUTE_TYPE_LABELS = {
    "0": "Tram / light rail",
    "1": "Metro / subway",
    "2": "Rail (intercity / regional)",
    "3": "Bus",
    "4": "Ferry / boat",
    "5": "Cable tram",
    "6": "Aerial lift / gondola",
    "7": "Funicular",
    "11": "Trolleybus",
    "12": "Monorail",
    # Extended GTFS route types used by some agencies
    "100": "Railway (intercity)",
    "101": "High-speed rail",
    "102": "Long-distance rail",
    "103": "Inter-regional rail",
    "104": "Car transport rail",
    "105": "Sleeper rail",
    "106": "Regional rail",
    "107": "Tourist railway",
    "108": "Rail shuttle",
    "109": "Suburban rail (S-Bahn)",
    "200": "Coach / long-distance bus",
    "201": "International coach",
    "202": "National coach",
    "204": "Regional coach",
    "400": "Urban rail",
    "401": "Metro",
    "402": "Underground",
    "403": "Urban railway",
    "404": "City rail",
    "405": "Monorail",
    "700": "Bus",
    "701": "Regional bus",
    "702": "Express bus",
    "704": "Local bus",
    "900": "Tram",
    "901": "City tram",
    "1000": "Water transport",
    "1300": "Aerial lift",
    "1400": "Funicular",
    "1700": "Miscellaneous",
}


def read_csv(filename: str) -> list[dict]:
    path = DATA_DIR / filename
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def show_files() -> None:
    section("Files in data/gtfs/")
    for p in sorted(DATA_DIR.glob("*.txt")):
        rows = read_csv(p.name)
        print(f"  {p.name:<30} {len(rows):>8,} rows")


def show_agencies() -> None:
    section("Agencies (agency.txt)")
    agencies = read_csv("agency.txt")
    print(f"  Total: {len(agencies)}")
    for a in agencies[:30]:
        print(f"  [{a.get('agency_id', '?')}] {a.get('agency_name', '?')}")
    if len(agencies) > 30:
        print(f"  ... and {len(agencies) - 30} more")


def show_route_types() -> None:
    section("Route types (routes.txt)")
    routes = read_csv("routes.txt")
    counts: Counter = Counter()
    for r in routes:
        counts[r.get("route_type", "?")] += 1
    for rtype, count in sorted(counts.items(), key=lambda x: -x[1]):
        label = ROUTE_TYPE_LABELS.get(rtype, f"unknown ({rtype})")
        print(f"  {label:<35} {count:>6,} routes")


def show_stops() -> None:
    section("Stops (stops.txt)")
    stops = read_csv("stops.txt")
    location_types: Counter = Counter()
    for s in stops:
        location_types[s.get("location_type", "0")] += 1
    print(f"  Total stops: {len(stops):,}")
    type_labels = {
        "0": "Stop / platform",
        "1": "Station (parent)",
        "2": "Station entrance/exit",
        "3": "Generic node",
        "4": "Boarding area",
    }
    for ltype, count in sorted(location_types.items()):
        label = type_labels.get(ltype, f"type {ltype}")
        print(f"  {label:<30} {count:>8,}")

    print("\n  Sample stops:")
    for s in stops[:5]:
        print(
            f"  [{s.get('stop_id')}] {s.get('stop_name')} "
            f"({s.get('stop_lat')}, {s.get('stop_lon')})"
        )


def show_calendar() -> None:
    section("Calendar (calendar.txt)")
    cal = read_csv("calendar.txt")
    print(f"  Total service IDs: {len(cal):,}")
    if cal:
        sample = cal[0]
        print(f"  Fields: {list(sample.keys())}")
        print(f"  Sample: {sample}")


def show_shapes() -> None:
    section("Shapes (shapes.txt)")
    shapes = read_csv("shapes.txt")
    if not shapes:
        print("  shapes.txt not present — will fall back to stop-to-stop straight lines")
        return
    shape_ids = {s["shape_id"] for s in shapes}
    print(f"  Total shape points: {len(shapes):,}")
    print(f"  Unique shapes:      {len(shape_ids):,}")


def show_stop_times_sample() -> None:
    section("Stop times (stop_times.txt) — first 5 rows")
    path = DATA_DIR / "stop_times.txt"
    if not path.exists():
        print("  stop_times.txt not found")
        return
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        print(f"  Fields: {reader.fieldnames}")
        for i, row in enumerate(reader):
            if i >= 5:
                break
            print(f"  {row}")


if __name__ == "__main__":
    show_files()
    show_agencies()
    show_route_types()
    show_stops()
    show_calendar()
    show_shapes()
    show_stop_times_sample()

    print("\n")
