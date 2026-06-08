# Leftover dot placement

## Problem

The dot-placement pipeline runs the perpendicular-sweep algorithm first (Candidate A) and then fills in any platforms it couldn't place onto a bar by running the old "baseline" algorithm. The baseline was originally designed as a standalone full-cluster algorithm; it computes one mean tangent across all extents in a sub-cluster, builds a perpendicular axis line through them, and moves each dot to its extent's first crossing of that axis. That design makes sense when every platform in the sub-cluster lies along the same track, but as a leftover-filler it produces visibly wrong dots — most clearly when a single leftover platform has a curved or looped extent next to a straight one. At Wabern Tram-Endstation the tram's loop extent gets placed at whichever loop point the perpendicular axis line happens to cross first, instead of the loop point closest to the rest of the cluster, producing a long connector across the loop.

The baseline algorithm also runs an "always compute Candidate A and Candidate B, keep the shorter pill geometry" comparison wrapped around it. The sweep has matured to the point where the comparison is no longer earning its keep; the goal now is to use the sweep result unconditionally and reduce the baseline to its true role of placing whatever the sweep didn't.

## Requirements

The leftover-filler replaces the current baseline. Its inputs are:

- the cluster's full platform list (each carries its extent polyline and the raw GTFS snap position),
- the set of dots already placed by Candidate A's bars in the current cluster ("bar dots").

Its job is to assign a final position to every platform that is not on a bar ("leftovers").

### Per-leftover rule

Each leftover's final position is **the point on its own extent polyline closest to the nearest already-placed dot in the cluster**. "Already-placed" includes bar dots and any leftovers placed earlier in the same pass.

### Bootstrap rule (no already-placed dot)

The first leftover to be placed in a cluster that has no bar dots has nothing to be "closest to". For that one platform — and only that one — the rule is: snap its extent to the point closest to the **GTFS centroid** of the cluster, defined as the mean of every platform's raw GTFS coordinate (before the placement pass). Subsequent leftovers in the same cluster fall back under the per-leftover rule using whatever has been placed so far.

### Placement order

Order matters because each leftover becomes an anchor for the ones placed after it. The leftover-filler tries every permutation of the leftover list, runs the placement under each ordering, and keeps the ordering that yields the **shortest total pill + 0.5 × connector geometry** for the cluster, using the same scoring function the rest of the pipeline uses to compare placements.

A safety cap applies: if the leftover count in a cluster exceeds a small ceiling (e.g. 8 — chosen so n! stays well under 10⁵ trials), fall back to a single deterministic ordering: leftovers sorted by descending line frequency, ties broken by ascending osm_id. The cap is a safety net, not the expected path; clusters with more than a handful of leftovers should be rare.

### Removal of the Candidate A / Candidate B comparison

`coordinate_dots_global_stab` currently computes the full baseline-on-all-platforms ("Candidate B") in parallel with Candidate A and is wired to keep whichever produces the shorter pill geometry. That comparison is removed. Candidate A's bar placements are applied unconditionally, and the leftover-filler defined above runs on everything Candidate A did not place.

## Constraints

- The leftover-filler must never move a bar dot; bar dots are an immutable input.
- A leftover with a degenerate extent (fewer than two distinct points) keeps its raw snap position regardless of which rule would otherwise apply — there is no extent to snap along. (This case is handled separately under the "stop at beginning of polyline" concept; it is not in scope here.)
- The scaled-coordinate (lon × cos_lat) round-trip currently performed by `coordinate_dots_global_stab` is retained; the leftover-filler operates inside the same scaled space as Candidate A.
- The diagnostic outputs (`_DIAG_BARS`, `_STABBED_PAIRS`) describe bar placements only and are not affected by the leftover-filler.
- The `debug.disable_snap_gate` config flag is unrelated to placement and stays as-is.
