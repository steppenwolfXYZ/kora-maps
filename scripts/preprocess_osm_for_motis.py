#!/usr/bin/env python3
"""Rewrite the Swiss OSM PBF so MOTIS accepts walkable Alp/forest roads.

MOTIS's OSR foot profile treats `access=agricultural` and `access=forestry`
as pedestrian-blacklisted — a defensible global default that is wrong for
CH, where those tags only forbid unauthorised motor vehicles (walking is
always legal on Alpstrassen and Forststrassen).

This step adds `foot=yes` to any way tagged `access=agricultural` or
`access=forestry` that doesn't already carry a `foot=*` override, so
MOTIS's per-profile access_override picks it up as a foot-whitelist.

Uses pyosmium's `FileProcessor` iterator so nodes, ways, and relations
all round-trip correctly (the older `SimpleHandler` + `SimpleWriter`
combo silently drops ways past ~37M nodes on this dataset).

Idempotent: re-running the script produces the same output.
"""

from pathlib import Path

import osmium

ROOT = Path(__file__).resolve().parent.parent
IN_PATH  = ROOT / "data" / "osm" / "switzerland-latest.osm.pbf"
OUT_PATH = ROOT / "data" / "osm" / "switzerland-motis.osm.pbf"

# `access=` values that block foot in MOTIS's OSR but are walkable in CH
# by legal convention. `no`, `emergency`, `delivery`, `private` are
# intentionally NOT overridden — those genuinely mean no.
OVERRIDE_ACCESS = {"agricultural", "forestry"}


def main() -> None:
    if not IN_PATH.exists():
        raise SystemExit(f"input PBF not found: {IN_PATH}")

    n_ways = n_patched = 0
    with osmium.SimpleWriter(str(OUT_PATH), overwrite=True) as writer:
        for obj in osmium.FileProcessor(str(IN_PATH)):
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
    print(f"→ {OUT_PATH}")


if __name__ == "__main__":
    main()
