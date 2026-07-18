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

**Design rule.** The regular-weight tiers (`small_train`, `normal_stop`) size-match the nearest heavier-weight tier just above them in the hierarchy: `small_train` matches `train_station`, `normal_stop` matches `big_station`. The weight change alone carries the visual hierarchy; dropping size on top of that reads as double-demotion and makes the regular labels look too small.

Three weights are in play — Saira Regular, Saira SemiBold, Saira Bold. `big_station`, `mountain_stop`, and `ferry_stop` always render SemiBold (a middle weight between the bold train / hub tiers and the regular rest). Every other tier is either Bold (if the zoom band puts it in the bold set) or Regular. The bold set grows with zoom so the bold-to-regular ratio stays roughly in the one-third range at every zoom band:

| Zoom | Bold tiers |
|---|---|
| z7–z8 | `major_train`, `main_train` |
| z9 | + `important_train` |
| z10 | + `train_station` |
| z11+ | + `major_hub`, `major_mountain` |

Always SemiBold (independent of zoom): `big_station`, `mountain_stop`, `ferry_stop`. Everything else renders regular. Implemented as a `step` on zoom with a `match` on `stop_tier` inside each branch — SemiBold overrides are placed first in the match so they win regardless of the bold set for that zoom.

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

- **Simple** — station has only a dot / endpoint disc or a single straight pill (no connector, no bent pill). Label sits to the east of the geometry with a small metric padding, exactly like a far-zoom label. No leader line.
- **Complex** — station has any connector, any pill with more than 2 vertices (bent), or more than one pill. Pick the "main pill" (currently longest by segment sum — a proxy for the `f_weighted`-ranked main pill; refine if this proxy misplaces labels). Place the label north-east of the main pill's easternmost point (default 8 m east + 5 m north), and emit a thin leader LineString from that point to the label anchor. The leader makes the label's association with the construct unambiguous — the classic "name tag" look.

### Anchor computation (step 07)

For each station (grouped by `parent_station`, or `stop_id` fallback), gather the band-C pill / connector / endpoint features (band C has the widest zoom range, so a single anchor stays stable across z14–z16). Classify as complex if any connector, any bent pill, or `pill_count > 1`.

- **Simple**: `anchor = (east_x + 3 m eastward, east_y)` where `east` is the easternmost coord across all the station's band-C features (plus the dot's coord as fallback for single-line stations with no pill).
- **Complex**: pick `main_pill` = longest pill by geodesic segment sum. Take its easternmost coord `east_pt`. `anchor = (east_pt.x + 8 m eastward, east_pt.y + 5 m northward)`. Emit a `LineString` from `east_pt` to `anchor` tagged `feature_type: "stop_label_leader"`.

Both cases emit a Point feature with `feature_type: "stop_label_anchor"`, carrying `stop_name`, `display_name`, `stop_tier`, `label_priority`, `mode`. All features bundled into the existing `transit_stop_pills` PMTile source — no new bundle.

### Rendering (style)

- **Symbol layer(s)** filtered on `feature_type == "stop_label_anchor"`, one per padding tier (`normal_stop` split), `text-anchor: "left"`, `text-field: coalesce(display_name, stop_name)`, same font-weight / size / sort-key / collision expressions as far-zoom, `text-justify: "left"` and the `-0.11` em Saira vertical correction.
- **Line layer** filtered on `feature_type == "stop_label_leader"`, thin dark hairline (~0.8 px, dark grey), drawn BELOW the symbol layer so the text halo covers the leader's endpoint at the anchor.

All three layers use `minzoom: 14`, `maxzoom: 17` (close-zoom takes over at z17). Anchor sits beside/outside the pill construct at every zoom in the band, so labels never overlap the drawn geometry.

## Requirements — close-zoom (z17+)

_To be filled in when this phase is worked on._

## Constraints

- Standard view is unchanged. Place labels still show; stop labels stay hidden.
- Existing far-zoom dot rendering (sizes, colors, dedup, per-zoom line lists) is not touched.
- No new PMTile source. `label_priority` piggybacks on the existing `transit_stops_*` bundles.
- Numeric font sizes and the `tier_rank` order are placeholders and expected to be revised after visual review — the shape of the mechanism (per-tier size curve + sort-key collision) is the fixed part.
