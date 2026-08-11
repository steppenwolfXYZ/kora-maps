# Transit Routing

## Problem

Kora Maps visualises the transit network but does not yet answer the question "how do I get from A to B?". A public-transit-first map without a trip planner is missing the core action. This concept defines the first step of adding routing: the panel UI, the backend integration, and the result display. Rendering the selected route on the map is deferred to a follow-up concept.

## Requirements

### Backend

- A local MOTIS v2 instance answers multi-modal trip queries (transit + pedestrian) over Swiss data.
- Data feed reuses artefacts already produced by the existing transit pipeline — the Swiss GTFS coming out of step 04 and the CH+neighbours pedestrian OSM coming out of step 03. No new download or extraction step is introduced.
- Pedestrian routing is used for three purposes: **first mile** (start → boarding stop), **last mile** (alighting stop → end), and **inter-transit walks** (route-to-route transfers, and walks between distinct stops that unlock non-official connections).
- Query modes: `leave-at` (default, time = now) and `arrive-by`.
- Each query returns up to 5 multi-criteria (duration, transfers, walking time) Pareto-optimal itineraries.

### Routing panel

The routing panel is a full-height side panel that occupies the same slot as the map menu and stop search. Only one of the three UI shells (menu, stop search, routing panel) is visible at a time. Opening the routing panel closes any open line-detail-view.

Panel content top to bottom:

- **Close** button (X) in the top-right, returning to the menu / search shell.
- **From** input row.
- **To** input row.
- **When** selector: two segments (`leave-at`, `arrive-by`, default `leave-at`), plus date and time pickers (default = now).
- **Results** list, appears once at least one query has been issued for the current inputs.

The panel is available in both view modes (`standard` and `transit-focus`). Opening the panel does not switch the view mode.

### Endpoint inputs (From / To)

Each input accepts three endpoint types, distinguished by a `type` tag on the internal endpoint value:

- `station` — chosen via typed search using the existing `stop_search_index.json` (the same index the stop search uses), or set indirectly by a popup or context-menu entry point (see below).
- `point` — a `lat,lng` pair set by the map context menu. Displayed in the input as **Point on map** for this first step. When reverse geocoding lands, a street-level label replaces this text.
- `current` — the user's current GPS location. Offered as the first suggestion when either input receives focus. Location permission is requested on first use; if denied, the option remains selectable and re-prompts on next attempt. The From field is prefilled with `current` when the panel opens fresh (no serialised state to restore).

Only these three types exist in this step. A fourth `address` type is added when forward geocoding ships.

### Entry points

Three ways to enter routing state:

1. **Station popup buttons** — every station popup gains two buttons, **Route from here** and **Route to here**. Clicking opens the routing panel (if not already open) with that station set as the corresponding endpoint; if the panel is already open, it overwrites the corresponding endpoint.
2. **Search-bar route icon** — a routing icon in the search bar opens the routing panel with an empty **To** and **From** prefilled to `current`.
3. **Map context menu** — right-click (desktop) or long-press (touch) on any map location opens a small context menu at the click point with two items, **Route from here** and **Route to here**. Selection opens the routing panel (if not already open) with a `point` endpoint at the click coord; if the panel is already open, overwrites the corresponding endpoint.

### Results

- Up to 5 alternatives per query.
- Sort for MVP: fastest, weighted against transfer count and total walking time. Exact weighting is left to iteration once real results are visible.
- Each result card shows: departure time and arrival time (HH:MM), total duration, transfer count, total walking time, and a horizontal strip of mode icons for the transit legs.
- Before any query is issued for the current inputs: the results list is absent (not "no results shown").
- No route found: a message row appears in place of cards.

### Deep link

Routing state is serialised into the URL query string, following the existing `?line=` deep-link precedent from `line-detail-view.md`:

- Query parameters: `from`, `to`, `mode` (`leave` or `arrive`), `time` (ISO 8601 or `now`).
- Endpoint serialisation: `station` → UIC; `point` → `lat,lng`; `current` → `me`.
- The URL is written on any input or time change, and on issuing a query, via SvelteKit's `replaceState`.
- Opening a route URL on cold load reproduces the panel state and issues the query.

## Constraints

- Does not modify the transit pipeline, the generated map style, or the line-detail-view feature.
- Does not introduce a page-level `<svelte:head>` title.
- The routing URL parameters coexist with the existing `#zoom/lat/lng` position hash and `?line=` deep link — none of these clobber each other.
- `stop_search_index.json` is the single station index used by both the stop search and the routing From / To search — no parallel index is introduced.
- The **Point on map** label for `point` endpoints is fixed for this step; reverse geocoding replaces it in a follow-up.
- The `current` endpoint requires a runtime location-permission grant. First-time use triggers the browser prompt; if denied, the option stays selectable and re-prompts on next attempt.
- Rendering the selected route on the map (polylines, station highlights, walk arcs) is out of scope and lands in a follow-up concept.
- Production deployment of MOTIS (shared Hetzner container vs dedicated VPS) is deferred. Local Mac only for this step.
