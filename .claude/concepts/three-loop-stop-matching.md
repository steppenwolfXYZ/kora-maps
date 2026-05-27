# Three-Loop Stop Assignment Architecture

## Problem

The current stop assignment pass processes each OSM route through a single inline cascade: string matching → tricks → geo-fallback, all for one route at a time before moving to the next. This produces three related problems:

1. **No global ordering guarantee.** A route processed early in the list can claim a GTFS line via geo-fallback that a route processed later would have claimed via simple string matching. Processing order determines results in ways that are invisible and unpredictable.

2. **Tricks are not uniformly sanity-checked.** The alpha-prefix trick (e.g. `"RE4"` → `"RE"`) currently sets `matched_gtfs_ref` to a generic key, which then enters the canonical lookup path where the sanity check fires only conditionally (Trigger 1). There is no guarantee that every trick-based match passes a sanity check.

3. **The geo-fallback pool is never pruned.** When geo-fallback runs for a route, it searches all of `_line_canonical_export` with no knowledge of what string matching has already settled. Lines that were confidently string-matched by other routes can still "attract" geo-fallback candidates for unrelated routes.

## Current workaround

`_group_reassign_stops` provides post-hoc correction of stop-level assignment within already-matched groups (e.g. preventing branch-stop leakage between S3 variants). This is orthogonal and remains valid after this overhaul — it operates on stop positions within settled line matches, not on which GTFS line an OSM route is matched to.

## Requirements

Stop assignment runs as **three sequential batch loops**. Each loop iterates over all OSM routes not yet settled by a prior loop. A route is "settled" when it receives a match that passes its required checks. A route that gets a candidate match but fails its checks is returned to the unmatched pool for the next loop — no partial result is written.

### Loop 1 — Simple string matching

Matching strategy: compare OSM `ref` against GTFS `long_name` (normalized: spaces stripped, case-insensitive) first, then `short_name` (same normalization). No tricks.

Sanity check: fires when the match is a name fallback (matched key differs from the exact OSM ref after normalization). Direct exact matches are trusted without sanity check.

Routes that match and pass: settled.
Routes that match but fail sanity check: returned to unmatched pool.
Routes with no string match: returned to unmatched pool.

### Loop 2 — Advanced string matching (tricks)

Runs only over the unmatched pool from Loop 1.

Matching strategy: string tricks applied after the same long_name/short_name attempt. Defined tricks (minimum):
- RE↔R conversion (MGB trains tagged RE in OSM, R in GTFS long_name)
- Name-prefix token extraction (OSM `name` field split on `:`, tokens tried individually)
- Alpha-prefix stripping (`"RE4"` → `"RE"`)

When alpha-prefix produces a pure-alpha match (no digits in the matched key, e.g. `"RE"`, `"R"`, `"S"`, `"IC"`): `gtfs` (freq/speed) may be set from it, but `matched_gtfs_ref` is NOT set. This sends the route to Loop 3 for stop assignment rather than using a generic canonical bucket.

Sanity check: runs unconditionally for every match in Loop 2, no exceptions.

Routes that match and pass sanity: settled.
Remaining: unmatched pool for Loop 3.

### Loop 3 — Geo-fallback

Runs only over the unmatched pool from Loop 2.

Matching strategy: full geo-fallback as currently implemented — scores all GTFS candidates in the bucket by bbox overlap, runs sanity check on each in ranked order, uses first that passes.

Sanity check: always runs (unchanged from current behaviour).

Routes that find no passing candidate: excluded (not drawn), same as current rule.

## Constraints

- `_group_reassign_stops` runs after all three loops complete. It is unaffected by this change — its inputs (settled matched groups) are cleaner after this overhaul, but its logic does not change.

- Mountain lines (processed GTFS-first in a separate loop) and ferry lines (with their own pier-stop geo collection) are not part of the three-loop architecture. They retain their current special handling.

- The main OSM loop that determines freq/speed and outputs `transit_lines.geojson` is a prerequisite to stop assignment and is not restructured by this change. The ordering fix (long_name before short_name, alpha-prefix does not set `matched_gtfs_ref`) applies to both the main loop cascade and the stop assignment cascade identically.

- The three loops must share a single parameterized implementation function (loop level 1 / 2 / 3 as parameter), so the candidate selection and sanity check logic cannot diverge between loops over time. The level parameter controls: which matching strategies are attempted, whether sanity check is conditional or unconditional, and whether the candidate pool is the full `_line_canonical_export` or a subset.

- "Claiming" a GTFS line is not exclusive: multiple OSM relations for the same line (different directions, short-turns) legitimately match the same GTFS line_key. Loop 2 and Loop 3 do not exclude already-settled GTFS lines from their candidate pool — the ordering benefit is about processing priority, not exclusion.
