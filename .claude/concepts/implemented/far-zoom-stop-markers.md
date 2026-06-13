# Far-Zoom Stop Markers

## Problem

At far zoom (below z12 for train, below z13 for every other mode) every station cluster shows one **centroid dot**, currently positioned at the arithmetic mean of cluster members' positions after the medium-zoom pill-placement algorithm has run. Those positions reflect platform geometry, so at a bus-tram crossroads the dot lands at the platform-group middle rather than at the junction itself — even though at far zoom the marker's job is to identify the service node, not the platform group. A bus dot can sit 50–100 m to the side of the intersection where its lines actually meet.

A separate ferry inconsistency: at z11–z12 ferry clusters currently render their **medium-zoom endpoint disc** (drawn through the pill paint stack) as the de-facto far-zoom marker. Every other non-train mode uses a low-zoom dot feature for this purpose at the same zooms. The two paint stacks have different visual weight, so the ferry disc reads inconsistently next to neighbouring modes' far-zoom dots.

## Requirements

### Position rule by mode family

Two mode families, each with its own fallback chain. The chain is evaluated in order; the first step that produces a position wins.

**Train + mountain rail-like** (train, mountain `rebucketed_rail` / `rack`):

1. The **largest pill or disc** in the cluster — pill and endpoint disc features are ranked together by line count (number of distinct lines whose dots the feature covers). Pill position = arc-length midpoint of its polyline; disc position = the endpoint position itself.
2. The existing centroid position (arithmetic mean of cluster members' positions after the medium-zoom pill algorithm).

**Every other mode** (metro, tram, bus, regional_bus, ferry):

1. **Intersection search** result (defined below). Fires only when a qualifying candidate exists.
2. The largest pill or disc (same as rail step 1).
3. The existing centroid position (same as rail step 2).

Tiebreaks within each ranking step:

- **Largest pill or disc**: logical-line count descending → **line frequencies** (each candidate's logical lines' `freq_score`s sorted descending; compare candidates lexicographically — the candidate whose highest-freq line is higher wins, then next-highest, etc.) → closer to cluster snap-centre. Counting logical lines (not `osm_id`s) is what stops the count from over-rewarding multi-direction lines: a single-direction tram terminating at a stop should not be outranked by a two-direction bus at the same stop just because the bus has more `osm_id`s. The frequency tiebreak then fires at stations where a high-frequency line sits alongside a low-frequency line at the same line count (Bern Ostring: tram 7 terminus disc and bus 40 pill, both 1 logical line — tram wins by `freq_score`).
- **Intersection-search top score** → closer to cluster snap-centre.

Every tiebreak is a total order; same input always produces the same position.

The cluster **arithmetic centre** used by every tiebreak is the mean of the cluster members' **pfaedle-snapped** positions (after snap, before pill placement). Snaps sit on the line polylines by construction, so the centre is biased toward where the network actually runs and is robust to GTFS stop-coord noise.

### Intersection search

**Logical line.** "Line" throughout this rule means a distinct logical line — the `(ref, mode, agency_id)` tuple. Direction variants and terminus variants of one route share a logical key and count once. Without this, a four-way parallel run of one bus route's direction × terminus variants would falsely score as a four-way intersection, while the actual route forms no intersection at all.

**Lines in scope.** Every logical line with at least one stop in the cluster. Both rail and non-rail lines count; mode is not consulted when scoring candidates — a tram-meets-bus crossing must outscore a tram-only crossing in the same cluster.

**Candidate set.** Union of:

- Every distinct **pfaedle-snapped stop position** of the cluster's members. These sit on a line polyline by construction.
- Every **pairwise polyline crossing** between two distinct in-scope lines (distinct by logical key, not by `osm_id`) whose intersection point lies within the cluster bounding box expanded by `intersection_bbox_pad_m`.

**Score.** For each candidate `p`, score = number of distinct in-scope logical lines with at least one polyline passing within `intersection_tol_m` of `p`. Direction variants of one line count once regardless of how many of their polylines fall near `p`.

**Pick.** Highest score, with a minimum of **2** — a candidate covered by only one logical line isn't an intersection. Among tied top-scoring candidates the closest to the cluster snap-centre wins.

If no candidate reaches score 2, the intersection search fails and the chain falls through.

### Required behaviour at characteristic cases

- **Crossroads.** Two or more line polylines cross. The crossing point is in the candidate set, scores `n_lines`, wins. Dot sits at the junction even when all platforms are set back from it.
- **Roundabout.** Lines share an arc of the loop without crossing each other. Pairwise crossings find nothing, but every stop snap on the shared arc sees the other lines within tolerance — those snaps tie at the top score. Arithmetic-centre tiebreak places the dot at the platform-group middle on the perimeter. The loop is sub-pixel at far zoom, so a perimeter position reads as the loop centre.
- **One-way pair / T-junction.** Lines converge at a single point. The convergence is captured either as a polyline crossing or as a near-crossing of stop snaps; the rule fires correctly.
- **Parallel running on one street.** Multiple lines share a street segment without crossing. Every stop snap on the segment scores `n_lines`. Tiebreak lands the dot at the platform-group middle on the street — same outcome as largest-pill, so the rule degenerates gracefully.
- **Multi-line metro interchange.** Underground lines cross. The crossing point scores `n_lines`, wins. Far-zoom dot sits at the interchange, not at the largest platform.
- **Ferry pier.** Visiting ferry lines share a pier OSM node. The shared node is a stop snap, every visiting line passes within tolerance, it wins. Result equals the canonical pier position the ferry code already produces for medium-zoom rendering.
- **Single-line stop.** Only one in-scope line; no candidate can reach score 2. The chain falls through.
- **Non-convergent ferry pier.** Visiting ferry lines don't share a vertex within the convergence threshold (per the existing ferry rendering rule). No candidate reaches score 2; chain falls through.

### Ferry far-zoom marker

The ferry far-zoom marker must be a low-zoom dot rendered through the same paint path as every other mode's far-zoom dot, not a medium-zoom endpoint disc rendered through the pill paint stack.

Concretely:

- Ferry clusters emit a far-zoom dot positioned per the intersection-search rule above. In practice this equals the canonical pier position the existing ferry code computes.
- The ferry endpoint disc's appear-zoom moves to **z13** so the medium-zoom ferry view begins at the same zoom as every other non-train pill mode. At z11–z12 only the far-zoom dot is visible for ferry, matching every other non-train mode at the same zoom range.

### No-jump invariant

Across the dot-to-pill switch zoom (z12 for train, z13 otherwise) the far-zoom dot's position must not produce a perceptible jump:

- When the rule returns a pill centre or disc position, that point is part of the medium-zoom geometry — the marker visually resolves into the pill / disc, not jumps.
- When the rule falls all the way through to the existing-centroid fallback, the medium-zoom view at that cluster has no pill and no disc to take over (this case is exactly the pill-collapse path). The dot remains at the same centroid coordinate above and below `mz`.
- The intersection-search position is permitted to be off the medium-zoom pill / disc geometry. At a bus crossroads the dot sits at the junction while the medium-zoom pills sit at the platforms. The transition is the intended one (marker resolves from "service node" to "platform geometry") and is not considered a jump.

### Visual style

Unchanged. The far-zoom dot keeps its existing low-zoom style: white fill, 1 px black border, sized off the line's `width_base` via the existing low-zoom dot curve. This concept changes **where** the dot sits, not how it looks.

### Configuration

New `far_zoom_marker` block in the transit config:

- `intersection_tol_m` — proximity threshold for counting a line as passing near an intersection candidate. Default `8`.
- `intersection_bbox_pad_m` — bounding-box padding when scanning for pairwise polyline crossings. Default `20`.

## Constraints

- This concept changes **far-zoom** (below each mode's `mz`) dot placement only. Medium-zoom pill, disc, and connector rendering are unchanged.
- The no-jump invariant binds the design: any future tweak to the position rule must preserve visual continuity at the `mz` boundary, with the explicit exception that the intersection-search case may differ from the medium-zoom geometry.
- Train and mountain rail-like modes skip the intersection search by design — train tracks rarely cross at stations, and where they do (throat junctions) the crossing sits far from the platforms.
- Intersection scoring is line-polyline-based, not platform-extent-based. Platform-extent geometry from `pill-rendering.md` is not consulted.
- The intersection search includes lines of every mode in the cluster, not just the dominant mode's lines. Mixed-mode clusters (tram + bus interchanges, train + tram interchanges) score every in-scope line equally.
- Single-line clusters cannot produce a qualifying intersection. The fall-through chain handles them without special-casing.
- The rule must be deterministic; identical inputs produce identical positions.
