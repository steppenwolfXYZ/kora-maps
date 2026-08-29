# Routing Search Preferences

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

### 4. Minimize walking

A checkbox below the walking-speed ruler ("Minimize walking"). When
active, the result ranking shifts its relative importance from today's
roughly timing 80 % / transfers 10 % / walking 10 % to
**timing 40 % / transfers 10 % / walking 50 %** — walking becomes the
dominant cost after feasibility. (The ranking is penalty-based, not
literal percentages; the requirement is the relative-importance shift,
mapped onto the existing penalty constants.)

Prerequisite: `walking-optimized-routing.md` must be implemented
first — re-weighting the ranking is only meaningful once the search
actually produces walking-optimized candidates to rank.

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
