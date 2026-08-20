#!/usr/bin/env python3
"""Rewrite an OSM PBF so MOTIS / Valhalla accept walkable Alp/forest roads.

MOTIS's OSR foot profile (and Valhalla's pedestrian costing) treat
`access=agricultural` and `access=forestry` as pedestrian-blacklisted —
a defensible global default that is wrong for CH, where those tags only
forbid unauthorised motor vehicles (walking is always legal on
Alpstrassen and Forststrassen).

This step adds `foot=yes` to any way tagged `access=agricultural` or
`access=forestry` that doesn't already carry a `foot=*` override, so the
per-profile access_override picks it up as a foot-whitelist.

Uses pyosmium's `FileProcessor` iterator so nodes, ways, and relations
all round-trip correctly (the older `SimpleHandler` + `SimpleWriter`
combo silently drops ways past ~37M nodes on this dataset).

Idempotent: re-running the script produces the same output.

Two default invocations:
  * no args → CH-only PBF for MOTIS's own OSR (as before):
      switzerland-latest → switzerland-motis
  * `--valhalla` → wide-bbox PBF for the Valhalla pedestrian router:
      ch_pfaedle → ch_pfaedle_walkable
    (`ch_pfaedle.osm.pbf` is produced by the pipeline step 03; it
    already covers CH + DE + FR + IT + AT + LI within our bbox, so
    Valhalla can snap cross-border GTFS stops that a CH-only extract
    misses — Basel Bad, Konstanz, Domodossola, Weil am Rhein, etc.)

Manual paths can be passed explicitly with --input / --output.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import osmium

ROOT = Path(__file__).resolve().parent.parent
OSM_DIR = ROOT / "data" / "osm"

# `access=` values that block foot in the OSR / Valhalla pedestrian
# defaults but are walkable in CH by legal convention. `no`,
# `emergency`, `delivery`, `private` are intentionally NOT overridden —
# those genuinely mean no.
OVERRIDE_ACCESS = {"agricultural", "forestry"}


def patch(in_path: Path, out_path: Path) -> None:
    if not in_path.exists():
        raise SystemExit(f"input PBF not found: {in_path}")

    n_ways = n_patched = 0
    with osmium.SimpleWriter(str(out_path), overwrite=True) as writer:
        for obj in osmium.FileProcessor(str(in_path)):
            if obj.is_node():
                writer.add_node(obj)
            elif obj.is_way():
                n_ways += 1
                tags = dict(obj.tags)
                if (tags.get("access") in OVERRIDE_ACCESS
                        and "foot" not in tags):
                    new_tags = [(k, v) for k, v in obj.tags] + [("foot", "yes")]
                    writer.add_way(obj.replace(tags=new_tags))
                    n_patched += 1
                else:
                    writer.add_way(obj)
            elif obj.is_relation():
                writer.add_relation(obj)

    print(f"ways: {n_ways:,}  patched (added foot=yes): {n_patched:,}")
    print(f"→ {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--valhalla", action="store_true",
                    help="Preset for Valhalla: patch data/osm/ch_pfaedle.osm.pbf "
                         "→ data/osm/ch_pfaedle_walkable.osm.pbf.")
    ap.add_argument("--input", type=Path, default=None,
                    help="Explicit input PBF (overrides the preset).")
    ap.add_argument("--output", type=Path, default=None,
                    help="Explicit output PBF (overrides the preset).")
    args = ap.parse_args()

    if args.input and args.output:
        patch(args.input, args.output)
        return

    if args.valhalla:
        patch(OSM_DIR / "ch_pfaedle.osm.pbf", OSM_DIR / "ch_pfaedle_walkable.osm.pbf")
    else:
        patch(OSM_DIR / "switzerland-latest.osm.pbf", OSM_DIR / "switzerland-motis.osm.pbf")


if __name__ == "__main__":
    main()
