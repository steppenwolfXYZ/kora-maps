# Routing Options

Walking speed, connection safety, minimize-walking, and step-free mode
for the connection search — plus the "more options" UI that hosts them.

## Problem

The routing search assumes one fixed pedestrian: 5.1 km/h, standard
transfer feasibility, one fixed ranking weighting. Real users differ —
elderly or people on crutches walk at 2 km/h, runners make connections
the router rejects, wheelchair and stroller users cannot take stairs at
all, and some users would always trade a few minutes of travel time for
less walking. None of this is expressible today, and tight transfers
are shown without any warning about how tight they actually are.

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
- Reckless requires the routing core to accept negative transfer
  slack (−60 s). This is the one backend-risky piece and is
  **separately shippable**: the other three modes must not depend on it.
  Until it ships, the ruler shows only three stops.
- Safety modes never suppress warnings — Balanced still shows the
  tight-connection warnings below.

### 3. Connection warnings (client-side)

Per transfer, compute spare time = (next departure − arrival at stop)
− walking time at the set speed. Four warning levels:

| Warning | Condition |
|---|---|
| Tight | less than 2 min to spare (but ≥ 20 s) |
| Very tight | less than 20 s to spare, down to needing up to 20 % faster walking |
| Extremely tight | needs 20–50 % faster walking |
| If you're lucky | needs more than 50 % faster walking, or is outright infeasible (Reckless connections) — visually distinct from the tight ladder |

- Each affected transfer is marked in the itinerary detail; the
  connection card carries the worst warning among its transfers.
- Warnings are pure client-side math from leg times — no backend
  involvement.

**Exceptions** (transfers that look tight but aren't):

- **Timed feeders (train → bus/tram/regional bus, tram → bus):**
  these transfers are typically Anschluss-timed in CH — the receiving
  vehicle waits for a late feeder. The tight ladder is suppressed as
  long as the spare at the set walking speed is ≥ 0 seconds; a
  negative spare (physically unmakeable walk) still warns with the
  normal ladder, and the "if you're lucky" tier is unaffected.
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
  | ≤ 5 min | +0 | +0 |
  | ≤ 10 min | +1 | +2 |
  | ≤ 20 min | +2 | +3 |
  | ≤ 40 min | +4 | +6 |
  | > 40 min | +8 | +6 |

  Rationale: 0–5 min walks are fine; avoiding a 5–10 min walk is
  worth an extra transfer; the 10–30 min band has the most potential
  and is priced steepest relative to boardings. No extra class above
  40 min: long walks are not this mode's search concern — wide-budget
  candidates with long walks may exist (see Escalation below), and
  demoting them is the client ranking's job.

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

- The walking cost is **not soft-capped** in this mode (full linear
  rate at any length) — discounting long walking is exactly what the
  mode must not do; capped costs made walk-heavy-vs-low-walk prunes
  hover at the allowance boundary, so near-identical connections fell
  on opposite sides of it.
- The non-overlapping dominance rule (Case 2) becomes
  **direction-blind**: the score-vs-allowance test applies regardless
  of which connection is faster, so a much-lower-walk connection can
  displace a faster walk-heavy one. The reverse direction (slower
  displaces faster) carries a **hard ceiling of 3 hours** on the
  primary axis: a low-walk alternative further away than that never
  displaces the only fast option — the cube-root allowance alone
  cannot provide this bound once walk costs are uncapped.
  Pareto-dominating pairs stay in the overlapping rule (Case 1), so
  mutual drops are impossible.
- Badges and auto-select use an **additive effective time**
  (duration + penalty score) instead of the multiplicative comfort
  factor — the multiplicative walking malus saturates, letting a few
  minutes of duration outvote a larger walking difference between two
  walk-heavy options. Auto-select picks the effective-time best
  (crown) connection instead of the chronological edge.

**Suppression rule while active:**

- Direct walk itineraries with more than 30 minutes of walking are
  never shown.

Related: `walking-optimized-routing.md` is not a prerequisite — try
the re-weighting on its own first, and implement walking-optimized
candidate generation afterwards if the re-ranked results aren't
walking-friendly enough.

### 5. Step-free mode (wheelchair / stroller)

One toggle ("Step-free"). It **composes with** walking speed and
safety — it does not replace them (electric vs. hand-driven wheelchairs
differ wildly in speed; you can run with a stroller).

- Pedestrian routing avoids stairs entirely and adds a fixed time
  penalty per elevator use. Live calls switch to the wheelchair-style
  costing; the plan request reuses the existing `pedestrianProfile`
  parameter.
- Transfers need a **second precomputed footpath matrix** built with
  the step-free costing; the import loads both and query time selects
  by profile. Separately shippable — until the matrix ships, the
  toggle is hidden.

### 6. UI: "more options" expander

- A "more options" button sits to the right of the Leave-at /
  Arrive-by toggle and expands the connection-search input area to
  reveal: walking-speed ruler, Minimize-walking checkbox, safety
  ruler, Step-free toggle.
- Ruler controls: a draggable handle that snaps to discrete stops;
  the description text below the ruler updates live while dragging.
- When the area is collapsed and any setting differs from its
  default, the button shows an indicator.
- Settings persist in localStorage under a single key
  (`kora_routing_prefs`) once the user changes anything; defaults are
  Normal / Balanced / both toggles off.
- Non-default settings also ride in the routing URL (`walk`, `safety`,
  `minWalk` — see `transit-routing.md` § Deep link), so a shared link
  reproduces the results. Restoring from a URL applies them
  session-only, never into the recipient's localStorage.
- Labels English only; i18n is out of scope.

## Constraints

- Default state (Normal, Balanced, no toggles) must produce
  byte-identical queries and results to today — no regression for
  users who never open the options.
- Transfer scaling is linear on the matrix durations. In step-free
  mode this also scales the elevator share of a transfer, which does
  not really get faster when you walk faster — accepted approximation,
  since matrix durations are opaque single numbers.
- Warning math and safety feasibility must use the same walking-speed
  value the backend used, or warnings will contradict the results.
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
- The standard table's `> 40 min` class ships as +9 in the current
  code; the intended value is +8 (typo). Correcting it changes default
  routing behavior slightly and is a deliberate, separate decision.
