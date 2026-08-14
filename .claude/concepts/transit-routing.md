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
- **Walking budget cascade.** First/last-mile walking budget starts narrow (2 h) for query speed and escalates to the server ceiling (8 h) when any of three signals fires: the initial search returns nothing; any returned itinerary contains a wait of more than 1 h at the start or between transit legs; or the narrow search — including empty hops during the time-advance cascade — reveals a ≥ 4 h stretch of local daytime (06–21) with no service (measured across query time, every result's anchor time, and the current search frontier). The escalation redoes the full narrow flow with the wide budget (the wider candidate set is not merge-comparable with the narrow one). Escalation is invisible to the user and adds latency only on the queries that need it.

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
- **Quality filter** — itineraries are pruned by a time-dominance rule against a quality score before slicing to 5. See § Ranking.
- The MOTIS response's `direct` walk-only options merge into the same list — walking is offered whenever it competes with transit.
- Each result card shows: departure time and arrival time (HH:MM), total duration, transfer count, total walking time, and a horizontal strip of mode icons for the transit legs with line-color badges (colour comes from `route_color_index.json`, mirroring what the map draws — see § Route color index).
- Each card also carries **at most one quality badge** and **zero or more warning icons** — see § Badges and § Warnings.
- Before any query is issued for the current inputs: the results list is absent (not "no results shown").
- No route found: a message row appears in place of cards.

### Badges

Every card is assigned at most one quality badge. Thresholds are **absolute**: they only depend on how much *worse* an itinerary is than the fastest, not on how it compares to the rest of the surviving set. Removing or adding another itinerary must never change whether an unaffected card is Good / Bad / Best.

- **Worseness** — a per-itinerary percentage of how much worse this option is than the fastest:

  `worseness = SPEED_WEIGHT * (this_duration / min_duration − 1) + (1 − SPEED_WEIGHT) * (this_score / min_score − 1)`

  - Both terms are ratios against the fastest, not against the range of the set. A 10% slower option contributes 0.10 on the speed side regardless of what other options exist.
  - `SPEED_WEIGHT = 0.8` (default).
  - `this_score` uses the existing quality score (transfers + walking). Degenerate case: if `min_score = 0` (theoretical), the comfort term is 0 for all itineraries with score 0 and treated as +∞ for the rest — they get the Bad badge as long as duration doesn't lift them into Best.
  - Note: two earlier formulations were tried and rejected. Linear min-max normalisation stretches near-ties into wide gaps (a 79 vs 72 min pair became speed_rating = 0.5 → bad). Ratio-based *rating* (`min_duration / this_duration`) fixed the shape but was still a comparison against the surviving set, so removing one alternative could re-rank the rest. Worseness is invariant to that.

- **Badge assignment** — three mutually exclusive states:

  - **Best** → the itinerary with the lowest worseness (in practice the fastest; comfort tiebreak). Icon: crown.
  - **Good** → `worseness ≤ GOOD_MAX_PCT` (default 7%). Icon: thumbs up.
  - **Bad** → `worseness ≥ BAD_MIN_PCT` (default 25% — tunable, likely revised once real distributions are visible). Icon: thumbs down.
  - Otherwise no badge.

  When only one itinerary survives filtering: it still gets the crown. Ties on worseness (very rare): pick the earliest-arriving itinerary as the sole crown holder; the others become Good.

- **Placement** — badge sits at the **top-right** of the card, overlapping the top border line (icon-only, tooltip on hover naming the state).

### Warnings

Warnings are independent flags — a single card can carry any number of them, rendered as small icons alongside the badge. Adding new warnings later must not require renumbering or reordering.

**Severity levels.** Every warning has one of three severities; a warning fires at its standard threshold and escalates through severities as the value grows. Only one icon per warning is ever shown — the highest severity reached.

- **standard** — plain red icon.
- **medium** — white icon inside a yellow circle.
- **strong** — white icon inside a red circle.

Initial warning set and thresholds:

- **Long walk** — any single WALK leg lasts **> 20 min** (standard), **> 40 min** (medium), **> 1 h** (strong). Icon: walking figure.
- **Long wait at transfer** — the gap between two consecutive transit legs is **≥ 1 h** (standard), **≥ 2 h** (medium), **≥ 3 h** (strong). Icon: hourglass / wait glyph.
- **Very slow** — total duration is **≥ 2 ×** (standard), **≥ 3 ×** (medium), **≥ 4 ×** (strong) the fastest surviving itinerary's duration. Icon: slow glyph.

**Placement** — warnings sit at the **top-left** of the card, inside the border, on the title line, immediately left of the title. Each warning is icon-only with a tooltip on hover naming the condition. Multiple warnings stack horizontally in a fixed order (long/very-long walk → long wait → very slow).

Thresholds are constants shared with the badge module. Warning definitions are additive — this list will grow.

### Ranking

MOTIS returns itineraries that are Pareto-optimal within a single query, but the time-advance cascade merges multiple windows, so dominated results (same start but later arrival, no comfort advantage) can accumulate. A post-processing quality filter runs on the merged list before it's sliced to 5. It applies at every cascade publish (the result list updates live as hops roll in), and the cascade's 5-result target counts **post-filter survivors** — the time-advance loop keeps hopping while fewer than 5 itineraries pass the filter. Dedup by fingerprint stays pre-filter.

The filter's axis is **(start, end) dominance**, not duration. An alternative that departs later *and* arrives later is a legitimate chronological option and survives tier 1 — the filter exists to remove options that are strictly worse in time with nothing to show for it, plus (tier 2) later alternatives whose comfort gap is absurd.

- **Score** — a single number combining transfer count and walking time, used only as the filters' escape hatch (never for sorting):

  `score = TRANSFER_PENALTY_SEC * transfers + WALK_PER_SEC * walk_seconds`

  - `transfers` = number of transit legs − 1 (same definition as the result card's transfer count); `walk_seconds` = sum of all WALK-leg durations including inter-station transfer walks (same as the card's walking total).
  - Walking cost is **linear**: the calibration anchors are that one transfer ≈ the difference between 5 and 10 minutes of walking — and equally between 40 and 50 minutes — so equal absolute walking differences cost the same at any baseline. `TRANSFER_PENALTY_SEC = 600`, `WALK_PER_SEC = 2` (5 min walking = one transfer). Consequence: 43 vs 126 minutes of walking is a gap of ~17 transfers — huge comfort differences score as huge.

- **Gap-scaled comfort tolerance** — a single rule replaces the earlier two-tier filter. Itinerary A is dropped when there exists another itinerary B such that:

  - B time-beats A on the query's **primary axis** (`leave-at`: `B.end ≤ A.end + T_SLACK`; `arrive-by`: `B.start ≥ A.start − T_SLACK`) **and**
  - A's comfort penalty over B exceeds a gap-scaled allowance: `A.score − B.score > MARGIN + gap · PENALTY_PER_SEC`, where `gap = min(|A.start − B.start|, |A.end − B.end|)` in seconds.

  In words: the closer two options are on their tighter time axis, the smaller the comfort penalty the worse one can afford before it's dropped. Options that are genuinely temporally distinct on *both* axes get a large allowance and survive even with big comfort differences.

  `min(|Δstart|, |Δend|)` captures how chronologically distinct A really is: an option that leaves 1 h later but arrives only 2 min later than B isn't a "1 h later" alternative — a user picking on arrival time gets essentially the same trip, minus the hour they wasted; the 2 min counts.

  Calibration (linear slope, no cap):

  - `T_SLACK` (~60 s) keeps near-identical start/end jitter from tipping the comparison.
  - `MARGIN` = 300 (≈ 2.5 min walking / 0.5 transfers) — the allowance at zero gap. Near-ties survive despite minor score differences.
  - `PENALTY_PER_SEC` ≈ 0.375, giving:
    - 2 min gap → ~345 (marginal)
    - 30 min gap → ~975 (medium, ≈ 8 min walking / 1.6 transfers)
    - 1 h gap → ~1650 (much worse, ≈ 14 min walking / 2.8 transfers)
    - 2 h gap → ~3000 (dramatic — matches the old absurd-comfort boundary)
    - beyond 2 h, the allowance keeps growing linearly so genuinely different time slots can absorb larger comfort penalties.

  Consequences:

  - same time (both axes within slack), worse comfort by more than `MARGIN` → dropped.
  - later start + earlier arrival by seconds, no comfort edge → dropped (tight domination).
  - later start + later arrival by 30 min each, ≈ 20 min extra walking → survives (comfort penalty within allowance).
  - later start by 1 h with the same arrival within slack → dropped unless comfort is essentially equal (`gap ≈ 0`).
  - a next-morning start + hours more walking, arriving no sooner → dropped once the comfort penalty passes the wide-but-finite 24 h allowance.

  The rule is symmetric — for `arrive-by` swap "primary axis" from arrival to departure; the comfort-vs-gap arithmetic is identical.

- **Chronological sort survives.** Ranking is applied only as a filter — surviving itineraries are still sorted earliest-arrival first (leave-at) or latest-departure first (arrive-by), so the "leave now" answer stays at the top.

- **Direct walk-only options** are scored the same way (`transfers = 0`, `walk = duration`). A multi-hour walk is dropped when a transit option time-dominates it or arrives no later with a hugely better score, and surfaces on its own when no transit option does — including walks that arrive sooner than any transit, which always survive.

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
