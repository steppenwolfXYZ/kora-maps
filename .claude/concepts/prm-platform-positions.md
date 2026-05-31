# PRM Platform Positions

## Problem

Per-platform stop positions in the rendered map are unreliable. The visible symptoms are stops placed in the middle of intersections, both directions of a bus collapsed onto a single point, and stops snapped to the wrong side of a station because no nearby OSM platform node could be matched.

The root cause is in the GTFS feed: every platform-level `stop_id` in the Swiss SBB feed (e.g. `8500010:7` for Basel SBB platform 7, `8507000:A-D` for Bern platforms A–D) shares its parent station's exact coordinates. The platform identity is encoded in the `platform_code` column and in the stop_id suffix, but the per-platform WGS84 lat/lon is not — it is always the station centroid. `trips.txt` and `stop_times.txt` correctly reference platform-specific stop_ids per direction, so the per-direction information exists; only the coordinates collapse it.

The standard GTFS feed published by opentransportdata.swiss has no `shapes.txt` either, but that is the pfaedle problem, not this one. This concept is exclusively about per-platform coordinates.

## Current workaround

For each GTFS stop assigned to a line:

1. Take the GTFS centroid coordinate (the station centroid, in practice).
2. Orthogonally snap it to the OSM line polyline of the line being drawn.
3. If the snap distance exceeds 50 m, search a grid-indexed table of precise OSM stop nodes (`stop_position`, `tram_stop`, `platform`, `halt`) for a name-matching node within 200 m of the GTFS centroid; if its own snap-to-line distance is smaller, replace the position.
4. A snap-distance gate (300 m rail / 150 m non-rail) discards stops whose snap is too far — currently disabled to keep misassigned stops visible.

This is insufficient for four reasons:

1. Step 2 is purely geometric. A station with two parallel tracks snaps to whichever track happens to be a centimetre closer. There is no notion of "which track does this line use."
2. The OSM-node lookup uses loose substring name matching. Multi-platform stations with a shared station name resolve ambiguously; the closest-to-line node is chosen, which is not always the correct platform.
3. The override only triggers when the raw snap is more than 50 m off. Cases where the GTFS centroid happens to land near the line but in the middle of an intersection (no platform on either side) are not corrected at all.
4. Two directions of the same line at the same station receive the same coordinate. The per-direction platform identity carried by `stop_times.txt` (different `stop_id` suffixes for the two directions) is lost when both collapse onto the station centroid.

## Requirements

An authoritative per-platform coordinate source is introduced. The source is the Swiss PRM (Persons with Reduced Mobility) platforms dataset published on opentransportdata.swiss, which contains one row per platform edge with WGS84 coordinates. The NeTEx Switzerland export's `Quay` layer is an equivalent richer alternative if the CSV proves insufficient.

### Lookup

A new `platform_position_lookup` keyed by GTFS `stop_id` returns a WGS84 coordinate when one is available. Two join paths populate the lookup, in priority order:

1. **SLOID direct join.** GTFS `stops.txt` carries the SLOID (Swiss Location Identifier) in the `original_stop_id` column from October 2025 onwards. PRM rows are keyed by the same SLOID (format `ch:1:sloid:<UIC>:<n>`). Direct join.
2. **`(UIC, platform_code)` decomposition.** GTFS `stop_id` of the form `XXXXXXX:N` decomposes as `<parent UIC>:<platform_code>`. PRM rows expose both `parentServicePointSloid` (carrying the parent UIC) and a platform code. Used when SLOID is absent from the GTFS row.

The lookup is built once per pipeline run, the same way the GTFS stop metadata is loaded.

### Replacement of the position chain

When the lookup returns a coordinate for a GTFS stop_id, that coordinate replaces the GTFS centroid as the input to downstream rendering. The current snap-to-line step may remain as a cosmetic alignment to keep dots visually on the line, but it is no longer the primary positioning mechanism. The OSM-stop-node name-matching fallback (the 50 m override path) is no longer needed when the lookup returns a hit.

### Fallback

When the lookup returns no result — small stops without PRM coverage, recently added stops, foreign stops near the Swiss border — the current chain (GTFS centroid → snap to line → OSM-node override) remains as fallback. The fallback path is not removed.

### Per-direction correctness

Because the lookup keys on the platform-specific `stop_id`, and because `stop_times.txt` already references different platform stop_ids per direction, the two directions of a line at the same station resolve to different platform coordinates automatically. No explicit direction logic is required.

## Constraints

- PRM coverage is not 100%. Coverage gaps must fall through to the existing fallback path, not produce missing stops.
- Stops outside Switzerland (e.g. Domodossola, Konstanz, Lindau, Annemasse) are not in PRM. The fallback path serves them.
- The PRM dataset updates roughly weekly. Refresh cadence is similar to GTFS, handled by the same download pipeline pattern.
- The SLOID format and its presence in GTFS `original_stop_id` are recent additions and may evolve. The decomposition fallback path provides resilience against schema drift.
- Mountain railway and ferry stops are typically present in PRM but the existing mountain/ferry handling (gtfs_stops embedded in line feature, straight-line geometry) is unchanged.
- The pill/connector/dot rendering pipeline downstream of stop positioning — clustering, parent_station merge, nearest-neighbor pill path, mode-dominant color selection — is unchanged. Only the per-stop coordinate input changes.
- This work is fully independent of the pfaedle migration and the gtfs-line-grouping concept. It changes only the per-stop coordinate input and lives in the stop-extraction stage. It can be implemented in parallel with or after either of those without conflict.
- International expansion is out of scope. PRM is Swiss-only. Per-country equivalents (DELFI for Germany, IDFM for France, VAO for Austria, etc.) have different schemas and identifiers and will be separate adapters when those countries are added.
