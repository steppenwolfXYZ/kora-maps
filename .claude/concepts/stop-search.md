# Stop text search

## Problem

There is no way to jump to a specific transit stop by name. Finding a known stop requires panning and zooming from memory.

## Requirements

### Visibility and scope
- The search input is visible **only in transit-focus view**. It disappears (or is hidden/removed from the DOM) in standard view. Uses the existing view-mode toggle; no new view mode is introduced.
- The input is prominently placed in the transit UI, easily discoverable without covering the primary map area.

### Search index
- The searchable set is every stop that appears on a drawn transit line — the same stops that render as dots / pills / pill-arrows on the map. Stops filtered out upstream (excluded agencies, EV-prefix routes, non-drawable trips, foreign termini) do not appear in results.
- Each entry carries: the stop's display name, its coordinates, and its merged-UIC identifier (the identifier the future highlight step will need to address the correct on-map feature).
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
- When multiple stops match the query, results are sorted by distance from the **current map view center** (closer first). This resolves same-named stops (many `Bahnhof`, `Post`, `Dorf` entries exist across the country) and biases toward what the user is likely looking at.
- Ordering is recomputed each keystroke against the live map center — panning between keystrokes changes the order.

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
