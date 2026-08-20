# Transit Routing

## Problem

Kora Maps visualises the transit network but does not yet answer the question "how do I get from A to B?". A public-transit-first map without a trip planner is missing the core action. This concept covers the routing engine and panel UI. Map rendering of the selected route is a separate concept — see `route-display.md`.

## Requirements

### Backend

- A local MOTIS v2 instance answers multi-modal trip queries (transit + pedestrian) over Swiss data.
- Data feed reuses artefacts the existing transit pipeline already produces — the pfaedle-routed GTFS coming out of step 05 (so MOTIS's `with_shapes: true` picks up the shaped polylines and returns real route geometry per leg, not stop-to-stop straight lines) — plus a country-wide OSM PBF fed through a preprocessing pass that adds `foot=yes` to `access=agricultural` / `access=forestry` ways (MOTIS's default OSR pedestrian profile blacklists those, but Swiss convention treats them as walkable). `access=no` / `private` / `emergency` / `delivery` are left untouched — those genuinely block foot access.
- **MOTIS reads a sidecar copy of the pipeline's GTFS** at `data/gtfs_motis/`, not `data/gtfs_routed/` directly. `scripts/preprocess_gtfs_for_motis.py` builds it: modified `stops.txt` where each stop whose `platform_code` matches an OSM `public_transport=platform` way's `local_ref` at the same station (via `uic_ref` equality with the parent station UIC) is snapped onto that platform's centroid; every other GTFS file is hardlinked from `data/gtfs_routed/`, no duplication, no re-pfaedle. This exists because GTFS parent-station centroids frequently sit on the road / tram track (canonical case: Bern Eigerplatz platform :C's GTFS coord is ~2 m from the tram track); MOTIS's OSR then computes the last-mile walk into that stop through short OSR edges carrying `sidewalk=separate` on the primary road, each costing +45 s in the foot profile — hundreds of seconds of penalty on a 15 m walk. Snapping the stop onto its OSM platform (a `public_transport=platform` way, which OSR whitelists) removes the road-side last-mile entirely. Scoped to the sidecar so map rendering (which reads `data/gtfs_routed/`) is untouched — pill-arrows and stop dots stay put. Route-drawing markers shift by 5–15 m on drawn routes (leg endpoints follow MOTIS's stop table); the polyline path itself is unchanged (drawn from pfaedle-generated `shapes.txt`, which the sidecar hardlinks unchanged). Stops without `platform_code`, or with no matching OSM platform, keep their raw GTFS coord — bounded fallback.
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
- `point` — a `lat,lng` pair, either set by the map context menu (reverse geocoding attaches an address label) or picked from the forward-search dropdown (address or POI, label from the picked feature). Displayed as `displayName` when present, otherwise as raw coordinates. See `geocoding-search.md`.
- `current` — the user's current GPS location. Offered as the first dropdown suggestion when the input is focused **and empty** — once the user starts typing, only search matches show; the current-location shortcut hides. Location permission is requested on first use; if denied, the option remains selectable and re-prompts on next attempt. The From field is prefilled with `current` when the panel opens fresh (no serialised state to restore).

The From/To input dropdown merges three sources: `current` (when applicable), transit-station matches from `stop_search_index.json`, and Photon geocoding results (addresses + POIs) — see `geocoding-search.md` for the geocoding contract, rate-limit + coalescing scheduler, and reverse-geocoding rules.

**Station coord = two fields.** *(Superseded by `valhalla-pedestrian-router.md`: station endpoints now go to MOTIS as stop IDs (`ch_Parent<uic>`), not coordinates — the forked MOTIS serves their WALK offsets from the imported Valhalla matrix, and Valhalla has no OSR sidewalk penalty, so the `cw` walkable-coord workaround below is obsolete. Step 07 no longer emits `cw`; the client ignores it; `c` remains for search ranking and fly-to.)* Each `stop_search_index.json` entry carries two coord fields:

- **`c`** — the GTFS-derived station coord. Stable across pipeline runs and stable for anything that needs the station's "official" location — search distance-ranking, map fly-to on selection, and any future consumer that expects the traffic-engineering centroid.
- **`cw`** — the *walkable* coord: centroid of the nearest OSM `public_transport=platform` way within 150 m of the GTFS coord, computed at step 07. Present only when a platform was found in range; omitted otherwise.

Routing sends `cw ?? c` as `fromPlace` / `toPlace` to MOTIS. GTFS parent centroids often land on the road right-of-way (canonical case: Bern Eigerplatz, whose centroid sits within 2 m of a `highway=primary, sidewalk=separate` way and a tram track); MOTIS's OSR foot profile then applies +45 s per edge for walking on a road with a mapped separate sidewalk, which pushes the actual station's platforms off the Pareto front and MOTIS boards the walker at a distant alternative stop instead. Sending `cw` instead lands the coord on a way MOTIS's OSR explicitly whitelists (`is_platform_` → `Whitelist` in the foot profile), avoiding the road penalty. Stations with no OSM platform inside the radius have no `cw` and the client falls through to `c` — a bounded fallback that doesn't make the situation worse.

The `c` / `cw` split exists so `c` never has to change semantics; any consumer that already used `c` keeps its behavior, and the routing-only optimisation lives on its own key.

### Entry points

Three ways to enter routing state:

1. **Station popup buttons** — every station popup gains two buttons, **Route from here** and **Route to here**. Clicking opens the routing panel (if not already open) with that station set as the corresponding endpoint; if the panel is already open, it overwrites the corresponding endpoint.
2. **Search-bar route icon** — a routing icon in the search bar opens the routing panel with an empty **To** and **From** prefilled to `current`.
3. **Map context menu** — right-click (desktop) or long-press (touch) on any map location opens a small context menu at the click point with two items, **Route from here** and **Route to here**. Selection opens the routing panel (if not already open) with a `point` endpoint at the click coord; if the panel is already open, overwrites the corresponding endpoint.

### Results

- Up to 5 alternatives per query.
- **Sort** — chronological ascending in both modes: earliest arrival first for `leave-at`, earliest departure first for `arrive-by`. Not by duration; the fastest ride that departs late correctly ranks below an earlier departure that arrives sooner. Walking-heavy itineraries surface at the top when they arrive sooner than any bus. Ascending order keeps the "Earlier connections" (top) / "Later connections" (bottom) load-more buttons aligned with the direction they load in both modes; the list is never inverted. Auto-select compensates by picking the most relevant end — the first for `leave-at`, the last for `arrive-by` (the latest departure).
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

  - `transfer_malus = 1 − (1 − 0.3)^boardings`, where `boardings` = number of transit legs (walk-only = 0, direct bus = 1, one transfer = 2, …) → 0 / 30 / 51 / 66 / 76 / … % (saturates toward 100% as boardings pile up). Counting boardings rather than transfers prices in schedule-dependence: a walk-only itinerary needs no vehicle at all, so a pure walk rates better than walking nearly as far plus a one-stop hop. The card display still shows transfers (legs − 1).
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

  When only one itinerary survives filtering: it still gets the crown. Ties on worseness (very rare): all itineraries tied on the minimum share the crown — an arbitrary tie-break would crown one and demote its identical siblings to Good.

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
- **Very slow** — total duration is **≥ 1.5 ×** (standard), **≥ 2 ×** (medium), **≥ 2.5 ×** (strong) the fastest surviving itinerary's duration, AND the absolute gap is **≥ 10 min** (so the ratio thresholds don't fire on short trips where a 1.5× ratio is only a few minutes). Icon: snail.

**Placement** — warnings sit at the **top-left** of the card, inside the border, on the title line, immediately left of the title. Each warning is icon-only with a tooltip on hover naming the condition. Multiple warnings stack horizontally in a fixed order (long walk → long wait → very slow).

Thresholds are constants shared with the badge module. Warning definitions are additive — this list will grow.

### Ranking

MOTIS returns itineraries that are Pareto-optimal within a single query, but the time-advance cascade merges multiple windows, so dominated results (same start but later arrival, no comfort advantage) can accumulate. A post-processing quality filter runs on the merged list before it's sliced to 5. It applies at every cascade publish (the result list updates live as hops roll in), and the cascade's 5-result target counts **post-filter survivors** — the time-advance loop keeps hopping while fewer than 5 itineraries pass the filter. Dedup by fingerprint stays pre-filter.

The filter has **two cases**, decided per pair (A, B) by their time relationship:

- **Overlapping** — B Pareto-dominates A in time: B departs later-or-equal AND arrives earlier-or-equal, with at least one endpoint strictly better beyond `T_SLACK`. A takes strictly more of the user's day for no time benefit. Handled by Case 1 below.
- **Non-overlapping** — neither Pareto-dominates: one leaves earlier and arrives earlier, the other leaves later and arrives later. Both are legitimate distinct time slots. Handled by Case 2 below.

The two cases exist because they warrant fundamentally different treatment. An overlapping worse option is only worth showing if it's essentially the same trip (near-tie in time AND comfort); otherwise the user pays real time cost for no chronological reason to consider it. A non-overlapping option is a genuine alternative time slot; the further apart the slots, the more comfort penalty the user might tolerate for a distinct schedule choice.

- **Score** — a single number combining transfer count and walking time, used only as Case 2's escape hatch (never for sorting):

  `score = TRANSFER_PENALTY_SEC * boardings + walk_cost(walk_seconds)`

  - `boardings` = number of transit legs (same definition as the badge comfort factor's `transfer_malus` — walk-only = 0, so a pure walk carries no vehicle penalty at all); `walk_seconds` = sum of all WALK-leg durations including inter-station transfer walks (same as the card's walking total).
  - Walking cost is **soft-capped**: full linear rate `WALK_PER_SEC = 2` for the first `WALK_SOFT_CAP_SEC = 30` min, then a much shallower `WALK_TAIL_PER_SEC = 0.5` beyond. `TRANSFER_PENALTY_SEC = 600`, so 5 min walking still costs about the same as one transfer at the short end. The knee bounds the score inflation from multi-hour hikes — a 30 min vs. 3 h walking difference no longer outweighs every realistic temporal-gap allowance — while keeping small walking differences (10 vs. 15 min) as sensitive as before.

- **Case 1 — overlapping: strict marginality.** When B Pareto-dominates A in time (per the overlapping definition above), A survives only if BOTH conditions hold:

  - **Time gap is marginal**: `max(|A.start − B.start|, |A.end − B.end|) ≤ OVERLAP_TIME_MAX` (default 9 min — single-digit minutes on both endpoints).
  - **Comfort gap is marginal**: A's effective time is at most `OVERLAP_COMFORT_MAX_PCT` worse than B's (default 20%), i.e. `A.effective_time / B.effective_time − 1 ≤ 0.20`. `effective_time` uses the same `duration · comfortFactor` definition as § Badges, so the two systems share their comfort semantics.

  If either fails, A is dropped — no gap-scaled allowance applies here. Rationale: an overlapping worse option is only worth surfacing when it's essentially the same trip. "Leave 6 h earlier and walk 3 h more, arriving 40 min later" is not a near-tie; the user gains nothing chronologically by considering A and pays real time on both ends.

- **Case 2 — non-overlapping: gap-scaled comfort tolerance.** When neither option Pareto-dominates the other in time, A is dropped when there exists another non-overlapping B such that:

  - B time-beats A on the query's **primary axis** (`leave-at`: `B.end ≤ A.end + T_SLACK`; `arrive-by`: `B.start ≥ A.start − T_SLACK`) **and**
  - A's comfort penalty over B exceeds the gap-scaled allowance: `A.score − B.score > −MARGIN + PENALTY_K · max(gap, GAP_FLOOR)^(1/3)`, where `gap = min(|A.start − B.start|, |A.end − B.end|)` in seconds and `GAP_FLOOR` = 120 s.

  In words: A survives unless it's meaningfully worse in comfort than the allowance at that gap — never less than the 2-min allowance (Case 2 removes only clearly worse connections; a similar non-overlapping connection is never removed), rising to a large positive at multi-hour gaps (A can afford substantial comfort penalties for distinct time slots). The cube-root shape rises fast enough that even a 2 min gap already tolerates a fairly steep comfort difference (so a rare fast option can't nuke its neighbours), then saturates gracefully so the 2 h allowance is "dramatic" rather than absurd. The floor exists because the raw curve went negative below ~0.3 s gap, so two identical-time near-ties (e.g. a direct walk vs. a walk + one-stop bus hybrid) each dropped the other, leaving neither.

  Across all pairs, this reduces to: for each candidate A, the tightest of the four axis-distances to any neighbor (|Δstart|, |Δend| to prev + next) sets the allowance ceiling — the closer A sits to a good neighbor on any single time axis, the less comfort penalty A is allowed.

  Calibration:

  - `T_SLACK` (~60 s) keeps near-identical start/end jitter from tipping the comparison.
  - `MARGIN` = 300, `PENALTY_K` = 430, `GAP_FLOOR` = 120 s, giving:
    - 0 gap up to 2 min → ~1820, the floor (drops only if ≥ ~15 min extra walking / ≥ 3 boardings)
    - 5 min gap → ~2570
    - 10 min gap → ~3230
    - 30 min gap → ~4920
    - 1 h gap → ~6290
    - 2 h gap → ~8000 (dramatic — ≥ ~65 min extra walking or ~13 boardings under the soft cap)
    - beyond 2 h keeps rising slowly.

  Both cases are symmetric — for `arrive-by` the Case 2 "primary axis" swaps from arrival to departure; the Pareto-dominance test in Case 1 and the comfort arithmetic in Case 2 are identical.

  Consequences:

  - overlapping, leaves 6 h earlier + arrives 40 min later (much worse comfort) → dropped by Case 1 (time test: 40 min > 9 min, comfort irrelevant).
  - overlapping, leaves 3 min earlier + arrives 5 min later, 30% worse effective time → dropped by Case 1 (comfort test: 30% > 20%).
  - overlapping, leaves 3 min earlier + arrives 5 min later, 10% worse effective time → survives Case 1 (both marginal).
  - non-overlapping, both endpoints within slack (essentially the same time), somewhat worse comfort → both survive Case 2 (the floor keeps the allowance at the 2-min value; dropped only if ≥ ~15 min extra walking / ≥ 3 boardings worse).
  - non-overlapping, later start + later arrival by 30 min each, ≈ 30 min extra walking → survives Case 2 (comfort penalty within allowance).
  - non-overlapping, a rare fast option surrounded by regular options with ~15 min more walking → the neighbours all survive Case 2 from ~2 min gap onward. (Neighbours that the rare fast Pareto-dominates in time — i.e. it leaves later AND arrives earlier than a specific neighbour — fall into Case 1 for that pair and are dropped there.)

- **Chronological sort survives.** Ranking is applied only as a filter — surviving itineraries are still sorted earliest-arrival first (leave-at) or latest-departure first (arrive-by), so the "leave now" answer stays at the top.

- **Direct walk-only options** are scored the same way (`boardings = 0`, `walk = duration`), which gives them an inherent comfort edge: every transit itinerary pays at least one boarding penalty (score + badge malus), so a pure walk rates better than walking nearly as far plus a short hop. A multi-hour walk is still dropped when a transit option time-dominates it or arrives no later with a hugely better score, and surfaces on its own when no transit option does — including walks that arrive sooner than any transit, which always survive.

### Route color index

- `route_color_index.json` (baked by step 07 of the transit pipeline alongside `line_index.json`) maps GTFS `route_id` → drawn color, so a routing result card's badge matches the map exactly.
- Missing entries (route not in the index) fall back to a per-bucket mid-tone matching the MapMenu legend (train red, tram turquoise, metro green, bus blue, ferry blue, mountain purple).

### Deep link

Routing state is serialised into the URL query string, following the existing `?line=` deep-link precedent from `line-detail-view.md`:

- Query parameters: `from`, `to`, `mode` (`leave` or `arrive`), `time` (ISO 8601 or `now`), and `fromName` / `toName` when the paired endpoint is a `point` with a display label (`geocoding-search.md` § URL persistence).
- Endpoint serialisation: `station` → UIC; `point` → `lat,lng`; `current` → `me`.
- The URL is written on any input or time change, and on issuing a query, via SvelteKit's `replaceState`.
- Opening a route URL on cold load reproduces the panel state and issues the query.
- The `?route=<fingerprint>` param carrying a selected itinerary belongs to `route-display.md` and is added by that concept — it coexists with the panel params here.

## Constraints

- Does not modify the transit pipeline's line/stop/pmtile outputs. The pipeline gains one new sibling output (`route_color_index.json`) and one new preprocessor script that consumes the country OSM download; nothing in the map-drawing pipeline changes.
- Does not introduce a page-level `<svelte:head>` title.
- The routing URL parameters coexist with the existing `#zoom/lat/lng` position hash, `?line=` deep link, and `?route=` selection (from `route-display.md`) — none of these clobber each other.
- `stop_search_index.json` is the single station index used by both the stop search and the routing From / To search — no parallel index is introduced.
- The `current` endpoint requires a runtime location-permission grant. First-time use triggers the browser prompt; if denied, the option stays selectable and re-prompts on next attempt.
- Rendering the selected route on the map (polylines, station highlights, walk arcs) is out of scope of this concept — that's `route-display.md`.
- Production deployment of MOTIS: same-origin nginx proxy at `/routing/` to a docker container on the shared Hetzner CAX11 (2 GB memory cap), serving prebuilt indexes imported on the local Mac and shipped via `scripts/deploy_motis.sh`. See `deployment.md` § MOTIS deploy.
- MOTIS's OSR pedestrian profile is used as-is; the CH walking-quality patch lives entirely in OSM preprocessing (adding `foot=yes` tags), not in a MOTIS fork. *(Later superseded by `valhalla-pedestrian-router.md`: the Kora MOTIS fork makes Valhalla the sole walking authority server-side — import-time transfer table from a precomputed Valhalla matrix, query-time WALK offsets (RAPTOR boarding-stop selection) and WALK legs via live Valhalla calls, no OSR walking fallback. The app makes one request to MOTIS and does no walk rewriting. OSR remains in the build for non-foot profiles and platform matching. See `motis/fork/README.md`.)*
