# GTFS trip overrides

This document covers the `gtfs_trip_overrides` framework in `scripts/transit/config.yaml`: structural, config-driven rewrites of trips on named routes, applied during GTFS preprocessing before pfaedle. Two actions, both implemented: `split_at_stop` (the original subject of this document, formerly titled "GTFS trip split at stop") and `insert_waypoint`.

---

# Action: split_at_stop

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

---

# Action: insert_waypoint

## Problem

Pfaedle sometimes routes a legally permitted but physically wrong path, because the real-world constraint is not encoded anywhere it can see. The mechanism is general: forcing the shape through a via point corrects any such misroute, anywhere along the trip, in any mode. Two known error classes so far, each illustrated by an example:

- **Wrong path in the first metres of a trip.** Example: Dornach Bahnhof, bus 56. The bus departs platform E, where a vehicle must stand facing north (doors toward the platform) and leave through the forecourt's northbound exit, turning around via Amthausstrasse to head south. The bay lane is correctly mapped as two-way in OSM (platform F uses it southbound), GTFS has no door-side concept, and pfaedle has no notion of "platform dictates standing direction" — so it legally exits south through the back of the bay. The wrong initial direction poisons everything downstream: direction classification at the platform, pill-arrow side and orientation, and the stop position line at close zoom.
- **Wrong route choice between parallel infrastructure.** Example: R43 and Glacier Express over the Furka. Pfaedle's cost model estimates travel time from tag-class speed assumptions, not real speeds: the base tunnel is demoted by `usage=branch` while the DFB heritage line over the pass matches no class and keeps the fastest default, so pfaedle draws regular services over the mountain line. A mid-tunnel waypoint forces the correct path.

No automatic signal can fix these errors; they need a manual, per-case override — but a reusable one, not route-specific code.

## Requirements

- A second `gtfs_trip_overrides` action: `insert_waypoint`. Per entry the user supplies: `agency_id`, `route_short_name`, `action: insert_waypoint`, `after_stop_id`, `before_stop_id`, `waypoint` (lon, lat), and a free-text `reason`.
- Applied during GTFS preprocessing, before pfaedle: on every trip of the matched route where `after_stop_id` is immediately followed by `before_stop_id`, a **synthetic stop** at the waypoint coordinate is inserted between the two, with times interpolated between its neighbours (non-decreasing; the existing arr/dep repair applies after insertion).
- Pfaedle then has to route through the waypoint, which forces the shape onto the intended path (for Dornach 56: north out of the forecourt, turnaround, then south).
- Synthetic stop ids carry a reserved marker prefix — `WPT:` — introduced by this action and used nowhere else.
- **The waypoint exists only for pfaedle.** Every post-routing consumer of stop sequences drops `WPT:`-prefixed stops on load. No stop, dot, pill, pill-arrow, popup, destination, or diagnostic stop entry may ever show a waypoint; direction keys, merged stop sets, and frequency computations must be identical to a hypothetical run where pfaedle had produced the correct shape unaided.
- Waypoint coordinate convention follows `gtfs_stop_overrides`: taken from the routable OSM way at the intended via position, so pfaedle snaps it reliably.
- Direction scoping is inherent: matching is on the ordered consecutive stop pair, so the opposite direction (where the pair does not occur in that order) is untouched.
- The trip-overrides audit output records each entry with the count of affected trips.

## Constraints

- Same identifier-robustness failure mode as `split_at_stop`: if the route is renumbered upstream, the override silently stops applying and the broken-shape baseline returns — visible on the map, fixable by updating the entry.
- Trips on the matched route that do not contain the ordered pair consecutively (short workings, variants) are left untouched without per-trip warnings; the audit's matched/total counts are the visibility mechanism.
- A `WPT:` id surviving into any rendered or diagnostic stop output is a bug, not a tuning issue.
- One waypoint per entry. Multiple waypoints between different stop pairs are simply multiple entries; multiple waypoints between the same pair are deferred until a case appears.
- `split_at_stop`, `insert_waypoint`, and `gtfs_stop_overrides` may coexist on the same route.
