# Four-Loop Stop Assignment Architecture

## Problem

The current stop assignment pass processes each OSM route through a single inline cascade: string matching → tricks → geo-fallback, all for one route at a time before moving to the next. This produces three related problems:

1. **No global ordering guarantee.** A route processed early in the list can claim a GTFS line via geo-fallback that a route processed later would have claimed via simple string matching. Processing order determines results in ways that are invisible and unpredictable.

2. **Tricks are not uniformly sanity-checked.** The alpha-prefix trick (e.g. `"RE4"` → `"RE"`) currently sets `matched_gtfs_ref` to a generic key, which then enters the canonical lookup path where the sanity check fires only conditionally (Trigger 1). There is no guarantee that every trick-based match passes a sanity check.

3. **The geo-fallback pool is never pruned.** When geo-fallback runs for a route, it searches all of `_line_canonical_export` with no knowledge of what string matching has already settled. Lines that were confidently string-matched by other routes can still "attract" geo-fallback candidates for unrelated routes.

## Requirements

Stop assignment runs as **four sequential batch loops**. Each loop iterates over all OSM routes not yet settled by a prior loop. A route is "settled" when it receives a match that passes its required checks. Once settled, an OSM route is assigned to exactly one GTFS line and does not participate in subsequent loops. A route that gets a candidate match but fails its checks is returned to the unmatched pool for the next loop — no partial result is written.

Note: a single GTFS line may be matched by multiple OSM routes (different directions, short-turns). Settling is one-directional — it constrains the OSM route, not the GTFS line.

### Bbox filtering (all loops)

Stop collection uses sub-bboxes: the OSM polyline is split into 20 km segments, each with its own bounding box (~2 km margin). A GTFS stop must fall inside at least one sub-bbox to be included. This prevents long-distance routes from absorbing stops of unrelated nearby lines.

### Terminal station gate and scoring (all loops)

**Terminal coverage** is computed as the number of OSM terminal stations (0, 1, or 2) that have a GTFS stop within a given threshold. OSM terminals are determined from `osm_stop_nodes[0]`/`[-1]` when available, falling back to all segment endpoints for MultiLineString geometries.

Two thresholds are used:
- **5 km** — gate threshold: routes failing this are returned to the unmatched pool.
- **0.5 km** — scoring threshold: used to rank candidates within a loop (tighter, not a gate).

**Scoring** is uniform across all loops: candidates are ranked by `(-bbox_score, -ep_count_0.5km, -n_stops)` where `bbox_score = n_stops_in_bbox / n_total_stops`. Loops 1–2 apply no cap (candidate pool is small by construction). Loops 3–4 cap at 50 candidates before running sanity checks.

### Loop 1 — Simple string matching

Matching strategy: compare OSM `ref` against GTFS `long_name` (normalized: spaces stripped, case-insensitive) first, then `short_name` (same normalization). No tricks. A match on a known generic prefix term (see Constraints) is treated as no match and the route passes to Loop 3.

Terminal gate: ep_count at 5 km must be ≥ 1. ep_count 0 → returned to pool.

Sanity check: skipped when ep_count at 5 km is 2. Otherwise runs unconditionally.

Routes that match, pass gate, and pass sanity: settled.
All others: returned to unmatched pool.

### Loop 2 — Advanced string matching (tricks)

Runs only over the unmatched pool from Loop 1.

Matching strategy: string tricks, excluding results that resolve to a known generic prefix term (those are deferred to Loop 3):
- RE↔R conversion (MGB trains tagged RE in OSM, R in GTFS long_name)
- Name-prefix extraction: OSM `name` split on `":"`. For each segment try (a) the normalized full segment (e.g. `"R311"` from `"R 311"`), then (b) each individual token (e.g. `"R"`, `"311"`). Generic-prefix tokens are skipped.
- Alpha-prefix stripping (e.g. `"RE4"` → `"RE"`) — result checked against generic prefix list; if generic, deferred to Loop 3.

Terminal gate: ep_count at 5 km must be ≥ 1. ep_count 0 → returned to pool.

Sanity check: skipped only when ep_count at 0.5 km is 2 (both terminals covered tightly). Otherwise runs unconditionally — no other exceptions.

Routes that match, pass gate, and pass sanity: settled.
Remaining: unmatched pool for Loop 3.

### Loop 3 — Generic string matching

Runs only over the unmatched pool from Loop 2.

Matching strategy: matches that resolve to a key on the known generic prefix list (see Constraints). Candidates are all GTFS lines indexed under the matched generic key. Ranked and capped at 50 before sanity check.

Terminal gate: ep_count at 5 km must be ≥ 1. ep_count 0 → returned to pool.

Sanity check: runs unconditionally for every candidate, no exceptions.

Routes that match, pass gate, and pass sanity: settled.
Remaining: unmatched pool for Loop 4.

### Loop 4 — Geo-fallback

Runs only over the unmatched pool from Loop 3.

Matching strategy: scores all GTFS candidates in the bucket by bbox overlap. Ranked and capped at 50 before sanity check.

**Coarse geographic pre-filter (Loop 4 only):** Before iterating individual stops for a candidate, take the candidate's first available stop coordinate and check if it falls within the OSM route's overall bounding box expanded by a large margin (~100 km). If it does not, skip the candidate without iterating its stops. This avoids O(n_stops) work for geographically distant lines. The existing sub-bbox scoring filter remains the authoritative geographic criterion; the coarse pre-filter is a cheap early exit only.

Terminal gate: ep_count at 5 km must be ≥ 1. ep_count 0 → excluded (not drawn).

Sanity check: runs unconditionally for every candidate.

Routes that find no passing candidate: excluded (not drawn).

### Shared helpers — `_try_assign` drawable-first priority

`_try_assign` makes two passes through the capped candidate pool:

- **Pass 1:** iterate candidates where `no_draw` is `None` only. Return the first that passes the terminal gate and sanity check.
- **Pass 2 (fallback):** if pass 1 found nothing, iterate only the `no_draw` candidates. Return the first that passes.

This ensures that a drawable GTFS match at any rank beats a `no_draw` match of equal or higher score. Without this, an OSM route on a shared corridor (e.g. a regular IR train and a seasonal tourist IR sharing the same stops) can settle on the low-frequency seasonal variant in Loop 3, be excluded as `no_draw`, and never attempt the drawable regular service.

The fallback pass preserves the diagnostic value: routes with no drawable candidate still record which low-frequency GTFS line they matched, rather than appearing as `matched_line_key: null`.

## Constraints

- `_group_reassign_stops` runs after all four loops complete. It addresses a different problem (stop bleeding between branches of the same GTFS line) and is fully independent of this architecture. It remains necessary after this overhaul.

- Mountain lines (processed GTFS-first in a separate loop) and ferry lines (with their own pier-stop geo collection) are not part of the four-loop architecture. They retain their current special handling.

- The main OSM loop that determines freq/speed and outputs `transit_lines.geojson` is a prerequisite to stop assignment and is not restructured by this change. The ordering fix (long_name before short_name) applies to both the main loop cascade and the stop assignment cascade identically.

- The four loops must share a single parameterized implementation function (loop level 1 / 2 / 3 / 4 as parameter), so the candidate selection and sanity check logic cannot diverge between loops over time. The level parameter controls which matching strategies are attempted and whether the sanity check is conditional or unconditional.

- A GTFS line is not exclusively claimed by an OSM route. Multiple OSM routes can match the same GTFS line. The ordering benefit is that string-matched routes are settled before geo-fallback runs, so the geo-fallback pool reflects reality more accurately — but GTFS lines are not removed from the pool after being matched.

- **Known generic prefix list** — terms that identify a category of lines rather than a specific one, and therefore require Loop 3 handling with unconditional sanity check. Single-letter refs used for specific tram or bus lines (`"A"`, `"B"`, `"Z"`, etc.) are not on this list.

  `S`, `R`, `RE`, `IR`, `IC`, `EC`, `ICE`, `TGV`, `RB`, `N`, `SN`, `NJ`, `RJX`, `TER`, `EV`, `EXT`, `PE`

  This list should be extended when new generic prefixes are identified in practice.
