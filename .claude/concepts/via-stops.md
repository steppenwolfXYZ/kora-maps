# Via Stops with Wait Time

## Problem

The routing panel answers "A → B" only. Two common needs have no expression:
a journey that must pass through a particular place (routing *via* a
corridor, without stopping), and a journey with an errand on the way —
pick something up, drop something off — which needs a deliberate **wait**
at that place. Modelling the errand as a normal transfer is wrong: the
router would optimise the stop away, and every duration-based judgement
in the panel (badges, warnings, ranking) would read the errand time as
wasted travel time.

## Requirements

### Via stops

- A route may carry up to **three** via stops, ordered between From and To.
  The ceiling is the routing engine's `kMaxVias`, raised from its upstream
  value of two to three in the Kora fork; the UI must never offer more
  than the engine's constant allows.
- A via is always a **transit station** — the same station entity the
  From / To inputs already accept. Addresses, POIs and current location
  are not selectable as vias.
- Vias are ordered along the direction of travel (From → via 1 → via 2 →
  To) in both `leave-at` and `arrive-by` mode. The mode does not reverse
  the order the user sees or enters.
- The route must visit every via in the given order. A journey that
  cannot honour the chain yields no results, exactly like any other
  unsatisfiable query — never a silently via-less answer.

### Wait time

- Every via carries a **wait**, in whole minutes, defaulting to **0**.
- The wait is a **minimum**, not a fixed dwell. The router must guarantee
  at least that much time at the via; the actual stay is whatever the
  timetable gives and may be longer.
- `wait = 0` means "pass through" — the traveller may stay on board, and
  no vehicle change is forced. This is the corridor case and must remain
  a single click away.
- `wait > 0` means the traveller leaves the vehicle for at least that
  long. This is the errand case.
- The wait covers everything the traveller does at that place, the walk
  from and back to the platform included. The UI must not imply that the
  walk to the actual errand is modelled — it is not.
- Offered values: 0, 5, 10, 15, 30, 45, 60, 90, 120 minutes, plus a
  custom whole-minute entry. A ceiling applies (see Constraints).

### Planned dwell and time judgement

- Each itinerary carries a **`plannedDwell`** — the sum of the *requested*
  wait minutes across all vias. Deliberately the requested minimum, not
  the realised stay: time the traveller asked for is theirs, but a via
  where the next departure is an hour away costs them real dead time and
  must still be visible to every comparison.
- `plannedDwell` is subtracted from an itinerary's duration before any
  **quality judgement**: the effective-time / comfort factor, the
  worseness ratio behind the badges, and the very-slow warning. The
  ranking filter's time comparisons are unaffected in shape.
- `plannedDwell` is **never** subtracted from anything the user reads as
  a clock or a duration. Departure, arrival, total duration and the
  door-to-door line all include the wait — it is real time away from home.
- Time spent at a via is never counted as walking.
- The **long-wait warning** measures, at a via, only the **excess** over
  the requested wait. A 15-minute stay on a 15-minute request is not a
  warning; a 90-minute stay on the same request is a 75-minute wait.
  At non-via transfers the warning is unchanged.
- The walking-budget escalation signal that watches for long waits uses
  the same excess rule, so a planned two-hour errand does not push every
  query onto the wide budget.
- A vehicle change **forced by a via with `wait > 0`** is not counted in
  the displayed transfer count — the traveller chose to get off there. A
  change that happens at a `wait = 0` via is an ordinary transfer and
  counts normally.

### Panel UI

- The endpoint block becomes an ordered list of rows: From, zero to two
  Via rows, To. Via rows are styled as the existing endpoint rows, with
  the label `Via`.
- Every row carries a **`+`** button at its right end meaning **insert a
  stop after this row**:
  - on the From row and on a Via row it inserts a new, empty Via row
    directly below;
  - on the To row it **demotes the current destination to a Via** (value
    and a fresh wait of 0 carried over) and opens a new, empty To row
    below it. Available only when the destination is a station — a point
    or current-location destination cannot become a via.
  - Every `+` disappears once two vias exist.
- A Via row's clear control **removes the row**, rather than emptying it
  the way the From / To clear controls do.
- A Via row is slightly narrower than From / To to make room for an
  inline **wait control** at its right: muted and reading as "no wait" at
  0, showing the value once set, and opening the preset list on click.
  The wait control exists on Via rows only.
- An empty Via row is ignored when the query is issued, and is not
  removed by issuing it.
- Swap reverses the entire chain, via order included; each wait travels
  with its via.
- The panel's "route is set" state is still governed by From and To
  alone; vias never gate it.

### Result display

- The expanded leg list renders each via stay as its own row, visually
  distinct from a transfer wait, naming the station and the actual stay.
  A via with `wait = 0` that the traveller passes through on board is
  marked on the intermediate stop, not given a row of its own.
- The card's collapsed state indicates that the connection carries vias.
- On the map, a via stop is marked distinctly from ordinary intermediate
  stops of the drawn route.

### Persistence and sharing

- Two new URL parameters: **`via`** — the ordered via stations, in the
  same serialisation the From / To station endpoints already use — and
  **`viaWait`** — the wait minutes, one per via, in the same order.
  `viaWait` is written only when at least one wait is non-zero.
- Both parameters are written on every via or wait change, and a cold
  load of such a URL reproduces the chain and issues the query.
- The via chain and its waits are part of the query fingerprint, so two
  otherwise identical routes with different vias never collide in caching,
  recents or the shared-connection identity.
- Recent routes store and display the via chain.

## Constraints

- Three vias maximum, stations only, minutes only — all three are the
  routing engine's limits, not preferences. The via ceiling is a
  compile-time constant in the engine: raising it further means editing
  the fork and rebuilding its image, and it costs per-query memory on
  every search, via-less ones included.
- A wait ceiling must exist and must be low enough that a chain of maximum
  waits still fits inside the total-travel-time ceiling sent to the
  engine; that ceiling has to grow by the requested dwell sum, otherwise
  a long errand silently returns no connections at all.
- A via query cannot use the engine's faster alternative search path, so
  via queries may be slower than the same query without vias. Acceptable;
  no behavioural difference in the results.
- Point / address vias are out of scope. They would need the journey split
  into chained queries, which is a different concept.
- Nothing in the transit pipeline, the tile artefacts or the stop search
  index changes.
- The new parameters coexist with the existing position hash, `?line=`,
  `?route=` and the routing panel's own parameters.
- Defaults are unchanged for anyone who never adds a via: a via-less query
  must produce byte-identical requests and identical results to today.
