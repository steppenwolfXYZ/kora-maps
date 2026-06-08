# Pill Rendering

## Problem

The current stop-pill rendering uses uniform pill shapes derived from spatial clustering of stop dots, without regard to actual platform geometry. Real platforms vary from 10 m bus bays to 500 m mainline train platforms, and at multi-line stations the lines run nearly parallel through the area. If each stop's dot is placed naively at the GTFS coordinate, dots sit at staggered points along each line and the pills connecting them become zig-zag rather than clean perpendicular bars.

The `prm-platform-positions` concept provides per-platform attributes: `length` for ~95% of rail platforms (with the GTFS coord at the platform centre) and reliable GTFS coordinates for tram/bus stops (with the coord at the front of the stop in the direction of travel). Combined with the per-line polylines pfaedle already produces, this is enough to give each platform an **allowed range** along its polyline, then place the platform's **dot** anywhere within that range — with the freedom to coordinate placement across a station so that the pills connecting the dots are short and visually clean.

## Multi-zoom-level stop styling

The eventual map will use three distinct stop-style systems, chosen by zoom range:

- **Far zoom** — a single circle per station. Conveys presence and mode, not platform geometry.
- **Medium zoom** — a precise dot-and-pill per platform, faithful to platform extent and clustered cleanly across multi-line stations.
- **Short zoom** — a detailed style emphasising platform-level structure. Vision not yet defined.

This concept covers only the **medium-zoom** layer. Far and short are placeholders; their concepts will be written when their designs are ready.

## Requirements

### Platform extent (the dot's allowed range)

Each platform has an **allowed range** along its line's polyline, of length L: the snapped GTFS coordinate (the GTFS coord projected onto the polyline) is always at the geometric centre of this range. Anchoring per mode:

- **Rail (train, metro)** — range is `[snapped − L/2, snapped + L/2]` along the polyline. When the polyline does not extend ±L/2 around the snapped coord (e.g. a terminating-track polyline ending at the buffer), the missing portion on the clipped side is filled by a straight-line extrapolation in the polyline's tangent direction at the snapped coord. The total range length stays L unless the polyline as a whole is shorter than L.
- **Tram / bus / regional_bus** — range is `[snapped − L, snapped]`: it starts at the snapped GTFS coord and extends backwards along the polyline (against the direction of travel). No extrapolation.

`length` comes from atlas for rail (about 95% coverage); the remaining 5% use a per-mode default. Tram/bus use the per-mode default unconditionally — atlas does not carry `length` for those modes.

Out-of-scope modes (ferry, mountain) render as today.

### Dot placement

For each platform a single dot is placed somewhere within its allowed range. Within a station cluster the placement runs in two stages: a perpendicular-sweep stage finds and places bar dots across coherent platform groups, then a leftover-fill stage places every platform the sweep did not touch. The cluster-length metric used by either stage when it has to break a tie is `sum(pill segments) + 0.5 × sum(connector segments)`.

**Equal-distance projection.** All algorithm internals — tangent computation, perpendicular construction, σ-line projection, dot intersection — are done after scaling the lon component of every coordinate by `cos(cluster_mean_lat)`, so a 2-D Cartesian perpendicular in that space corresponds to a real-geography perpendicular on the rendered Mercator map. Without this, diagonal tracks at Swiss latitudes display with up to ~20° of skew. The placed positions are unscaled back to true lon/lat before the function returns.

**Perpendicular sweep per tangent group.**

- Platforms whose extent tangents are within ~10° of each other (mod π, union-find with transitive closure so curved-but-coherent sets stay together) form a tangent group.
- Each tangent group is then split into **σ-clumps** along the group's mean tangent: every member's extent is projected onto that mean tangent, and members whose σ-intervals overlap (within a small ~5 m slack to absorb the mismatch between the group's mean tangent and each member's own tangent) form a clump via 1-D union-find. Each σ-clump of ≥ 2 members is swept independently — without this split, a tangent group spread across hundreds of metres of one street (typical at large stations where bays for multiple lines sit along a curb) produces only one bar near whichever clump contains the 2-D centroid, and the far-away clump is unreachable because the sweep is bounded to the central member's ~30 m extent. σ-clumps of size 1 fall through to the leftover-fill as a leftover.
- For each σ-clump of ≥ 2 platforms, a perpendicular bar is found by sweeping along the **central member's platform extent** (the same per-stop polyline drawn as the debug overlay) at a fixed resolution of **10 m** over the extent's full length, plus the projections of **every clump member's extent endpoints** onto the central extent (so the sweep also tests the sub-metre-precise positions where the stab count or geometry transitions, not just the 10 m grid). The sweep never walks the underlying full line polyline — using the extent keeps it anchored to the actual platform region and its length is bounded by the per-mode platform length (so the sweep is intrinsically fast regardless of how long the underlying line is). The central member is the platform whose snapped GTFS position is closest to the clump's spatial centroid, picked from the **inner 70 %** of the σ-clump only: the 30 % of members lying furthest from the clump's spatial centroid are excluded from the central-member pick so an off-to-the-side member cannot become central and drag the sweep away from the clump middle. Excluded members still count for stab scoring; they are only excluded from being chosen as central.
- At each sweep position the **local tangent** of the central extent is averaged over a 40 m window so small pfaedle-routing kinks do not tilt the bar. The bar at that position is the line perpendicular to that smoothed tangent passing through the position.
- A group member counts as **scoring-stabbed** by a sweep position's bar only if **both** (a) its extent crosses the bar and (b) its own extent tangent is within ~10° (mod π) of the bar's tangent at that position. Only scoring-stabbed members contribute to the stab count that picks the winning sweep position, and only their dots determine the bar's drawn length (the bar spans the perpendicular extent of the scoring-stabbed dots plus a small margin). Wrong-angle members do not contribute to scoring or to bar length.
- A sweep position is only kept as a candidate if its scoring-stabbed members cover at least **two distinct platform positions** (i.e. two different snapped GTFS coords). Multiple lines stopping at the same physical platform count as one — a bar that scores only by stacking N lines on a single coord is rejected, so a single-platform stop served by many lines does not produce a bar that spans only that one spot.
- All sweep positions tied at the maximum scoring-stab count survive into a per-cluster tie-breaking pass (described below). Each scoring-stabbed platform's dot is placed at the intersection of its extent with the bar; if the bar lies just past the polyline (e.g. the extrapolated portion of an asymmetric extent), the dot snaps to the extent endpoint closer to the bar.
- After the winning bar's length is fixed by the scoring-stabbed dots, any wrong-angle member whose extent crosses the bar **and** whose crossing point falls within the bar's drawn span (i.e. between scoring-stabbed dots) is also placed on the bar at that crossing point. Such members are treated as covered by the bar — they are not handed to the leftover fill — but they had no influence on whether the bar was chosen or how long it was drawn.
- **Recursive rerun on unmatched members.** After a σ-clump's bar is chosen and its scoring-stabbed + covered members placed, count how many σ-clump members remain unplaced. Members sharing one snapped GTFS position count once. If at least **two distinct-position** members remain, re-run the σ-clump split + perpendicular sweep on the remaining members — pick a fresh central member, recompute σ-intervals (the leftover members may now split into more than one sub-σ-clump), and apply each new bar the same way as the first. Repeat as long as the previous pass produced at least one bar and at least two distinct-position members are still unplaced. The loop terminates when the next sweep finds no candidate position, or fewer than two distinct-position members are left; anything still unmatched falls through to the leftover fill. This catches σ-clumps that contain two parallel sub-clusters on different transverse axes — both share enough σ-overlap to stay in one σ-clump, but no single bar can stab both. The first sweep finds one sub-cluster, the rerun finds the other.

**Tie-breaking among equally-stabbing sweep positions.** The sweep typically produces multiple positions tied at the max scoring-stab count (often a contiguous range of 10 m steps where the same set of members is stabbed). Choosing among them is what gives the cluster its final shape, so it is done deliberately per cluster:

- **Multi-group cluster (≥ 2 tangent groups):** enumerate combinations of one tied option per group. Pick the combination minimising the **sum of pairwise distances between groups' bar centers**. Bar center = centroid of an option's scoring-stabbed placed dots. This is a cheap proxy for the inter-group connector length; the leftover baseline runs only once afterwards with the chosen options applied. No per-combination baseline rerun is needed.
- **Single-group cluster with > 1 tied option:** enumerate the group's tied options. For each option, place the bar's scoring + covered dots, run the leftover-fill if any leftovers remain, and measure **pill + 0.5 × connector length** across the whole cluster. Pick the shortest. This runs regardless of leftover count — even a single leftover still participates in the cluster's NN-path / MST connector, so the connector's length depends on where the bar sits.
- **Any case the metric above leaves tied (including a single-group cluster with only 1 tied option):** break the tie by picking the option minimising the **sum of placed-dot-to-snapped-GTFS-coord distance** — keeping dots near where GTFS placed them when nothing else differentiates the options.

No hard cap on the number of tied options or combinations is currently applied. The cost is bounded by `n_tied × leftover_baseline_cost` for single-group clusters and `prod(n_tied_i) × O(1)` for multi-group clusters, both manageable in practice. If profiling later shows a problem on a real station, the smart escape hatch is to keep only positions that aren't surrounded by other positions with the same metric value, and downsample the resolution of flat stretches without dropping their middle.
- Platforms not placed on any bar (singleton tangent groups, or members whose extent did not cross any candidate bar in their group at a good angle) are handed to the leftover-fill described below.

**Leftover fill — placing platforms the sweep didn't touch.**

Each platform the sweep did not place on a bar is a leftover. Leftovers are placed against the set of already-placed dots in the cluster — at first only the bar dots, then progressively including earlier-placed leftovers within the same pass.

- **Per-leftover rule.** Each leftover's final position is the point on its own extent polyline closest to the nearest already-placed dot in the cluster.
- **Bootstrap rule.** When no dot has been placed yet (the cluster has no bars), the first leftover snaps to the point on its extent closest to the **GTFS centroid** of the cluster — the mean of every platform's raw GTFS coordinate, computed before any placement runs. Subsequent leftovers in the same cluster fall back to the per-leftover rule using whatever has been placed so far.
- **Placement order.** Order matters because each leftover becomes an anchor for the ones placed after it, but the early picks dominate: the first placement anchors the cluster, the second relative to it, and by the fourth or fifth the geometry is mostly fixed. The fill therefore enumerates only the **prefix** of the ordering — every distinct length-k pick of the first k leftovers — and completes the tail in a deterministic fallback order (descending line frequency, ties broken by ascending osm_id). The trial that yields the shortest pill + 0.5 × connector length wins.
- **Prefix depth.** k is the deepest such that the count of length-k prefixes (`n × (n−1) × … × (n−k+1)`) stays within the trial budget of **50** per fill call. Concretely: n ≤ 4 enumerates fully; n = 5–7 enumerates the first two picks; n ≥ 8 enumerates only the first pick. Trial count per fill is therefore bounded by 50 regardless of cluster size, with the cost spent on the choices that materially affect the layout.

A leftover with a degenerate extent (fewer than two distinct points along its polyline) keeps its raw snap position regardless of which rule would otherwise apply — there is no extent to snap along.

### Pills and connectors

Pill geometry is built from the placed dots by the existing greedy nearest-neighbour path through every dot in the cluster. Each NN-path segment is a candidate gap; whether it actually splits the path into separate pills depends on the gap's length and the shape of the surrounding NN-path. The generous straight-threshold `PILL_GAP_STRAIGHT_M` (50 m absolute) applies when **either** of these holds:

- From each gap-adjacent dot, the NN-path continues **dead straight** in line with the gap direction for at least the gap length, walking into the rest of the path away from the gap on each side (no angle tolerance — any bend at all stops the walk).
- **Perpendicular-platforms rule:** both gap-adjacent dots have at least one platform whose extent tangent is 90° ±2° from the gap direction. The gap lies along a bar's perpendicular axis, so the bar continues through the gap even when the surrounding NN-path is too sparse to prove it via the walk. When multiple platforms stack at the same dot, only **one** needs to satisfy the perpendicularity check.

If neither holds, the tighter `PILL_GAP_ANGLED_M` (12 m absolute) threshold applies — the gap is an angled or T-junction connector. The gap splits the NN path iff its length exceeds the chosen threshold. Both thresholds are absolute metres — they do **not** scale with `width_base` (which controls pill / disc width, not gap length).

Each post-split group of ≥ 2 dots is emitted as a pill; a singleton group is emitted as an `endpoint` Point feature whose dedicated style layer (drawn between connector-casing and connector-fill) renders it as a colored disc with a white stroke, so the connector's white casing is hidden under the disc rather than crossing the connecting stop's outline. Singletons also participate in MST connector selection. MST connectors (Kruskal's) join all groups — pill groups and singleton (endpoint) groups alike — at their nearest dot pair.

Before the NN-path runs, stop positions within **`DEDUP_TOL_M` (0.5 m)** of each other are collapsed to a single position. Without this collapse, two stops that the bar coordination snaps onto the same logical spot — but which the `cos_lat` scale / unscale in `coordinate_dots_global_stab` leaves at slightly different float values — survive as two unique positions and emit as a 2-point near-degenerate pill. MapLibre cannot render such a pill reliably: with both vertices effectively coincident, the line direction vector is zero and the round caps that should form the disc fail to draw. The tolerance is set small enough to leave legitimate 3–6 m short pills intact and large enough to catch the observed twin spreads (sub-µm float noise up to ~11 cm bar-coordination drift).

Pill rendering style (thickness, casing, mode-coloured stroke) is unchanged from today; only the dot positions change.

### Pill grouping (which dots a pill connects)

Which dots a pill connects is determined by the existing clustering — rail by `parent_station`, others spatially within a per-mode radius — and the nearest-neighbour path within each cluster. This concept changes only *where* each dot sits, not which dots cluster.

### Terminus dedup

At a terminus station, the same physical platform is visited by two distinct `(osm_id, stop_id)` entries: one line ends there (`stop_id` is the last entry of its trip — the **arrival** side) and another begins there (`stop_id` is the first entry of its trip — the **departure** side). Rendered naively this produces two dots and two platform extents on top of each other at every terminus, with the departure side's extent additionally always collapsed (the non-rail extent rule extends backward from the snapped coord, and the polyline starts at the snapped coord so there is nothing behind it).

Whenever the same `stop_id` appears as the first entry of one `osm_id` **and** as the last entry of a different `osm_id`, and the two snapped positions are within `TERMINUS_DEDUP_RADIUS_M` (10 m) of each other, the **departure-side entry is omitted from all rendered outputs**: production stop dots, pill clusters, debug platform extents and debug dots. The arrival side remains as the single visible dot and the single visible extent.

The popup data is built from a separate pass that ignores this filter, so clicking the surviving dot still lists both the arrival and the departure line in the badge list. The debug overlay's outline-ring marker therefore continues to identify which of the two listed lines produced the dot the user clicked.

Loop trips (a line whose first and last entries are the same `stop_id` on the same `osm_id`) are not deduped against themselves — the self-match is excluded. A loop is only deduped if a different line's arrival also lands at that stop_id within the radius.

### Per-mode defaults and sanity ranges

The following values live in `config.yaml` and are tuned via configuration only, not code changes:

| Mode | Default length | Sanity min | Sanity max |
|---|---:|---:|---:|
| train        | 100 m | 30 m | 700 m |
| metro        |  60 m | 30 m | 400 m |
| tram         |  35 m | 10 m | 100 m |
| bus          |  30 m |  5 m | 100 m |
| regional_bus |  30 m |  5 m | 100 m |

The default applies when atlas does not provide a length (always for tram / bus / regional_bus; rare for train / metro). The sanity range filters atlas values: anything below the min (e.g. 0 m placeholders) or above the max (e.g. kilometre-scale ferry-route mislabels) falls through to the default. The bus and regional_bus rows are identical: the vehicles are effectively the same, and shared platforms with stacked buses justify a roomy upper bound. The bus default of 30 m is set so a bus platform's allowed range reaches across the typical front-to-front offset between a bus stop and the trams or other buses sharing the same street, letting the dot coordination algorithm pull all three onto a common station axis.

### Fallback chain

For each platform's allowed range:

1. If atlas provides a sane `length` (within the per-mode sanity range), use it, anchored per the mode rule.
2. Otherwise use the per-mode default length with the same anchor rule.
3. For rail/metro: if the polyline does not symmetrically support ±L/2 around the snapped GTFS coord, the missing side is filled with a tangent-direction extrapolation so the total range stays L.
4. If the polyline as a whole is shorter than the required length, the range is clipped to whatever polyline exists.

### Debug overlay

Three debug elements render on top of the production style, filtered to the modes in scope (train, metro, tram, bus, regional_bus):

- A **thin black line** tracing each platform's full allowed range along its line's polyline — one per `(line, stop)` pair, including the extrapolated portions of any extent.
- A **clickable circle** at the snapped GTFS coordinate. Filled black if that `(line, stop)` was placed on a perpendicular bar by the sweep; hollow (white fill, black outline) otherwise. Clicking opens a popup with the stop name, mode, atlas platform length (or `– (default)` when atlas had none), and mode-coloured badges for each line stopping there. Hovering a badge shows the line's `origin → destination` as a tooltip. The badge for the specific `(line, direction)` whose polyline produced the clicked dot is outlined with a black-on-white ring, so that when multiple dots overlap the same stop (e.g. both directions of a terminus) the user can tell which one is selected.
- A **thick white line** drawn over each perpendicular bar produced by the sweep — spans the perpendicular extent of the bar's stabbed dots plus a small margin.

These are development-time visual aids only; not part of the medium-zoom production style. Gated by `transit.debug_overlay` in `scripts/config.yaml` so they can be flipped off without code changes — the overlay may survive past first install for in-the-wild diagnosis, but the whole thing (layers, debug sources, the `stabbed` property + case-expression fill, the `_STABBED_PAIRS` / `_DIAG_BARS` module-level state, the `tl_debug_*.pmtiles` build steps, and the `transit.debug_overlay` flag itself) is still to be removed before production.

## Open work

Items deferred from the current pass, in roughly the order they should be addressed:

- **Re-verification across the network after the duplicate-inclusion fix.** A bug in the spatial clustering caused stops to land in more than one cluster (a stop near a cell boundary was claimed by every group whose seed was within the radius). Roughly half of Zürich HB's "226 platforms" turned out to be duplicates; the same factor likely affected many other stations. Bern, the small clean-track stations, multi-deck stations, and the Stadtbahn-style mixed-orientation case all need a fresh visual pass — cluster sizes have changed materially.
- **Tangent tolerance is 10°** (the gate threshold and the tangent-group union-find both use this). It's deliberately strict; depending on what the re-verification shows, may need to be loosened to ~15°.
- **`PILL_GAP_STRAIGHT_M` / `PILL_GAP_ANGLED_M`** (currently 50 m / 12 m) are the dead-straight vs. angled gap-split thresholds. Tuned by hand; worth revisiting after the re-verification pass.

## Constraints

- Far-zoom and short-zoom stop styles are out of scope.
- `compass_direction` from atlas is intentionally not consumed. The polyline tangent at the dot position is the orientation source for pill geometry.
- Per-mode default lengths and atlas-`length` sanity ranges are configuration values.
- Ferries and mountain modes are out of scope; they render as today.
- The bar-finding sweep runs only on σ-clumps with at least two distinct-position platforms, and reruns recursively on a σ-clump's still-unmatched members under the same condition. Singletons, singleton tangent groups, singleton σ-clumps within a multi-clump tangent group, and members left over after the recursion terminates all flow through the leftover-fill alongside the bars.
- This concept depends on `prm-platform-positions` being implemented and `stop_attributes_sources.json` being emitted.
- The implementation must not regress the rendering of stops without atlas data — the fallback chain guarantees an allowed range is always producible.
