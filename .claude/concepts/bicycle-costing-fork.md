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
no concept of dangerous crossings, and it cannot exclude stairs. Full
control over the weighting is required, and costing is query-time —
owning it means tuning iterations without rebuilding tiles.

That verification ran on the router as it stood before the September
2026 Valhalla update: the archived gis-ops image, whose build had
stopped moving in 2024. The stack now runs upstream Valhalla 3.8.3,
pinned. Reading the 3.8.3 costing confirms the findings rather than
overturning them — upstream changed nothing bicycle-specific between
those versions beyond surface smoothness — but see § Baseline: the
benchmark case is re-run on 3.8.3 before fork work starts.

Two stock facts worth stating precisely, because the fork builds on
them:

- **Stairs:** stock rides steps at 1 km/h with an 8× cost factor, no
  option excludes them, and up and down cost the same.
- **Cycle-route relations:** the graph already carries a per-edge
  flag for membership in an OSM cycle-route relation (a single bit —
  any network level, no route identity), and stock costing rewards it
  with a 5 % cost reduction that the lane-type weights swamp. So the
  signal is query-time-visible today; it is just far too weak and too
  coarse.

## Requirements

### Ownership & iteration

- The bicycle costing becomes Kora-maintained code in our Valhalla
  build, following the established fork pattern (locally built image,
  pinned upstream version, documented bump procedure).
- **Fork base = the version the tiles were built with.** The fork
  pins upstream Valhalla at exactly the tag the current tiles and
  footpath matrix were produced with (3.8.3 today). The pin is one
  string shared by image, tiles and matrix — there is never a
  situation where the served costing and the served tiles come from
  different upstream versions.
- **Drop-in for the upstream image.** The fork image replaces the
  pinned upstream scripted image in both the local and the production
  router configuration and keeps its environment interface (tile
  build parameters from environment, serve-only mode on the server,
  same container name and shared network). Nothing else in the
  routing stack notices the swap.
- **Adopting the fork forces no rebuild.** Same graph version, same
  tiles: switching to the fork image must not trigger a tile rebuild
  or a footpath-matrix rebuild. The routing setup's tile-freshness
  guard currently treats *any* change to the router configuration as
  a version bump and wipes the tiles; the image swap must not fire
  it — the guard has to key on the upstream version, not on the
  configuration file changing.
- Changing weights must never require a tile rebuild — rebuild and
  restart of the router only.
- Pedestrian costing and everything the transit stack uses (walk
  legs, offsets, transfer matrix) stay byte-identical — the fork
  touches bicycle only. This now includes the level / elevator
  awareness the update restored (step and elevator penalties, level
  search filter, wheelchair profile), which the old image silently
  lacked: the fork builds from the 3.8.3 baseline, never from older
  source.
- **Deploy channel ships the image.** The Valhalla deploy channel
  today ships data only; with the fork it also ships the image, the
  way the MOTIS channel does (no registry, built for the server's
  arm64). The data machine's amd64 image never ships — the channel
  gets the same software / data-only split as the MOTIS one.
- **Bump procedure** covers three things: re-applying the costing
  overlay against the new upstream, the tile rebuild and the matrix
  rebuild a version bump already implies.
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
  detours. The signal starts from the existing per-edge membership
  flag (see § Problem). If the model needs more than that bit —
  network level (national / regional / local) or route identity — the
  graph-build change is part of the same fork, and it is the one
  deliberate exception to "no tile rebuild".
- **Hills:** the strong hill-avoidance default from the main concept
  is preserved and must compose with the tier model (a flat bad road
  vs. a hilly fine road remains a meaningful trade-off, not an
  override).
- **Stairs:** heavily penalized by default, upward more than
  downward — replacing stock's flat 8× factor. A costing option
  excludes them entirely — this backs the V1-mandatory avoid-stairs
  toggle.
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

### Baseline

- Before any fork change, the Bern benchmark case is re-run on stock
  3.8.3 and its result recorded. That run — not the pre-update one —
  is the baseline every tuning iteration is compared against.
- The same run re-confirms the § Problem claims on the current
  version (knobs don't move the case; stairs not excludable). If a
  claim no longer holds, the concept is corrected before work starts.

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
- One version pin. Bumping upstream Valhalla is a deliberate act
  (tile + matrix rebuild); the fork must not make it more tempting to
  drift, and must never run against tiles of another version.
- Switzerland is the calibration target; penalties assume Swiss road
  design and traffic culture.
- The spike outcome so far: the crossing rule needs only
  query-time-visible data (road classes of the two edges, turn
  direction, signal flag) — if a later requirement exceeds that, any
  graph-build change becomes part of the same fork, not a separate
  system.
