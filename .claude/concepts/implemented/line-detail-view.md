# Line Detail View

## Problem

The map always shows the full network. There is no way to inspect a single line — where it runs, which stops it serves — without visually untangling it from every parallel and crossing line. The popups already show line badges; clicking one currently does nothing.

## Requirements

### Trigger and line identity

- Clicking a line badge — in the station popup's badge list (collapsed or expanded) or in the line popup's row list — enters **line detail view** for that line.
- "The line" means **all variants** of the `(ref, agency_id, mode)` group: both directions, all branch/short-turn variants. Everything below (highlight, bbox, stop membership) is defined over that full variant set.
- `(ref, mode)` alone is not sufficient identity — same-numbered lines in different cities (e.g. BernMobil bus 10 vs Stadtbus Winterthur bus 10) must not be conflated. `agency_id` disambiguates.

### Camera

- On entry, the map moves and zooms so the full line — the union of all variants' geometry — is visible and centered, with comfortable padding.
- Panning and zooming stay fully enabled while in the view.

### Visual state

While the view is active:

- **Basemap dimmed slightly** — everything below the transit lines loses contrast; the exact treatment (uniform translucent veil or equivalent) is an implementation choice.
- **Selected line wider** — all variants of the selected line render wider than their normal frequency-derived width, in their normal color.
- **Other lines de-emphasized** — every non-selected transit line is grayed out or made more transparent (whichever is easier); it must clearly read as background.
- **Non-member stops hidden** — stop symbology (far-zoom dots, pills, endpoints, connectors, color-dot indicators, stop name labels) is hidden for stations the selected line does not serve. Member stations keep their normal rendering.
- Pill-arrows (z17+) are **out of scope** — they keep their normal behavior and are not filtered. The view is expected to be used at far and pill zoom.

### Stop membership

- A station belongs to the line if any variant of the line serves it.
- Far-zoom dots that absorb other stops count as members if the station itself **or any absorbed stop** is served by the line (zoom-independent union — a dot must not disappear while representing a member station).

### Title bar

- While the view is active, a title element sits at the top of the screen showing the line badge (same styling as popup badges: color, ref, Saira ExtraBold) and the line's route text (`A ↔ B` across all variants, same rule as the line popup).
- An **X button** in the title bar closes the view: all filters, paint overrides, and the dim are reverted; the camera stays where the user left it.
- Clicking a different line badge while the view is active switches the view to that line (no need to close first).

### Data prerequisites (build time)

Baked at pipeline build time so the client needs no extra fetches:

- Badge entries (the per-line objects in `lines_json` and the line popup's capture set) additionally carry:
  - `agency_id` — for unambiguous line identity.
  - `bbox` — `[minLon, minLat, maxLon, maxLat]` union over all variants of the line, for the camera fit.
- Stop features that must be filterable (dots, pills, endpoints, connectors, indicators, label anchors) carry a new property `line_keys`: a delimiter-padded string of the line keys served (each key `ref|agency_id|mode`), padded so exact-key substring matching cannot false-positive on prefix collisions.
- Transit line features already carry `ref`, `agency_id`, and `mode` — no change needed there.

## Constraints

- Everything is client-side state on the one shared `style.json` — no style regeneration per selection, consistent with the view-modes mechanism.
- The view works in both `standard` and `transit-focus` view modes; the stop-hiding requirement only has visible effect in `transit-focus` (standard hides stop symbology anyway).
- Normal popup behavior (clicking stops/lines) remains available inside the view.
- Selected-line width emphasis must not alter the frequency→width encoding of the rest of the map — only the selected line deviates, and only while the view is active.
