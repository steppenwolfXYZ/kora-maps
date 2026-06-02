# GTFS Source Switch

## Problem

The current GTFS feed is sourced from the third-party Geops mirror (`gtfs.geops.ch`), which strips three pieces of information present in the upstream Swiss data:

1. The Swiss Location ID (SLOID), which the official feed exposes in `stops.txt` via the `original_stop_id` column, populated for ~97% of stops. Without it, joins to SBB atlas master data fall back to UIC + platform-code decomposition.
2. The track-number prefix on sector-range platform codes. Bern S-Bahn departures show up as `A-C`, `E-H`, `AB` instead of `12A-C`, `1E-H`, `13AB`. Cross-verified: Google Maps still has the full info because it consumes a richer source. The official OTD GTFS feed also retains it.
3. Roughly 36 000 stops carried by the official feed and absent from Geops entirely.

The official "Timetable 2026 (GTFS2020)" dataset on opentransportdata.swiss carries all three. Switching the source is a prerequisite for the PRM-platform-positions concept (which is designed around the SLOID-direct join) and a precondition for any later feature that needs the missing fields.

## Requirements

### Source

The GTFS download is switched from `gtfs.geops.ch` to the official "Timetable 2026 (GTFS2020)" dataset on opentransportdata.swiss. The release-date suffix in the resource URL means the download step discovers the current resource at runtime rather than hardcoding a URL.

### Schema differences absorbed

The pipeline adapts to the following behaviour-affecting differences relative to the Geops feed:

- **SLOID column.** `stops.txt` carries `original_stop_id` with `ch:1:sloid:…` strings for ~97% of stops. The column is read but does not displace `stop_id` as the primary key.
- **Stop_id format.** Stop_ids gain an extra middle segment (`8507000:0:12` instead of `8507000:7`). The parent UIC remains the first colon-delimited segment.
- **Parent_station prefix.** Parent references in `stops.txt` carry a `Parent` prefix (e.g. `Parent8507000`). The prefix is stripped wherever parent_station is consumed as a stop_id reference.
- **Extended `route_type`.** `routes.txt` uses the extended GTFS code space (3–4 digit codes such as 109, 700, 900, 1300, 1400) instead of the basic single-digit codes Geops emits (2, 3, 5, 6, 7).
- **Sector-range platforms become resolvable.** Full platform codes (`12A-C`, `4F-H`, `13AB`, etc.) appear in `platform_code`. Sector-range stops typically reuse their parent track's SLOID, so the SLOID-direct join lands them on the correct atlas BOARDING_PLATFORM coordinate.

### Bucket re-classification

The route_type-to-bucket mapping in the trip-loading stage is rewritten against the extended code space. Each existing bucket — train, tram, city_bus, regional_bus, ferry, mountain — gets a documented set of accepted extended codes. The `mountain_agency_ids` rebucketing rule (which overrides rail-coded routes from listed agencies to the mountain bucket) is preserved unchanged.

### Verified parity

After the switch, the emitted line set is compared against the pre-switch output for an overlapping date. The diff is reviewed for lines that move between buckets, are newly drawn, or stop being drawn. Bucket boundaries are tuned until the differences correspond to deliberate decisions, not silent reclassification.

### Diagnostic record

A diagnostic record per emitted line surfaces the raw `route_type` and the assigned bucket, so the new mapping is inspectable post-run and can drive the parity review.

## Constraints

- This concept must land before prm-platform-positions. PRM depends on the SLOID-direct join, which only works with the official feed.
- The download cadence drops from daily (Geops) to ~twice weekly (official). Acceptable; the underlying schedule changes less often than that.
- The official feed is ~54% larger (102k stops vs 66k). Memory and runtime in the `stop_times.txt` streaming stage may need profiling. Optimisation is in scope only if a measurable regression appears.
- The agency exclusion filter (substring match on `agency_name`) is unchanged.
- The bus mode classification rules established by the implemented `bus-mode-classification.md` concept are reapplied on top of the new bucket map; classification semantics do not change.
- Geops-only columns (`stop_code`, `stop_desc`, `stop_elevation`, `zone_id`, `stop_url`, `ch_station_*`, `attributes_ch`, `bikes_allowed`, `shape_dist_traveled`) are dropped from pipeline reads where currently used.
- The ~3% of platform-level stops without a SLOID, and stops where SLOID resolution still misses atlas (~0.8% of those with SLOID), fall through to the existing UIC + platform_code path and ultimately to the GTFS centroid. The PRM-platform-positions diagnostic surfaces them.
- International expansion is unaffected: foreign GTFS feeds (DELFI, IDFM, VAO, etc.) remain separate adapters.
