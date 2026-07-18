# Stop text search

## Problem

There is no way to jump to a specific transit stop by name. Finding a known stop requires panning and zooming from memory.

## Requirements

### Visibility and scope
- The search input is visible **only in transit-focus view**. It disappears (or is hidden/removed from the DOM) in standard view. Uses the existing view-mode toggle; no new view mode is introduced.
- The input is prominently placed in the transit UI, easily discoverable without covering the primary map area.

### Search index
- The searchable set is every stop that appears on a drawn transit line — the same stops that render as dots / pills / pill-arrows on the map. Stops filtered out upstream (excluded agencies, EV-prefix routes, non-drawable trips, foreign termini) do not appear in results.
- Each entry carries: display name, coordinates, merged-UIC identifier, transport mode, and stop importance tier (the pipeline's `stop_tier`, e.g. `major_train` … `small_bus`). Mode and tier drive ranking; the UIC is kept for the future highlight step.
- One entry per unique station (dedup by merged UIC). When a station is served by multiple modes, the entry keeps the highest-ranked mode (train wins over metro, over tram, over bus, etc.) — matches the mode-rank order used elsewhere in the pipeline.
- The index is built at transit-pipeline time and shipped as a static JSON asset. Small enough to load once and search entirely client-side.

### Display names
- Names come from GTFS as-is. Swiss stops typically carry a city prefix (`Bern, Bahnhof`, `Zürich, Hauptbahnhof`) which supplies most of the disambiguation.
- Where a name does not include a city prefix, no synthesised disambiguation is added — the distance ranking handles same-named stops.

### Match behavior
- Case-insensitive substring match against the display name.
- Diacritic-insensitive: query `zurich` matches `Zürich`, `geneve` matches `Genève`. The fold applies to the query and to the comparison key; the displayed name keeps its diacritics.
- No fuzzy matching in this iteration — substring only.

### Result list (preview)
- As the user types, a dropdown appears below the input showing up to N matching stops (cap in the ~10 range; exact number a design choice, not a requirement).
- The preview does **not** move the map. No flyTo, no zoom, no pan while typing.
- When there are no matches, the dropdown shows a "no results" message.

### Ranking

Results are sorted by a weighted score. All signals are normalised to 0–100, the weighted sum decides the order (higher = better), the top N are shown. Ordering is recomputed on every keystroke and against the live map center — panning between keystrokes changes the order.

**Signals and their 0–100 normalisation:**

- **Match quality** — one of 5 discrete tiers scoring the query against the stop name (folded case + diacritics):
  1. Exact match — query equals the full name. Score `100`.
  2. Prefix of stop name — query starts the name. Score `70`.
  3. Full-word match anywhere — query equals a whole word inside the name (bounded by start, space, comma, or end). Score `40`.
  4. Word-prefix match anywhere — query starts some word in the name (but not the first). Score `20`.
  5. Substring match — appears mid-word. Score `10`.
- **Mode** — pipeline `MODE_RANK` (train = 0, metro = 1, tram = 2, bus = 3, mountain = 4, ferry = 5, regional_bus = 6). Normalised: `(6 − rank) / 6 × 100`.
- **Stop tier** — pipeline `stop_tier` string (`major_train` … `small_bus`, 12 buckets). Normalised inversely to the tier rank (0 = highest → `100`; 11 = lowest → `0`).
- **Distance to map view center** — exponential decay, `100 × exp(−distance_km / 30)`. Bounded [0, 100]; ~37 at 30 km, ~14 at 60 km, ~1 at 150 km.

**Weights** (starting values; expected to be tuned):

| Signal | Weight |
|---|---|
| Match quality | 5 |
| Mode | 1 |
| Stop tier | 1 |
| Distance | 1 |

**Design intent:** the 5× weight on match quality makes tier 1 (exact match) uncatchable by any lower tier; tier 2 (name prefix) can be caught by a much-better-signal tier 3 in edge cases (a very close major-station word-prefix outranks a distant unimportant name-prefix). Weights are deliberately not "hard" tiers — the point is to let strong secondary signals promote well-placed lower-tier matches, without ever letting a random substring in `Alchenflüh, Bernstrasse` outrank the actual `Bern` train station. Values are starting points; adjust after observing behaviour.

### Selection
- Selection is the only action that moves the map. Two ways to select:
  - **Enter** selects the top (highest-ranked) result.
  - **Click** on any list entry selects that entry.
- Selection triggers a `flyTo` on the map to the selected stop's coordinates at a zoom level where the stop's pill / pill-arrow is legible (roughly z16, exact value a design choice).
- After selection: the dropdown closes. Whether the input clears or keeps the selected name is a UX detail, not a hard requirement.

### Highlight after flyTo (deferred)
- After the map settles on the selected stop, the stop should be visually highlighted (flash, ring, or similar) so the user can spot it among neighbouring stops. This is a **second step**, deferred to a later iteration. The initial implementation ships without it; the index carries the merged-UIC so this can be added later without a data-model change.

## Constraints

- The index is derived from the transit pipeline's output — it is regenerated whenever the pipeline runs, not maintained separately.
- Search is entirely client-side. No external geocoder, no API call.
- Nothing outside transit-focus view depends on this feature. Standard view is unchanged.
- Ranking uses map view center, not browser geolocation. Geolocation is not requested.
