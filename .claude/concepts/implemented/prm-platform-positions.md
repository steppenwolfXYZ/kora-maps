# PRM Platform Attributes

## Problem

The rendering pipeline needs per-platform attributes — platform `length` and compass orientation — that are not present in GTFS. These are needed by the upcoming pill-alignment concept and likely later features. The Swiss atlas master data system carries them; no other publicly available dataset does.

Per-platform coordinates, originally also in scope under this concept's name, are no longer needed. The gtfs-source-switch concept moved the pipeline to the official OTD GTFS feed, which carries proper per-platform coordinates for every numbered and sector-range platform (e.g. Bern's `12A-C` resolves to a point on track 12, distinct from the station centroid). Atlas's role here narrows to attribute enrichment.

## Current behaviour

No atlas data is loaded. Per-platform attributes are unavailable to downstream code.

## Requirements

### Source

Per-platform attributes are loaded from the Swiss atlas v2 "Zones and stop places" CSV (`traffic-point-v2` on opentransportdata.swiss, refreshed daily). Rows with `trafficPointElementType = BOARDING_PLATFORM` carry the SLOID, the parent station SLOID and UIC (`parentSloidServicePoint`, `number`), the WGS84 coordinate, and the per-platform attributes `length` (m) and `compassDirection` (degrees, geographic). Only those attribute fields are consumed by this concept.

The PRM accessibility companion dataset (`platform-v2`, same portal) is named here only as the future home of wheelchair-area dimensions, inclination, and tactile/boarding device fields if later features want them.

### Lookup

A new `platform_attributes_lookup` keyed by GTFS `stop_id` returns `{length, compassDirection}` when atlas has a row for the stop. The lookup is populated from `traffic-point-v2` rows filtered to `trafficPointElementType = BOARDING_PLATFORM`. Single join path:

- **SLOID direct join.** GTFS `stops.txt` carries the SLOID in the `original_stop_id` column, populated for ~97% of platform-level stops in the OTD feed. Atlas rows are keyed by the same SLOID. Direct join on the SLOID string.

No fallback decomposition is needed: the lookup is for attribute enrichment, not positioning, so a stop without atlas data simply has no attributes attached. SLOIDs are unique per BOARDING_PLATFORM row in atlas, so the join is single-valued by construction; no multi-match resolution is required.

### Per-stop positions are unchanged

The GTFS coordinate stays the rendered stop position; this concept never overrides it. Snap-to-line cosmetic alignment continues as today. The legacy OSM-stop-node name-matching override and the snap-distance gate (dead since the pfaedle migration) are removed as part of this work.

### Diagnostic output

A new diagnostic file `stop_attributes_sources.json` is written under `data/transit/`, keyed by GTFS `stop_id`, with one entry per stop that appears in any drawn line. Each entry records whether atlas attributes were found and the picked SLOID. Stops without an atlas match are recorded with a `no_atlas_match` tag so coverage gaps are inspectable without re-running the pipeline.

### Source download and refresh

The atlas v2 traffic-point CSV download is added to the existing GTFS download stage; the two sources refresh together. A new `--force-atlas` flag is added to the rebuild script, consistent with the `--force-gtfs` / `--force-osm` pattern established by gtfs-source-switch; the bare `--force` re-fetches atlas alongside the others.

## Constraints

- Atlas v2 coverage of attributes is partial: `length` is populated on ~30% of BOARDING_PLATFORM rows, `compassDirection` on ~38%. Even when the SLOID join succeeds, the attribute fields may be empty. Downstream consumers must handle the empty case.
- Stops outside Switzerland (e.g. Domodossola, Konstanz, Lindau, Annemasse) are not in atlas. They simply have no attributes.
- The atlas v2 traffic-point dataset updates daily. Refresh is handled by the `--force` / skip-if-present pattern, with the new `--force-atlas` flag for selective refresh.
- The SLOID format and its presence in GTFS `original_stop_id` may evolve. Missing matches degrade gracefully to "no attributes" — no downstream consumer is allowed to require an atlas hit.
- BOARDING_AREA rows (the platform-body records, no per-track coordinates) are not consumed; the concept reads only BOARDING_PLATFORM (track edge) rows.
- This work depends on gtfs-source-switch and direction-coverage being landed first.
- International expansion is out of scope. Atlas v2 is Swiss-only. Per-country equivalents (DELFI, IDFM, VAO, etc.) will be separate adapters when those countries are added.
