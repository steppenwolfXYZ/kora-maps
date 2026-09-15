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
- **fine** (the plateau) — low-traffic streets, no-through-traffic
  streets, painted bike lanes in 30 km/h zones. All approximately
  equal cost; none may meaningfully outweigh another. Among fine
  options, shorter/faster wins.
- **through roads with paint** — a painted bike lane or a sharrow on a
  through-traffic road at 50 km/h or more sits slightly below the
  plateau, not on it: a painted lane a touch worse than a quiet street
  (about 1.05), a sharrow — a bike pictogram in the car lane itself,
  `cycleway=shared_lane`, no lane of one's own — clearly worse (about
  1.15) but still better than no infrastructure at all. Each lane per
  direction beyond the first adds a further step (about 0.2): a
  multi-lane street with paint is not a quiet street with paint. Bus
  lanes do not count (see the crossing rule). Faster roads scale these
  factors up along the same speed curve as the bare tier. (Previously
  every painted or shared lane sat on the plateau, which let the
  five-lane Laupenstrasse with its sharrow tie with the Mühlematt
  quiet-street corridor — the second Bern benchmark case.)
- **bad** — through-traffic roads without bike infrastructure, priced
  by their speed limit rather than their road class: Swiss city roads
  are never extremely dangerous for bikes. 30 km/h zones carry no
  penalty at all whatever the class; 50 km/h a noticeable penalty
  (about 1.4); 60 km/h more; 80 km/h the full bad-road factor — strong
  enough to avoid when an alternative exists, not so strong that
  absurd detours win. Each lane per direction beyond the first adds
  the same step as on painted roads. Bus lanes never count as lanes:
  the OSM preprocessing subtracts bus/PSV lanes from the lane tags
  before the tile build, since riding beside a bus lane is safer, not
  more dangerous. The speed that prices a road is the POSTED limit
  (the `maxspeed` the tiles keep alongside the edge), never the edge's
  own travel speed: the tile build replaces that with a density-inferred
  default (23 km/h on an urban primary), which read every city through
  road as a 30 zone and silently switched this tier off until
  2026-09-14. Untagged through roads assume the Swiss in-town 50 unless
  the inferred speed says the road is faster.
- **Service roads** (`highway=service`: bus-only links, depot and
  parking aisles, driveways) are ridable and carry only a small
  per-metre surcharge (about 1.2 on riding time), enough that the
  search does not wander through a depot by accident, never enough to
  cost a route a 25 m link. There is deliberately NO flat entry fee:
  the engine's stock 15 s fee on entering a service road was inherited
  unnoticed and made a short bus-only link between Hirschengraben and
  Bubenbergplatz decide the Bern Eichmattweg → Aarbergergasse case.

Additional signals:

- **Turn cost:** every real direction change costs a few seconds of
  time and cost — tight turns force braking, and a route with many
  turns is harder to navigate. Left turns cost more than right (they
  cross traffic); roundabout circulation is exempt. Keeps zigzag mazes
  through quiet grids from tying with a straight corridor of equal
  length.
- **Deviation penalty (cost only):** leaving a road that visibly goes
  on adds navigation load even when the turn is gentle. The intuitive
  continuation is found by road category and geometry, two stages: an
  edge of the same class going roughly straight, else an edge within
  one class going exactly straight. Deviating while a continuation
  exists costs a few seconds of pure cost; none exists (T-junctions,
  ends, unresolved forks) → free. Right of way proper is not in the
  graph, and name continuity is actively wrong (side branches share
  names; the natural flow changes name while the name turns and
  yields — Mühlemattstrasse / Philosophenweg is the canonical case),
  so category + geometry is the deliberate proxy.
- **Crossing penalty:** entering a through-traffic road at a real
  crossing costs extra. The junction decides, never the road being
  left: a cyclist arriving from a quiet street, a cycle track beside
  the main road, or a footway crosses the same carriageway as one
  arriving on the main road (canonical: Tscharnerstrasse → Eigerplatz,
  and the Effingerstrasse cycle track → Seilerstrasse, both of which
  the original both-roads-through rule let pass for free). A real
  crossing has four or more through-class arms at the junction; the
  penalty scales with the widest through arm's lane count per
  direction: a small base for a single-lane crossing, a strong step
  per additional lane — every further lane is what makes a crossing
  genuinely hostile. Bus lanes do not count: the OSM preprocessing
  subtracts bus/PSV lanes from the lane tags before the tile build (a
  bus lane does not make a crossing harder). The turn direction scales
  the penalty: turning across (left) pays it in full; straight on from
  a side arm pays half, since the graph cannot tell whether the
  carriageway is crossed; turning with traffic (right) pays a quarter —
  reduced, not exempt, a big junction is unpleasant whichever way you
  turn. A T-junction (three through arms) is free when its widest arm
  has one lane per direction (canonical: Simmentalstrasse →
  Frutigenstrasse in Spiezwiler) and pays three quarters of the
  crossing penalty when it has two or more. Roundabouts stay exempt —
  a Kreisel is the safe way across a big road, not a crossing to
  avoid. For straight-ahead passage along a through road, a traffic
  signal at the node may serve as the proxy for "a real crossing of
  two big roads".
- **Official bicycle routes** (OSM cycle-route relations) are
  slightly favored: membership gives an edge a small bonus in the
  same spirit as the *great* tier — enough to tip the balance between
  otherwise comparable options, never enough to win meaningful
  detours. The signal starts from the existing per-edge membership
  flag (see § Problem). If the model needs more than that bit —
  network level (national / regional / local) or route identity — the
  graph-build change is part of the same fork, and it is the one
  deliberate exception to "no tile rebuild".
- **Destination-only streets** (`motor_vehicle=destination`) carry no
  penalty for bicycles — the restriction does not apply to bikes, and
  these are precisely the quiet streets of the *fine* plateau. (The
  engine's stock default priced them like a ~3 km detour, which chased
  routes out of entire quiet quarters — the Bern benchmark's Mühlematt
  quarter is the canonical case.)
- **Ferries and car shuttles** (Lötschberg / Furka / Vereina
  Autoverlad; all carry bikes) are usable and priced identically: time
  on board from the service's own speed, a flat expected boarding wait
  (30 min), and a deliberately high per-kilometre on-board cost — every
  kilometre aboard must be bought by saving several kilometres of
  riding. That encodes the sporting rule: a crossing wins only where
  the land alternative is disproportionately worse (the roadless
  Lötschberg hop, a short lake crossing saving a huge detour), never as
  a shortcut past ridable ground (the through-shuttle to Iselle under
  the Simplon pass, a long lake cruise beside a shore road). Whenever a
  crossing wins anyway, the client offers per-crossing land variants,
  judged by distance rather than time and preferred outright when the
  detour is modest (bands in the main concept's § Query &
  alternatives) — so riding stays the sporting default and the rider
  makes the call. The stock engine
  effectively banned car shuttles for bikes (a six-hour penalty from an
  unparsed preference) and priced the crossing as pedaling a fake
  alpine grade — which is how Bern→Valais routes ended up over Grimsel
  instead of through the Lötschberg.
- **Alpine terrain** (the international OSM `sac_scale`, T1–T6, baked
  per edge): T1 is fine, even ridable, no penalty. T2 carries a very
  hard cost factor — riding or pushing there is a last resort. T3 and
  above refuse the bike outright, ridden or pushed. This is the guard
  grade limits provably cannot replace: the elevation model smears
  cliffs, and upstream flattens bridge-tagged edges — the Gabi
  Klettersteig (T5) read as a 3 % grade. Additionally, an impassable
  SURFACE (the graph's single surface attribute, worst of OSM surface
  and smoothness) refuses pushing — the pushed branch's surface waiver
  is right for gravel and wrong for rock.
- **Hills — honest time, not avoidance.** The primary hill mechanism
  is a realistic grade→speed curve for an everyday utility rider at
  constant comfortable power: speed halves around a 3 % climb (not the
  athletic 10 % the stock engine assumes) and reaches walking pace
  near 10 %; descents are capped by city braking. With time priced
  honestly, altitude avoids itself and no separate hill-avoidance
  weight is needed. An extra discomfort penalty exists only in pushing
  territory (≥ ~10 %, where most everyday cyclists dismount) and on
  treacherous descents. An e-bike mode (later: own mode, probably a
  slider — modern e-bikes climb nearly without slowing) will select a
  flatter curve; the current curve is the muscle-bike profile.
- **Grade cap on through roads (elevation-artifact fallback).** The
  DEM samples the structures a road passes under, so underpasses carry
  fake 10-15 % spikes (canonical case: Schwarzenburgstrasse under the
  rail line at Weissenstein — a level ride that read as a mountain and
  bought an 840 m detour). Engineered through roads are never that
  steep in a city, so their grade is capped at ~6.5 %; small streets
  keep their full grades — steep lanes are real, even in cities. This
  is an interim guard: the correct fix — endpoint-interpolated
  elevation for under-passing (`layer<0`) ways at graph build, the
  same treatment bridges and tunnels already get — is queued for the
  next tile rebuild, and the cap stays as the fallback thereafter.
  Known interim cost: sustained alpine climbs on primary roads read a
  touch too fast.
- **Stairs:** two honest components, mostly time. Hauling pace: a
  per-metre time rate, uphill far worse than down. Committing fees at
  length checkpoints: below 2 m a stair is trivial (lift the bike
  over, nothing extra); from 2 m the carry must be figured out, from
  4 m it is real hauling — each checkpoint fee counts once as time and
  once more as cost, downward fees half the upward ones. A real
  staircase stays a last resort through sheer honest slowness. A
  costing option excludes them entirely — this backs the V1-mandatory
  avoid-stairs toggle.
- **Turn restrictions** bind a bicycle only where the maneuver crosses
  traffic that matters. A bike can always dismount, so a restriction
  never forbids a movement outright — it can only force a push around
  the corner, which is what a wrongly mapped `no_right_turn` produced
  in Heimberg (a 17 m sidewalk loop to dodge a sign that applies to
  nobody; OSRM, Google and Apple all turn right there). With-traffic
  turns are therefore never restricted, and any other restricted turn
  is obeyed only when a road posted above 30 km/h meets at the junction
  (for a left or U-turn every road at the node counts, since oncoming
  traffic is crossed either way; straight on, only the intersecting
  roads). Paths, cycleways, 30-zone streets and untagged quiet streets
  do not count. This holds for all-mode restrictions and for
  mode-specific ones alike (`except=psv`, the Heimberg case, is the
  latter). Via-way restrictions (multi-edge, in practice U-turn bans on
  dual carriageways) are ignored for bikes altogether: the engine
  evaluates them outside the costing, and a bike can dismount anyway.
- **Pushed-bike access** — a core requirement, not a follow-up: without
  it whole neighbourhoods route nonsensically (the Bern benchmark's
  Zieglerstrasse crossing is the canonical case). Any edge that is
  walkable but not ridable is traversable by pushing the bike: foot-only
  ways (sidewalks, crossings, pedestrian zones) and streets that are
  oneway against the direction of travel. Pushing happens at walking
  pace; SHORT pushes cost nothing beyond their honest time — they are
  often genuinely the best move — while pushed metres beyond a small
  free allowance (~20 m) accrue a per-metre penalty, so it is LONG
  pushes that get discouraged (on climbs riding is barely faster than
  pushing, and without the length rule footpath shortcuts crept into
  hilly routes). The allowance counts per push SECTION — contiguous
  pushed edges uninterrupted by riding — never per edge: the graph
  chops footways into fragments, and a per-edge allowance made long
  pushes free. Pushed sections are reported to the
  client as walking-mode segments; the map draws them dotted (the same
  visual language as walking legs). The connection cards do not mention
  them. Implemented at query time — access to the walkable graph is
  already in the tiles, so this forces no tile rebuild.
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

**Recorded 2026-09-03** against the pinned upstream 3.8.3 image, hybrid
bike, hill avoidance 0.1, Eichmattweg 7 (7.4301984, 46.9406065) →
Viktoriastrasse (7.45191, 46.9544477), with the road-preference knob
at 0, 0.25 and 1:

- Primary, identical for all three knob values: 3.17 km / 11 min via
  Tscharnerstrasse › Schwarztorstrasse › Belpstrasse › Bahnhofplatz ›
  Spitalgasse › Waisenhausplatz › Kornhausplatz › Viktoriaplatz.
- Alternates: Eigerstrasse › Aegertenstrasse › Casinoplatz ›
  Theaterplatz (3.3 km), and a 4.8 km outlier over Thunplatz.
- Neither Mühlematt nor Monbijou appears in any returned route. The
  knob does not move the case; the § Problem claims stand on 3.8.3.

### Benchmark set

- A curated, versioned set of origin–destination pairs, each with a
  short description of the expected corridor and why (the first
  entry: Bern Eichmattweg → Viktoriastrasse via the
  Mühlematt/Monbijou corridor).
- Every tuning iteration runs against the full set; a change ships
  only if no pair regresses.
- Every bad route discovered in hand-testing is added as a new pair
  before it is fixed. Second Bern pair: Eichmattweg → Aarbergergasse,
  where the Mühlematt corridor must win over Belpstrasse › Seilerstrasse
  › Laupenstrasse; before the junction-based crossing rule, the painted-
  road factors and the service-road change, the two tied within 1 %
  and the alternates list still carried the Zieglerstrasse route at
  30 % worse cost.

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
