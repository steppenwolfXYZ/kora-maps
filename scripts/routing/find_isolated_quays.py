#!/usr/bin/env python3
"""Find quays the pedestrian router cannot leave.

Reads the precomputed stop-to-stop walking matrix
(`motis/data/valhalla_footpath_matrix.csv`) and the MOTIS stop sidecar
(`data/gtfs_motis/stops.txt`) and reports every quay whose walking
partners all lie within a few metres of itself, or that has no partner at
all: a quay standing on an island of the Valhalla graph.

Why it matters beyond the quay being unreachable: Valhalla's one-to-many
matrix (the fork's WALK-offset call for coordinate endpoints) can only
declare a target unreachable after exhausting the whole graph within the
walking limit, so one island quay in a query's candidate radius costs
seconds on every cold query (Bolligen, Zürich HB, Lugano Centro, 2026-09).
The station walk network builder no longer emits walk lines that weld to
nothing (`station-walk-network.md`); this is the check that it — and the
OSM data — actually delivered a connected graph.

Run it after every matrix build (Kranich's update cycle, or a fetch):

    python3 scripts/routing/find_isolated_quays.py

Writes `data/transit/isolated_quays.json` and prints a per-station summary.
Only quays whose stop id starts with `ch:1:sloid` (the Swiss feed's own
stops) are reported by default — foreign stops at the edge of the routing
bbox are legitimately without partners. `--all` includes them.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MATRIX_CSV = ROOT / "motis" / "data" / "valhalla_footpath_matrix.csv"
STOPS_TXT = ROOT / "data" / "gtfs_motis" / "stops.txt"
OUT_JSON = ROOT / "data" / "transit" / "isolated_quays.json"

# A quay whose farthest walking partner is closer than this is on an
# island: real neighbourhoods reach the next station in the matrix (its
# radius is a two-hour walk), so "everything within a platform's length"
# means the graph stops at the platform's edge.
ISLAND_RADIUS_M = 250.0


def dist_m(a, b) -> float:
    (la, lo), (lb, lob) = a, b
    return math.hypot((la - lb) * 111_000.0,
                      (lo - lob) * 111_000.0 * math.cos(math.radians(la)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--all", action="store_true",
                    help="Include stops outside the Swiss feed's id scheme.")
    ap.add_argument("--matrix", type=Path, default=MATRIX_CSV)
    ap.add_argument("--stops", type=Path, default=STOPS_TXT)
    ap.add_argument("--out", type=Path, default=OUT_JSON)
    args = ap.parse_args()

    for f in (args.matrix, args.stops):
        if not f.exists():
            sys.exit(f"missing {f}")

    stops = {}
    with args.stops.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            try:
                stops[row["stop_id"]] = (
                    float(row["stop_lat"]), float(row["stop_lon"]),
                    row.get("stop_name", ""), row.get("platform_code", ""),
                    row.get("location_type", "") or "0",
                    row.get("parent_station", ""))
            except (KeyError, ValueError):
                continue

    def wanted(sid: str) -> bool:
        rec = stops.get(sid)
        if rec is None or rec[4] != "0" or sid.startswith("Parent"):
            return False
        return args.all or sid.startswith("ch:1:sloid")

    farthest = defaultdict(float)
    seen = set()
    with args.matrix.open() as fh:
        reader = csv.reader(fh)
        next(reader, None)
        for a, b, _ in reader:
            seen.add(a)
            seen.add(b)
            if a in stops and b in stops:
                d = dist_m(stops[a][:2], stops[b][:2])
                if d > farthest[a]:
                    farthest[a] = d
                if d > farthest[b]:
                    farthest[b] = d

    islands = []
    for sid in stops:
        if not wanted(sid):
            continue
        if sid not in seen:
            islands.append((sid, "no_rows", 0.0))
        elif farthest[sid] < ISLAND_RADIUS_M:
            islands.append((sid, "island", farthest[sid]))

    by_station = defaultdict(list)
    for sid, kind, far in islands:
        lat, lon, name, code, _, parent = stops[sid]
        by_station[name].append({
            "stop_id": sid, "platform_code": code, "parent": parent,
            "lat": lat, "lon": lon, "kind": kind,
            "farthest_partner_m": round(far),
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "island_radius_m": ISLAND_RADIUS_M,
        "quays_checked": sum(1 for s in stops if wanted(s)),
        "isolated": len(islands),
        "stations": {k: v for k, v in sorted(by_station.items())},
    }, indent=1, ensure_ascii=False))

    real = {k: [q for q in v if q["kind"] == "island"]
            for k, v in by_station.items()}
    real = {k: v for k, v in real.items() if v}
    no_rows = sum(1 for _, kind, _ in islands if kind == "no_rows")
    print(f"quays checked {sum(1 for s in stops if wanted(s)):,}: "
          f"{sum(len(v) for v in real.values()):,} on islands at "
          f"{len(real):,} stations, {no_rows:,} without any matrix row "
          f"(mostly outside the routing bbox or lone summit quays — "
          f"listed in the JSON, not a graph defect by themselves)")
    for name, quays in sorted(real.items(), key=lambda kv: -len(kv[1])):
        codes = ", ".join(sorted(q["platform_code"] or "?" for q in quays))
        print(f"  {len(quays):3d}  {name}  [{codes}]")
    print(f"→ {args.out}")
    return 1 if real else 0


if __name__ == "__main__":
    sys.exit(main())
