# Routing map / details split

## Problem

Viewing a routed connection on the map barely works on phones: the routing panel sits at the top of the screen spanning nearly full width, can grow to fill the whole viewport, and the only way to see the route underneath is to close the panel — which wipes the routing state. The camera framing (`fitBounds` padding) is desktop-biased and frames the route under the panel on narrow screens. Separately, the interaction model conflates two things in one click: expanding a result card's details and showing that connection on the map. There is no way to peek at a route on the map without opening its card, and no way to open details without affecting the map.

## Requirements

### Two independent per-connection states

- **Expanded** — the card's details (leg list) are open in the results list.
- **Selected** — the connection is rendered on the map (the existing route overlay; unchanged visually).

Neither state implies the other as a hard rule; the click behaviors below define how they interact per platform. The URL `?route=` param continues to carry only the *selection* (existing fingerprint mechanism). Expansion and map-mode are ephemeral UI state — not serialised, not restored on cold load.

### Result card anatomy

- Each card gains a single **map icon button** at the **bottom right**, in the summary meta row (transfers · walking) — it shares width only with the summary text, which doesn't need it. Icon: the filled Material Symbols `map` glyph in the brand red (`#740013`), the one coloured accent on the otherwise monochrome card. Visible at all times, not hover-only.
- The existing badge / warning placement is preserved; the map icon must not collide with them.
- **Primary click on the card is a two-step, identical on both platforms** (revised after the first version, which expanded on click and selected only on desktop): the first click makes the connection the **active** one (selected — the highlight, and the map render where the map is visible); clicking the already-active card **toggles its details**, opening and then closing them. Double-clicking a fresh card consequently selects and opens it in one gesture. Rationale for dropping the original expand-on-click: on desktop the map sits beside the list, so a click on a connection means "show me that one" — routing that everyday action through the map icon felt unnatural; a device-split click was tried in between and rejected as more confusing than the shared two-step. It never enters any map mode on either platform.

### Desktop (wide viewport)

- Clicking a connection that is not the active one: selects it on the map, details untouched. Clicking the **already-active** connection: opens its details, and closes them on the next click — the same path as the chevron, which also re-frames the route overview (undoing a prior per-leg zoom).
- Clicking the map icon: same as clicking the card body (kept for symmetry with mobile and as the explicit affordance).
- The desktop panel layout is otherwise unchanged.
- The attribution control is bounded to the space right of the panel, so expanded credits never slide underneath it.

### Mobile (narrow viewport)

- The routing panel becomes **full-width** — a full-bleed page, not a floating card with margins.
- Clicking a connection: the same two-step as desktop — first click selects (highlight only; the map is not visible here), second click toggles the details. It does **not** enter map mode — the map icon does that.
- Clicking the map icon: enters **fullscreen map mode** for that connection (selects it and shows the map).
- **Fullscreen map mode**: the results list / panel is hidden and the map fills the viewport. A **summary header** sits at the top of the map (same visual pattern as the line-detail view's top bar) containing:
  - the connection summary: departure–arrival times, duration, transfers, mode-icon strip with line badges,
  - an **× button** at the right that returns to the results list (selection persists). *(First version had a back arrow plus a list/details button — collapsed into the single × after review, as the pair read as redundant.)*
- While the routing list is open on mobile, the zoom / orientation / geolocate map controls are **hidden**; in fullscreen map mode they stay visible, **pushed down below the summary header**.
- Switching to a different connection happens only via the list (×, then map icon on another card). No prev/next arrows in the header in this step.

### Camera framing

- `fitBounds` padding accounts for the actual overlay at each breakpoint instead of a fixed desktop-biased guess:
  - Desktop: keep the left-heavy padding that clears the side panel.
  - Mobile fullscreen map mode: pad the top by the summary header's height plus margin (measured or fixed constant matching the header), modest padding elsewhere.
- The same rule applies to per-leg focus framing from an expanded card.

### State and history

- Selection keeps its existing history semantics (push on user selection, back closes the route view).
- Mobile fullscreen map mode is a local UI flag. It opens automatically nowhere; it is entered only via the map icon and left only via the header's × button. Browser back while in map mode follows the existing selection-history behavior (it closes the selection, which also leaves map mode).

## Constraints

- No changes to MOTIS client, query cascade, ranking, badges, or warnings logic.
- The route overlay rendering (polylines, discs, walk dashes, markers, dim veil, desaturation) is unchanged; only when/where it is framed and what chrome surrounds it.
- The line-detail-view top-bar pattern is reused for the map-mode header; the two views never coexist (opening routing already closes line detail).
- One breakpoint constant decides narrow vs wide layout, shared by the panel CSS, the map-icon behavior, and the camera framing (the current code uses two different values — 600 px in panel CSS, 700 px in `fitBounds`; unify).
- Auto-select on a fresh query picks the first result for `leave-at` and the last for `arrive-by` (the most relevant end — see `transit-routing.md` § Results Sort). On mobile it must not enter map mode by itself; the selection may still be set so the map icon has something to show.
