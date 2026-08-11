# Route Display

## Problem

Transit routing (MOTIS) returns a sequence of legs describing a chosen journey. We need to display the found route on the map. Two things make this harder than the default map: (a) a leg may use a line or variant we deliberately don't render on the default map (replacement services, depot runs, seasonal variants), and (b) the trip uses specific platforms while the default map renders merged station pills.

## Requirements

### Route geometry source

- Use MOTIS's per-leg polylines directly as they come back from the routing engine.
- Do not precompute route variants in the pipeline. Do not run pfaedle on the fly.
- A leg's polyline covers the vehicle's path from the boarding stop to the alighting stop; its endpoints correspond to where the passenger boards and alights.

### Per-leg rendering

- **Transit leg** — polyline drawn in the leg's mode/line color, with the same visual language as a selected line in the line detail view (same casing, same width scaling). A transit leg using a line normally hidden on the default map renders identically to a visible one; the user should not have to notice the difference.
- **Walking leg** — thick dashed line in a neutral color. Applies to any walk: to the first station, between stations during a transfer that involves outdoor walking, from the final station to the goal.

### Stops on the route

- **Transfer stop** (change of vehicle) — two neutral discs, one for the arrival platform and one for the departure platform, connected by a neutral connector. Discs and connector all in the same neutral routing color; leg colors live only on the polylines that touch each disc.
- **First boarding**
  - If a walking segment precedes it → single neutral disc (same styling as a transfer disc, no connector).
  - If the journey starts at that station itself with no preceding walk → replace the disc with the **start icon** (small filled circle).
- **Final alighting** (mirror of first boarding)
  - If a walking segment follows → single neutral disc.
  - If the journey ends at that station itself → replace the disc with the **goal icon** (checkered flag).
- **Pass-through stops** — stops the vehicle serves within a leg without the passenger transferring: small neutral dots along the polyline.
- **Disc position** — every disc snaps to the leg polyline, preferably at the polyline endpoint (the arrival disc at the end of the arriving leg's polyline; the departure disc at the start of the departing leg's polyline). Same "snap to line" logic used in pill design.

### Everything else on the map

- Non-route transit lines: desaturated, same treatment as non-selected lines in the line detail view.
- Non-route stops: matching reduced style, same treatment as in the line detail view.
- Basemap layers unchanged.

### Lifecycle

- The route is deep-linkable. URL carries the **full leg breakdown** as the source of truth: canonical `line_key` per transit leg, boarding and alighting stop ids (with platform suffix), leg times, walking-leg endpoints.
- On load, MOTIS is queried again to verify the URL's legs are still valid against the current timetable state.
  - If valid → render the route as described.
  - If no longer valid (trip removed, platform changed, timetable rebuilt) → show an error message. The route is not partially rendered from stale URL data.
- The map auto-frames to the route's bbox on open.
- Opening the route pushes a history entry, so browser back closes it (same pattern the line detail view uses). A close X in the route title bar also closes it and consumes the history entry.

### Identifiers introduced

- URL param `?route=…` carrying the encoded full leg breakdown.
- Route-scoped "neutral routing color" for discs, connectors, walking dashes, and pass-through dots — one shared color, decided during implementation.
- `start` and `goal` icons: filled circle and checkered flag respectively.

## Constraints

- No pipeline output is modified to precompute route variants.
- The two-disc transfer visual is a new dedicated element for route display; it is **not** a variant of the per-platform pill or the merged pill and does not interact with the existing pill/pill-arrow rendering.
- Discs, connectors, walking dashes, and pass-through dots are always in the same neutral color. Leg mode/line colors appear only on the transit-leg polylines.
- The route line uses no fallback shaping (no on-the-fly pfaedle, no straight-line fallback). If MOTIS returns no polyline for a leg, treat that as an invalid-route condition rather than filling with a heuristic.
- Platform-accurate rendering here is scoped to the transfer discs sitting on the leg polyline endpoints. The default map's merged pills are not changed by this feature.
