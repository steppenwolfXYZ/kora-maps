# Dedup Agency Scoping

**Status:** Implemented

## Problem

Two unrelated GTFS lines from different operators can share the same name (e.g. SBB R2 Lausanne–Bex and RhB R2 Landquart–Davos). The dedup currently groups entries by `gtfs_ref` string alone, treating them as the same line. A direct-ref OSM match for one operator then incorrectly removes the other operator's fallback-matched entry, leaving a corridor undrawn with no error or warning.

## Current workaround and why it's wrong

A `_dedup_cell` (0.5° grid cell derived from the centroid of matched stops) was added as a stopgap, grouping dedup by `(gtfs_ref, geo_cell)`. This is conceptually wrong: the centroid of matched stops varies depending on which stops fall inside the OSM route's bbox, so two OSM relations of the same line can land in different cells and escape a dedup they should be subject to. This has been observed causing S-Bahn and bus lines to appear as duplicates. The geo-cell workaround must be removed.

## Requirements

Dedup must group entries by the identity of the matched GTFS line, not just its name. The correct identity is `line_key_full = (short_name, long_name, bucket, agency_id)` — the full logical line identity scoped to the operator. Two entries with the same name but different `agency_id` are unrelated lines and must never be in the same dedup group.

`agency_id` is available in the GTFS `routes.txt` feed but is currently not carried through the pipeline. It must be made available at the point where `line_stops_out` entries are written, and stored as `line_key_full` so the dedup can group by it.

`line_key` (currently `(short_name, long_name, bucket)`) must not change — `line_key_full` is a separate, dedup-only concept. Agency identity is not needed elsewhere in the pipeline.

## Constraints

- **Co-operated lines (ChurBus / PostAuto):** One bus line in Chur is jointly operated by two agencies. With agency-scoped dedup each operator forms its own group — this is acceptable, no special handling needed.
- **Geo-cell workaround must be fully reverted** as part of this change.
