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
- **Tram / bus / regional_bus** — range is `[snapped − L, snapped]`: it starts at the snapped GTFS coord and extends backwards along the polyline (against the direction of travel). When the polyline does not extend L metres backward from the snapped coord (e.g. the stop is the first entry of the trip, so the polyline begins at the snapped coord), the missing backward portion is filled per the **missing-range fill** rule below.

**Missing-range fill (tram / bus / regional_bus only).** When the backward portion of a tram/bus/regional_bus extent is shorter than L, the missing arc-length is filled by the first of the following that succeeds. The total range length stays L unless every option fails. Rail (train, metro) is **not** subject to this rule and keeps its straight-line extrapolation behaviour unchanged.

1. **Borrow from a sibling variant.** A sibling is any other emitted line feature in the same `(line_key, agency_id)` group. At the snapped GTFS coord `p` with tangent `T`, scan siblings for any whose polyline passes within ~2 m of `p` and whose own tangent at the nearest point is within ~15° of `T` (mod π). If one qualifies, walk that sibling's polyline backward from the nearest point by the missing arc-length and use that segment as the fill. The first qualifying sibling wins — no ranking across siblings.
2. **Straight-line tangent extrapolation.** If no sibling qualifies, fall back to a straight line of the missing length in the `−T` direction at `p`.
3. **No fill.** If the polyline is too short to even compute a usable tangent, the extent is left as whatever on-polyline geometry exists (possibly collapsed).

The two-metre proximity gate rejects siblings that physically run on a different alignment at `p` (typical of one-way-pair buses with separate platforms on parallel streets — those siblings sit 20+ m away and are correctly excluded). The 15° tangent gate rejects siblings that share `p` but diverge in direction (typical of tram turning loops at termini, where two directions share a platform but curve apart within 20 m). Circular lines are their own siblings — the same feature is allowed to match itself, with the scan starting from the polyline's other end.

`length` comes from atlas for rail (about 95% coverage); the remaining 5% use a per-mode default. Tram/bus use the per-mode default unconditionally — atlas does not carry `length` for those modes.

Out-of-scope modes (ferry, mountain) render as today.

### Dot placement

For each platform a single dot is placed somewhere within its allowed range. Within a station cluster the placement runs in two stages: a perpendicular-sweep stage finds and places bar dots across coherent platform groups, then a leftover-fill stage places every platform the sweep did not touch. The cluster-length metric used by either stage when it has to break a tie is `sum(pill segments) + 0.5 × sum(connector segments)`, **except** that any pill or connector segment whose both endpoints sit within `ON_PLATFORM_TOL_M` (0.5 m) of the **same** platform extent polyline has its base factor scaled by `ON_PLATFORM_PENALTY` (2.0): an on-platform pill counts at 2.0× its length and an on-platform connector at 1.0× its length. This **platform-overlap penalty** discourages routing pills or connectors along a platform extent — sometimes unavoidable, but when an alternative placement reaches the same dots without overlap, the alternative wins. The "same platform" wording matters: a pill within a perpendicular bar typically connects two dots on **different** platforms (each on its own extent), so the bar's pills are not penalised; the case the penalty bites is two dots that share one platform's extent (e.g. two leftover stops at one physical platform that the leftover-fill placed at opposite ends of their parallel extents).

**Equal-distance projection.** All algorithm internals — tangent computation, perpendicular construction, σ-line projection, dot intersection — are done after scaling the lon component of every coordinate by `cos(cluster_mean_lat)`, so a 2-D Cartesian perpendicular in that space corresponds to a real-geography perpendicular on the rendered Mercator map. Without this, diagonal tracks at Swiss latitudes display with up to ~20° of skew. The placed positions are unscaled back to true lon/lat before the function returns.

**Perpendicular sweep per tangent group.**

- Each platform's tangent is the local polyline tangent of its extent at the dot position — the snap point projected onto the extent — averaged over a `TANGENT_WINDOW_M` (10 m) window. For tram/bus/regional_bus the snap sits at the extent's forward end, so the window can only extend backward; for rail it sits at the extent's middle and extends symmetrically. The window is sized to stay inside the per-mode extent length so the averaged direction reflects what's happening at the dot, not the chord of the whole extent — the chord can land degrees off the local direction at any bend within the extent and split platforms that ride the same OSM way into separate tangent groups. Platforms whose tangents are within ~12° of each other (mod π, union-find with transitive closure so curved-but-coherent sets stay together) form a tangent group. The tolerance is set above 10° so that one-way pairs whose stops sit on the same OSM way but on opposite sides of an OSM bend (Eigerplatz SE corner, ~11° between C and D) still group together.
- Each tangent group is then split into **σ-clumps** along the group's mean tangent: every member's extent is projected onto that mean tangent, and members whose σ-intervals overlap (within a small ~5 m slack to absorb the mismatch between the group's mean tangent and each member's own tangent) form a clump via 1-D union-find. Each σ-clump of ≥ 2 members is swept independently — without this split, a tangent group spread across hundreds of metres of one street (typical at large stations where bays for multiple lines sit along a curb) produces only one bar near whichever clump contains the 2-D centroid, and the far-away clump is unreachable because the sweep is bounded to the central member's ~30 m extent. σ-clumps of size 1 fall through to the leftover-fill as a leftover.
- For each σ-clump of ≥ 2 platforms, a perpendicular bar is found by sweeping along the **central member's platform extent** (the same per-stop polyline drawn as the debug overlay) at a fixed resolution of **10 m** over the extent's full length, plus the projections of **every clump member's extent endpoints** onto the central extent (so the sweep also tests the sub-metre-precise positions where the stab count or geometry transitions, not just the 10 m grid). The sweep never walks the underlying full line polyline — using the extent keeps it anchored to the actual platform region and its length is bounded by the per-mode platform length (so the sweep is intrinsically fast regardless of how long the underlying line is). The central member is the platform whose snapped GTFS position is closest to the clump's spatial centroid, picked from the **inner 70 %** of the σ-clump only: the 30 % of members lying furthest from the clump's spatial centroid are excluded from the central-member pick so an off-to-the-side member cannot become central and drag the sweep away from the clump middle. Excluded members still count for stab scoring; they are only excluded from being chosen as central.
- At each sweep position the bar's tangent is the **circular median (mod π) of every σ-clump member's local tangent at the closest point on its own extent to the sweep position, averaged over the same `TANGENT_WINDOW_M` (10 m) window used for tangent grouping**. Curvature-aware (each member contributes its local direction at the bar's location, not its extent's start→end chord — important for long curved approaches like the western pill at Bern HB, where a chord-based angle would skew the bar by several degrees from the local track direction) and robust to a single outlier whose pfaedle shape is rotated relative to the rest of the σ-clump (the canonical case is Zürich HB, where one of many parallel tracks is pfaedle-shaped a couple of degrees off and would otherwise tilt every bar drawn through the σ-clump if its platform happened to be picked as central). The sweep still walks the central member's extent for sweep positions; only the orientation is decoupled from a single representative member.
- A group member counts as **scoring-stabbed** by a sweep position's bar only if **both** (a) its extent crosses the bar — within a `SIGMA_BOUNDARY_TOL_M` (0.5 m) tolerance on the σ-projection check, so a member whose extent boundary coincides with the sweep position stays in scoring instead of dropping by float-precision noise — and (b) its own local tangent is within ~12° (mod π) of the bar's tangent at that position. Only scoring members anchor the bar's drawn span (the bar spans the perpendicular extent of the scoring-stabbed dots plus a small margin) — the span is never extended to reach a wrong-angle member.
- **Lone-outlier drop on the scoring set.** Before the bar's drawn span is fixed, the scoring members' bar-intersection points are projected onto the bar axis and collapsed within `DEDUP_TOL_M` (0.5 m). If any adjacent gap in the resulting 1-D sequence is ≥ the cluster's **lone-outlier gap threshold** — `LONE_OUTLIER_GAP_RAIL_METRO_M` (50 m) for rail and metro clusters, `LONE_OUTLIER_GAP_BUS_TRAM_M` (20 m) for tram / bus / regional_bus — **and** exactly one side of that gap contains a single distinct-position dot, that dot's members are dropped from the candidate's scoring set. Bus and tram platforms are physically much shorter than rail platforms, so a 20 m off-axis member is already clearly a separate bay rather than the far end of one long platform group. The check repeats — dropping a dot can expose a new wide gap on the now-shorter sequence. If both sides of a wide gap have a single distinct-position dot, both are dropped (the candidate then typically fails the two-distinct-positions gate below). The post-drop scoring set is what anchors the drawn span and what feeds the stab total and ranking. Dropped members re-enter the σ-clump's unplaced pool and flow through the existing recursive rerun → leftover-fill path, which is where an isolated platform belongs. Per-cluster mode classification: non-rail clusters use the dominant mode by `MODE_RANK` — any cluster containing a metro stop is treated as metro and keeps 50 m.
- After the bar's drawn span is fixed by the (post-drop) scoring dots, any wrong-angle member whose extent crosses the bar **and** whose crossing point falls within the drawn span is added to the bar's **covered** set and placed on it. Covered members count alongside scoring toward the **stab total** (`len(scoring) + len(covered)`) that picks the winning sweep position. A wrong-angle stop that happens to fall on the bar is a real placement, so it should help that bar win against alternative positions that miss it — but it cannot drag the span out to include itself, so the bar still represents the aligned platforms it was built for.
- A sweep position is only kept as a candidate if its (post-drop) **scoring** members cover at least **two distinct platform positions** (two different snapped GTFS coords). Multiple lines stopping at one physical platform count as one anchor; covered members never count toward the distinct-position requirement because the drawn span only spans scoring anchors.
- A near-zero-span bar — where every scoring member's bar-intersection collapses to within float noise of one point — is kept, not dropped. It correctly converges a sub-cluster of platforms that ride the same OSM way through the bar position (Eigerplatz: opposite-direction buses sharing one street). The earlier "degenerate-bar drop" handed such cases to leftover-fill, which placed each platform's dot independently and produced spurious sub-metre pills.
- All sweep positions tied at the maximum stab total survive into a per-cluster tie-breaking pass (described below). Each scoring member's dot is placed at the intersection of its extent with the bar; if the bar lies just past the polyline (e.g. the extrapolated portion of an asymmetric extent), the dot snaps to the extent endpoint closer to the bar. Each covered member's dot is placed at the bar-extent crossing point inside the drawn span.
- **Recursive rerun on unmatched members.** After a σ-clump's local-pick option (min `gtfs_dist` among the tied options) has its scoring + covered members peeled, count how many σ-clump members remain unplaced. Members sharing one snapped GTFS position count once. If at least **two distinct-position** members remain, re-run the σ-clump split + perpendicular sweep on the remaining members — pick a fresh central member, recompute σ-intervals (the leftover members may now split into more than one sub-σ-clump), and yield each new bar the same way as the first. Repeat as long as the previous pass yielded a bar and at least two distinct-position members are still unplaced. The loop terminates when the next sweep finds no candidate position, or fewer than two distinct-position members are left; anything still unmatched falls through to the leftover fill. The peel-off uses the local pick rather than the eventual cluster-tie-break choice; any leak from this mismatch (parent's chosen option covers a member the local pick missed, so the recursion saw it and built a bar around it) is rejected by the multi-clump tie-break's no-double-cover guard described below.

**Tie-breaking among equally-stabbing sweep positions.** The sweep typically produces multiple positions tied at the max stab total (often a contiguous range of 10 m steps where the same set of scoring + covered members is stabbed). Choosing among them is what gives the cluster its final shape, so it is done deliberately per cluster:

- **Multi-clump cluster (≥ 2 yielded clumps — whether sibling top-level σ-clumps, recursion children, or a mix of tangent groups):** enumerate combinations of one tied option per yielded clump. Reject a combination if either:
  - Two of its bars are in the **same tangent group** and their bar centers project within the protection radius along the older bar's tangent (**30 m for rail, 10 m for everything else**). Different tangent groups point in different directions and impose no along-tangent constraint on each other. Transverse offset (along the bar's drawn axis) is unconstrained — that's the legitimate parallel-sub-cluster case the rerun exists for.
  - Any member appears in more than one chosen bar's scoring + covered set. The drawn bars are supposed to cover **disjoint** stops; a stop assigned to two bars is wasted coverage.
  Score the surviving combinations by **sum of pairwise bar-center distances** (cheap proxy for inter-bar connector length), tie-break by total `gtfs_dist`. If no combination passes both validity checks, fall back to each entry's min-`gtfs_dist` option independently — the structural guarantees are gone in that fallback, but the algorithm still produces a deterministic result. Bar center = centroid of an option's scoring-stabbed placed dots.
- **Single-group cluster with > 1 tied option AND ≥ 1 leftover:** enumerate the group's tied options. For each option, place the bar's scoring + covered dots, run the leftover-fill, and measure **pill + 0.5 × connector length** across the whole cluster. Pick the shortest. Even a single leftover still participates in the cluster's NN-path / MST connector, so the connector's length depends on where the bar sits.
- **Single-group cluster with > 1 tied option AND no leftovers:** the length metric is degenerate here — every tied option produces the same pill (the bar itself), so its measured length varies only with float noise along the sweep and carries no real signal. Skip the metric entirely and fall through to the gtfs_dist tiebreak.
- **Any case the metric above leaves tied (including a single-group cluster with only 1 tied option, or one with no leftovers):** break the tie by picking the option minimising the **sum of placed-dot-to-snapped-GTFS-coord distance** — keeping dots near where GTFS placed them when nothing else differentiates the options.

No hard cap on the number of tied options or combinations is currently applied. The cost is bounded by `n_tied × leftover_baseline_cost` for single-group clusters and `prod(n_tied_i) × O(1)` for multi-group clusters, both manageable in practice. If profiling later shows a problem on a real station, the smart escape hatch is to keep only positions that aren't surrounded by other positions with the same metric value, and downsample the resolution of flat stretches without dropping their middle.
- Platforms not placed on any bar (singleton tangent groups, or members whose extent did not cross any candidate bar in their group at a good angle) are handed to the leftover-fill described below.

**Leftover fill — placing platforms the sweep didn't touch.**

Each platform the sweep did not place on a bar is a leftover. Leftovers are placed against the set of already-placed dots in the cluster — at first only the bar dots, then progressively including earlier-placed leftovers within the same pass.

- **Per-leftover rule.** Each leftover's final position is the point on its own extent polyline closest to the nearest already-placed dot in the cluster.
- **Bootstrap rule.** When no dot has been placed yet (the cluster has no bars), the first leftover snaps to the point on its extent closest to the **GTFS centroid** of the cluster — the mean of every platform's raw GTFS coordinate, computed before any placement runs. Subsequent leftovers in the same cluster fall back to the per-leftover rule using whatever has been placed so far.
- **Placement order.** Order matters because each leftover becomes an anchor for the ones placed after it, but the early picks dominate: the first placement anchors the cluster, the second relative to it, and by the fourth or fifth the geometry is mostly fixed. The fill therefore enumerates only the **prefix** of the ordering — every distinct length-k pick of the first k leftovers — and completes the tail in a deterministic fallback order (descending line frequency, ties broken by ascending osm_id). The trial that yields the shortest pill + 0.5 × connector length wins.
- **Prefix depth.** k is the deepest such that the count of length-k prefixes (`n × (n−1) × … × (n−k+1)`) stays within the trial budget of **50** per fill call. Concretely: n ≤ 4 enumerates fully; n = 5–7 enumerates the first two picks; n ≥ 8 enumerates only the first pick. Trial count per fill is therefore bounded by 50 regardless of cluster size, with the cost spent on the choices that materially affect the layout.

A leftover with a degenerate extent (fewer than two distinct points along its polyline) keeps its raw snap position regardless of which rule would otherwise apply — there is no extent to snap along.

**Parallel-stub drop (rail clusters only).** Some long shared rail platforms host a small subset of trips that depart from only part of the platform, encoded as a separate `stop_id`. Naively, that stop's leftover-placed dot lands further down the same line, connected to the main pill by a connector running parallel to the track — a visual stub that does not correspond to a separate platform.

After `_leftover_fill` returns on a rail (train) cluster, every leftover is checked. Let `p` be a leftover and `q` its nearest **non-coincident** other cluster member (within-`DEDUP_TOL_M` neighbors are skipped — they represent the same physical dot collapsed by `_dedup_stop_positions` later, and offer no useful gap). The leftover is **dropped from rendering** when both:

- the gap `|p → q|` is < `PARALLEL_STUB_GAP_M` (100 m), and
- the gap direction is within `PARALLEL_STUB_TOL_DEG` (15°, mod π) of either `p`'s or `q`'s extent tangent. `p`'s tangent is preferred; the fallback to `q`'s tangent handles **degenerate-extent leftovers** — sub-platform `stop_id`s sometimes land with no shapeable extent at all, so their own tangent is `None` but the neighbor's tangent (along the line direction) is still usable.

The dropped stop's `lon`/`lat` is rewritten to `q`'s position so `_dedup_stop_positions` collapses it onto that dot. The dropped stop remains in `cluster`, so its line still surfaces in `lines_json` (popup) at the absorbing dot. After a drop, `_leftover_fill` is re-run on the remaining leftovers and the check repeats; this loop converges in at most one iteration per leftover. Non-rail clusters skip the check entirely.

The 100 m gap covers typical sub-platform offsets within one long platform. The parallel-tangent check is what prevents false positives at multi-track stations: dots on adjacent parallel platforms separate **perpendicular** to the line direction (across tracks), so their gap vector is perpendicular to the extent tangent and fails the parallel test even when the dots sit < 100 m apart.

### Pills and connectors

Pill geometry is built from the placed dots by the existing greedy nearest-neighbour path through every dot in the cluster. Each NN-path segment is a candidate gap; whether it actually splits the path into separate pills depends on the gap's length and the shape of the surrounding NN-path. The generous straight-threshold `PILL_GAP_STRAIGHT_M` (50 m absolute) applies when **either** of these holds:

- From each gap-adjacent dot, the NN-path continues **dead straight** in line with the gap direction for at least the gap length, walking into the rest of the path away from the gap on each side (no angle tolerance — any bend at all stops the walk).
- **Perpendicular-platforms rule:** both gap-adjacent dots have at least one platform whose extent tangent is 90° ± `perp_platform_tol_deg` (config, currently 5°) from the gap direction. The gap lies along a bar's perpendicular axis, so the bar continues through the gap even when the surrounding NN-path is too sparse to prove it via the walk. When multiple platforms stack at the same dot, only **one** needs to satisfy the perpendicularity check.

If neither holds, the tighter `PILL_GAP_ANGLED_M` (12 m absolute) threshold applies — the gap is an angled or T-junction connector. The gap splits the NN path iff its length exceeds the chosen threshold. Both thresholds are absolute metres — they do **not** scale with `width_base` (which controls pill / disc width, not gap length).

Each post-split group of ≥ 2 dots is emitted as a pill; a singleton group is emitted as an `endpoint` Point feature whose dedicated style layer (drawn between connector-casing and connector-fill) renders it as a colored disc with a white stroke, so the connector's white casing is hidden under the disc rather than crossing the connecting stop's outline. Singletons also participate in MST connector selection. MST connectors (Kruskal's) join all groups — pill groups and singleton (endpoint) groups alike — at their nearest dot pair.

Before the NN-path runs, stop positions within **`DEDUP_TOL_M` (0.5 m)** of each other are collapsed to a single position. Without this collapse, two stops that the bar coordination snaps onto the same logical spot — but which the `cos_lat` scale / unscale in `coordinate_dots_global_stab` leaves at slightly different float values — survive as two unique positions and emit as a 2-point near-degenerate pill. MapLibre cannot render such a pill reliably: with both vertices effectively coincident, the line direction vector is zero and the round caps that should form the disc fail to draw. The tolerance is set small enough to leave legitimate 3–6 m short pills intact and large enough to catch the observed twin spreads (sub-µm float noise up to ~11 cm bar-coordination drift).

The same `DEDUP_TOL_M` tolerance also governs **per-position dot dedup at the dot-emission stage**: at single-platform multi-line halts (e.g. Guarda with R15 + RE4), `coordinate_dots_global_stab` correctly snaps every cluster member to the same physical platform position, but the multi-line dot-emission loop iterates `cluster` directly and would emit one feature per member. Members with different `width_base` then render as concentric circles in the data-driven `circle-radius` layer. Members within `DEDUP_TOL_M` of each other are therefore grouped into one feature per unique placed position, with `dominant_line` applied per group (max `width_base`, dominant mode's darkest color) — same policy as the cluster-centroid dot.

Pill rendering style (thickness, casing, mode-coloured stroke) is unchanged from today; only the dot positions change.

### Connector curving

After pill placement and MST topology are final, each connector emitted by the step above is replaced with a curved polyline. Curving changes connector geometry only — it never changes which pills are connected, and pills themselves remain straight polylines through their dots.

Two distinct constructions are used, depending on what sits at each end:

- **Pill ↔ pill** — symmetric arc with optional straight stubs at each end. Shape: `[A, stub, curve, stub, B]`. The two stubs may differ in length; only the curve between them is required to be symmetric.
- **Pill ↔ disc** — asymmetric arc-then-straight. The curve begins at the pill tip with no pill-side stub and bends at the per-mode radius toward the disc until its forward tangent points at the disc; a straight segment then runs from that tangent point to the disc. Shape: `[A, curve, P, B]` (with P collapsing out when it coincides with B).
- **Disc ↔ disc** — straight 2-point line. Neither tangent is constrained.

**Tangent at each pill endpoint.**

- **Pill tip** (connector leaves the first or last dot of a pill): default tangent is **axial** — the direction of the pill's terminal segment, extended outward. Also evaluate the two **perpendiculars** to that axis. A perpendicular replaces axial only when its resulting connector length is ≤ `CURVE_PERP_PREF_RATIO` (0.7) × the axial-tangent connector length; if both perpendiculars qualify, the shorter wins.
- **Pill interior** (connector leaves an interior dot of a pill): tangent is **perpendicular** to the local pill direction at that dot, on whichever side faces the other endpoint. Local pill direction is the average of the incoming and outgoing segment directions at that dot. No alternatives are evaluated — the axial directions at an interior dot would run along the pill.

For pill ↔ pill, tangent selection is joint across the two ends — when both ends are pill tips and a perpendicular qualifies at one or both ends, the chosen `(tA, tB)` pair is the one minimising total connector length under the per-end rules above.

**Symmetric arc (pill ↔ pill).** Given the chosen tangents `tA` (at A) and `tB` (at B), pick stub lengths `sA, sB ≥ 0` such that the curve drawn between `A' = A + sA·tA` and `B' = B + sB·(−tB)` is mirror-symmetric across the perpendicular bisector of `A'B'`. Mirror symmetry requires `tA` and `−tB` to make equal angles with the chord `A'B'`, which determines the stub-length ratio.

**Pill-to-disc arc.** The arc starts at the pill tip A tangent to the chosen pill-end tangent `tA`, with the arc center placed on the side of `tA` that contains the disc B. The arc bends at the per-mode radius until the forward tangent at point `P` on the circle points at B (`CP ⊥ BP`). From `P`, a straight segment runs to B. When the disc lies inside the curve circle — which happens when the disc is closer than the radius forces the circle out toward — the axial tangent admits no valid arc; the picker falls back to the shortest valid perpendicular tangent rather than to a straight line, since the asymmetric pill-disc construction cannot produce L-shape detours.

**Maximum curve radius.** Capped per mode: `CURVE_MAX_RADIUS_M_BY_MODE` is `30 m` for train and metro, `20 m` for tram, bus, and regional_bus — so the curve scales with the physically larger rail pills. Tighter natural curves use the smaller natural radius; gentler natural curves are tightened to the cap and the straight stubs absorb the remaining distance.

**Fallback to straight.** A pill ↔ pill connector falls back to a straight 2-point line when no valid symmetric arc exists at the default tangent combination (axial at every tip, the prescribed perp at every interior dot) — typical when the two tip axials are parallel or anti-parallel. A pill ↔ disc connector falls back to straight only when no tangent candidate at the pill end produces any valid arc (e.g. the disc lies on the pill's axis line). Disc ↔ disc connectors are always straight.

### Pill grouping (which dots a pill connects)

Which dots a pill connects is determined by the existing clustering — rail by `parent_station`, others spatially within a per-mode radius — and the nearest-neighbour path within each cluster. This concept changes only *where* each dot sits, not which dots cluster.

### Terminus dedup

At a terminus station, the same physical platform is visited by two distinct `(osm_id, stop_id)` entries: one line ends there (`stop_id` is the last entry of its trip — the **arrival** side) and another begins there (`stop_id` is the first entry of its trip — the **departure** side). Rendered naively this produces two dots and two platform extents on top of each other at every terminus, with the departure side's extent additionally always collapsed (the non-rail extent rule extends backward from the snapped coord, and the polyline starts at the snapped coord so there is nothing behind it).

Whenever the same `stop_id` appears as the first entry of one `osm_id` **and** as the last entry of a different `osm_id`, and the two snapped positions are within `TERMINUS_DEDUP_RADIUS_M` (10 m) of each other, the **departure-side entry is omitted from all rendered outputs**: production stop dots, pill clusters, debug platform extents and debug dots. The arrival side remains as the single visible dot and the single visible extent.

The popup data is built from a separate pass that ignores this filter, so clicking the surviving dot still lists both the arrival and the departure line in the badge list. The debug overlay's outline-ring marker therefore continues to identify which of the two listed lines produced the dot the user clicked.

Loop trips (a line whose first and last entries are the same `stop_id` on the same `osm_id`) are not deduped against themselves — the self-match is excluded. A loop is only deduped if a different line's arrival also lands at that stop_id within the radius.

For tram / bus / regional_bus only, two additional rules drop arrival entries that the rule above did not pair, OR that pair only with another off-platform entry. Both treat the offending arrival as **nonexistent by the pill-construction algorithm** and omit it from production stop dots, pill clusters, debug platform extents and debug dots — the same scope of omission as the rule above. The underlying polyline is unchanged in both cases.

1. **Unpaired arrivals.** The arrival entry's `stop_id` did not match any other feature's departure `stop_id` within the radius. Typical case: a layover bay where the vehicle drops passengers ~100 m from the real terminus before moving to the actual departure stop; GTFS encodes the layover and the departure as separate `stop_id`s, so the `stop_id`-keyed pairing above cannot match them.

2. **Layover arrivals shadowed by a real-platform sibling.** The arrival entry's `stop_id` has no `platform_code` (the bare-numeric `:NNNN` form that some feeds use for layover / pre-departure positioning entries), AND some other feature in the same sibling group visits the same UIC at a `stop_id` that DOES have a `platform_code`. The same-UIC platform-coded entry already produces a rendered dot at the real platform, so the layover entry is redundant. Sibling group is the existing `(ref, agency_id, mode)` key used elsewhere in step 07 (consistent with the missing-range-fill sibling rule). The same-line platform-coded entry may sit at any index of any of the sibling features (terminus or mid-route) — it is the existence of the real-platform visit that matters, not its position.

Loop trips are technically in scope for rule 1; in Swiss feeds this rarely matters because circular routes are split into two directional segments. Rule 2 is independent of pairing — it fires whether or not rule 1 already would.

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
3. For rail (train, metro): if the polyline does not symmetrically support ±L/2 around the snapped GTFS coord, the missing side is filled with a straight-line tangent-direction extrapolation so the total range stays L.
4. For tram/bus/regional_bus: if the polyline does not extend L metres backward from the snapped GTFS coord, the missing backward portion is filled per the **missing-range fill** rule above — sibling borrow first, straight-line tangent extrapolation otherwise.
5. If no fill step succeeds (e.g. polyline too short to compute a tangent), the range is clipped to whatever on-polyline geometry exists.

### Debug overlay

Three debug elements render on top of the production style, filtered to the modes in scope (train, metro, tram, bus, regional_bus):

- A **thin black line** tracing each platform's full allowed range along its line's polyline — one per `(line, stop)` pair, including the extrapolated portions of any extent.
- A **clickable circle** at the snapped GTFS coordinate. Filled black if that `(line, stop)` was placed on a perpendicular bar by the sweep; hollow (white fill, black outline) otherwise. Clicking opens a popup with the stop name, mode, atlas platform length (or `– (default)` when atlas had none), and mode-coloured badges for each line stopping there. Hovering a badge shows the line's `origin → destination` as a tooltip. The badge for the specific `(line, direction)` whose polyline produced the clicked dot is outlined with a black-on-white ring, so that when multiple dots overlap the same stop (e.g. both directions of a terminus) the user can tell which one is selected.
- A **thick white line** drawn over each perpendicular bar produced by the sweep — spans the perpendicular extent of the bar's stabbed dots plus a small margin.

These are development-time visual aids only; not part of the medium-zoom production style. Gated by `transit.debug_overlay` in `scripts/config.yaml` so they can be flipped off without code changes — the overlay may survive past first install for in-the-wild diagnosis, but the whole thing (layers, debug sources, the `stabbed` property + case-expression fill, the `_STABBED_PAIRS` / `_DIAG_BARS` module-level state, the `tl_debug_*.pmtiles` build steps, and the `transit.debug_overlay` flag itself) is still to be removed before production.

## Open work

Items deferred from the current pass, in roughly the order they should be addressed:

- **Re-verification across the network after the duplicate-inclusion fix.** A bug in the spatial clustering caused stops to land in more than one cluster (a stop near a cell boundary was claimed by every group whose seed was within the radius). Roughly half of Zürich HB's "226 platforms" turned out to be duplicates; the same factor likely affected many other stations. Bern, the small clean-track stations, multi-deck stations, and the Stadtbahn-style mixed-orientation case all need a fresh visual pass — cluster sizes have changed materially.
- **`PILL_GAP_STRAIGHT_M` / `PILL_GAP_ANGLED_M`** (currently 50 m / 12 m) are the dead-straight vs. angled gap-split thresholds. Tuned by hand; worth revisiting after the re-verification pass.

## Constraints

- Far-zoom and short-zoom stop styles are out of scope.
- `compass_direction` from atlas is intentionally not consumed. The polyline tangent at the dot position is the orientation source for pill geometry.
- Per-mode default lengths and atlas-`length` sanity ranges are configuration values.
- Ferries and mountain modes are out of scope; they render as today.
- The bar-finding sweep runs only on σ-clumps with at least two distinct-position platforms, and reruns recursively on a σ-clump's still-unmatched members under the same condition. Singletons, singleton tangent groups, singleton σ-clumps within a multi-clump tangent group, and members left over after the recursion terminates all flow through the leftover-fill alongside the bars.
- This concept depends on `prm-platform-positions` being implemented and `stop_attributes_sources.json` being emitted.
- The implementation must not regress the rendering of stops without atlas data — the fallback chain guarantees an allowed range is always producible.
