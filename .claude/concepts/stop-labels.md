# Stop labels

## Problem

In transit-focus view the place labels are hidden (see `view-modes.md`) so stops render as unlabelled dots (far-zoom) or pills (pill- / close-zoom) — the map identifies where lines run but not which stops they are. Top-tier hubs (Zürich HB, Bern, Basel SBB) should read as prominent place labels; smaller stops should fill in as the map zooms in, without overwhelming the display at any zoom.

The single umbrella concept covers labels across every zoom band. Implementation is phased — far-zoom (z7–z13.99) first; pill-zoom (z14–z16.99) and close-zoom (z17+) label rules come later and will be filled in below when their turn arrives.

## Requirements — shared

- Labels are visible only in `transit-focus` view. Toggled client-side alongside the existing stop-symbology layers.
- `text-field` is the stop name (the `stop_name` property already carried on stop features).
- Dark text with a light halo, working against the green background used in transit-focus.
- Density is delegated to MapLibre collision — no pre-computed density filter, no per-zoom filtering by tier:
  - `text-allow-overlap: false` — labels drop when their placement box collides with an already-placed label.
  - `symbol-sort-key: ["get", "label_priority"]` — lower value wins, so higher-priority labels are placed first and eat the space.

### `label_priority` field

A new numeric property stamped onto every stop dot feature by the pipeline (step 07), computed from the stop's `stop_tier` and `stop_score`:

```
label_priority = tier_rank × 1000 − stop_score
```

`tier_rank` orders tiers from highest priority (rank 0) to lowest, following the far-zoom dedup mode-hierarchy (train family > mountain / ferry > tram/bus) and, within each mode family, from most important to least:

| Rank | Tier |
|---|---|
| 0 | major_train |
| 1 | main_train |
| 2 | important_train |
| 3 | train_station |
| 4 | small_train |
| 5 | major_mountain |
| 6 | ferry_stop |
| 7 | mountain_stop |
| 8 | major_hub |
| 9 | big_station |
| 10 | normal_stop |
| 11 | small_bus |

Multiplier 1000 is chosen larger than any realistic `stop_score` (top scores are in the low hundreds) so tier rank always dominates. Within a tier, higher `stop_score` wins collisions. Consequence: a Small train station always beats every tram/bus tier for a collision slot regardless of raw score — the exact inversion the tier system exists to enforce.

The same `label_priority` is used across every zoom band; each band's label layer just re-uses it as its sort key.

## Requirements — far-zoom (z7–z13.99)

### Rendering

- New symbol layer over the far-zoom stop dots. Reads from the same PMTile sources as the existing far-zoom dot layers (`transit_stops_rail` / `_tram` / `_regional` / `_bus`).
- `maxzoom: 14` — the pill-zoom label layer takes over at z14.
- The label sits beside its dot (offset placement, one anchor — placement details are style-tuning, not a requirement).

### Font size

Per-tier size defined at four zoom anchors (z7 / z10 / z12 / z13); MapLibre linear-interpolates zoom between anchors and extrapolates past z13 into z13.99. A dash means the tier is not yet participating at that zoom (text size 0 → not rendered). Numbers are starting values, expected to be tuned against screenshots.

| Tier | z7 | z10 | z12 | z13 |
|---|---|---|---|---|
| major_train | 11 | 16 | 20 | 22 |
| main_train | 10 | 14 | 16 | 18 |
| important_train | 9 | 12 | 14 | 15 |
| train_station | — | 11 | 12 | 13 |
| small_train | — | 11 | 12 | 13 |
| major_mountain | 9 | 11 | 12 | 13 |
| ferry_stop | 9 | 11 | 12 | 13 |
| mountain_stop | — | — | 10 | 11 |
| major_hub | — | — | 11 | 13 |
| big_station | — | — | 10 | 11 |
| normal_stop | — | — | 10 | 11 |
| small_bus | — | — | — | — |

`small_bus` is never labelled at far-zoom — the tier is the smallest-dot tier and its label range starts at pill-zoom (z14+). `normal_stop` labels appear from z12 in areas where nothing outranks them.

At **z14 (pill-zoom boundary)** every tier participates — including `small_bus` — and a readability floor kicks in so no label drops below 12 px. z14 sizes (px):

| Tier | z14 |
|---|---|
| major_train | 24 |
| main_train | 20 |
| important_train | 17 |
| train_station | 15 |
| small_train | 15 |
| major_mountain | 15 |
| ferry_stop | 15 |
| mountain_stop | 13 |
| major_hub | 15 |
| big_station | 13 |
| normal_stop | 13 |
| small_bus | 12 |

The style's `text-size` interpolate has anchors at z7 / z10 / z12 / z13 / z14 — MapLibre linearly interpolates between them (so z13.5 has sizes half-way between the two rows).

**Design rule.** The regular-weight tiers (`small_train`, `normal_stop`) size-match the nearest heavier-weight tier just above them in the hierarchy: `small_train` matches `train_station`, `normal_stop` matches `big_station`. The weight change alone carries the visual hierarchy; dropping size on top of that reads as double-demotion and makes the regular labels look too small.

Three weights are in play — Saira Regular, Saira SemiBold, Saira Bold. `mountain_stop` and `ferry_stop` always render SemiBold at every zoom band; `big_station` joins them at z12+. SemiBold is a middle weight between the bold train / hub tiers and the regular rest. Every other tier is either Bold (if the zoom band puts it in the bold set) or Regular. The bold set grows with zoom so the bold-to-regular ratio stays roughly in the one-third range at every zoom band:

| Zoom | Bold tiers |
|---|---|
| z7–z8 | `major_train`, `main_train` |
| z9 | + `important_train` |
| z10 | + `train_station` |
| z11 | + `major_hub`, `major_mountain` |
| z12+ | + `small_train` |

SemiBold: `mountain_stop`, `ferry_stop` at every zoom band; `big_station` from z12+. Everything else renders regular. Implemented as a `step` on zoom with a `match` on `stop_tier` inside each branch — SemiBold overrides are placed first in the match so they win regardless of the bold set for that zoom.

Collision buffer: `text-padding: 4` px for every tier except `normal_stop`, which uses 20 px. The wider buffer on `normal_stop` forces rural stops of that tier to space out from each other rather than pack wherever MapLibre finds room; higher-priority tiers still get the tight 4 px so they collide only when they actually touch.

### City-prefix stripping (`display_name`)

Swiss GTFS names non-train stops as `"City, Streetname"` (Bern, Bahnhofplatz / Zürich, Bellevue). In cities the "City, " prefix is redundant because a nearby train-station label already carries the city; in villages without a train station the prefix is what tells the reader where they are.

The pipeline stamps a `display_name` on every dot feature:

- Build a lookup keyed by city name from every train-station stop: for each train-station `stop_name`, add both its full first-comma-segment (catches "Bern") and its space-split first word (catches "Zürich" from "Zürich HB" or "Basel" from "Basel SBB") to a `{city_key → [coords]}` map. Case-folded.
- For each dot feature, split `stop_name` on `", "`. If a `city_key` matching the prefix has any coordinate within 25 km of the stop, drop the prefix (same rule as the pill-arrow's `strip_city_prefix` — matches `city + ","` or `city + " "`, so "Berneck" is not matched by "Bern").
- Otherwise `display_name = stop_name` (unchanged).

The style reads `text-field: ["coalesce", ["get", "display_name"], ["get", "stop_name"]]` so it degrades gracefully on features emitted before the pass existed. The 25 km radius is a starting point; it should cover the greater commute belt of every Swiss city hub without letting distant same-named stops (rare) trigger a false strip.

The city-prefix helper is shared with the pill-arrow destination shortener (`strip_city_prefix` in `close_zoom/text.py`).

### Absorbed stops

Stops that far-zoom dedup absorbed away are already hidden from the far-zoom layer via `tippecanoe.minzoom`. Their labels vanish along with their dots — no separate handling.

## Requirements — pill-zoom (z14–z16.99)

Two shapes depending on the station's construct:

- **Simple** — station has only a dot / endpoint disc or a single straight pill (no connector, no bent pill). Label sits 5 m east of the easternmost coord across all the station's band features.
- **Complex** — station has any connector, any pill with more than 2 vertices (bent), or more than one pill. Pick the "main pill" (currently longest by segment sum — a proxy for the `f_weighted`-ranked main pill; refine if this proxy misplaces labels). Label sits 5 m east of the main pill's easternmost coord.

Both cases place the label to the east; the difference is only WHICH geometry the eastward-padding is measured from. No leader / connector line — a visual "name tag" connector was tried and reverted; if a future iteration reintroduces it, define the rule in this section first.

### Anchor computation (step 07)

For each station (grouped by `parent_station`, or `stop_id` fallback), the anchor is computed **per pill design band** (A = z14, B = z15, C = z16+) because bands can produce different pill layouts — a station that's one long pill in band C can split into two discs + a connector in band A. Classification (simple vs complex) is also re-evaluated per band.

Per band, from that band's pill / connector / endpoint features:

- **Simple with no pill** (endpoint disc or dot only): `anchor = (east_x + 5 m eastward, east_y)` where `east` is the easternmost coord across the band's features. Falls back to the far-zoom dot's coord only if the band emitted no pill-zoom geometry at all.
- **Any pill or endpoint disc present**: pills and endpoints are ranked together by sum of `f_weighted` across their distinct logical-line keys (`(ref, mode, agency_id)`) — same rank rule `_largest_pill_or_disc_position` in `stops/far_zoom.py` uses. Both pills and endpoints carry a `pill_osm_ids` property stamped by `make_pill_features` (comma-separated string); the anchor code looks up `f_weighted` per osm_id via `line_lookup`.
  - **If the top candidate's score is more than 1.25× the runner-up's** (or there's only one candidate), the winner is used. If the winner is a pill, the vertical/horizontal orientation rule applies; if the winner is an endpoint, the endpoint's coord is the base.
  - **Otherwise** (no clear winner): fall back to the easternmost coord across all pills + endpoints (connectors deliberately excluded so a curved connector's east swing can't win).

For the pill case, which point on the pill depends on its orientation:
  - **Vertical pill** (first→last endpoint's `dy > dx` in metric coords, i.e. steeper than 45°): base = polyline midpoint of the centerline (so the label sits beside the pill's middle rather than at one end).
  - **Horizontal pill** (else): base = pill's easternmost coord (so the label sits past the east end).
  Then `anchor = (base.x + 5 m eastward, base.y)`.

**Dedup across bands.** After per-band computation, bands whose anchor is identical to 6 decimal places are grouped, and the group is emitted as a single feature covering the merged tippecanoe zoom range. Most stations produce the same anchor across all three bands and emit a single feature (minzoom 14, maxzoom 17); the minority whose topology changes with the band emit 2–3 features with per-band `tippecanoe.minzoom/maxzoom` matching the band's own zoom range (A: 14, B: 15, C: 16–17). Contiguous bands with the same anchor merge into one range (`{A, B}` → 14–15); non-contiguous bands emit separately.

All features bundled into the existing `transit_stop_pills` PMTile source with `feature_type: "stop_label_anchor"` (Point), carrying `stop_name`, `display_name`, `stop_tier`, `label_priority`, `mode`.

### Rendering (style)

- **Symbol layer(s)** filtered on `feature_type == "stop_label_anchor"`, one per padding tier (`normal_stop` split), `text-anchor: "left"`, `text-field: coalesce(display_name, stop_name)`, same font-weight / size / sort-key / collision expressions as far-zoom, `text-justify: "left"`, `text-offset: [0.5, -0.11]` em (0.5 for clearance from the anchor point, -0.11 for the Saira cap-height correction).

Both symbol layers use `minzoom: 14`, `maxzoom: 17` (close-zoom takes over at z17), declared AFTER every pill / disc / connector / indicator paint layer so labels render on top of the drawn geometry.

## Requirements — close-zoom (z17+)

_To be filled in when this phase is worked on._

## Constraints

- Standard view is unchanged. Place labels still show; stop labels stay hidden.
- Existing far-zoom dot rendering (sizes, colors, dedup, per-zoom line lists) is not touched.
- No new PMTile source. `label_priority` piggybacks on the existing `transit_stops_*` bundles.
- Numeric font sizes and the `tier_rank` order are placeholders and expected to be revised after visual review — the shape of the mechanism (per-tier size curve + sort-key collision) is the fixed part.
