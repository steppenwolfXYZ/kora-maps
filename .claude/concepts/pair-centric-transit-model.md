# Pair-Centric Transit Model

## Problem

The current pipeline treats each transit line as the primary rendering entity. Style decisions (color, width, saturation) are derived from per-line attributes (mode, frequency score, speed). This produces three structural mismatches between the data model and what the map should communicate:

1. **Corridor density is invisible.** A trunk segment carrying IC + RE + S1 is rendered as three overlapping lines. The actual high-density character of the corridor — the experience of waiting at a station served by many trains — is not expressed visually. Each line's frequency is computed and rendered in isolation.

2. **Express service on a shared physical line has no relationship to the local service it shares track with.** An IC that skips many local stops between Bern and Thun is a separate feature in the output, drawn over the S1 with no semantic link. There is no way to say "this segment is served at the IC's higher speed" because speed is a line attribute, not a segment attribute.

3. **Parallel routes with shared endpoints cannot be distinguished by stop alone.** Brig↔Visp via Lötschberg base tunnel vs via the mountain route, Chur↔Landquart via SBB vs via RhB with intermediate stops on its own track — these connect the same stop pair but follow different physical lines. The line-centric model handles them only because each line carries its own OSM match independently.

A secondary problem: overlapping line features make label placement effectively impossible. With multiple lines on one corridor, no single line can carry a readable inline label.

## Current workaround

Per-line `transit_lines.geojson` features, each with its own frequency, speed, color, and OSM-derived geometry. Lines are drawn in z-order by mode hierarchy. Overlapping segments stack visually with no aggregation. Click/hover and label placement are not implemented; the model can't support them cleanly.

## Requirements

The primary rendering entity is the **stop pair**, defined as the unordered pair of two stops connected by a trip without an intermediate stop on the same trip. The line-as-primary-entity model is replaced. Rendering walks pairs and emits chunks; per-line features are no longer the output unit.

### Base version

**Pair extraction:**
Pairs are extracted from trips after GTFS line grouping (see the `gtfs-line-grouping` concept). Each pair carries:
- Unordered stop pair using **merged stop identities** (parent_station + post-pipeline clustering), not raw stop_ids.
- Mode.
- Set of contributing lines, referenced by trip-group ID from the line-grouping pass.
- Per-time-bucket frequency contribution from each contributing line (existing time buckets: core_wd, eve_wd, we).
- Aggregated pair frequency: a separate score per segment, computed by recomputing frequency from total trip counts per time bucket on the pair, not by summing line-level freq scores.
- List of OSM relations covering this pair (potentially more than one).

**OSM matching at pair granularity:**
Each pair is matched against OSM relations whose geometry covers both endpoints. Tiebreaking prefers relations whose contributing lines (by trip-group identity from the line-grouping pass) match the pair's contributing lines. Geometric contiguity with other matched pairs of the same relation is a fallback rule. A pair may resolve to multiple OSM relations and is then rendered along each.

**Rendering by chunks:**
The visual line is no longer the unit of rendering. The pipeline walks the sequence of pairs along each OSM relation and emits **chunks** — runs of consecutive pairs sharing `(mode, frequency, speed, contributing-lines set)`. Each chunk becomes one polyline feature. Chunks break wherever any of those four attributes change.

**Click / hover / labels:**
Each chunk feature carries its `lines` attribute (the set of contributing trip-group IDs). Click and hover surface the line names from this attribute. Inline label rendering at high zoom levels is out of scope for this concept but is the natural fit for the pair model.

### Extended version (hierarchy)

Pairs form a hierarchy. A pair `(A, C)` whose trip-variant traverses an intermediate stop `B` on a different trip-variant of the same physical line is the **parent** of pairs `(A, B)` and `(B, C)`.

**Parent identification does not require shared OSM relation.** The parent–child relation is established at trip level: a parent pair exists when some nonstop trip in the network traverses the parent endpoints on the same physical line that the child pairs serve. This admits cases like Chur↔Landquart where the parent SBB pair and the RhB child pairs traverse different OSM relations.

**Speed determination uses hierarchy walk.** For any pair, walk up the chain of ancestor nonstop pairs that contain it. The speed used for rendering is the speed of the **topmost** containing nonstop variant. Example: Lenzburg↔Zürich (local) ⊂ Aarau↔Zürich (nonstop) ⊂ Olten↔Zürich (nonstop) ⊂ Bern↔Zürich (nonstop) — every sub-pair in the chain is rendered at the Bern↔Zürich speed.

The hierarchy is also the foundation for any later work on network-level visualization decisions (spine identification, interchange complexes, corridor-thickening rules).

### Mountain and ferry

Mountain and ferry lines participate in the pair model but require special handling. Mountain pairs may have no OSM relation backing them and fall through to straight-line rendering between GTFS stop coordinates, as today. Ferry pairs use pier-to-pier rendering without OSM line matching. The exact shape of their special handling is to be defined when implementing.

## Constraints

- The line-grouping concept (`gtfs-line-grouping`) is a prerequisite. Pair `lines` attributes reference trip-group IDs from that pass.
- Pair direction is normalized: `(A→B)` and `(B→A)` are the same pair.
- Stop identities for pair extraction are merged. The merging rule itself is part of the existing parent_station and post-pipeline clustering work, which is its own complex topic and is not redefined here.
- The chunk-merge key is `(mode, frequency, speed, contributing-lines set)`. All four must match for adjacent pairs to merge into one feature. The contributing-lines set is part of the key because a corridor where one line drops at the same frequency must still chunk-break for the `lines` attribute to remain correct.
- Mode color palette is unchanged. The palette is applied at chunk level rather than line level.
- Branching where two lines share a physical line up to a divergence point that has no station is rendered as overlapping pair chunks until the first station after divergence. This is preferred over geometric handling of stationless splits.
- Speed coloring depends on the extended hierarchy version. The base version alone cannot reproduce express speeds on shared track.
- Time-of-day model is unchanged from current: bucketed (core_wd, eve_wd, we). Date-aware rendering for construction or seasonal services is out of scope and may be revisited long after MVP.
- **Known unresolved challenge (not solved by this concept):** any two stops can in principle be "connected" through the network graph via arbitrary paths that are not part of any real line. The parent–child rule must distinguish legitimate parents (a nonstop trip on the same physical line as the children) from accidental graph connections. The precise rule needs further research at implementation time; this is a known general problem with known patterns.
- **Future refinement (not part of MVP):** pair-frequency aggregation should evolve from trip-count summation toward average-departure-interval. Two IC trains spaced 4 minutes apart inside a 30-minute window are not equivalent to 8 trains per hour. The long-term model uses average gap, not count, for the rendered frequency.
- This concept implies a substantial rendering overhaul. `transit_lines.geojson` as a per-line output is replaced by a per-chunk output. Style code that derives per-line visuals is replaced by chunk-level paint rules. The change is scoped to transit features and does not affect the rest of the basemap.
