# Stop Color Indicators

## Problem

The medium-zoom stop family (dots, endpoint discs, pills) renders with white fill and a 1 px black border, with mode color carried as a feature attribute but unused by the production paint. Once the map is zoomed close enough that a station's individual platforms are distinguishable, the user has no in-place signal for *which* line modes serve a station — a busy interchange (tram + city bus + regional bus + train) is visually indistinguishable from a quiet single-line stop. The popup carries the info but requires a click. At zoom 16 and closer there is enough screen room inside the stop construct to surface a compact per-mode indicator without a click.

## Requirements

### When

The indicators appear starting at **zoom 15**. Below z15 they are not rendered. They fade in via a fast opacity ramp from `15.0 → 15.3` (genuine opacity fade across that short zoom window — not the longer `appear → appear + 1.0` curve used by other stop family layers).

### What an indicator is

A small filled circle drawn inside a stop dot, endpoint disc, or pill. **No border.** Sized so multiple fit comfortably inside the parent feature at every zoom from 16 upward. The indicator's diameter scales with the stop family's `dot/disc/pill diameter` so the relative size is constant across zoom.

### Color groups

One indicator is emitted per **color group** that serves the location. The groups are: `train`, `metro`, `tram`, `bus`, `regional_bus`, `mountain`. Ferry collapses into the `bus` group (the two share a color, so showing both as separate indicators would produce two identical marks at the few stops where both meet). Modes outside this set do not contribute an indicator.

### Color picked per group

Within a group, the indicator's color is the color of the **fastest line** in that group at the location — the line with the highest `f_weighted` (the weighted trips-per-hour value already produced by step 06). Ties are broken by ascending `gtfs_ref`.

The pick is data-driven: the indicator just reads the color attribute of the picked line. It does **not** evaluate luminance or any other property of the color hex, so future changes to the per-mode color scheme automatically flow through.

### Indicator emission rule

An indicator is emitted for every color group present at the location, including locations with a **single** group (a single-mode bus stop shows one mini blue dot). The rule is uniform across dots, endpoint discs, and pills — no special-casing single-group locations.

### Placement

A unified **centered row** layout is used for all three parent kinds (stop dot, endpoint disc, pill), centered on the parent's center coordinate (the dot's center, the disc's center, or the pill's midpoint).

The row's orientation depends on the parent kind:

- **Stop dot / endpoint disc** — screen-horizontal (no inherent tangent).
- **Pill** — aligned with the pill's local tangent at its midpoint, in map space (rotates with the map).

Within the row, indicators appear in `COLOR_GROUP_ORDER` (`train`, `metro`, `tram`, `bus`, `regional_bus`, `mountain`). Adjacent indicators sit tightly next to each other (half-spacing of `0.28 em` per `slot_units` unit), with a small visible gap between them.

For N indicators, each indicator's position along the row is given by `slot_units = 2*i - (N-1)`, where `i` is its index in the row. This produces a centered, symmetric, integer-stepped sequence:

| N | slot_units sequence |
|---|---|
| 1 | `{0}` |
| 2 | `{-1, +1}` |
| 3 | `{-2, 0, +2}` |
| 4 | `{-3, -1, +1, +3}` |
| 5 | `{-4, -2, 0, +2, +4}` |
| 6 | `{-5, -3, -1, +1, +3, +5}` |

A single-group location naturally lands at `slot_units = 0`, centered on the parent.

Pills use the same layout at the pill's midpoint. At the indicator's small size relative to the pill (~0.35 × pill width), even a 6-indicator row stays well inside the pill body.

### Sizing

Indicator diameter scales with the parent's `width_base` AND with zoom, so indicators on a large train pill look proportionally large and those on a small bus dot look proportionally small. The size grows with zoom from z15 to z20, matching the rest of the stop family's natural growth.

The indicator-to-parent size ratio is **not exactly constant across zooms** — at low zoom the indicators are slightly larger relative to the parent than at high zoom. The mismatch comes from the gap between `text-size` (an MapLibre font-size value) and the actual visible "●" glyph diameter inside the em-box, which doesn't scale strictly linearly with `text-size` the way circle radii do. Anchoring `text-size` to the parent's own diameter curve (`1.5 × wb` at z14, `12 × wb` at z20) keeps the proportions close enough to look right at every zoom from z15 onward, and that's where the trade-off was left.

A new identifier introduced by this concept:

- `INDICATOR_MIN_ZOOM = 15` — appearance zoom.

### Data model

A new feature type is emitted into `tl_stop_pills.pmtiles` under a new `feature_type = "indicator"` value. Each indicator feature is a Point with properties:

- `color` — the picked hex color for this color group at this location.
- `slot_units` — integer in `[-5, +5]`, giving the indicator's position in the centered row (see § Placement).
- `tangent_deg` — row orientation in degrees, clockwise from east in map space (MapLibre `text-rotate` convention with `text-rotation-alignment: map`). `0` for dot / disc indicators; pill's local tangent at midpoint for pill indicators.
- `width_base` — the parent's width_base, so the style can size the indicator from the same expression family used by the parent.

The point's coordinate is the parent's center: dot center, disc center, or pill midpoint. The row offset is applied at the paint stage by the style (em-based `text-offset`), so a single feature per group covers every parent kind uniformly.

Dot stop features come from the `transit_stops_*` sources, which are split per mode and don't carry a `feature_type` column. To keep all indicators in one source, every indicator (including those visually attached to a `transit_stops_*` dot) is emitted into the `tl_stop_pills.pmtiles` source. The style layer therefore filters on `feature_type=indicator` from that source alone.

A single location can yield up to 6 indicator features (one per color group present).

### Style layer

Indicators render via a **single symbol style layer** `transit-stop-indicator` in `build_station_layers`, sourced from `transit_stop_pills` with `feature_type=indicator`:

- `text-field = "●"` (Unicode U+25CF BLACK CIRCLE) in `Noto Sans Regular`. The glyph's visible diameter is approximately 0.7 em.
- `text-size` is data-driven on `width_base` and zoom-interpolated: `5 × width_base` px at z15 and `12 × width_base` px at z20. Indicators scale with both zoom and the parent's frequency-driven width.
- `text-offset` is in em (so it auto-scales with `text-size`) via a `match` on `slot_units` returning literal pairs. Each unit corresponds to `0.28 em` horizontally, with a constant `-0.1 em` vertical compensation for the "●" glyph's vertical asymmetry inside its em-box (Noto Sans renders the bullet slightly below bbox center; the compensation nudges the anchor up in glyph-local space and rotates with the row).
- `text-rotate` reads `tangent_deg` from the feature; combined with `text-rotation-alignment: map`, the entire row (offsets + glyphs) rotates with the parent's tangent.
- `text-allow-overlap` / `text-ignore-placement` are true so all indicators always render, regardless of collisions.
- `text-color = ["get", "color"]`, `text-opacity` ramps `15.0 → 15.3` from 0 to 1.
- No border / stroke.

The layer is drawn **after** every existing stop-family layer (dot fill, pill fill, endpoint fill, connector fill) so the indicators sit on top of the white field.

### Connectors

Connectors are intentionally excluded — they are subordinate links between stops, not stops in their own right, and crowding them with indicators duplicates the info already shown at the connected ends.

## Constraints

- Indicators must not enlarge the parent stop family's hit region or change pill / dot placement; they are decorative.
- Indicators must not change the existing popup payload (`lines_json`); they are read-only consumers of per-location mode info.
- The 1 px black border on the parent dot / disc / pill stays unchanged; indicators sit inside that border.
- Below z16 indicators are not rendered — no perf cost for far-zoom views.
- Pill indicator placement uses the **simplified pill polyline** the renderer draws, not the raw NN-path, to stay consistent with the rest of `07_extract_stops.py`'s downstream geometry. The pill's midpoint is the midpoint of the simplified polyline by arc length.
