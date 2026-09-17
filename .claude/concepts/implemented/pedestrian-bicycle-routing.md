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
- For each ferry / car-shuttle crossing aboard the winning route, one
  extra query avoids that single crossing (others stay usable). The
  land variant is judged by the DISTANCE ratio of the land SECTION
  against the crossing itself — never of the whole route, which would
  make the verdict depend on how far away the crossing happens to sit
  (the Simplon shuttle won from Visp and lost from Bern). Three bands:
  within 2.5× the land section becomes the suggested route itself (the
  crossing demoted to an alternative — riding is the sporting default,
  a pass instead of a tunnel ride included); within 4× it joins as an
  alternative; beyond that it is not offered (circumnavigating a lake
  or the Lötschberg massif). Distance, not time, on purpose: a
  mountain pass instead of a shuttle rides similar kilometres in many
  more hours and must stay on offer.
  Crossing routes carry a chip — "ferry" for ships, "car shuttle" for
  an Autoverlad; a train through a mountain is never called a ferry.
  The elevation profile and ascent totals treat on-board sections as
  flat (the DEM samples the massif above a tunnel, not the ride).

### 3. Result cards

One card per route, analogous to the transit connection cards:

- **Duration**, **distance**, **ascent meters**, **descent meters**.
- The selected card additionally shows an **elevation profile** graph
  of the route.
- Card layout must leave room for future additions (surface quality,
  share of dedicated paths, …) without redesign.

### 3a. Narrow-screen layout (revised)

The first version docked the panel at the bottom of the screen as a
bottom sheet once a cycling / walking query had run. That was reverted:
moving the panel to the other edge changed nothing about its size and
only made the position jump. The panel stays at the top, where it was
before the query and where the transit tab keeps it.

- **Collapsed search once a route is loaded.** After a query the
  editing chrome (mode tabs, endpoint inputs, action row) collapses
  into one summary row: from → to plus an edit button. Tapping the row
  or the button expands the editing chrome again in place; running a
  fresh query collapses it again. The panel does not move. Two simple
  rules: loading a route collapses; opening the panel never does,
  independent of whether the open loads a route. So the tabs and
  endpoint fields are always shown on open — on a reopen that
  restores retained results as much as on an open whose query runs
  right away (deep link, route from / to here) — and the next query
  the user triggers collapses.
- **Result cards below the summary row**, as in the transit tab. The
  panel's height is capped so a strip of map stays visible below it.
- **Resize handle on the bottom edge** of the collapsed panel — the
  same grab bar the bottom sheet had, mirrored: dragging down grows
  the result list over the map, dragging up shrinks it back to the
  default cap. The override lasts until the panel expands or the
  query changes.
- **Map button on a card enters fullscreen map mode** for that route,
  exactly as the transit connection cards do (see
  `routing-map-details-split.md` § Mobile): the panel hides, the map
  fills the viewport under the summary header, × in the header returns
  to the list. The button's previous narrow-screen action — collapse
  the panel back to the bottom sheet — is gone with the sheet.
- Camera framing after a query and after expanding the panel targets
  the visible strip of map, i.e. the area below the panel, not above.
- Desktop is unaffected: the side panel keeps its full editing chrome
  and cards regardless of query state.

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
- **Stairs:** stairs under 2 m cost only their carry time; longer ones
  add committing fees at 2 m and 4 m and a slow hauling pace, upward
  far worse than downward (model in `bicycle-costing-fork.md`). An
  **avoid-stairs toggle** removes them entirely. The toggle is **mandatory for V1** — stairs are an
  absolute no-go for e-bikes, which are increasingly the norm; bicycle
  routing does not ship without it.

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

- Sensible defaults; stairs allowed and entirely unpenalized — a
  normal part of walking, no warning on the cards either. Avoiding them
  belongs to the later wheelchair / stroller mode.
- Ferries are strongly avoided on foot (roughly four hours of cost per
  boarding plus a heavy on-board factor — walking is slow, so at
  neutral pricing every slightly-viable ship would win), and
  car-shuttle trains are avoided at the engine's maximum. Both via
  request options on the walking tab's queries only — transit walk
  legs and the footpath matrix stay untouched. The per-crossing
  walk-around variants apply as on the cycling tab. (Step-free
  pedestrian routing is
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
