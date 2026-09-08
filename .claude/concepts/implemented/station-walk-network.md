# Station Walk Network

## Problem

Walking legs into and out of stations end at the wrong place. MOTIS asks
Valhalla to walk to a quay's raw GTFS coordinate; Valhalla snaps that
coordinate to the nearest routable edge measured in plan view only, with
no awareness of what is above or below. Where a station has stacked
infrastructure, the nearest edge is often on a different level than the
platform.

The canonical case is Bern, tracks 9 and 10: the nearest routable edge to
the GTFS coordinate is a footway on the bus/parking deck two levels above
the tracks, 11 m away; the ramp that actually reaches the platform is
14.7 m away and loses. The walk climbs the deck ramp and is declared
finished directly above the platform. The route is legal and connected —
it is simply not going where the passenger goes. Measured against the
real path it is 507 m / 6:25 versus the reported 308 m / 4:00, so the
error is ~2.5 minutes of missing walking time, not only a wrong drawing.

Two further defects share the same root:

- Platform surfaces are not routable at all. Swiss platforms are mostly
  mapped as OSM areas, and the pedestrian router does not traverse areas.
  A walk can reach a stair head but cannot continue along the platform.
- Stairs, ramps and lifts are therefore never *chosen* — the walk ends
  wherever the flat snap landed, so their traversal time never enters the
  result.

This blocks two planned features and undermines a shipped one:
step-free / wheelchair mode cannot be trusted if the router never
commits to a specific vertical connector, and transfer-safety warnings
are computed from transfer times that are systematically optimistic at
exactly the large interchanges where transfers are tight.

## Requirements

### Platform walk surfaces

- Every platform that is mapped in OSM as an area gains a routable
  **platform walk line** in the pedestrian routing graph, running along
  the platform's long axis. This covers rail, tram and bus platforms
  alike — anything tagged as a platform without a routable highway value.
- The walk line follows the platform's shape. A platform that curves must
  produce a curved walk line; a straight rectangular platform must
  produce a straight one. The line must stay inside the platform
  footprint over its whole length, and must not wander laterally where
  the footprint merely changes width — a stair opening or a widened head
  is not a bend in the platform.
- Platforms already mapped as open ways are used directly and are not
  re-synthesised. A platform way that carries a walkable highway value is
  already in the graph and needs no synthetic twin.
- Walk lines carry the level of the platform they were derived from.
- Walk lines are marked as synthetic so they are distinguishable from
  surveyed OSM geometry at any later stage. The marker tags introduced by
  this concept are `kora:platform_walk` for the walk line and
  `kora:platform_link` for the connectors below. Every synthetic object
  additionally records the OSM object it was derived from in
  `kora:source`.

### Level-aware welding

- Each platform walk line is connected to the existing pedestrian
  network at every point where a real pedestrian way (stairs, ramp,
  footway, corridor, lift, or an ordinary street at a small halt) meets
  that platform, within a small tolerance for stair heads drawn a little
  short of the platform edge.
- **A connection may only be made between geometry on compatible
  levels.** Level compatibility is decided from the OSM `level` tag,
  falling back to `layer`; multi-level values (`-1;0`, `0-2`) count as
  every level they span. A way whose level set does not intersect the
  platform's level set must never be welded, even where it passes
  directly over or under the platform.
- Level compatibility is **asymmetric**. A platform that says nothing
  about its level is an ordinary at-grade stop — most of Switzerland is
  mapped that way — and there anything nearby may connect. But once a
  platform declares a level, silence from the candidate is not
  agreement: an undeclared candidate must not be welded to it.
  (Treating silence as agreement welded two untagged
  `tunnel=building_passage` ways on Bern's Welle overpass onto the
  level-0 platform 1/2, and the router then routed passengers off the
  street onto a platform that has no street access.)
- Welding must reuse the identity of the existing pedestrian node, so
  that the connection is a real graph connection and not a second,
  parallel piece of geometry.
- A platform with no level-compatible pedestrian way touching it gets a
  walk line but no connection; it must not be welded to something on the
  wrong level as a fallback.

### Platform seams

- A long platform is regularly mapped as two abutting OSM areas laid end
  to end (every Bern platform is). Each half gets its own walk line, and
  welding only ever attaches walk lines to real pedestrian ways — so the
  halves would be severed at the seam and a passenger arriving on one
  half could not reach a boarding point on the other.
- Abutting platform areas are therefore joined at their seam: walk lines
  of areas that share boundary nodes are welded end to end, subject to
  the same level compatibility rule, and only when the join is a few
  metres long. A longer join means the areas merely touch at a corner,
  and joining them would invent a shortcut across whatever lies between.

### Quay anchors

- Every GTFS quay with a platform code at a station with a parent is
  anchored onto the walk line of its own platform, regardless of mode.
  The anchor is the point on that walk line nearest the quay's published
  coordinate, and only walk lines within a short radius are eligible.
- Only walk lines that are actually connected to the surrounding network
  are anchor targets. An isolated walk line would be a worse snap target
  than the status quo, because the router would either fail or fall back
  to the very edge the concept exists to avoid.
- Platform identity is matched first by platform designation (the GTFS
  platform code against the OSM platform reference, which may name
  several tracks at once), and only where that fails by proximity.
- Anchors are consumed by the routing backend only. Map rendering keeps
  using the published GTFS coordinates and is bit-identical before and
  after this change.
- Anchors replace, and are strictly preferred over, the pre-existing
  platform-code snap in the MOTIS sidecar builder, which remains as the
  lower tier for quays no walk line covers — notably platforms with no
  mapped body. That lower tier is applied by the sidecar builder itself
  and is not labelled in the anchor record.
- Each anchor records which tier produced it, the platform it landed on
  and its distance from the published coordinate. The tier values are
  `centerline_ref` (platform designation matched), `centerline_near`
  (proximity matched) and `unanchored`.

### Quay source

- The quay list is read from the **filtered** feed (step 04's output),
  not the routed one. The orchestrated pipeline builds this network in
  parallel with pfaedle, so the routed feed is being rewritten at that
  moment; reading it anchored against the previous run's quays.
- Anchors are keyed by stop id, and the source feed renumbers quays
  between releases. A stale anchor set is therefore not a partial
  improvement but a silent regression: the renumbered quay matches no
  anchor, keeps its published coordinate, and — when that coordinate sits
  off the walkable graph — drops out of the footpath matrix entirely, so
  trains calling there cannot be boarded at all.
- Consequently the anchor set must be rebuilt whenever *either* the OSM
  extract or the filtered stops change. Freshness may not be judged on
  the OSM side alone.

### Unanchored quays and residual gaps

- A quay that cannot be anchored keeps its published coordinate. No quay
  may be dropped or moved to a different platform to force a match.
- Wherever a computed walking leg does not physically reach its
  endpoint — an unanchored quay, or any snap that lands short — the
  remaining straight-line gap is charged as walking time at a speed
  **below** normal walking pace, on the assumption that an unmodelled
  gap is more likely to contain stairs or a detour than a clear straight
  run.
- The penalty must be proportionate. Gaps of a metre or less are snapping
  noise and charged nothing. The slow pace applies to the first stretch
  of a gap only; anything beyond that is charged at normal walking pace,
  because a long gap means the requested point simply sits off the
  network (a free-form map click in a field), and penalising all of it
  would let the gap dominate the leg. The slow speed, the noise threshold
  and the slow-stretch length are three named constants, not per-case
  values.
- The gap distance is added to the leg's reported distance, and the gap
  is drawn as part of the walking line so the user sees an unbroken path.
- This applies to point-to-point walks only. The one-to-many offsets that
  seed the search return durations without snapped points, so no gap can
  be measured there.

### Lifts

- Lifts mapped as ways or areas must become routable. Previously only
  node-mapped lifts reached the graph, so at stations where the lift is
  drawn as a shaft the step-free path did not exist at all.
- A lift shaft becomes a single hub node joined to every pedestrian node
  touching the shaft, whatever level it is on — joining the levels is the
  point. A shaft touching fewer than two ways connects nothing and is
  skipped.
- Lift traversal must remain distinguishable from ordinary footway
  traversal, so a later step-free mode can price or prefer it. The
  marker tag introduced for this is `kora:elevator`, on the hub and on
  its connectors.
- A lift must not become a free vertical shortcut. The hub therefore
  carries the tag the router already prices as a lift, so an able
  walker's route does not prefer it over the stairs beside it.

### Pedestrian areas

- Squares, plazas and other pedestrian surfaces mapped as OSM areas are
  made routable. They are the same defect as platforms in a different
  guise: the router cannot traverse an area, so a square that people walk
  across every day is a hole in the graph.
- Crossings are **direct**, not routed via a central hub. A hub would
  drag a walk to the middle of a long thin square even when the real
  route clips a corner.
- Where a direct line is not possible — a concave outline, or an
  obstacle inside the area — the crossing bends around the obstruction
  by the shortest available path, using the area's own corners. It must
  never pass through a hole, and never leave the area's outline; a
  candidate crossing is proven clear at several points along its length,
  not just at its midpoint, because sliver holes defeat a single test.
- This requirement is as much about what is drawn as about timing. A walk
  line that cuts through a building is wrong on the map before it is
  wrong in the schedule, and it stays wrong under any future aerial
  imagery. Because the drawn line is the routed geometry, one mechanism
  has to serve both.
- Entry points are the area's boundary nodes shared with other walkable
  ways. No level check is applied to them: an area's entry is by
  definition a node the area shares with the way, so the two are the same
  geometry.
- An area with fewer than two usable entry points contributes nothing and
  is skipped rather than connected to something arbitrary. Areas whose
  crossing graph would exceed a fixed node budget (a handful of enormous
  plazas) are skipped too, and reported rather than silently truncated.
- The marker tag introduced for these crossings is `kora:area_cross`.

### Coverage and diagnostics

- The work produces a coverage record with overlay totals — platforms
  traced and synthesised, open platform ways reused, welds made,
  platforms left unwelded, seams welded or refused, lift hubs and links,
  pedestrian areas seen, crossed, skipped for want of entry points or
  size, and crossing edges kept or rejected as obstructed — plus, per
  station, how many quays were anchored by which tier. This is the
  artefact used to judge whether a station is modelled well enough to
  answer a step-free query.
- Anchor coverage on the current data (all modes, every quay with a
  platform code): roughly 7,300 quays anchored, about half of them by
  platform designation, against roughly 6,300 unanchored. The unanchored
  residual is dominated by quays with no mapped platform body — most bus
  quays — and falls to the platform-code snap or the published
  coordinate.

## Constraints

- The synthetic network exists only in the pedestrian router's input. The
  map pipeline's OSM inputs, the drawn geometry, stop dots, pill-arrows
  and every rendered artefact are untouched.
- Synthetic geometry must never collide with real OSM identities, and
  re-running the build on unchanged inputs must produce identical output.
- No walking authority other than Valhalla is introduced or restored.
  There is no OSR walking fallback.
- Platform walk lines are a routing convenience, not a claim about
  physical accessibility. Nothing in this concept may be read as
  asserting that a platform is step-free; that claim needs the Swiss
  accessibility datasets, which are attribute-only and out of scope here.
- The Swiss open-data feeds carry no in-station walking geometry — the
  national NeTEx profile has stop places, quays and level references but
  no navigation paths or path links. OSM therefore remains the sole
  source of station interior geometry, and this concept must not be
  designed around a future feed that would supply it.
- Applying the change requires rebuilding the pedestrian routing tiles
  and re-importing the routing backend, because both the graph and the
  precomputed stop-to-stop walking matrix change.
- Transfer times get longer at large interchanges. That is the intended
  correction, and any transfer-safety thresholds tuned against the old
  optimistic values need re-checking afterwards.
