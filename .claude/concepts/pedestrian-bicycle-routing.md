# Pedestrian & Bicycle Routing

Walking and cycling as first-class route-planning modes in the routing
panel, computed by the existing Valhalla instance. Route *planning*
only — live navigation (GPS follow, wake lock) is a separate later
concept.

## Problem

The routing panel is public-transit-only. Kora positions itself as a
walkability-focused map, yet a user cannot ask it the simplest
question: "how do I walk (or ride) from A to B?" The Valhalla engine
that already powers all transit walking can answer both — it is just
not reachable from the client and has no UI.

## Requirements

### 1. Mode tabs

- Three tabs at the top of the routing panel, in this order:
  **public transit, cycling, walking**. Default is public transit.
- The last selected mode stays active across queries and across
  visits (persisted locally); a deep link's mode overrides the
  persisted choice for that visit.
- Endpoint inputs (from / to, swap) are shared across all three modes.
- **Endpoint search ranking is mode-dependent:** on the transit tab,
  stations keep today's dedicated area at the top. On cycling and
  walking, stations are **mixed into** the result list, ranked by
  plain match quality with no category boost — but each station row
  keeps its existing structure (mode icon, styling), so stations stay
  findable as landmarks without dominating the list.
- The date/time controls (leave-at / arrive-by, time selector) and the
  transit "more options" area appear **only** on the transit tab —
  cycling and walking have no time controls.

### 2. Query & alternatives

- A cycling/walking query requests up to 3 route alternatives in a
  single request.
- All returned routes are drawn on the map simultaneously: the
  selected route in full mode color, the alternatives visually muted
  (lighter/desaturated).
- Selection is two-way: tapping an alternative's card selects it, and
  tapping a muted route line on the map selects its card.

### 3. Result cards

One card per route, analogous to the transit connection cards:

- **Duration**, **distance**, **ascent meters**, **descent meters**.
- The selected card additionally shows an **elevation profile** graph
  of the route.
- Card layout must leave room for future additions (surface quality,
  share of dedicated paths, …) without redesign.

### 4. Bicycle costing behavior

- **Hills cost their honest time.** The engine models an everyday
  rider at constant comfortable power (speed halves around a 3 %
  climb), so hilly routes lose on time alone; an extra penalty exists
  only where most cyclists would push the bike (≥ ~10 %). Details and
  the elevation-artifact guard live in `bicycle-costing-fork.md`. A
  user-facing hilliness preference and an e-bike mode are planned for
  later.
- **Pushed-bike access** (mandatory — bike routing is nonsensical in
  large parts without it): ways where cycling is not permitted but
  walking is (pedestrian-only paths, sidewalks, crossings, dismount
  zones) are usable at walking speed, as are streets oneway against the
  direction of travel. On the map, pushed sections are drawn dotted —
  the walking-leg visual language; the cards do not call them out.
  Weighting details live in `bicycle-costing-fork.md`.
- **Stairs:** heavily penalized, upward more than downward. An
  **avoid-stairs toggle** removes them entirely. The toggle is
  **mandatory for V1** — stairs are an absolute no-go for e-bikes,
  which are increasingly the norm; bicycle routing does not ship
  without it.

### 4a. Route quality & weighting requirements

The shipping bar: bicycle routing ships only when it decisively beats
Google Maps and hand-testing consistently yields routes that make
sense. The current engine defaults are far from that. The
architecture requirement is **full access to the weighting system
from the beginning** — request-level knobs on a stock costing are not
enough (verified: no stock option moves the known bad cases). The
weighting model itself (quality tiers, plateau principle, crossing
penalty, cycle-route-relation signal, benchmark set) is specified in
`bicycle-costing-fork.md`.

### 5. Pedestrian costing behavior

- Sensible defaults; stairs allowed. (Step-free pedestrian routing is
  owned by `routing-options.md` § Step-free mode and is out of scope
  here.)

### 6. Deep links

- The URL carries everything needed to reproduce a query: both
  endpoints (same encoding as transit deep links) plus a new mode
  parameter **`mode`** with values **`bike`** and **`walk`**; absent
  means public transit. Opening such a link activates the right tab
  and runs the query.

### 7. Backend exposure

- Valhalla becomes reachable from the browser same-origin (the
  existing optional debug proxy is promoted to a supported endpoint).
- The endpoint is restricted to what the feature needs; it must not
  expose arbitrary engine actions publicly.

## Constraints

- The transit tab's behavior, request shape, and one-request-per-query
  property are untouched. The "no client Valhalla calls" constraint in
  `routing-options.md` applies to the transit connection search only;
  cycling/walking queries go to Valhalla directly by design.
- No live navigation, no audio guidance — later concepts.
- The OSM preprocessing for Valhalla was tuned for pedestrians
  (`foot=yes` on alp/forest roads); bicycle route quality on such ways
  is unverified and must be spot-checked before release.
- Cycling and walking results must respect the map's SSR constraints
  like every other client feature (no map-asset access during SSR).
- Labels English only; i18n out of scope.
