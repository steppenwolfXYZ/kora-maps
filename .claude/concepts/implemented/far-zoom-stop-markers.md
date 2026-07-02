# Far-Zoom Stop Markers

## Problem

At far zoom (below z14, uniform across every mode) every station cluster shows one **centroid dot**, currently positioned at the arithmetic mean of cluster members' positions after the medium-zoom pill-placement algorithm has run. Those positions reflect platform geometry, so at a bus-tram crossroads the dot lands at the platform-group middle rather than at the junction itself — even though at far zoom the marker's job is to identify the service node, not the platform group. A bus dot can sit 50–100 m to the side of the intersection where its lines actually meet.

A separate ferry inconsistency: below z14 ferry clusters currently render their **medium-zoom endpoint disc** (drawn through the pill paint stack) as the de-facto far-zoom marker. Every other non-train mode uses a low-zoom dot feature for this purpose at the same zooms. The two paint stacks have different visual weight, so the ferry disc reads inconsistently next to neighbouring modes' far-zoom dots.

## Requirements

### Position rule by mode family

Two mode families, each with its own fallback chain. The chain is evaluated in order; the first step that produces a position wins.

**Train + mountain rail-like** (train, mountain `rebucketed_rail` / `rack`):

1. The **largest pill or disc** in the cluster — pill and endpoint disc features are ranked together by combined frequency (sum of `f_weighted` across the distinct logical lines whose dots the feature covers). Pill position = arc-length midpoint of its polyline; disc position = the endpoint position itself.
2. The existing centroid position (arithmetic mean of cluster members' positions after the medium-zoom pill algorithm).

**Every other mode** (metro, tram, bus, regional_bus, ferry):

1. **Intersection search** result (defined below). Fires only when a qualifying candidate exists.
2. The largest pill or disc (same as rail step 1).
3. The existing centroid position (same as rail step 2).

Tiebreaks within each ranking step:

- **Largest pill or disc**: **combined frequency** (sum of `f_weighted`, weighted trips/h, across the candidate's logical lines) descending → closer to cluster snap-centre. Logical-line keys are `(ref, mode, agency_id)`, so direction and terminus variants of one route contribute once (their `f_weighted` is shared). The combined-frequency rule means a single high-frequency tram outweighs several low-frequency buses at the same stop — Bern Breitenrain (tram 9 ≈ 18.4 trips/h vs. RBS bus 26+36+41 ≈ 17.3 trips/h combined) lands on the tram side, and Bern Ostring (tram 7 ≫ bus 40) lands on the tram disc.
- **Intersection-search top score** → closer to cluster snap-centre.

Every tiebreak is a total order; same input always produces the same position.

The cluster **arithmetic centre** used by every tiebreak is the mean of the cluster members' **pfaedle-snapped** positions (after snap, before pill placement). Snaps sit on the line polylines by construction, so the centre is biased toward where the network actually runs and is robust to GTFS stop-coord noise.

### Intersection search

**Logical line.** "Line" throughout this rule means a distinct logical line — the `(ref, mode, agency_id)` tuple. Direction variants and terminus variants of one route share a logical key and count once. Without this, a four-way parallel run of one bus route's direction × terminus variants would falsely score as a four-way intersection, while the actual route forms no intersection at all.

**Lines in scope.** Every logical line with at least one stop in the cluster. Both rail and non-rail lines count; mode is not consulted when scoring candidates — a tram-meets-bus crossing must outscore a tram-only crossing in the same cluster.

**Candidate set.** Union of:

- Every distinct **pfaedle-snapped stop position** of the cluster's members. These sit on a line polyline by construction.
- Every **pairwise polyline crossing** between two distinct in-scope lines (distinct by logical key, not by `osm_id`) whose intersection point lies within the cluster bounding box expanded by **1.5 × the mean stop-to-snap-centre distance** within the cluster. The pad scales with each cluster's own footprint — wider for stretched-out interchanges where the actual junction sits well off the platforms (Bern Viktoriaplatz: roundabout ~60 m from the platforms), tighter for compact clusters so neighbouring crossings stay out of scope.

**Score.** For each candidate `p`, score = sum of `f_weighted` (weighted trips/h) across the distinct in-scope logical lines with at least one polyline passing within `intersection_tol_m` of `p`. Direction variants of one line contribute once (shared `f_weighted`).

**Pick.** Highest score, with a hard minimum of **≥2 distinct logical lines near the candidate** — a point covered by only one logical line isn't an intersection regardless of how frequent that line is. Among tied top-scoring candidates the closest to the cluster snap-centre wins.

If no candidate has ≥2 distinct logical lines, the intersection search fails and the chain falls through.

### Bad-intersection gate

A winning intersection result is **kept** only when it sits within the rendered pill spread. Concretely: compute the cluster snap centre (the same arithmetic centre used by the tiebreaks). The intersection result is discarded — and the chain falls through to the largest-pill/disc step — when its distance to that centre exceeds the **mean** distance of the cluster's pill midpoints and endpoint-disc positions to the same centre.

**Full-cluster carve-out.** The gate is skipped when every in-scope logical line passes within `intersection_tol_m` of the winning candidate — i.e. the candidate is the meeting point of *all* the cluster's lines, not just two of them. A junction that every line in the cluster actually traverses is the correct service node regardless of how far it sits from the platform centroid. Without this carve-out, asymmetric layouts where the true junction sits beyond the platform-derived budget (Bern Viktoriaplatz: tram 9 + bus 10 meet at a roundabout ~60 m from the snap centre, outside the ~46 m mean pill distance) would falsely fall through to the largest-pill step.

Why this exists: at clusters like Bern Breitenrain the intersection candidate scores at a single-mode platform (three buses sharing one bus stop) that sits ~80 m outside the dominant-mode pill geometry (tram pill, 80 m south). The no-jump invariant breaks because the far-zoom dot would visually leap onto the pill at the dot-to-pill switch. The mean-distance threshold catches this case while leaving normal intersections — which always sit near the platform group's centre by construction — untouched.

The gate has no configurable threshold. It uses the mean pill/disc-to-centre distance directly so it scales with each cluster's geometry. Clusters with tightly grouped pills set a tight bar; stretched-out platform arrays set a looser bar. Clusters with no pills / discs (the pill-collapse case) skip the gate — there is no rendered geometry to compare against, so the intersection is the only signal available and is kept.

### Required behaviour at characteristic cases

- **Crossroads.** Two or more line polylines cross. The crossing point is in the candidate set, scores the full `sum(f_weighted)` over all crossing logical lines, wins. Dot sits at the junction even when all platforms are set back from it.
- **Roundabout.** Lines enter the loop from different approach streets and cross at one or two points where their polylines traverse the loop. The dynamic bbox pad (1.5 × mean stop-to-snap-centre distance) reaches out to the loop even when the platforms are set back along the approach streets. The crossing nearest the snap centre wins on the closest-to-centre tiebreak; when every cluster line passes within tolerance of that crossing the bad-intersection gate skips the centroid-distance check (Bern Viktoriaplatz: tram 9 + bus 10 meet at four crossings around the roundabout; closest sits ~62 m from the snap centre, well outside the mean pill distance but kept because both logical lines are present).
- **One-way pair / T-junction.** Lines converge at a single point. The convergence is captured either as a polyline crossing or as a near-crossing of stop snaps; the rule fires correctly.
- **Parallel running on one street.** Multiple lines share a street segment without crossing. Every stop snap on the segment sees the same set of logical lines and ties at the full `sum(f_weighted)`. Tiebreak lands the dot at the platform-group middle on the street — same outcome as largest-pill, so the rule degenerates gracefully.
- **Multi-line metro interchange.** Underground lines cross. The crossing point scores the full `sum(f_weighted)` of the crossing lines, wins. Far-zoom dot sits at the interchange, not at the largest platform.
- **Ferry pier.** Visiting ferry lines share a pier OSM node. The shared node is a stop snap, every visiting line passes within tolerance, it wins. Result equals the canonical pier position the ferry code already produces for medium-zoom rendering.
- **Single-line stop.** Only one in-scope line; no candidate can reach score 2. The chain falls through.
- **Non-convergent ferry pier.** Visiting ferry lines don't share a vertex within the convergence threshold (per the existing ferry rendering rule). No candidate reaches score 2; chain falls through.
- **Off-pill multi-line single-mode platform** (Bern Breitenrain: 3 buses share one platform, 1 tram on platforms 80 m south). Intersection scores ≥2 distinct lines at the bus snap and would win on combined frequency, but its distance to the cluster snap centre exceeds the mean pill/disc-to-centre distance — the bad-intersection gate discards it. The chain falls through to the tram pill.

### Ferry far-zoom marker

The ferry far-zoom marker must be a low-zoom dot rendered through the same paint path as every other mode's far-zoom dot, not a medium-zoom endpoint disc rendered through the pill paint stack.

Concretely:

- Ferry clusters emit a far-zoom dot positioned per the intersection-search rule above. In practice this equals the canonical pier position the existing ferry code computes.
- The ferry endpoint disc's appear-zoom moves to **z14** so the medium-zoom ferry view begins at the same zoom as every other pill mode. Below z14 only the far-zoom dot is visible for ferry, matching every other mode at the same zoom range.

### No-jump invariant

Across the dot-to-pill switch zoom (uniform z14 for every mode) the far-zoom dot's position must not produce a perceptible jump:

- When the rule returns a pill centre or disc position, that point is part of the medium-zoom geometry — the marker visually resolves into the pill / disc, not jumps.
- When the rule falls all the way through to the existing-centroid fallback, the medium-zoom view at that cluster has no pill and no disc to take over (this case is exactly the pill-collapse path). The dot remains at the same centroid coordinate above and below `mz`.
- The intersection-search position is permitted to be off the medium-zoom pill / disc geometry. At a bus crossroads the dot sits at the junction while the medium-zoom pills sit at the platforms. The transition is the intended one (marker resolves from "service node" to "platform geometry") and is not considered a jump.

### Visual style

Unchanged. The far-zoom dot keeps its existing low-zoom style: white fill, 1 px black border, sized off the line's `width_base` via the existing low-zoom dot curve. This concept changes **where** the dot sits, not how it looks.

### Configuration

New `far_zoom_marker` block in the transit config:

- `intersection_tol_m` — proximity threshold for counting a line as passing near an intersection candidate. Default `8`.

## Constraints

- This concept changes **far-zoom** (below each mode's `mz`) dot placement only. Medium-zoom pill, disc, and connector rendering are unchanged.
- The no-jump invariant binds the design: any future tweak to the position rule must preserve visual continuity at the `mz` boundary, with the explicit exception that the intersection-search case may differ from the medium-zoom geometry.
- Train and mountain rail-like modes skip the intersection search by design — train tracks rarely cross at stations, and where they do (throat junctions) the crossing sits far from the platforms.
- Intersection scoring is line-polyline-based, not platform-extent-based. Platform-extent geometry from `pill-rendering.md` is not consulted.
- The intersection search includes lines of every mode in the cluster, not just the dominant mode's lines. Mixed-mode clusters (tram + bus interchanges, train + tram interchanges) score every in-scope line equally.
- Single-line clusters cannot produce a qualifying intersection. The fall-through chain handles them without special-casing.
- The rule must be deterministic; identical inputs produce identical positions.
