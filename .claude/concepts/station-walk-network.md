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

- Every rail platform that is mapped in OSM as an area gains a routable
  **platform walk line** in the pedestrian routing graph, running along
  the platform's long axis.
- The walk line follows the platform's shape. A platform that curves must
  produce a curved walk line; a straight rectangular platform must
  produce a straight one. The line must stay inside the platform
  footprint over its whole length, and must not wander laterally where
  the footprint merely changes width — a stair opening or a widened head
  is not a bend in the platform.
- Platforms already mapped as open ways are used directly and are not
  re-synthesised.
- Walk lines carry the level of the platform they were derived from.
- Walk lines are marked as synthetic so they are distinguishable from
  surveyed OSM geometry at any later stage. The marker tags introduced by
  this concept are `kora:platform_walk` for the walk line and
  `kora:platform_link` for the connectors below.

### Level-aware welding

- Each platform walk line is connected to the existing pedestrian
  network at every point where a real pedestrian way (stairs, ramp,
  footway, corridor, lift) meets that platform.
- **A connection may only be made between geometry on compatible
  levels.** Level compatibility is decided from the OSM `level` tag,
  falling back to `layer`. A way whose level set does not intersect the
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

### Quay anchors

- Every GTFS quay served by rail at a Swiss station is anchored onto the
  walk line of its own platform. The anchor is the point on that walk
  line nearest the quay's published coordinate.
- Platform identity is matched first by platform designation (the GTFS
  platform code against the OSM platform reference, which may name
  several tracks at once), and only where that fails by proximity.
- Anchors are consumed by the routing backend only. Map rendering keeps
  using the published GTFS coordinates and must be bit-identical before
  and after this change.
- Anchors replace, and are strictly preferred over, the existing
  platform-code snap. The existing snap remains as a lower tier for stops
  this concept does not cover (notably non-rail platforms).
- Each anchor records which tier produced it. The tier values introduced
  are `centerline_ref` (platform designation matched), `centerline_near`
  (proximity matched), `platform_snap` (pre-existing tier) and
  `unanchored`.

### Unanchored quays and residual gaps

- A quay that cannot be anchored keeps its published coordinate. No quay
  may be dropped or moved to a different platform to force a match.
- Wherever a computed walking leg does not physically reach its
  endpoint — an unanchored quay, or any snap that lands short — the
  remaining straight-line gap is charged as walking time at a speed
  **below** normal walking pace, on the assumption that an unmodelled
  gap is more likely to contain stairs or a detour than a clear straight
  run. The penalty must be proportionate: a few seconds for a few
  metres, never a dominant term.
- The gap distance is added to the leg's reported distance, and the gap
  is drawn as part of the walking line so the user sees an unbroken path.
- The reduced speed is a single named constant, not a per-case value.

### Lifts

- Lifts mapped as ways or areas must become routable. Today only
  node-mapped lifts reach the graph, so at stations where the lift is
  drawn as a shaft the step-free path does not exist at all.
- Lift traversal must remain distinguishable from ordinary footway
  traversal, so a later step-free mode can price or prefer it. The
  marker tag introduced for this is `kora:elevator`.
- A lift must not become a free vertical shortcut. Whatever carries the
  connection has to be something the router already prices as a lift,
  or an able walker's route will prefer it over the stairs beside it.

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
  never pass through a hole, and never leave the area's outline.
- This requirement is as much about what is drawn as about timing. A walk
  line that cuts through a building is wrong on the map before it is
  wrong in the schedule, and it stays wrong under any future aerial
  imagery. Because the drawn line is the routed geometry, one mechanism
  has to serve both.
- Entry points are the area's boundary nodes shared with other walkable
  ways, subject to the same level compatibility rule as platform welds.
- An area with fewer than two usable entry points contributes nothing and
  is skipped rather than connected to something arbitrary.
- The marker tag introduced for these crossings is `kora:area_cross`.

### Coverage and diagnostics

- The work produces a coverage record listing, per station, how many
  quays were anchored and by which tier, which platforms produced a walk
  line with no level-compatible connection, and how many pedestrian areas
  were crossed, skipped for want of entry points, or had crossings
  rejected as obstructed. This is the artefact
  used to judge whether a station is modelled well enough to answer a
  step-free query.
- Known coverage as measured on the current data: 98% of Swiss rail quay
  positions lie within 10 m of mapped platform geometry, and 117 of 118
  stations with four or more distinct quay positions are fully covered.
  The residual is dominated by small narrow-gauge halts with no mapped
  platform body and by stations outside the Swiss extract.

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
- Transfer times will get longer at large interchanges. That is the
  intended correction, and any transfer-safety thresholds tuned against
  the old optimistic values need re-checking afterwards.
