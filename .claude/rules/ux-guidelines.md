# UX / Design Guidelines

App-UI design language (SvelteKit side, `src/`). The map's own design
language lives in `project.md` § Basemap design language.

## Brand colors

Three brand colors, all defined as tokens in `src/variables.css` and
**always used via the variables**, never as literals:

| Token | Value | Role |
|---|---|---|
| `--gradient-brand` | kora green → kora brown, 135° | selected/active states, accents |
| `--brand` (red) | `#740013` | primary actions, hover emphasis |
| `--anthracite` | `#333` | dark fills, titles, dark chrome |

Supporting tokens: `--kora-green` / `--kora-brown` (the gradient
endpoints), `--gradient-brand-input` (165° variant), `--brand-hover`.

The splash screen in `app.html` duplicates the gradient values as
literals (it paints before any stylesheet loads) — keep them in sync
with `variables.css`.

## Usage rules

**Gradient** (always diagonal — 135°, or the 165° `--gradient-brand-input`
variant on thin wide elements where 135° would read as a flat
left-to-right fade):

- Selected/active states: segmented-toggle active segment, switch
  on-state, open menu disc, selected time row — always **white**
  text/icons on the gradient.
- Panel accents: 3px hairline along the top edge of floating panels
  (routing panel, menu panel), as a layered background so it follows
  the corner radius and never scrolls.
- Focus rings on text inputs (steep variant): permanent 2px transparent
  border + double background (`padding-box` fill, `border-box`
  gradient) so focusing never shifts layout.
- Selected connection card: 4px gradient strip on the left edge,
  painted *below* the selection border (the border is an `::after`
  overlay ring so it stays above the strip and all content).

**Brand red:**

- Primary action buttons (share primary, route button) — fill on
  hover/active, red glyph at rest for the route entry point.
- Hover color for icon buttons (see button system below) and for
  button-styled links. **Not** for normal text-link hovers — too much.
- Semantic accent (e.g. the "now" row in the time dropdown). No
  foreign accent colors (the old blue is gone).
- The routing panel's title icon: red circle, white glyph.

**Anthracite:**

- Panel/section titles (uppercase micro-titles).
- Keyboard-highlight rows in dropdowns (white text on anthracite) —
  highlight is hover-like, so it stays dark, not gradient.
- Dark fills like the loader track.

**Gray ramp:** everything else — list hovers, secondary buttons,
body text. List/row hovers stay gray, never gradient.

**Never:** red + gradient on the same element; kora green or kora
brown as standalone text/icon colors (tried, reverted — they only work
inside the gradient and the loader ball).

## Toggles (segmented controls)

No container border. Inactive segments `--gray-100` with dark text;
active segment gradient with white text. (View toggle, Leave-at /
Arrive-by.)

## Icon button system

`.icon-btn` in `app.css` § Shared patterns: backgroundless button that
gets a circular light-gray fill and a **brand-red** glyph on hover.
Used by all small utility buttons — × close/clear buttons, date
chevrons, swap, reset-to-now. Components add only sizing (padding,
font-size, margins) and must not re-set color/background, or the hover
state loses the cascade. Any new small icon button uses this class.

## Route endpoint glyphs

One iconography for "start / stop on the way / destination" everywhere
the routing endpoints appear, built from a dot and a line
(`src/lib/routing/routeGlyphs.ts`, `routeGlyphSvg(side, box)`):

| Side | Glyph | Meaning |
|---|---|---|
| `from` | `o──` | dot at the outer edge, line running to the inner edge |
| `via` | `──o──` | line across, dot in the middle |
| `to` | `──o` | mirror of `from` |

The SVG fills its host box edge to edge with **no padding**, so the line
really reaches the edge — that is what lets neighbouring glyphs fuse.
Two arrangements:

- **Side by side** (station / place popup route buttons): from + to in
  a brand-red segmented pill split by a white hairline, white glyphs;
  the hairline joins the two line ends into one route line `o──|──o`.
  The line thickness matches the hairline (2px) so the stroke is
  continuous. The lead is a small anthracite "Route" text label, never
  an icon — an icon beside two icon buttons reads as a third, dead
  control.
- **Stacked** (map context menu items, the routing panel's From / Via /
  To field labels): dot flush at the outer edge (`inset` = `r`) so every
  row spans the same width and the lines align as one column, only the
  dot moves. Brand red on the plain background — a red chip behind each
  was tried and found too heavy.

The map's route pins (start / via / goal, `routeLayers.ts`) carry the
same three glyphs clipped to the pin head: brand-red teardrop, white
glyph, thin white outline, colors via the `--brand` / `--white` tokens
(inline SVG in the document, so CSS vars resolve). The old play / stop /
skip-next shapes are gone everywhere; do not reintroduce them.

## Loader

The routing "searching" indicator (`RoutingPanel.svelte`): full-width
2.2rem pill, anthracite fill, 2px gradient border, ball swinging
side-to-side (1s alternate ease-in-out) fading kora green ↔ kora
brown. The border gradient is deliberately **horizontal** (the one
exception to the diagonal rule): the ball's color fade tracks its
x-position, so the border above/below the ball always matches the
ball's color.
