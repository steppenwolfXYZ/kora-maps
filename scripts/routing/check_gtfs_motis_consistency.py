#!/usr/bin/env python3
"""Preflight for the MOTIS import: is `data/gtfs_motis/` internally consistent?

The sidecar is a hardlink farm over `data/gtfs_routed/` with one file of
its own — the platform-anchored `stops.txt`. That single independent file
is also the one the old `sync_to_mac.sh` pushed on its own, which is how a
Mac ended up importing this machine's *new* `stops.txt` on top of its own
*old* `stop_times.txt`. SBB renumbers quays between releases (Bern
platform 8 went `ch:1:sloid:7000:0:229097` → `ch:1:sloid:7000:4:8`), so
the old stop_times referenced ids the new stops.txt no longer had. MOTIS
does not fail on that: nigiri drops the unresolvable stop, keeps the
trip, and reports a count. The IC1 then ran Fribourg → Zürich without
ever calling at Bern, and no query from Bern could find it.

Two cheap set checks catch every mixed-vintage combination we have seen:

  1. every `stop_id` in stop_times.txt exists in stops.txt
  2. every `service_id` in trips.txt exists in calendar.txt or
     calendar_dates.txt

Exit code 0 = consistent, 1 = inconsistent (with examples), 2 = missing
input. Called by setup_routing.sh step 7 before the importer runs.

Usage:
  python3 scripts/routing/check_gtfs_motis_consistency.py [--gtfs-dir DIR]
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DIR = ROOT / "data" / "gtfs_motis"

# How many offending ids to print per check. Enough to recognise the
# pattern (one station, one release's id scheme), short enough to read.
EXAMPLES = 5


def _column(path: Path, name: str):
    """Yield one column of a GTFS table by name, streaming."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return
        try:
            idx = header.index(name)
        except ValueError:
            sys.exit(f"{path.name}: no '{name}' column")
        for row in reader:
            if len(row) > idx:
                yield row[idx]


def _ids(path: Path, name: str) -> set[str]:
    return set(_column(path, name))


def check(gtfs_dir: Path) -> int:
    required = ["stops.txt", "stop_times.txt", "trips.txt", "calendar.txt"]
    for f in required:
        if not (gtfs_dir / f).is_file():
            print(f"✗ missing {gtfs_dir / f}", file=sys.stderr)
            return 2

    failures = []

    # 1 — stop references. The expensive one (stop_times is ~2.8 GB), so
    # count distinct offenders rather than collecting every row.
    known_stops = _ids(gtfs_dir / "stops.txt", "stop_id")
    dangling_stops: set[str] = set()
    n_rows = 0
    for sid in _column(gtfs_dir / "stop_times.txt", "stop_id"):
        n_rows += 1
        if sid not in known_stops:
            dangling_stops.add(sid)
    if dangling_stops:
        failures.append(
            f"{len(dangling_stops)} stop_id(s) in stop_times.txt are not in "
            f"stops.txt, e.g. {sorted(dangling_stops)[:EXAMPLES]}"
        )

    # 2 — service references. calendar_dates may legitimately be absent.
    known_services = _ids(gtfs_dir / "calendar.txt", "service_id")
    cal_dates = gtfs_dir / "calendar_dates.txt"
    if cal_dates.is_file():
        known_services |= _ids(cal_dates, "service_id")
    dangling_services = {
        s for s in _column(gtfs_dir / "trips.txt", "service_id")
        if s not in known_services
    }
    if dangling_services:
        failures.append(
            f"{len(dangling_services)} service_id(s) in trips.txt are in "
            f"neither calendar.txt nor calendar_dates.txt, e.g. "
            f"{sorted(dangling_services)[:EXAMPLES]}"
        )

    if not failures:
        print(
            f"  ✓ {gtfs_dir.name} consistent "
            f"({len(known_stops)} stops, {n_rows} stop_times rows)"
        )
        return 0

    print(f"✗ {gtfs_dir} is internally inconsistent:", file=sys.stderr)
    for f in failures:
        print(f"    - {f}", file=sys.stderr)
    print(
        "\n  This is a mixed-vintage feed: some files come from a different\n"
        "  GTFS release than the others. MOTIS would import it anyway and\n"
        "  silently drop the unresolvable stops, so trains keep running in\n"
        "  /stoptimes but vanish from /plan at the affected stations.\n"
        "\n"
        "  data/gtfs_motis/ is hardlinked from data/gtfs_routed/ except for\n"
        "  stops.txt, so the usual cause is a stops.txt that arrived on its\n"
        "  own — check mtimes:\n"
        f"      ls -la {gtfs_dir}\n"
        "  On the data machine, regenerate the sidecar from one feed:\n"
        "      python3 scripts/routing/preprocess_gtfs_for_motis.py\n"
        "  On the dev Mac, re-sync the feed whole instead — rebuilding here\n"
        "  only makes the sidecar agree with whatever old feed is present:\n"
        "      ./scripts/fetch_build.sh --only routed",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gtfs-dir", type=Path, default=DEFAULT_DIR,
                    help="feed directory to check (default: data/gtfs_motis)")
    args = ap.parse_args()
    return check(args.gtfs_dir)


if __name__ == "__main__":
    sys.exit(main())
