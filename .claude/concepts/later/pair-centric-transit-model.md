# Pair-Centric Transit Model

## Problem

The drawn transit line layer treats each line (trip group) as the rendering
entity. Color, width and saturation are derived from per-line attributes
(mode, frequency, speed). This produces three structural mismatches between
the data model and what the map should communicate:

1. **Corridor density is invisible.** A trunk segment carrying IC + RE + S1 is
   rendered as three overlapping lines. The actual high-density character of
   the corridor — the experience of waiting at a station served by many
   trains — is not expressed visually. Each line's frequency is computed and
   rendered in isolation.

2. **Express service on a shared physical line has no relationship to the
   local service it shares track with.** An IC that skips the local stops
   between Bern and Thun is a separate feature, drawn over the S1 with no
   semantic link. There is no way to say "this segment is served at the IC's
   higher speed" because speed is a line attribute, not a segment attribute.

3. **Parallel routes with shared endpoints cannot be distinguished by stops
   alone.** Brig↔Visp via the SBB line vs via the MGB line with its own
   intermediate stops, Chur↔Landquart via SBB vs via RhB — these connect the
   same stop pair but follow different physical lines. The line-centric model
   handles them only because each line carries its own geometry
   independently.

A secondary problem: overlapping line features make label placement
effectively impossible. With multiple lines on one corridor, no single line
can carry a readable inline label.

## Current state

Every trip group (`gtfs-line-grouping.md`, `trip-group-as-sole-line-identity.md`)
emits one feature per direction variant, with its own `f_weighted`, speed,
color and pfaedle-routed geometry (`pfaedle-line-geometry.md`). Overlapping
segments stack visually with no aggregation; the faster line sorts on top.

A lot has since been built on those per-line features and keys on the
`line_key` identity: the line detail view and its `line_keys` membership on
every stop feature, popups, the route color index the routing result cards
use, stop dot colors, the stop-extent fill and the pill-arrow courses derived
from a line's own polyline, and per-feature zoom thresholds. None of that is
replaced by this concept (see Constraints).

## Requirements

The rendering entity of the **drawn line layer** is the **stop pair**: two
stops served consecutively by a trip, with no intermediate stop on that trip.
Rendering walks pairs and emits chunks; per-line features are no longer the
unit of the drawn line layer.

### Pair identity

- A pair is **ordered** (`A→B` and `B→A` are two pairs). Each direction has
  its own geometry, its own frequency and its own chunks. Where both
  directions run on the same track the two chunks overlap, as the two
  direction variants of a line do today.
- Stop identity is the **merged station identity** of `sloid-stop-identity.md`
  (the UIC, all quays of a station collapsed), the same identity the trip
  grouping uses.
- A pair carries a **mode**. Pairs of different modes between the same two
  stations are different pairs.
- Pairs are extracted from the trips that **survive every group-level
  gate** (frequency gate, rare-variant filter, active-days gate,
  post-emission split, EV exclusion), so garage runs and dropped variants
  never form pairs.

### Pair attributes

- **Contributing lines**: the set of `line_key`s whose trips serve the pair,
  plus the express lines folded in through the hierarchy (below).
- **Frequency**: `f_weighted` recomputed from the trips on the pair, using
  the existing sampling windows (core / eve / we) and window weights, exactly
  as for a line today — but counted over the pair's own trips, not summed
  from line-level values. Trips of parent pairs (below) count into every
  child pair they span. Since today's line width is already per direction
  variant, widths stay as they are wherever a single line serves a pair, and
  grow only where several lines or express trips add up.
- **Speed**: distance over departure-to-arrival time between the two stops,
  averaged over the pair's own trips, then overridden by the hierarchy
  (below). The per-line speed continues to exist for everything that is not
  line color (sort order, popups, detail view).
- **Geometry**: the slice of a pfaedle shape between the two stops. A pair
  served by trips with different shapes (e.g. a bus variant on a detour) has
  more than one geometry and is drawn along each.

### Hierarchy (express over local)

A pair `A→C` whose trips skip stops that other trips of the **same mode**
serve between `A` and `C` is a **parent** of the chain of pairs
`A→B`, `B→C` (recursively, any depth) **if and only if the parent's shape
slice coincides geometrically with the concatenated child slices**. The
hierarchy never crosses modes.

The geometric test is what separates the two cases in problem 3: on
Bern↔Thun the S1 chain lies on the IC's track, so the IC pair is its parent;
on Chur↔Landquart the RhB chain lies on its own track, so the SBB pair is
not their parent and both remain separate, correctly drawn pairs. Without the
geometric test the SBB track would vanish, since parents are not drawn. GTFS
carries no non-geometric signal for this — Bern↔Thun is cross-agency too.

- **Parent pairs are never drawn.** Their trips contribute to the frequency
  and the contributing-lines set of every child pair they span, and their
  speed becomes the children's speed.
- **Speed**: every pair renders at the speed of the **topmost** parent
  containing it. Example: Lenzburg→Zürich (local) ⊂ Aarau→Zürich (nonstop)
  ⊂ Olten→Zürich (nonstop) ⊂ Bern→Zürich (nonstop) — every pair in the chain
  is rendered at the Bern→Zürich speed.
- A pair without a parent keeps its own per-pair speed. Speed then varies
  from segment to segment along a purely local line, and so does the color;
  that is accepted as the honest reading of the timetable.
- A pair that has no parent-free chain (an express pair whose chain does not
  geometrically coincide) is an ordinary drawn pair.

The hierarchy is also the foundation for any later work on network-level
visualization (spine identification, interchange complexes,
corridor-thickening rules).

### Chunks

The pipeline walks consecutive pairs along each shape and emits **chunks** —
runs of consecutive pairs that share `(mode, frequency, speed, contributing
lines)`. Each chunk is one polyline feature. A chunk breaks at every stop
where any of the four changes; where all four match, the line draws through.

Frequency and speed are compared **rounded**, so noise-level differences do
not break a chunk (roughly 100 distinguishable steps over each value's
range; the exact rounding is an implementation choice). Contributing lines
must match exactly: a corridor where one line leaves at the same frequency
must still break, or the chunk's `lines` attribute would be wrong.

### Chunk attributes and interaction

- `lines`: the contributing `line_key`s. Click and hover surface the line
  names from this attribute; the line detail view's selection filter uses it
  the way it uses `line_keys` on stop features today.
- Color from mode palette + speed, width from frequency, applied at chunk
  level with the existing per-mode curves. Widths may exceed today's maxima
  where pair frequency exceeds a single line's `best_freq`; that is accepted
  and the width curve may need re-tuning per mode afterwards.
- Inline line labels at high zoom are out of scope but are the natural fit
  for the pair model.

### Mountain and ferry

Funicular, rack and ferry trips are pfaedle-routed like everything else and
need no special treatment: one line per pair, chunk = line. Aerial lifts keep
the existing straight-line fallback between the two stops when no shape
exists.

## Constraints

- **Per-line features survive as an internal product.** Step 07 (stop dots,
  pills, pill-arrows, stop-extent fill), the line detail view, popups, the
  route color index and the stop search index keep consuming per-line
  features and the `line_key` identity as today. Only the drawn line layer
  switches to chunks. Optimising those consumers for the pair model is a
  second step, not part of this concept.
- Per-line speed is kept for everything except line color. Line color comes
  from pair speed only.
- Mode color palette, frequency windows, window weights and the per-mode
  `best_freq` / `worst_freq` gates are unchanged. Date-aware rendering for
  construction or seasonal services stays out of scope.
- Branching where two lines share track up to a divergence point without a
  station is rendered as overlapping chunks until the first station after the
  divergence. This is preferred over geometric handling of stationless
  splits.
- The geometric parent test must stay cheap: candidates are only pairs whose
  endpoints are bridged by a same-mode chain of other pairs, and the
  comparison is between shape slices already on hand. If it turns out to be
  a performance problem, that is a reason to revisit the test, not to drop
  the hierarchy.
- **Future refinement (not part of MVP):** pair frequency should evolve from
  trip counting toward average departure interval. Two IC trains four
  minutes apart inside a 30-minute window are not eight trains per hour.
