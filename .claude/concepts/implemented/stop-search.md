# Stop text search

## Problem

There is no way to jump to a specific transit stop by name. Finding a known stop requires panning and zooming from memory.

## Requirements

### Visibility and scope
- The search input is visible **only in transit-focus view**. It disappears (or is hidden/removed from the DOM) in standard view. Uses the existing view-mode toggle; no new view mode is introduced.
- The input is prominently placed in the transit UI, easily discoverable without covering the primary map area.

### Search index
- The searchable set is every stop that appears on a drawn transit line — the same stops that render as dots / pills / pill-arrows on the map. Stops filtered out upstream (excluded agencies, EV-prefix routes, non-drawable trips, foreign termini) do not appear in results.
- Each entry carries: display name, coordinates, merged-UIC identifier, transport mode, stop importance tier (the pipeline's `stop_tier`, e.g. `major_train` … `small_bus`), and the station colors `cd` (dominant — the station's drawn dot color) / `ca` (mean-RGB average over every distinct line color at the station, all modes). Mode and tier drive ranking; the UIC is kept for the future highlight step; the colors feed the routing panel's Connect tiles and recent-route stop boxes (a `ca`→`cd` gradient), not search itself.
- One entry per unique station (dedup by merged UIC). When a station is served by multiple modes, the entry keeps the highest-ranked mode (train wins over metro, over tram, over bus, etc.) — matches the mode-rank order used elsewhere in the pipeline.
- The index is built at transit-pipeline time and shipped as a static JSON asset. Small enough to load once and search entirely client-side.

### Display names
- Names come from GTFS as-is. Swiss stops typically carry a city prefix (`Bern, Bahnhof`, `Zürich, Hauptbahnhof`) which supplies most of the disambiguation.
- Where a name does not include a city prefix, no synthesised disambiguation is added — the distance ranking handles same-named stops.

### Match behavior
- Case-insensitive matching against the display name.
- Diacritic-insensitive: query `zurich` matches `Zürich`, `geneve` matches `Genève`. The fold applies to the query and to the comparison key; the displayed name keeps its diacritics.
- **Multi-word queries**: the query is split on whitespace into tokens. Every token must match somewhere in the name (word, word-prefix, or substring) for the stop to be a hit — order-insensitive, so `eigerplatz bern` finds `Bern, Eigerplatz`. Name words are split on whitespace and punctuation (commas etc. are separators, not word content).
- No typo-tolerant fuzzy matching in this iteration. It is an anticipated future requirement; when it comes, the plan is to swap the match-quality signal for a fuzzy-match score (e.g. a fuzzysort/uFuzzy-style library) rather than extending the tier cascade.

### Result list (preview)
- As the user types, a dropdown appears below the input showing up to N matching stops (cap in the ~10 range; exact number a design choice, not a requirement).
- The preview does **not** move the map. No flyTo, no zoom, no pan while typing.
- When there are no matches, the dropdown shows a "no results" message.

### Ranking

Results are sorted by a weighted score. All signals are normalised to 0–100, the weighted sum decides the order (higher = better), the top N are shown. Ordering is recomputed on every keystroke and against the live map center — panning between keystrokes changes the order.

**Signals and their 0–100 normalisation:**

- **Match quality** — one of 8 discrete tiers scoring the query tokens against the stop name's words (folded case + diacritics). Evaluated top-down; the first tier whose condition holds wins. "Prefix of a word" includes the full word itself (every word is its own prefix), so lower tiers' "at least substring" covers full and prefix matches too.
  1. **Exact** (`100`) — every query token full-word matches and every word of the name is matched. Order and punctuation don't matter: `eigerplatz bern` = `Bern, Eigerplatz`.
  2. **Name-prefix start** (`80`) — every token is a prefix of some name word, and the name's **first word** is fully matched by some token. `bern eigerpl` → `Bern, Eigerplatz`.
  3. **Contains name prefix** (`70`) — some token is a prefix of the name's first word, all other tokens match at least as substring. `ber eigerplatz` → `Bern, Eigerplatz`.
  4. **All words full match** (`50`) — every token full-word matches (somewhere in the name).
  5. **Some words full match** (`40`) — at least one token full-word matches, the rest match at least as substring.
  6. **All word-prefix** (`30`) — every token is a prefix of some name word.
  7. **Some word-prefix** (`20`) — at least one token is a prefix of some name word, the rest match at least as substring.
  8. **Substring only** (`10`) — every token appears somewhere, none at a word boundary.

  Deliberate consequence: tier 3 outranks tier 4 — matching the start of the name (city prefix) with the rest as loose substrings beats full-word matches scattered elsewhere in the name.
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

**Design intent:** with the 5× weight, one match-quality tier step of 10 points equals 50 weighted points, while the other three signals together contribute at most 300 — so adjacent tiers can be overtaken by strong secondary signals, but a gap of two or more large tier steps (e.g. exact vs. full-word, 100 vs. 50) is effectively uncatchable. Weights are deliberately not "hard" tiers — the point is to let strong secondary signals promote well-placed lower-tier matches, without ever letting a random substring in `Alchenflüh, Bernstrasse` outrank the actual `Bern` train station. Values are starting points; adjust after observing behaviour.

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
