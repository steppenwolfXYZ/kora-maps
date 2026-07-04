# View Modes: Standard / Transit Focus

## Problem

Stop dots and pills render on top of city names, degrading both: place labels are
obscured exactly where stops cluster (city centers), and the stop symbols compete
with text for the same visual space. One combined view cannot serve both "orient
myself geographically" and "read the transit network" at once.

## Requirements

### View model

- The map has exactly two views: **`standard`** and **`transit-focus`**. These two
  identifiers are the canonical names for the modes everywhere (UI state, code,
  docs).
- **`standard` is the default view** on page load. *(Implementation note: while
  stop rendering is under active development, the code temporarily defaults to
  `transit-focus` via a `DEFAULT_VIEW` dev override. Flip it back to `standard`
  before shipping.)*
- The user switches views via a visible control on the map UI (frontend,
  `Map.svelte` level). Switching is instant and client-side: no page reload, no
  style re-fetch, no loss of camera position or zoom.
- Both views are served by the **same generated `style.json`**. The view switch
  toggles layer visibility at runtime; the generator does not produce two style
  files.

### Standard view

- All place labels visible exactly as today (cities, towns, villages,
  suburbs/quarters, regions, countries). No change to current label styling,
  sizing, or collision priorities.
- Transit lines visible exactly as today (same colors, widths, zoom behavior).
- **All stop symbology completely hidden**: far-zoom dots, pill-zoom dots, pills,
  pill endpoints, connectors, and the per-color indicator dots. Hidden means not
  rendered at any zoom — not faded, not shrunk.
- Because stop layers are hidden, clicking where a stop would be must NOT open a
  stop popup; line-click popups keep working.

### Transit-focus view

- **All place-name labels hidden**: city, town, village, suburb / neighbourhood /
  quarter, state/region, and country labels. "Everything" is literal — no place
  name of any class or rank renders in this view.
- Non-place labels (water names, street names, POI labels) are unaffected and
  render the same as in standard view.
- All stop symbology visible with its current styling and zoom behavior (far-zoom
  dots, pills, connectors, indicators — unchanged from today's rendering).
- Transit lines identical to the standard view.
- Stop click popups work as today.
- This view is the future home of **stop labels** (station names attached to
  dots/pills). Stop labels are explicitly out of scope for this concept — a
  follow-up concept will define them — but nothing in this change may preclude
  adding a stop-label layer group that is visible only in transit-focus.

## Constraints

- Transit line rendering must be byte-identical between the two views — the
  toggle affects only stop symbology and place labels.
- No pipeline changes: PMTiles content, `07_extract_stops.py` outputs, and all
  `data/transit/` artifacts stay as they are. This is a style/frontend concern
  only.
- The debug stop layer (`debug-stop-dot`) is a developer probe and is not part of
  either view's definition; it keeps its current independent behavior.
- The view toggle state does not need to persist across page reloads (default
  back to standard is acceptable for now).
- Hover cursor behavior must match visibility: hidden stop layers must not switch
  the cursor to pointer in standard view.
