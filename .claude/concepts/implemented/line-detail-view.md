# Line Detail View

## Problem

The map always shows the full network. There is no way to inspect a single line — where it runs, which stops it serves — without visually untangling it from every parallel and crossing line. The popups already show line badges; clicking one currently does nothing.

## Requirements

### Trigger and line identity

- Clicking a line badge — in the station popup's badge list (collapsed or expanded) or in the line popup's row list — enters **line detail view** for that line.
- "The line" means **all variants** of the `(ref, agency_id, mode, trip_group_id)` group: both directions, all branch/short-turn merged_stop_sets that step 06's union-find placed under the same `trip_group_id`. Everything below (highlight, bbox, stop membership) is defined over that full variant set.
- `(ref, mode)` alone is not sufficient identity — same-numbered lines in different cities (e.g. BernMobil bus 10 vs Stadtbus Winterthur bus 10) must not be conflated; `agency_id` disambiguates them. `agency_id` alone is not sufficient either — one agency can reuse the same number in disjoint regions (canonical case: PostAuto bus 100 exists as Rheinfelden ↔ Gelterkinden AND as Aarberg ↔ Bern, both under agency 801). Step 06's union-find gives geographically disjoint variants distinct `trip_group_id`s, and the identity must carry it through.

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
- Far-zoom dots that absorb other stops count as members **only at the zoom levels where the absorption is active**. An eater dot represents an eaten member's line at zooms where the eaten is invisible; at zoom levels where the eaten dot renders on its own, only the eaten dot carries the membership and the eater falls back to its own line set. This prevents both dots being highlighted at the same time once they visually separate.

### Title bar

- While the view is active, a title element sits at the top of the screen showing the line badge (same styling as popup badges: color, ref, Saira ExtraBold) and the line's route text (`A ↔ B` across all variants, same rule as the line popup).
- An **X button** in the title bar closes the view: all filters, paint overrides, and the dim are reverted; the camera stays where the user left it.
- Clicking a different line badge while the view is active switches the view to that line (no need to close first).

### Deep link

- The current line detail view is reflected in the URL via a `line` query parameter carrying the line's canonical key (`ref`, `agency_id`, `mode`). Format is an implementation detail; the requirement is that the key round-trips unambiguously and is URL-safe.
- Opening the page with `?line=<key>` present enters line detail view for that line automatically, as if the badge had been clicked — same camera fit, same visual state, same title bar.
- Entering, switching, and closing the view keeps the URL in sync (entry / switch sets the param, close removes it) so any moment of the view is a shareable link. The URL update must not push a history entry per interaction (replace, don't push) — the browser back button should not become a per-click undo of highlight state.
- An unknown or malformed `line` key on load is ignored silently: the map opens normally with no line highlighted and the param is dropped from the URL.
- The `view` param (see `view-modes.md`) is independent. A deep link may combine both (e.g. `?view=transit-focus&line=...`); a `line` link without `view` respects the user's current / default view mode — line detail does not force transit-focus.

### Data prerequisites (build time)

Baked at pipeline build time so the client needs no extra fetches:

- Badge entries (the per-line objects in `lines_json` and the line popup's capture set) additionally carry:
  - `agency_id` — for unambiguous line identity.
  - `bbox` — `[minLon, minLat, maxLon, maxLat]` union over all variants of the line, for the camera fit.
- Stop features that must be filterable (dots, pills, endpoints, connectors, indicators, label anchors) carry a new property `line_keys`: a delimiter-padded string of the line keys served (each key `ref~agency_id~mode~trip_group_id`), padded so exact-key substring matching cannot false-positive on prefix collisions.
- Transit line features already carry `ref`, `agency_id`, and `mode` — no change needed there.

## Constraints

- Everything is client-side state on the one shared `style.json` — no style regeneration per selection, consistent with the view-modes mechanism.
- The view works in both `standard` and `transit-focus` view modes; the stop-hiding requirement only has visible effect in `transit-focus` (standard hides stop symbology anyway).
- Normal popup behavior (clicking stops/lines) remains available inside the view.
- Selected-line width emphasis must not alter the frequency→width encoding of the rest of the map — only the selected line deviates, and only while the view is active.
