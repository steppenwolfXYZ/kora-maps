# Route Display

## Problem

Transit routing (MOTIS) returns a sequence of legs describing a chosen journey. We need to display the found route on the map. Two things make this harder than the default map: (a) a leg may use a line or variant we deliberately don't render on the default map (replacement services, depot runs, seasonal variants), and (b) the trip uses specific platforms while the default map renders merged station pills.

## Requirements

### Route geometry source

- Use MOTIS's per-leg polylines directly as they come back from the routing engine.
- Do not precompute route variants in the pipeline. Do not run pfaedle on the fly.
- A leg's polyline covers the vehicle's path from the boarding stop to the alighting stop; its endpoints correspond to where the passenger boards and alights.

### Per-leg rendering

- **Transit leg** — polyline drawn in the leg's mode/line color, with white casing (same casing color as the map's own transit lines). Drawn substantially **wider** than any map line — thicker than the line-detail view's highlight — so the route reads unambiguously as the primary content against the desaturated basemap. A transit leg using a line normally hidden on the default map renders identically to a visible one; the user should not have to notice the difference.
- **Walking leg** — thick dashed line in a neutral color. Applies to any walk: to the first station, between stations during a transfer that involves outdoor walking, from the final station to the goal.

### Stops on the route

- **Transfer stop** (change of vehicle) — two neutral discs, one for the arrival platform and one for the departure platform, connected by a neutral connector. Discs and connector all in the same neutral routing color; leg colors live only on the polylines that touch each disc.
- **First boarding** — always a single neutral disc at the boarding stop (same styling as a transfer disc, no connector). The **start icon** (small filled circle) marks the walk's origin when a walk precedes the transit leg, and overlays the disc when the journey starts at the station itself.
- **Final alighting** (mirror of first boarding) — always a single neutral disc at the alighting stop. The **goal icon** (checkered flag) marks the walk's endpoint when a walk follows the transit leg, and overlays the disc when the journey ends at the station itself.
- **Pass-through stops** — stops the vehicle serves within a leg without the passenger transferring: small neutral dots along the polyline.
- **Disc position** — every disc snaps to the leg polyline, preferably at the polyline endpoint (the arrival disc at the end of the arriving leg's polyline; the departure disc at the start of the departing leg's polyline). Same "snap to line" logic used in pill design.

### Everything else on the map

- Non-route transit lines: desaturated, same treatment as non-selected lines in the line detail view.
- Non-route stops: matching reduced style, same treatment as in the line detail view.
- Basemap layers unchanged.

### Lifecycle

- The route is deep-linkable. URL carries a **fingerprint** of the leg breakdown as the source of truth: a short stable hash derived from every leg's mode, transit route / trip identity, boarding + alighting stop ids (with platform suffix), and leg times. The fingerprint identifies one specific itinerary within the panel query's result set; the panel query itself (from / to / mode / time) rides in the URL alongside.
- On load, MOTIS is queried again with the panel-query params, and the URL fingerprint is matched against the returned itineraries' fingerprints:
  - Match → render that itinerary as described.
  - No match (trip removed, platform changed, timetable rebuilt) → show an error message in the routing panel. The route is not partially rendered from stale URL data.
- On a fresh in-session query with no fingerprint pending, the **first result is auto-selected** so the user sees a route on the map immediately.
- The map auto-frames to the route's bbox on open.
- Opening the route pushes a history entry, so browser back closes it (same pattern the line detail view uses). A close × on the selected result card also closes it and consumes the history entry.

### Identifiers introduced

- URL param `?route=…` carrying the itinerary fingerprint.
- Route-scoped "neutral routing color" for discs, connectors, walking dashes, and pass-through dots — one shared color, decided during implementation.
- `start` and `goal` icons: filled circle and checkered flag respectively.

## Constraints

- No pipeline output is modified to precompute route variants. Shape availability for routing lives in the pfaedle output that already exists (see transit-routing.md § Backend).
- The two-disc transfer visual is a new dedicated element for route display; it is **not** a variant of the per-platform pill or the merged pill and does not interact with the existing pill/pill-arrow rendering.
- Discs, connectors, walking dashes, and pass-through dots are always in the same neutral color. Leg mode/line colors appear only on the transit-leg polylines.
- The route line uses no fallback shaping (no on-the-fly pfaedle, no straight-line fallback). If MOTIS returns no polyline for a leg, treat that as an invalid-route condition rather than filling with a heuristic.
- Platform-accurate rendering here is scoped to the transfer discs sitting on the leg polyline endpoints. The default map's merged pills are not changed by this feature.
- Route display and line-detail view are mutually exclusive: opening the routing panel closes any open line-detail (already covered by transit-routing.md); entering line-detail from a route context — clicking a line badge in a popup while a route is displayed — closes the routing panel and tears the route overlay down first.
