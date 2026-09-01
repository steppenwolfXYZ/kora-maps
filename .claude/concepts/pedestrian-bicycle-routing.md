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

- **Strong hill avoidance by default.** A user-facing hilliness
  preference is planned for later; until then the default leans hard
  toward flat routes.
- **Pushed-bike access:** ways where cycling is not permitted but
  walking is (pedestrian-only paths, dismount zones) are usable, at
  walking speed. Sections where the bike must be pushed are visible in
  the route detail.
- **Stairs:** heavily penalized, upward more than downward. An
  **avoid-stairs toggle** removes them entirely. If pushed-bike access
  or the stairs behavior cannot be achieved with reasonable effort in
  this step, they may ship in a follow-up — but the concept treats
  them as part of the target state.

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
