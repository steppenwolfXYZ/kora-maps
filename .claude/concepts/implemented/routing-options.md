# Routing Options

Walking speed, connection safety, minimize-walking, and stroller mode
for the connection search — plus the "more options" UI that hosts them.

## Problem

The routing search assumes one fixed pedestrian: 5.1 km/h, standard
transfer feasibility, one fixed ranking weighting. Real users differ —
elderly or people on crutches walk at 2 km/h, runners make connections
the router rejects, wheelchair and stroller users cannot take stairs at
all, and some users would always trade a few minutes of travel time for
less walking. None of this is expressible today, and tight transfers
are shown without any warning about how tight they actually are.

## Implementation status

Shipped: the five walking-speed tiers, the Cautious / Balanced / Daring
safety modes, the connection-warning ladder including both exceptions,
minimize walking (client ranking + server-side candidate generation),
and the "more options" UI with persistence and URL round-trip.

Open:

- **Reckless** (§ 2, safety stop 4) — **deferred** (decided 2026-09-18).
  The ruler ships with three stops; still needs the routing core to
  accept negative transfer slack. Not scheduled.
- **Stroller mode** (§ 5) — implemented 2026-09-18 (replaces the earlier
  step-free / wheelchair plan; a wheelchair tier is deferred). Needs the
  second, stroller-costed footpath matrix in every deployment.
- ~~The `> 40 min` standard walk-point class~~ — resolved 2026-09-18
  by the finer ladder in § 4 (both tables now top out at +6).

## Requirements

### 1. Walking speed (5 tiers)

A ruler-style control with five stops. The selected speed applies to
**everything pedestrian**: first/last-mile walk legs and boarding-stop
offsets, stop-to-stop transfer times, pure walk itineraries, and all
client-side warning math.

| Tier | Label | Speed | Audience |
|---|---|---|---|
| 1 | Slow | 2 km/h | elderly, crutches |
| 2 | Leisurely | 4 km/h | "gemütlich" |
| 3 | Normal | 5.1 km/h | current default |
| 4 | Brisk | 7.5 km/h | fast walker |
| 5 | Running | 11 km/h | runner |

- Live pedestrian-router calls (walk legs, offsets) must use the
  selected speed. The plan request carries a new `walkingSpeed`
  parameter (km/h); absent = 5.1 (today's behavior, byte-identical).
- Transfer times come from the precomputed matrix; they are scaled at
  query time by `5.1 / selected` (routes are near-shortest-path, so
  linear scaling is an accepted approximation). The scaling rides on
  the existing transfer-time-factor mechanism and composes
  multiplicatively with the Daring safety mode below.

### 2. Connection safety (4 modes)

A second ruler-style control with four stops. All feasibility math is
based on the **selected walking speed**.

| Mode | Label | Rule |
|---|---|---|
| 1 | Cautious | no connection with less than 5 min to spare, walking included |
| 2 | Balanced | default feasibility (spare ≥ 0 at set speed) |
| 3 | Daring | transfers computed at 2× the set walking speed — surfaces connections needing up to double speed |
| 4 | Reckless | additionally allows connections up to 1 min infeasible ("if you're lucky") |

- Cautious maps to +5 min additional transfer time; Daring maps to a
  0.5 transfer-time factor (both already supported by the routing API).
- **Safety modes never change a walking time shown in the UI.** Walking
  time is a function of the walking speed alone — the same transfer at
  the same station must read identically in Cautious, Balanced and
  Daring, in the leg rows, the strip, the walked total and the
  ranking. The safety factor is a *search* knob: it decides which
  connections are offered, never how long a walk is said to take. The
  tight-transfer math in § 3 follows the same rule — the walk is
  always measured at the set speed.
- **Daring never produces a zero-minute transfer.** Alighting and
  boarding at the same instant is Reckless (mode 4) by definition;
  Daring may demand a sprint, but the traveller always keeps at least
  one minute. Every query whose transfer-time factor drops below 1 —
  Daring, and the Brisk / Running walking tiers on their own — carries
  a one-minute floor on the transfer time. This is the query-side half
  of the guarantee; the transfer table carries its own floor at import
  (`transfer-point-optimization.md` § Minimum transfer time), and the
  query floor catches what halving that value would truncate away.
- **Walking times are never rounded down.** A walk shorter than a
  minute reads `<1 min`; `0 min` must never surface for a walk that
  covers distance.
- **Reckless is not implemented yet.** It requires the routing core to
  accept negative transfer slack (−60 s). This is the one backend-risky piece and is
  **separately shippable**: the other three modes must not depend on it.
  Until it ships, the ruler shows only three stops.
- Safety modes never suppress warnings — Balanced still shows the
  tight-connection warnings below.

### 3. Connection warnings (client-side)

Per transfer, compute spare time = (next departure − arrival at stop)
− walking time at the set speed. Four warning levels:

| Warning | Condition | Wording |
|---|---|---|
| Tight | less than 20 s to spare (but still makeable) | ≥ 30 s: "~1 min to spare"; below that: "no time to spare" |
| Very tight | spare below −5 s, needing up to 20 % faster walking | "you need to run" |
| Extremely tight | needs 20–50 % faster walking | "you may not make it" |
| If you're lucky | needs more than 50 % faster walking, or is outright infeasible (Reckless connections) — visually distinct from the tight ladder | "only if you are lucky" |

**No seconds in the UI.** Walking times are not second-accurate, so
neither the transfer chip nor the tooltip ever states a spare figure —
the tier, plus the one band boundary inside Tight, carries the message.
The "~1 min to spare" band is defined up to 90 s and so only shows
below the 20 s tight threshold today; it exists so the wording survives
a future widening of the warning window.

**Rounding guard.** Walk-leg durations arrive as whole seconds while the
schedule window is exact, so a walk that exactly fills its window can
report a spare of −1 s. A spare down to −5 s therefore still counts as
makeable ("no time to spare") and never escalates a tier; the same
tolerance applies to the timed-feeder exception below.

The ladder is pitched so the tier reads as a safety-mode signal.
Balanced only returns transfers the set speed makes, so it can reach
**Tight and nothing above** — and only inside the last 20 s of margin,
which makes a warning there the exception. Every higher tier requires a
meaningfully negative spare, which only Daring's halved transfer times
produce, and Cautious (spare ≥ 5 min by construction) never warns at
all. Accepted loss: a 20 s–2 min buffer in Balanced gets no heads-up
even though a delayed feeder would break it.

Thresholds are calibrated on Switzerland, where a positive spare on the
Valhalla matrix genuinely means makeable. Other countries will need
their own values.

- Each affected transfer is marked in the itinerary detail; the
  connection card carries the worst warning among its transfers.
- Warnings are pure client-side math from leg times — no backend
  involvement.

**Exceptions** (transfers that look tight but aren't):

- **Timed feeders (train → bus/tram/regional bus, tram → bus):**
  these transfers are typically Anschluss-timed in CH — the receiving
  vehicle waits for a late feeder. The tight ladder is suppressed as
  long as the spare at the set walking speed is ≥ −5 seconds (the
  rounding guard above); a spare below that (physically unmakeable
  walk) still warns with the normal ladder, and the "if you're lucky"
  tier is unaffected.
  tram → bus is an interim blanket rule (city buses don't actually
  wait for city trams); the per-line/per-station refinement is
  planned — see `regio-tram-timed-transfers.md`. tram → tram is not
  exempt. Accepted trade-off: genuinely tight transfers at large
  city stations (where nothing waits) also lose their warning until
  that refinement lands.
- **Continuous gondolas:** mountain routes whose GTFS service is
  frequencies-based with short headways (≤ 5 min) run continuously —
  their per-minute timetable departures are an artifact, and missing
  one just means taking the next. The pipeline flags such routes as
  `hf_gondolas` inside `route_color_index.json` (file shape becomes
  `{ colors, hf_gondolas }`); boarding a flagged route never produces
  any tight-transfer warning. Scheduled (rare-departure) gondolas are
  not flagged and warn normally.

### 4. Minimize walking

A toggle below the rulers ("Minimize walking").

**Goal.** The target is the 10–30 minute walk band: prevent
connections that walk 10–30 minutes when an option with less walking
exists — even when that option is clearly slower. Canonical cases: a
detour bus to a different train station beats walking all the way to
the train; a worse-timed connection from/to a closer bus stop beats a
long walk to the better-timed one. Multi-hour walking marathons are
NOT the focus — they simply must not be offered while the toggle is
on, but the search does not need to reason about them.

**Client-side ranking** (unchanged from the original requirement):
the result ranking shifts its relative importance from today's roughly
timing 80 % / transfers 10 % / walking 10 % to
**timing 40 % / transfers 10 % / walking 50 %** — walking becomes the
dominant cost after feasibility. (The ranking is penalty-based, not
literal percentages; the requirement is the relative-importance shift,
mapped onto the existing penalty constants.)

**Server-side candidate generation.** Re-ranking can only choose among
what the server returns, and a walking-light connection that is slower
is often Pareto-dominated and never emitted. Therefore, when the
toggle is active:

- The plan request carries a new fork-only parameter
  `koraWalkPoints=minwalk`. It switches the walk-weighted transfer
  points (the fork's second RAPTOR Pareto criterion) to a steeper
  per-query class table, so walking-light journeys survive as their
  own Pareto points:

  | walk | standard | minwalk |
  |---|---|---|
  | ≤ 3 min | +0 | +0 |
  | ≤ 5 min | +0 | +1 |
  | ≤ 10 min | +1 | +2 |
  | ≤ 20 min | +2 | +3 |
  | ≤ 30 min | +3 | +4 |
  | ≤ 40 min | +4 | +5 |
  | ≤ 50 min | +5 | +6 |
  | > 50 min | +6 | +6 |

  (Ladder refined 2026-09-18; the original shipped as 0/1/2/4/9 vs
  0/2/3/6/6 with class edges at 5/10/20/40 min.) Rationale: one point
  per ~10 min of walking, minwalk one class ahead of standard —
  avoiding a 5–10 min walk is worth an extra transfer, and the
  10–30 min band is where minimize walking has the most potential.
  Class boundaries cost nothing (a walk's price is the level it lands
  on, not the number of rows), so the ladder is fine-grained up to
  50 min and flat above: long walks are not this mode's search
  concern — wide-budget candidates with long walks may exist (see
  Escalation below), and demoting them is the client ranking's job.
  The flat top (+6 instead of the former +9) also caps the highest
  level a walk-heavy query can reach, so RAPTOR runs fewer rounds.

  The table applies everywhere the standard one does: transfer walks,
  access/egress seeds, reconstruction, alternates pricing.
- The ε-alternates knobs widen from 540 s / 3 to **900 s / 5**, so
  more low-walk endpoint variants (closer stop, slightly later
  arrival) come back for the ranking to choose from.

**Escalation.** The wide walking-budget escalation applies normally
under this toggle. (An earlier version of this concept suppressed it;
that was wrong — on rural routes the low-walk connections themselves
only exist in the wide candidate set, so minimize-walking NEEDS wide
as a candidate source. Selecting low-walk options among the
candidates is the ranking's job, below.)

**Ranking (client), beyond the weight shift:**

- *Superseded by the Case 2 rework (`transit-routing.md` § Ranking):*
  the uncapped ×4 walking cost and the comfort score it fed are gone.
  Case 2 now compares the mode's effective time (additive under this
  option, `comfort-walk-baseline.md`), is direction-blind in every
  mode, and bounds displacement by concurrency instead of the former
  3-hour ceiling on the primary axis.
- The overlapping dominance rule (Case 1) gains a **walking
  exception**: when the Pareto-dominated connection walks meaningfully
  less (> 60 s) than its dominator, the 9-minute marginality window is
  skipped and the comfort test alone decides its fate. Case 1's time
  window is a pure time argument, and applying it unconditionally
  deleted exactly the connections this mode exists to surface — a
  low-walk option arriving at the same minute as a walk-heavier one
  that departs a few minutes later. Mutual drops stay impossible: the
  exception only widens who reaches the comfort test, and Pareto
  dominance still runs in one direction only.
- The same-route-minus-a-vehicle prune (Rule 0) gains a **marginal-
  saving extension**: a variant riding a subset of another's vehicles
  (walked instead of ridden) normally survives by being faster, but
  under this mode a marginal saving must not buy meaningful extra
  walking — it survives only when the time saved is at least **3×**
  the extra walking (> 60 s extra to engage at all). Fires only when
  the subset side is equal-or-better on both time endpoints, so only
  the subset side can drop and mutual drops stay impossible. Canonical
  case: same train to Bern, then a 15-min walk arriving 2 min before
  the tram variant with 6 min walking — the 2-min saving does not
  justify 9 min more walking.
- Badges use an **additive effective time** (duration + penalty score)
  instead of the multiplicative comfort factor — the multiplicative
  walking malus saturates, letting a few minutes of duration outvote a
  larger walking difference between two walk-heavy options.
  *Superseded by `comfort-walk-baseline.md`: minimize-walking now
  prices walking in absolute minutes over the query's unavoidable walk
  (concave, uncapped), and that one additive effective time serves
  pruning and badges alike; normal mode keeps a multiplicative factor
  with a linear walk malus.*
- Auto-select is NOT re-weighted: it stays on the chronological edge
  (leave-at first, arrive-by last) in every mode. Picking the crown
  here made the selection unpredictable — after an option change the
  list reloads and the marked connection jumped to an arbitrary
  position.

**Suppression rule while active:**

- Direct walk itineraries with more than 30 minutes of walking are
  never shown.

Related: `walking-optimized-routing.md` is not a prerequisite — try
the re-weighting on its own first, and implement walking-optimized
candidate generation afterwards if the re-ranked results aren't
walking-friendly enough.

### 5. Stroller mode

One toggle ("Stroller"), on the transit tab's more-options area and on
the walking tab's control row. It **composes with** walking speed,
safety and minimize walking — it does not replace them (you can run
with a stroller). The original plan was a wheelchair / step-free mode;
that is deferred: a wheelchair tier needs kerb, gradient and surface
rules of its own and would become a third matrix. Should it come, the
toggle grows into an accessibility ruler (Normal / Stroller /
Wheelchair) rather than a second switch.

**Stairs pricing.** Stairs are not banned — a stroller can be carried —
they are priced by altitude, in three classes. Altitude is derived from
the stair's length at a fixed rise-per-metre (0.5), because the terrain
model is far too coarse to measure a single flight; the classes are
therefore length classes in disguise:

| Stairs | Elapsed time | Search-only penalty | Warning |
|---|---|---|---|
| 1–2 steps (length ≤ 1.5 m) | unchanged | ~3 s | none |
| up to 2 m of rise | +40 s per metre of rise | none | **medium** (level 2) |
| more than 2 m of rise | +80 s per metre of rise | +5 min per metre of rise | **strong** (level 3) |

- "Elapsed time" is real walking time: it reaches the displayed leg
  durations and the transfer matrix. The search-only penalty is cost
  the router minimises but never shows, so a long flight is taken only
  when nothing else connects the two points — and then at honest time.
- The long-stairs elapsed rate is deliberately steeper than the medium
  one (80 vs 40 s/m). The penalty alone decides only the path between
  one stop pair; between journeys RAPTOR compares durations, so a
  transfer whose sole option is a long flight would otherwise compete
  as if it were harmless. Doubling its real time makes stair-free
  journeys elsewhere win where they exist.
- Elevators keep their normal 60 s per ride. Kerbs, surfaces and
  gradients are untouched — those are wheelchair concerns.

**Two matrices.** Transfers in stroller mode come from a **second
precomputed footpath matrix** built with the stroller costing; the
import loads both and the query selects the table by profile
(`koraProfile=stroller`, a fork-only plan parameter). The stroller
matrix is capped at 30 minutes — the same cap as the default foot
table, so query performance is the default's — and the cascade's
escalation (wide walking budget, full 2-hour table) is **dropped
entirely** while the toggle is on: the stroller query runs the narrow
flow only. Station endpoints read the stroller table; coordinate
endpoints and walk legs go to the live router with the stroller
costing. A deployment whose index lacks the stroller table refuses
stroller queries with an error rather than degrading to the foot table.

**Walking tab.** The same toggle applies the stroller costing to the
direct walking routes; a walk route's stairs metres are then shown on
its card.

**Warnings.** Every displayed walk (transit walk leg or direct route)
carries its stairs metres from the router. A stroller-mode connection
gets the stairs warning at the severity of its worst walk per the
table above (medium / strong); short flights never warn. Off stroller
mode stairs stay unremarkable and produce no warning.

### 6. UI: "more options" expander

- A "more options" button sits to the right of the Leave-at /
  Arrive-by toggle and expands the connection-search input area to
  reveal: walking-speed ruler, Minimize-walking checkbox, safety
  ruler, Stroller toggle.
- Ruler controls: a draggable handle that snaps to discrete stops;
  the description text below the ruler updates live while dragging.
- When the area is collapsed and any setting differs from its
  default, the button shows an indicator.
- Settings persist in localStorage under a single key
  (`kora_routing_prefs`) once the user changes anything; defaults are
  Normal / Balanced / both toggles off.
- Non-default settings also ride in the routing URL (`walk`, `safety`,
  `minWalk`, `stroller` — see `transit-routing.md` § Deep link), so a
  shared link reproduces the results. `stroller` is the one option the
  walking tab's links carry too. Restoring from a URL applies them
  session-only, never into the recipient's localStorage.
- Labels English only; i18n is out of scope.

## Constraints

- Default state (Normal, Balanced, no toggles) must produce
  byte-identical queries and results to today — no regression for
  users who never open the options.
- Transfer scaling is linear on the matrix durations. In stroller mode
  this also scales the elevator and stair-carrying share of a transfer,
  which does not really get faster when you walk faster — accepted
  approximation, since matrix durations are opaque single numbers.
- Warning math and safety feasibility must use the same walking-speed
  value the backend used, or warnings will contradict the results.
- The transfer table's one-minute resolution must not leak into the
  UI. A transfer walk's displayed duration comes from the pedestrian
  router's own seconds, never from the gap between the two transit
  legs — that gap is minute-quantised and carries the safety scaling,
  so reading walking time off it makes a sub-minute walk vanish
  entirely once a factor below 1 truncates it to zero.
- Reckless connections are real itineraries the user may miss — the
  "if you're lucky" warning is mandatory on every one of them.
- The browser still makes exactly one request per query; no direct
  Valhalla calls from the client.
- The minwalk point table must stay moderate: RAPTOR's journey cap is
  45 points, and a steeper table consumes it faster — long multi-leg
  journeys with several long walks must remain representable.
- Minimize walking must never fake a different walking speed toward
  the server — that would drop connections a normal-pace walker can
  make and distort every displayed time.
- The walk-point ladder is a compile-time table in the fork; changing
  it changes default routing behavior and needs a fork image rebuild
  (query-time only, no re-import).
- The stroller matrix and the live stroller costing must describe the
  same walker, exactly as the foot matrix and the foot costing do:
  changing the stair constants means rebuilding the stroller matrix.
- Stroller mode composes with every other option; it never fakes a
  walking speed and never changes the foot matrix.
