# Bicycle Costing Fork

A Kora-owned bicycle weighting model inside our Valhalla instance,
replacing the stock bicycle costing. Companion to
`pedestrian-bicycle-routing.md` (which owns the UI/feature side and
the originating quality requirements in its § 4a).

## Problem

Stock Valhalla bicycle costing fails the quality bar. Verified on the
Bern benchmark case: it prefers cycle-lane-tagged main roads over a
corridor it itself rates shorter *and* faster, because painted cycle
lanes on big roads outweigh quiet residential streets in its cost
model. No request-level knob changes the outcome (all tested), it has
no concept of dangerous crossings, cannot exclude stairs, and ignores
official cycle-route relations. Full control over the weighting is
required, and costing is query-time — owning it means tuning
iterations without rebuilding tiles.

## Requirements

### Ownership & iteration

- The bicycle costing becomes Kora-maintained code in our Valhalla
  build, following the established fork pattern (locally built image,
  pinned upstream version, documented bump procedure).
- Changing weights must never require a tile rebuild — rebuild and
  restart of the router only.
- Pedestrian costing and everything the transit stack uses (walk
  legs, offsets, transfer matrix) stay byte-identical — the fork
  touches bicycle only.
- The request API stays compatible with the existing client; new
  behavior is exposed as additional costing options, not breaking
  changes.

### Weighting model

Edges are weighted by a three-tier quality model:

- **great** — physically separated cycle infrastructure: separated
  bike lanes, dedicated bike paths (e.g. through a park). Slight
  bonus, deliberately small: it must never justify meaningful
  detours.
- **fine** (the plateau) — painted bike lanes, low-traffic streets,
  no-through-traffic streets. All approximately equal cost; none may
  meaningfully outweigh another. Among fine options, shorter/faster
  wins.
- **bad** — through-traffic roads without bike infrastructure,
  multi-lane roads. Significant penalty, but calibrated for
  Switzerland — strong enough to avoid when an alternative exists,
  not so strong that absurd detours win.

Additional signals:

- **Crossing penalty:** a transition where both roads are
  through-traffic class costs extra; right turns are exempt. For
  straight-ahead passage along a through road, a traffic signal at
  the node may serve as the proxy for "a real crossing of two big
  roads".
- **Official bicycle routes** (OSM cycle-route relations) are
  slightly favored: membership gives an edge a small bonus in the
  same spirit as the *great* tier — enough to tip the balance between
  otherwise comparable options, never enough to win meaningful
  detours. If the needed information is not available at query time,
  making it available is in scope for the fork.
- **Hills:** the strong hill-avoidance default from the main concept
  is preserved and must compose with the tier model (a flat bad road
  vs. a hilly fine road remains a meaningful trade-off, not an
  override).
- **Stairs:** heavily penalized by default, upward more than
  downward. A costing option excludes them entirely — this backs the
  V1-mandatory avoid-stairs toggle.
- **Pushed-bike access** (walking-speed use of foot-only ways) may
  arrive in a follow-up, per the main concept.
- All tunable constants live in one central, documented place so
  tuning stays reviewable and future user-facing preferences
  (hilliness, stairs) map onto them cleanly.

### Later enhancements (very provisional)

Not part of this step, subject to change — noted so the constants are
shaped with them in mind. User-facing ruler settings in the style of
the transit options, each a per-request scaling of existing
constants: fast ↔ calm (strength of the bad-tier and crossing
penalties), hill avoidance, stronger favoring of official bicycle
routes.

### Benchmark set

- A curated, versioned set of origin–destination pairs, each with a
  short description of the expected corridor and why (the first
  entry: Bern Eichmattweg → Viktoriastrasse via the
  Mühlematt/Monbijou corridor).
- Every tuning iteration runs against the full set; a change ships
  only if no pair regresses.
- Every bad route discovered in hand-testing is added as a new pair
  before it is fixed.

### Quality bar

Bicycle routing ships only when it decisively beats Google Maps and
hand-testing consistently produces routes that make sense. The
benchmark set is the evidence trail for that judgment, but the final
call is a human one.

## Constraints

- One engine: no second routing service. The forked costing runs in
  the same Valhalla instance that serves pedestrian routing and the
  transit stack.
- Switzerland is the calibration target; penalties assume Swiss road
  design and traffic culture.
- The spike outcome so far: the crossing rule needs only
  query-time-visible data (road classes of the two edges, turn
  direction, signal flag) — if a later requirement exceeds that, any
  graph-build change becomes part of the same fork, not a separate
  system.
