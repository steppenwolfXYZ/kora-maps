# Transit Preprocessing

## Problem

The current "main loop" performs OSM→GTFS matching for every OSM route to obtain freq/speed data, and drops routes whose match is below `MIN_FREQ_SCORE` or finds no match at all. Because this matching is inferior to the 4-loop architecture, it prematurely drops routes the 4-loop would have correctly matched.

A compounding problem: `_line_canonical_export` currently excludes low-frequency GTFS lines at build time. An OSM route that correctly belongs to a low-frequency GTFS line cannot find its match in the 4-loop pool and falls to Loop 4 geo-fallback, where it wastes expensive sanity checks on unrelated candidates and risks a false-positive match.

Both problems share the same root cause: the draw/no-draw decision is made before the best OSM→GTFS match has been found.

## Current workaround

None. The main loop is the current architecture. Its GTFS matching cascade acts as the sole draw gate.

## Requirements

The main loop is replaced by two lightweight preprocessing passes — one over GTFS data, one over OSM routes — that feed the 4-loop. Neither pass does OSM→GTFS matching.

### GTFS preprocessing pass

Runs at `_line_canonical_export` build time. All GTFS lines are retained in `_line_canonical_export`. Lines that fail any of the following checks carry a `no_draw` flag with a `no_draw_reason` string:

| Check | Reason string |
|---|---|
| Frequency below `MIN_FREQ_SCORE` | `"low_frequency"` |

Mountain lines are exempt from `no_draw` flagging regardless of frequency.

### OSM preprocessing pass

Runs over `osm_routes` before the 4-loop. No GTFS matching. Does two things: hard exclusions (remove routes from the pool entirely) and classification (derive mode, bucket, and remapped ref for each remaining route).

**Hard exclusions** (route not entered into the 4-loop pool):
- Non-transit route tags: hiking, cycling, foot, fitness_trail
- `osm_to_mode()` returns `None` (unrecognized or explicitly excluded operators/networks such as Flixbus, BlaBlaCar)
- TER routes (OSM `ref` starts with "TER") — cross-border/French-domestic services, identifiable from OSM tags alone

**Classification** (pure OSM-tag logic, no GTFS):
- Mode: `osm_to_mode()` + bus→regional_bus refinement + mountain operator overrides (`MOUNTAIN_RAIL_OPERATORS`)
- Ref remapping (e.g. Forchbahn `ref.lstrip("S")`)
- Mountain routing: routes classified as mountain mode, or train-tagged routes whose ref exists in the GTFS mountain bucket (`osm_train_refs_in_mountain_gtfs`), are diverted to the GTFS-first mountain loop and excluded from the 4-loop pool. The mountain bucket check uses `gtfs_index` for a ref-existence test only — no matching or scoring.

### 4-loop (unchanged in matching logic)

The `no_draw` flag on GTFS candidates does not affect matching logic. The 4-loop settles on the best candidate regardless of the flag. `_try_assign` propagates the `no_draw` value from the settled `CanonEntry` into the returned entry dict so the post-4-loop draw gate can read it from `line_stops_out`.

### Post-4-loop draw gate

After all four loops complete (and after `_group_reassign_stops`):

1. Routes settled on a `no_draw`-flagged GTFS line are excluded from drawn output. Their `no_draw_reason` is recorded in diagnostic output alongside the matched line key.
2. Routes excluded by Loop 4 (no passing candidate found) continue to be excluded as before.
3. For all surviving settled routes: freq/speed is looked up using `short_name` + `bucket` from `_line_key_full` via `gtfs_index`. The corridor frequency boost (`pair_freq`) is applied using the settled canonical stops. Visual properties (`color`, `width_base`) are computed from the result. `transit_lines.geojson` is written from settled routes only.

## Sideline: replace `gtfs_ref` with direct name matching in dedup

`gtfs_ref` currently exists solely to drive `_refs_match` in the post-matching dedup, which classifies settled entries as "direct ref match" vs "fallback match" within each `_line_key_full` group. The way `gtfs_ref` is set is historically grown and indirect.

Replace `_refs_match(osm_ref, gtfs_ref)` with `_is_direct_match(osm_ref, short_name, long_name)` that works directly from `_line_key_full`:

- Normalise all three inputs: remove spaces, lowercase
- A direct match is when `norm(osm_ref)` equals `norm(short_name)` or `norm(long_name)`
- Exception: if the matching name is a generic term (defined in `GENERIC_GTFS_PREFIXES` in stop-assignment-architecture), it is not considered a direct match

`gtfs_ref` is then unused and removed from `line_stops_out` entries. `_try_assign` no longer needs to compute it.

## Sideline: remove `find_best_gtfs_candidate`

`find_best_gtfs_candidate` is currently called only in the main loop to obtain freq/speed data for visual styling. With the main loop removed and freq/speed lookup moved to post-4-loop, it has no remaining callers in the pipeline and must be removed.

## Sideline: adapt `diagnose_transit_line.py`

When a line is excluded because its matched GTFS entry carries a `no_draw` flag, `diagnose_transit_line.py` must display the exclusion reason (`no_draw_reason`) alongside the matched GTFS line key, so the user can distinguish a frequency-filtered exclusion from a Loop 4 geo-fallback exclusion.

## Constraints

- `_line_canonical_export` entries must be wrapped in a named structure (e.g. `CanonEntry` namedtuple) with fields `line_key`, `stops`, `dir_aware`, `agency_id`, `no_draw`. All pack/unpack of these entries must go through that structure so adding fields in future requires only a one-line change, not touching every call site.
- The `no_draw` flag must reside on `_line_canonical_export` entries. Flagging only `gtfs_index` is insufficient — the 4-loop reads candidates from `_line_canonical_export`.
- `MIN_FREQ_SCORE` threshold value is unchanged.
- Mountain GTFS-first loop is unaffected — it is already separate and remains so.
- Ferry pier-stop geo collection is unaffected.
- `main_loop_dropped.json` diagnostic output is preserved. Entries previously recorded as `no_gtfs`/`zero_freq` are now recorded as post-4-loop exclusions: settled-on-`no_draw` entries record the matched line key and `no_draw_reason`; Loop 4 exclusions record `matched_line_key: null`.
