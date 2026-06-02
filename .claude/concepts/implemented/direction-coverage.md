# Direction Coverage

## Problem

When two opposite directions of a line share the same merged stop set, the pipeline bundles them into a single emitted feature with one rep trip. The rep trip's pfaedle shape covers only that direction's physical tracks, so in any inner-city one-way loop (visible on Bern's Tram 3, 7, 8 around Bahnhof) or bus roundabout, the opposite direction's geometry is missing from the rendered line. The same issue affects rail and mountain rack rail wherever the two directions follow different OSM ways.

The same collapse drops half the stop-pill information: a station's two platforms (one per direction) end up represented by a single stop. This blocks any future per-direction platform display (PRM platform extension).

## Requirements

### Per-direction emission

Every emitted line feature for an in-scope mode represents service in exactly one direction. In-scope modes are train, tram, metro, bus, regional_bus, and the `rebucketed_rail` subset of mountain. A line that operates both directions emits two features. A line observed in only one direction continues to emit one.

Out-of-scope modes (ferry, plus `aerial` and `funicular` mountain origins) collapse both directions into a single feature per merged stop set — see **Mode exemptions** below for the full rule.

### Direction key

A new property `direction_key` is set on every line feature and every stop-pill feature. `direction_key` is derived from the **first and last merged stop UICs** of the trips it represents, written as a stable ordered pair. GTFS `direction_id` is informational only and is not used as the partitioning input (it is empty across many Swiss feeds, including all BernMobil trams).

The two directions of one variant carry distinct `direction_key` values: the pair `(first_uic, last_uic)` and its reverse `(last_uic, first_uic)` are the two directions.

### Variant partitioning

Trip-group → variant partitioning still groups trips by merged stop set. Within each variant a new **per-direction sub-partition** is introduced, keyed by `direction_key`. Each sub-partition produces its own feature with its own pfaedle shape derived from its own rep trip.

### Per-direction filters

- **Rare-variant filter** (10% / 5% fallback): operates on the per-direction sub-partition's weighted sample-day count, not the merged-set total. One direction can survive while a much weaker opposite direction is dropped.
- **Active-days filter** (`min_active_days`): applies per direction. One direction with fewer than `min_active_days` active days is dropped independently of the other.
- **Freq-score gate** (`MIN_FREQ_SCORE`): continues to apply at the trip-group level. If the line as a whole is sub-threshold, neither direction is drawn.

### Rep-trip selection

Within a direction sub-partition, rep-trip selection follows the existing rule: the most popular raw-stop-set by sample-day-weighted count wins, with tiebreaks on smallest min trip_id and best service-day coverage. The chosen rep's pfaedle shape becomes that direction's geometry. Opposite-direction shapes are never flipped or synthesised from the other direction.

### Stop pills, per direction

The per-feature stop list (`line_stops`) records the rep trip's stop sequence in its actual order — no reversal, no canonicalisation. Each stop entry retains the **platform-suffixed GTFS stop_id** (e.g. `8576646:J`), not the parent-station UIC.

The stop-pill builder consumes per-direction stop lists. At any station served by both directions, the two platforms become two distinct stop-pill features carrying different `direction_key` values. Their geometries may sit at different positions (one per platform) once PRM platform positions are wired in.

### Emission identity

The full emission key becomes `(line_key, agency_id, trip_group_id, merged_stop_set, direction_key)`. Feature IDs remain globally unique.

### Diagnostics

`gtfs_groups_full.json` gains a per-direction sub-entry inside each variant entry, exposing `direction_key`, weighted trip count, active-days count, kept-or-dropped status, and emission reason for each direction.

### Mode exemptions

Three rules — per-direction split, freq-score gate, active-days gate — share the same exemption set:

- **Ferry bucket** — fully exempt. Per-direction split is skipped (both directions collapse into one feature), and both gates are skipped.
- **True mountain modes** — GTFS `route_type` 5 (cable car), 6 (gondola), 7 (funicular). Fully exempt: per-direction split skipped, both gates skipped.
- **Rebucketed mountain rail** — trips that started life as GTFS `route_type` 2 and were rebucketed to `mountain` via the `mountain_agency_ids` whitelist are **not** exempt from any of the three rules. The per-direction split, the freq-score gate, and the active-days gate all apply to them the same way they apply to normal `train` lines. The rendering style (light-yellow mountain color, fixed width) stays unchanged; only the pipeline behaviour matches `train`. A `mountain_origin` field on each emission record distinguishes `aerial` / `funicular` / `rebucketed_rail` so downstream code can branch correctly.

For exempt modes the variant key collapses to `(merged_stop_set,)` — `direction_key` is set to a single canonical value on every trip in the group so all directions merge. Emitted features still carry a `direction_key` property; for exempt modes it carries the canonical sentinel (it does not represent a real terminus pair).

The aerial-mountain dedup rule (cable-car / gondola bbox overlap, ref-keyed) is unchanged in spirit: same-ref aerial features that overlap collapse to the best-vertex-count winner. Because aerial is now fully exempt from the per-direction split, the rule no longer needs to key on `direction_key` — it keys on `ref` alone and never applies to `rebucketed_rail`.

## Constraints

- Where both directions follow the same physical track (typical suburban regional bus on a two-way street), the two emitted features overlap geometrically. This is acceptable — the visual result is identical to one feature; the difference is in the data model (two features carrying per-direction stop lists).
- The pfaedle shape attached to each direction is whatever pfaedle produced for that direction's rep trip. No attempt is made to flip or synthesise a missing direction's geometry from the opposite direction.
- Lines that genuinely operate one direction only (loop services, unidirectional special runs that survive filtering) emit one feature with a single `direction_key`.
- Loop trips (same first and last UIC) cannot be split by terminus pair and emit one feature, regardless of how many physical orientations the loop has.
- Map style consumers that assume one feature per `(line_key, agency_id, trip_group_id, merged_stop_set)` must accept up to two. The change is additive on the feature side; no existing property is removed.
- The PRM platform-positions concept depends on per-direction stop pills already being in place. This concept must land before per-direction platform coordinates can be delivered.
- GTFS `direction_id` is not normative. Feeds that populate it correctly will agree with the terminus-pair `direction_key`, but the pipeline never reads `direction_id` for grouping.
