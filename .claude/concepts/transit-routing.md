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

**Station coord = walkable-platform snap.** For `station` endpoints, `stop_search_index.json`'s `c` (which the client sends to MOTIS as `fromPlace` / `toPlace`) is not the raw GTFS parent-station centroid. Instead, step 07 snaps each station's coord onto the centroid of the nearest OSM `public_transport=platform` way within 150 m. GTFS parent centroids often land on the road right-of-way (canonical case: Bern Eigerplatz, whose centroid sits within 2 m of a `highway=primary, sidewalk=separate` way and a tram track); MOTIS's OSR foot profile then applies +45 s per edge for walking on a road with a mapped separate sidewalk, which pushes the actual station's platforms off the Pareto front and MOTIS boards the walker at a distant alternative stop instead. Snapping onto an OSM platform lands the coord on a way MOTIS's OSR explicitly whitelists (`is_platform_` → `Whitelist` in the foot profile), avoiding the road penalty. Stations with no OSM platform inside the radius keep the raw GTFS coord — a bounded fallback that doesn't make the situation worse.

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

- **Effective time** — comfort penalty is baked into a multiplicative factor on trip duration, not blended in as a separate ratio term:

  `effective_time = duration · (1 + 0.1 · (walk_malus + transfer_malus))`

  - `transfer_malus = 1 − (1 − 0.3)^transfers` → 0 / 30 / 51 / 66 / 76 / 83 / … % (saturates toward 100% as transfers pile up).
  - `walk_malus = t² / (t² + 30²)` with `t = walk_minutes` → 10 min ≈ 10%, 20 ≈ 31%, 30 ≈ 50%, 40 ≈ 64%, 1 h ≈ 80%, 2 h ≈ 94% (saturates toward 100%).
  - Both maluses live in [0, 1]; they add, so the comfort factor lives in [1.0, 1.2]. Max 20% inflation on top of duration, regardless of how bad the trip's comfort is.
  - Rationale for the multiplicative shape: expressing comfort in absolute seconds would tie its weight to trip length (2 transfers on a 15-min trip vs a 3 h trip would score identically). A factor scales naturally with duration and needs no clamps.
  - Rationale for the additive combination of the two maluses (rather than probabilistic OR or max): each axis is an independent kind of discomfort; a trip with both should be worse than one with only one, and the shared 20% cap keeps the sum bounded without extra machinery.

- **Worseness** — a single ratio:

  `worseness = effective_time / min_effective_time − 1`

  - Set-independent in shape: only `min_effective_time` comes from the surviving set (unavoidable — the comparison needs a reference). No normalisation over the set's spread, so removing or adding another itinerary doesn't re-rank the rest.
  - Historical note: earlier formulations blended `duration/min_duration` and `score/min_score` with a fixed weight (e.g. 80/20). The comfort ratio was unbounded — a lean min_score made small absolute penalties look catastrophic — so the "20%" term routinely outran the "80%" one, and the fastest option frequently lost the crown to a comfier slower one when it shouldn't. The comfort-factor form removes that failure mode: comfort can at most add 20% to a trip's own time, so a truly faster trip always wins the crown unless its comfort penalty exceeds the raw speed gap by exactly that much.

- **Badge assignment** — three mutually exclusive states:

  - **Best** → the itinerary with the lowest worseness (i.e. lowest effective time). Icon: crown.
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

**Placement** — warnings sit at the **top-left** of the card, inside the border, on the title line, immediately left of the title. Each warning is icon-only with a tooltip on hover naming the condition. Multiple warnings stack horizontally in a fixed order (long walk → long wait → very slow).

Thresholds are constants shared with the badge module. Warning definitions are additive — this list will grow.

### Ranking

MOTIS returns itineraries that are Pareto-optimal within a single query, but the time-advance cascade merges multiple windows, so dominated results (same start but later arrival, no comfort advantage) can accumulate. A post-processing quality filter runs on the merged list before it's sliced to 5. It applies at every cascade publish (the result list updates live as hops roll in), and the cascade's 5-result target counts **post-filter survivors** — the time-advance loop keeps hopping while fewer than 5 itineraries pass the filter. Dedup by fingerprint stays pre-filter.

The filter's axis is **(start, end) dominance**, not duration. An alternative that departs later *and* arrives later is a legitimate chronological option and survives tier 1 — the filter exists to remove options that are strictly worse in time with nothing to show for it, plus (tier 2) later alternatives whose comfort gap is absurd.

- **Score** — a single number combining transfer count and walking time, used only as the filter's escape hatch (never for sorting):

  `score = TRANSFER_PENALTY_SEC * transfers + walk_cost(walk_seconds)`

  - `transfers` = number of transit legs − 1 (same definition as the result card's transfer count); `walk_seconds` = sum of all WALK-leg durations including inter-station transfer walks (same as the card's walking total).
  - Walking cost is **soft-capped**: full linear rate `WALK_PER_SEC = 2` for the first `WALK_SOFT_CAP_SEC = 30` min, then a much shallower `WALK_TAIL_PER_SEC = 0.5` beyond. `TRANSFER_PENALTY_SEC = 600`, so 5 min walking still costs about the same as one transfer at the short end. The knee bounds the score inflation from multi-hour hikes — a 30 min vs. 3 h walking difference no longer outweighs every realistic temporal-gap allowance — while keeping small walking differences (10 vs. 15 min) as sensitive as before.

- **Gap-scaled comfort tolerance** — a single rule replaces the earlier two-tier filter. Itinerary A is dropped when there exists another itinerary B such that:

  - B time-beats A on the query's **primary axis** (`leave-at`: `B.end ≤ A.end + T_SLACK`; `arrive-by`: `B.start ≥ A.start − T_SLACK`) **and**
  - A's comfort penalty over B exceeds the gap-scaled allowance: `A.score − B.score > −MARGIN + PENALTY_K · gap^(1/3)`, where `gap = min(|A.start − B.start|, |A.end − B.end|)` in seconds.

  In words: when B is time-competitive, A survives unless it's meaningfully worse in comfort than the allowance at that gap — a negative allowance at zero gap (A must have a comfort edge) rising to a large positive at multi-hour gaps (A can afford substantial comfort penalties for distinct time slots). The cube-root shape is chosen deliberately: it rises fast enough that even a 2 min gap already tolerates a fairly steep comfort difference (so a rare fast option can't nuke its neighbours), then saturates gracefully so the 2 h allowance is "dramatic" rather than absurd.

  `min(|Δstart|, |Δend|)` captures how chronologically distinct A really is: an option that leaves 1 h later but arrives only 2 min later than B isn't a "1 h later" alternative — a user picking on arrival time gets essentially the same trip, minus the hour they wasted; the 2 min counts.

  Calibration:

  - `T_SLACK` (~60 s) keeps near-identical start/end jitter from tipping the comparison.
  - `MARGIN` = 300 (≈ 2.5 min walking / 0.5 transfers) — at zero gap, allowance = `−MARGIN` so A must be more comfortable than B by more than `MARGIN` to survive.
  - `PENALTY_K` = 430, giving:
    - 0 gap → −300 (A must be more comfy by > 300)
    - 2 min gap → ~1820 (drops only if ≥ ~15 min extra walking / ≥ 3 transfers)
    - 5 min gap → ~2570
    - 10 min gap → ~3230
    - 30 min gap → ~4920
    - 1 h gap → ~6290
    - 2 h gap → ~8000 (dramatic — ≥ ~65 min extra walking or ~13 transfers under the soft cap)
    - beyond 2 h keeps rising slowly.

  Consequences:

  - same time (both axes within slack), same or worse comfort → dropped (matches the old tier-1 strict-domination rule).
  - later start + earlier arrival by seconds, no comfort edge → dropped (tight domination).
  - a rare fast option surrounded by regular options with ~15 min more walking → the neighbours all survive from ~2 min gap onward.
  - later start + later arrival by 30 min each, ≈ 30 min extra walking → survives (comfort penalty within allowance).
  - later start by 1 h with the same arrival within slack → dropped unless comfort is meaningfully better (gap ≈ 0 → strict rule applies).

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
