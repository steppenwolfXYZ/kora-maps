# PRM Platform Positions

## Problem

Per-platform stop positions in the rendered map are unreliable. The visible symptoms are stops placed in the middle of intersections, both directions of a bus collapsed onto a single point, and stops snapped to the wrong side of a station because no nearby OSM platform node could be matched.

The root cause is in the GTFS feed: every platform-level `stop_id` in the Swiss SBB feed (e.g. `8500010:7` for Basel SBB platform 7, `8507000:A-D` for Bern platforms A–D) shares its parent station's exact coordinates. The platform identity is encoded in the `platform_code` column and in the stop_id suffix, but the per-platform WGS84 lat/lon is not — it is always the station centroid. `trips.txt` and `stop_times.txt` correctly reference platform-specific stop_ids per direction, so the per-direction information exists; only the coordinates collapse it.

The standard GTFS feed published by opentransportdata.swiss has no `shapes.txt` either, but that is the pfaedle problem, not this one. This concept is exclusively about per-platform coordinates.

## Current behaviour

The pipeline today uses the GTFS centroid coordinate as the rendered stop position. No OSM platform data is consumed and no per-platform refinement is applied, so every platform-level `stop_id` at a station resolves to the parent station's centroid. An OSM stop-node name-matching override and a snap-distance gate exist in the code from before the pfaedle migration but produce no meaningful corrections any more; both are removed by this work.

## Requirements

An authoritative per-platform coordinate source is introduced. The source is the Swiss atlas v2 "Zones and stop places" CSV (dataset id `traffic-point-v2` on opentransportdata.swiss, refreshed daily). Rows with `trafficPointElementType = BOARDING_PLATFORM` carry one WGS84 coordinate per physical track edge, together with the SLOID, the parent station SLOID in `parentSloidServicePoint` and the parent station UIC in `number`, the customer-facing platform code in `designation`, and edge length and orientation (`length`, `compassDirection`). `boardingAreaHeight` is the accessibility metric "platform height above rail" and is not consumed.

The PRM accessibility companion dataset (`platform-v2`, same portal) carries no coordinates; it is named here only as the future home of accessibility attributes (wheelchair-area dimensions, boarding device, inclination) that other features may want later. The colloquial name "PRM platforms" in this concept refers to atlas's per-platform coordinate model, not to that companion file.

The Swiss NeTEx export carries the same data but is not used: ~4 GB streaming XML versus ~22 MB pandas CSV, with no field benefit for stop positioning. NeTEx adoption is deferred until an independent need (e.g. inter-Quay interchange paths) justifies its setup cost; at that point platform coordinates can move with it.

### Lookup

A new `platform_position_lookup` keyed by GTFS `stop_id` returns a WGS84 coordinate when one is available. The lookup is populated from `traffic-point-v2` rows filtered to `trafficPointElementType = BOARDING_PLATFORM`. Two join paths are tried, in priority order:

1. **SLOID direct join.** GTFS `stops.txt` carries the SLOID in the `original_stop_id` column from October 2025 onwards. Atlas rows are keyed by the same SLOID. Direct join on the SLOID string.
2. **`(UIC, platform_code)` decomposition.** GTFS `stop_id` of the form `XXXXXXX:N` decomposes as `<parent UIC>:<platform_code>`. Atlas rows expose the parent UIC in `number` and the platform code in `designation`. Used when SLOID is absent from the GTFS row.

The lookup is built once per pipeline run, at the stop-extraction stage (the stage that currently finalises each stop's rendered coordinate). The atlas CSV is loaded there and consumed there; no other stage references it.

### Multi-match resolution

When the `(UIC, platform_code)` decomposition matches more than one BOARDING_PLATFORM row (stub tracks, sectorised long platforms), the candidate with the lexicographically smallest SLOID is selected. This rule is deterministic and stable across atlas refreshes. Refinement is deferred until evidence of wrong picks surfaces; the diagnostic output below makes such cases inspectable.

### Diagnostic output

A new diagnostic file `stop_position_sources.json` is written under `data/transit/`, keyed by GTFS `stop_id`, with one entry per stop that appears in any drawn line. Each entry records the resolution path used (`sloid_join`, `uic_join`, or `centroid_fallback`), the selected atlas SLOID when an atlas join succeeded, and the rejected candidate SLOIDs when the `(UIC, platform_code)` join returned more than one row. Stops resolved via the centroid fallback are recorded with the path tag only and no SLOID. This makes both wrong-track picks (the multi-match failure mode) and atlas-coverage gaps inspectable without re-running the pipeline.

### Position chain

After this work, every stop's position is produced by:

1. Look up the GTFS stop_id in `platform_position_lookup`. If a coordinate is returned, use it. Otherwise use the GTFS centroid.
2. Orthogonally snap the result to the line polyline of the line being drawn. The snap is cosmetic — it keeps the rendered dot visually on the line.

The OSM stop-node name-matching override and the snap-distance gate that exist in the current pipeline are removed in both branches of step 1. The grid of OSM stop nodes (`stop_position`, `tram_stop`, `platform`, `halt`) that fed only the override becomes unused and is removed at the same time. Atlas is the single source of platform positions; there is no second mechanism competing with it.

### Per-direction correctness

Because the lookup keys on the platform-specific `stop_id`, and because `stop_times.txt` already references different platform stop_ids per direction, the two directions of a line at the same station resolve to different platform coordinates automatically. No explicit direction logic is required.

### Source download and refresh

The atlas v2 traffic-point CSV download is added to the existing GTFS download stage; the two sources refresh together. A new `--force` flag on the rebuild script forces re-download of all external source data (GTFS, atlas, OSM); without the flag, each download step skips when the target file already exists locally. This lets the rebuild be re-run from an early stage without paying the multi-GB OSM download every time.

## Constraints

- Atlas v2 coverage is not 100%. Roadside bus stops often lack a per-edge BOARDING_PLATFORM row; coverage gaps fall through to the GTFS centroid branch of the position chain, not to a missing stop.
- Stops outside Switzerland (e.g. Domodossola, Konstanz, Lindau, Annemasse) are not in atlas and use the centroid branch.
- The atlas v2 traffic-point dataset updates daily. Refresh is handled by the same download pipeline pattern as GTFS.
- The SLOID format and its presence in GTFS `original_stop_id` are recent additions and may evolve. The `(UIC, platform_code)` decomposition is the resilient join path.
- BOARDING_AREA rows in atlas (the platform-body records) are not consumed. Swiss train stop_ids encode the track number, which maps to BOARDING_PLATFORM `designation` directly; tram and bus feeds use customer-facing platform letters or numbers that also map to BOARDING_PLATFORM `designation`. The lookup can be extended later if a feed surfaces platform-body codes.
- This work depends on direction-coverage being landed first: per-direction stop_ids must already flow through the stop-extraction stage. It is independent of the pfaedle migration and the gtfs-line-grouping concept.
- International expansion is out of scope. Atlas v2 is Swiss-only. Per-country equivalents (DELFI for Germany, IDFM for France, VAO for Austria, etc.) have different schemas and identifiers and will be separate adapters when those countries are added.
