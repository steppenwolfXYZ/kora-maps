# GTFS Line Grouping

## Problem

`_line_canonical_export` currently partitions GTFS trips with the same `line_key` into a coarse 0.5° geo-bucket grid. The cell of each trip is determined by its origin terminus (first stop with known coordinates). This produces three structural failures:

1. **Origin-cell assignment is arbitrary.** Each direction of a round-trip line lands in a different cell because the directions start at different termini. The grid is also brittle at cell boundaries.

2. **The grid is a proxy for regional network identity, but the property it approximates is not actually in GTFS.** SBB operates S-numbered S-Bahn lines in multiple networks (Zürich, Basel, Luzern) under one agency_id with identical `short_name` and `long_name` (`"S 3"`, `"S 6"`, …). Three regional S3s sharing `(short_name="S3", long_name="S 3", bucket="train", agency_id="000011")` are kept apart today only because their origin termini happen to fall in geographically separated 0.5° cells. There is no GTFS field — neither in `routes.txt` nor in `trips.txt` (`route_desc`, `route_color`, `attributes_ch`, headsign) — that cleanly identifies the regional network. The route_id 6-digit prefix is partially discriminating but not reliable.

3. **Downstream code assumes `_line_key_full = (short_name, long_name, bucket, agency_id)` is unique per physical line.** Dedup groups OSM relations by `_line_key_full` and removes fallback-matched entries when any direct-ref-matched entry exists in the group. Group reassignment pools all `_line_canonical_export` entries with the matching `line_key` and redistributes stops. Both fail silently when one `_line_key_full` covers multiple physical lines — a direct match for one regional S3 can wipe a fallback match for another, and the pool for stop reassignment becomes nationwide.

## Current workaround

The 0.5° geo-bucket grid acts as the network proxy. The `_group_reassign_stops` pass cleans up stop bleeding within a `_line_key_full` group at output time. Both of these are tolerable today only because Swiss regional S-Bahn networks are geographically well-separated. Closer-packed networks would fail silently.

## Requirements

A GTFS line grouping pass runs at `_line_canonical_export` build time, before any OSM matching. It replaces the geo-bucket partition with a partition derived from trip structure.

### Partition

Trips are partitioned by `(long_name_norm, agency_id, bucket)` where `long_name_norm` is `long_name` with spaces stripped, lowercased. When `long_name` is empty, fall back to `short_name`. The S18/Tram 18 ref/bucket remapping (Forchbahn) must continue to apply before partitioning, so the remapped bucket is used.

### Trip-graph connectivity merge

Within each partition, two trips are connected iff they share at least **2 stops** (using merged stop identities — `parent_station` and existing post-pipeline clustering). Connected components are computed across the partition. Each connected component is one **trip group** representing one physical line.

This produces:
- Regional S3 networks (Zürich, Basel, Luzern) become three separate trip groups because they share zero stops.
- Short-turns and full-route variants merge into one group because they share trunk stops.
- Y-shaped lines merge into one group via the shared trunk.
- Express and local variants of the same line group merge if they share enough stops.

### New identifier

A `trip_group_id` is assigned to each connected component, unique within its partition. `_line_canonical_export` entries are keyed by `(line_key, trip_group_id)`. `_line_key_full` is extended to `(short_name, long_name, bucket, agency_id, trip_group_id)` and becomes unique per physical line by construction.

### Downstream simplifications

Dedup and group reassignment continue to group by `_line_key_full`. Because that key is now unique per physical line, no further changes to these passes are required — the bugs they have today (cross-network dedup, nationwide stop pool) disappear.

### Pre-implementation research

Before implementing, an empirical scan of the GTFS feed must validate:
- The actual distribution of trip-group sizes per partition (catch under-merging and over-merging).
- The number of partitions where the empty-`long_name` fallback to `short_name` applies and whether those collide with other partitions in ways that need a stricter rule.
- Whether the threshold of 2 shared stops correctly groups known cases (S3 networks separate, short-turns merge, Y-shapes merge) and whether any real line type requires a different threshold.

The threshold of 2 is the starting value. If research shows degenerate cases, the rule may become proportional (`share ≥2 stops OR ≥X% of the shorter trip's stops`) or filter out global anchor stops (e.g. Zürich HB) from the shared-stop count.

## Constraints

- The 10%/5% rare-variant filter (garage runs and one-off specials) is unchanged. It applies within each trip group, after partitioning and connectivity merge.
- Direction-aware emission is unchanged. When a trip group contains multiple maximal variants (genuinely divergent directions, not subsets), they continue to be emitted as separate `dir_aware=True` canonical entries.
- Mountain bucket exemptions (no `no_draw` flag, no rare-variant filter) are unchanged.
- The S18/Tram 18 (Forchbahn) ref and bucket remapping continues to apply before partitioning.
- This concept does not address corridor-level frequency aggregation (the "peak reinforcement" case where IC + RE + S1 should accumulate). That is the domain of the pair-centric transit model concept. Concept 1 alone fixes disambiguation and short-turn merging; it does not change the line-as-primary-entity rendering contract.
- This concept does not address express-on-local visualization. Same reason — that is a property of the pair model.
- The `gtfs_unmatched.json` accounting becomes per-trip-group rather than per-`line_key`. Unmatched groups are surfaced individually.
