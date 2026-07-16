#!/usr/bin/env python3
"""
Build transit stop GeoJSON files:

  transit_stops.geojson      — Point features (circle dots, low-zoom)
  transit_stop_pills.geojson — LineString features (pill/capsule shapes, high-zoom)

Stop dot rules:
  - Every stop of every matched line gets a dot, visible from the same
    zoom level the line itself appears.
  - Rail (train): stops clustered within 300m → one dot per physical station.
  - All other modes: one dot per stop, snapped to the line geometry.
  - Every dot carries: color, mode, width_base (for data-driven circle radius).

Pill rules:
  - Pills appear when a cluster has ≥2 distinct OSM line IDs (osm_id).
  - Pill-appear zoom is determined by line count and dominant mode.
  - Ferry: no pills, but each parent_station emits a two-dot + connector
    pattern (snap-side dot, optional GTFS-side dot, optional connector). See
    stops-pill-zoom.md § "Ferry stops".
  - Mountain modes: no pills.
  - Pill geometry is derived from dot positions using a nearest-neighbor path:
      → Build a greedy nearest-neighbor path through ALL dot positions
        in the cluster. This ensures every dot is at a vertex of the pill.
      → If the path has a large gap between two groups (> gap threshold),
        split there and emit two pills + a thin connector.
      → Pills prefer cross-track orientation naturally: for parallel-track
        stops the NN path connects the nearby dots directly.
  - Cross-mode clustering: tram + bus at same location → one pill in tram color.
  - Color = dominant line at stop (by mode hierarchy, then width_base).
  - Width encoded as width_base → style applies ×2 multiplier.

The full pipeline lives in the `stops` package (entry: `stops.pipeline_setup`);
this file stays a thin driver. Shared constants live in `_state.py`.
"""


def main():
    from stops.pipeline_setup import run
    run()


if __name__ == "__main__":
    main()
