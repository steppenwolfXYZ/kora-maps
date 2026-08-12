# Transit Routing

## Problem

Kora Maps visualises the transit network but does not yet answer the question "how do I get from A to B?". A public-transit-first map without a trip planner is missing the core action. This concept covers the routing engine and panel UI. Map rendering of the selected route is a separate concept — see `route-display.md`.

## Requirements

### Backend

- A local MOTIS v2 instance answers multi-modal trip queries (transit + pedestrian) over Swiss data.
- Data feed reuses artefacts the existing transit pipeline already produces — the pfaedle-routed GTFS coming out of step 05 (so MOTIS's `with_shapes: true` picks up the shaped polylines and returns real route geometry per leg, not stop-to-stop straight lines) — plus a country-wide OSM PBF fed through a preprocessing pass that adds `foot=yes` to `access=agricultural` / `access=forestry` ways (MOTIS's default OSR pedestrian profile blacklists those, but Swiss convention treats them as walkable). `access=no` / `private` / `emergency` / `delivery` are left untouched — those genuinely block foot access.
- Pedestrian routing is used for three purposes: **first mile** (start → boarding stop), **last mile** (alighting stop → end), and **inter-transit walks** (route-to-route transfers, and walks between distinct stops that unlock non-official connections). Transfer walks up to 2 h are permitted.
- Query modes: `leave-at` (default, time = now) and `arrive-by`.
- **Direct walking** is always attempted regardless of distance. A multi-hour walk still surfaces when it beats every transit option; MOTIS's `direct` walk-only itineraries are merged into the same list as transit itineraries.
- **No artificial time-of-day cutoff.** A query at 00:30 must surface the first morning departures; a weekend query must surface the Monday-morning departures. The search expands progressively until either the target of 5 results is reached or the timetable is exhausted.
- **Walking budget cascade.** First/last-mile walking budget starts narrow (2 h) for query speed and escalates to the server ceiling (8 h) when the initial search returns nothing OR when any returned itinerary contains a wait of more than 1 h at the start or between transit legs. The escalation is invisible to the user and adds latency only on the queries that need it.

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
- `current` — the user's current GPS location. Offered as the first dropdown suggestion when the input is focused **and empty** — once the user starts typing, only station matches show; the current-location shortcut hides. Location permission is requested on first use; if denied, the option remains selectable and re-prompts on next attempt. The From field is prefilled with `current` when the panel opens fresh (no serialised state to restore).

Only these three types exist in this step. A fourth `address` type is added when forward geocoding ships.

### Entry points

Three ways to enter routing state:

1. **Station popup buttons** — every station popup gains two buttons, **Route from here** and **Route to here**. Clicking opens the routing panel (if not already open) with that station set as the corresponding endpoint; if the panel is already open, it overwrites the corresponding endpoint.
2. **Search-bar route icon** — a routing icon in the search bar opens the routing panel with an empty **To** and **From** prefilled to `current`.
3. **Map context menu** — right-click (desktop) or long-press (touch) on any map location opens a small context menu at the click point with two items, **Route from here** and **Route to here**. Selection opens the routing panel (if not already open) with a `point` endpoint at the click coord; if the panel is already open, overwrites the corresponding endpoint.

### Results

- Up to 5 alternatives per query.
- **Sort** — earliest arrival first for `leave-at`, latest departure first for `arrive-by`. Chronological, not by duration; the fastest ride that departs late correctly ranks below an earlier departure that arrives sooner. Walking-heavy itineraries surface at the top when they arrive sooner than any bus.
- **Quality filter** — after the cascade has merged windows, itineraries are pruned by a soft-Pareto rule against a quality score before slicing to 5. See § Ranking.
- The MOTIS response's `direct` walk-only options merge into the same list — walking is offered whenever it competes with transit.
- Each result card shows: departure time and arrival time (HH:MM), total duration, transfer count, total walking time, and a horizontal strip of mode icons for the transit legs with line-color badges (colour comes from `route_color_index.json`, mirroring what the map draws — see § Route color index).
- Before any query is issued for the current inputs: the results list is absent (not "no results shown").
- No route found: a message row appears in place of cards.

### Ranking

MOTIS returns itineraries that are Pareto-optimal within a single query, but the time-advance cascade merges multiple windows, so nonsensical results (huge walking, extra transfers) can accumulate. A post-processing quality filter runs on the merged list before it's sliced to 5.

- **Score** — a single number combining transfer count and walking time:

  `score = TRANSFER_PENALTY_SEC * transfers + WALK_WEIGHT * sqrt(walk_seconds)`

  - Walking cost is **concave** (`sqrt`), so an extra 20 min of walking hurts far more when the baseline is 3 min than when it's 60 min. Matches the intuition that "20 more minutes on a long walk is a minor regression, 20 more minutes on a short walk is a major one".
  - `TRANSFER_PENALTY_SEC` — cost of one transfer, tuned so ~1 transfer ≈ ~10 min of mid-range walking (600s baseline).
  - `WALK_WEIGHT` — chosen so `WALK_WEIGHT * sqrt(600s)` ≈ one `TRANSFER_PENALTY_SEC` (i.e. `WALK_WEIGHT ≈ 25` when `TRANSFER_PENALTY_SEC = 600`).

- **Soft Pareto filter** — itinerary A is dropped when there exists another itinerary B such that:

  `B.duration ≤ A.duration + SLACK_SEC`  **AND**  `B.score < A.score - MARGIN`

  In words: a slower option only survives if it isn't clearly beaten on the score. `SLACK_SEC` (~120s) and `MARGIN` (~180s) leave near-ties intact so marginal differences don't force a prune.

- **Chronological sort survives.** Ranking is applied only as a filter — surviving itineraries are still sorted earliest-arrival first (leave-at) or latest-departure first (arrive-by), so the "leave now" answer stays at the top.

- **Direct walk-only options** are scored the same way (`transfers = 0`, `walk = duration`). A multi-hour walk is dropped when a transit option matches its arrival with a much better score, and surfaces on its own when it wins outright.

### Route color index

- `route_color_index.json` (baked by step 07 of the transit pipeline alongside `line_index.json`) maps GTFS `route_id` → drawn color, so a routing result card's badge matches the map exactly.
- Missing entries (route not in the index) fall back to a per-bucket mid-tone matching the MapMenu legend (train red, tram turquoise, metro green, bus blue, ferry blue, mountain purple).

### Deep link

Routing state is serialised into the URL query string, following the existing `?line=` deep-link precedent from `line-detail-view.md`:

- Query parameters: `from`, `to`, `mode` (`leave` or `arrive`), `time` (ISO 8601 or `now`).
- Endpoint serialisation: `station` → UIC; `point` → `lat,lng`; `current` → `me`.
- The URL is written on any input or time change, and on issuing a query, via SvelteKit's `replaceState`.
- Opening a route URL on cold load reproduces the panel state and issues the query.
- The `?route=<fingerprint>` param carrying a selected itinerary belongs to `route-display.md` and is added by that concept — it coexists with the panel params here.

## Constraints

- Does not modify the transit pipeline's line/stop/pmtile outputs. The pipeline gains one new sibling output (`route_color_index.json`) and one new preprocessor script that consumes the country OSM download; nothing in the map-drawing pipeline changes.
- Does not introduce a page-level `<svelte:head>` title.
- The routing URL parameters coexist with the existing `#zoom/lat/lng` position hash, `?line=` deep link, and `?route=` selection (from `route-display.md`) — none of these clobber each other.
- `stop_search_index.json` is the single station index used by both the stop search and the routing From / To search — no parallel index is introduced.
- The **Point on map** label for `point` endpoints is fixed for this step; reverse geocoding replaces it in a follow-up.
- The `current` endpoint requires a runtime location-permission grant. First-time use triggers the browser prompt; if denied, the option stays selectable and re-prompts on next attempt.
- Rendering the selected route on the map (polylines, station highlights, walk arcs) is out of scope of this concept — that's `route-display.md`.
- Production deployment of MOTIS (shared Hetzner container vs dedicated VPS) is deferred. Local Mac only for this step.
- MOTIS's OSR pedestrian profile is used as-is; the CH walking-quality patch lives entirely in OSM preprocessing (adding `foot=yes` tags), not in a MOTIS fork.
