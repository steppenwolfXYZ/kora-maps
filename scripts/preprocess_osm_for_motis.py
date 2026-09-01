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

With `--overlay` it also merges the synthetic station walk network
(`build_station_walk_network.py`) into the output: platform walk lines,
their level-checked welds into the real pedestrian graph, and lift hubs.
Valhalla cannot route on areas, so without this a platform has no
routable geometry and walks end on whatever edge is nearest in plan view
— at a stacked station regularly a deck two levels up. See
`.claude/concepts/station-walk-network.md`.

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

OVERLAY_PBF = OSM_DIR / "station_walk_network.osm.pbf"


def _read_overlay(path: Path):
    """Load the synthetic overlay into memory (it is a few MB at most)."""
    nodes, ways = [], []
    for obj in osmium.FileProcessor(str(path)):
        if obj.is_node():
            nodes.append((obj.id, obj.location.lon, obj.location.lat,
                          dict(obj.tags)))
        elif obj.is_way():
            ways.append((obj.id, [n.ref for n in obj.nodes], dict(obj.tags)))
    return nodes, ways


def patch(in_path: Path, out_path: Path, overlay: Path | None = None) -> None:
    if not in_path.exists():
        raise SystemExit(f"input PBF not found: {in_path}")

    ov_nodes, ov_ways = ([], [])
    if overlay is not None:
        if not overlay.exists():
            raise SystemExit(
                f"overlay not found: {overlay} — run "
                "scripts/build_station_walk_network.py first")
        ov_nodes, ov_ways = _read_overlay(overlay)

    # Synthetic ids sit far above every live OSM id, so appending each
    # synthetic block after the corresponding real block keeps the output
    # sorted by (type, id) the way every osmium consumer expects.
    n_ways = n_patched = 0
    wrote_nodes = wrote_ways = False
    with osmium.SimpleWriter(str(out_path), overwrite=True) as writer:
        def flush_nodes():
            nonlocal wrote_nodes
            if wrote_nodes:
                return
            wrote_nodes = True
            for nid, lon, lat, tags in ov_nodes:
                writer.add_node(osmium.osm.mutable.Node(
                    id=nid, location=(lon, lat), tags=tags))

        def flush_ways():
            nonlocal wrote_ways
            if wrote_ways:
                return
            wrote_ways = True
            for wid, refs, tags in ov_ways:
                writer.add_way(osmium.osm.mutable.Way(
                    id=wid, nodes=refs, tags=tags))

        for obj in osmium.FileProcessor(str(in_path)):
            if obj.is_node():
                writer.add_node(obj)
            elif obj.is_way():
                flush_nodes()
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
                flush_nodes()
                flush_ways()
                writer.add_relation(obj)
        flush_nodes()
        flush_ways()

    print(f"ways: {n_ways:,}  patched (added foot=yes): {n_patched:,}")
    if overlay is not None:
        print(f"overlay merged: {len(ov_nodes):,} nodes, {len(ov_ways):,} ways")
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
    ap.add_argument("--overlay", type=Path, nargs="?", default=None,
                    const=OVERLAY_PBF,
                    help="Merge the station walk network overlay "
                         f"(default {OVERLAY_PBF.name}). Implied by --valhalla.")
    ap.add_argument("--no-overlay", action="store_true",
                    help="Skip the overlay even for --valhalla.")
    args = ap.parse_args()

    overlay = args.overlay
    if args.valhalla and overlay is None:
        overlay = OVERLAY_PBF
    if args.no_overlay:
        overlay = None

    if args.input and args.output:
        patch(args.input, args.output, overlay)
        return

    if args.valhalla:
        patch(OSM_DIR / "ch_pfaedle.osm.pbf",
              OSM_DIR / "ch_pfaedle_walkable.osm.pbf", overlay)
    else:
        # MOTIS's own OSR graph is not a walking authority (Valhalla is),
        # so the overlay is deliberately not merged here.
        patch(OSM_DIR / "switzerland-latest.osm.pbf",
              OSM_DIR / "switzerland-motis.osm.pbf")


if __name__ == "__main__":
    main()
