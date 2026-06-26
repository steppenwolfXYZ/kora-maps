# GTFS trip split at stop

## Problem

Some transit services are physically two separate vehicles operating on disconnected infrastructure with a mandatory transfer at a middle station, but GTFS encodes them as one continuous trip whose stop sequence spans both segments. OSM treats the two segments as a disconnected sub-graph (no node connection at the transfer), so pfaedle can route only the segment adjacent to whichever terminal it starts from, then connects the unrouted side with a kilometre-plus straight diagonal to the next stop. The result is a half-real, half-broken polyline in each direction.

The canonical case is Niesenbahn (Mülenen — Schwandegg — Kulm), a two-cable funicular where passengers physically change trains at Schwandegg. The Swiss GTFS feed correctly encodes the service as one continuous trip end-to-end, but the right visual is two independent lines meeting at the transfer stop.

## Requirements

- A new config-driven override list — `gtfs_trip_overrides` in `scripts/transit/config.yaml` — solves this case without any code path specific to a single route. Each entry names one route to split, by stable identifier.
- Per entry, the user supplies: `agency_id`, `route_short_name`, `action: split_at_stop`, `transfer_stop_id`, and a free-text `reason` field (ignored by the pipeline, present for human documentation).
- Splitting happens during GTFS preprocessing, before pfaedle runs. Every trip on the matched route is replaced by two new trips that share the original trip's route_id, service_id, direction_id, agency, headsign, and calendar, but each covers only one segment's stop sequence. The transfer stop appears as the terminus of one new trip and the origin of the other.
- Both directions of the original route are split. A Mülenen→Kulm trip becomes Mülenen→Schwandegg plus Schwandegg→Kulm; a Kulm→Mülenen trip becomes Kulm→Schwandegg plus Schwandegg→Mülenen.
- Service frequency on each segment is preserved. If the original route runs N times at a given sample date, each of the two segments must show N runs at that sample. The split is a structural rewrite of trips, not a doubling or halving of service.
- The rest of the pipeline treats the two segments as independent lines. Pfaedle routes each independently, trip grouping produces two trip groups, the emission loop produces two features. There is no post-pfaedle re-merge.
- The transfer stop keeps its original GTFS stop_id and coordinate. Stop and pill rendering at the transfer stop use the existing default ruleset — no transfer-specific rendering code is added. Whatever the default pill algorithm produces given two line endpoints landing at the same stop_id is the intended visual.
- Diagnostic outputs reference the new split trip ids. A mapping from each original trip id to its two split trip ids must be retrievable from the diagnostics so a debugger can trace either segment back to its source.

## Constraints

- Identifier robustness: `(agency_id, route_short_name)` is the matching key. If the route is renumbered upstream the override silently stops applying and the broken-shape baseline returns. This is the correct failure mode — visible on the map, fixable by updating the override.
- A trip on a matched route whose stop sequence does not include the named transfer stop is left unsplit, with a warning logged. The override must not silently drop or corrupt trips whose pattern doesn't match the assumption.
- Both segments of every split trip must contain at least two stops. The transfer stop cannot be the first or last stop in the original trip. Trips violating this are left unsplit with a warning.
- The split runs after the agency and EV-prefix filters already applied during GTFS preprocessing. Trips removed by those filters are not split.
- Only one transfer stop per override entry is supported. A hypothetical three-segment service would need either a second override entry chained on top of the first or a future extension accepting a list of transfer stops. Defer until a second case appears.
- Loop trips on a route subject to this override (first stop == last stop) are not expected and not supported.
- The original GTFS source files on disk are not modified. The split materialises in the same filtered-GTFS folder produced by the preprocessing step that the rest of the pipeline reads.
- Stop coordinate overrides (`gtfs_stop_overrides`) and trip splits coexist on the same route if both are needed.
- Trip-count diagnostics (e.g. `trip_groups.json` `trip_count`) reflect the post-split trips; the count is naturally higher than for an unsplit route. Frequency-related diagnostics (`f_weighted`, `freq_score`) are computed per segment from the post-split trips and are the correct per-segment values.
