# Stop Color Indicators

## Problem

The medium-zoom stop family (dots, endpoint discs, pills) renders with white fill and a 1 px black border, with mode color carried as a feature attribute but unused by the production paint. Once the map is zoomed close enough that a station's individual platforms are distinguishable, the user has no in-place signal for *which* line modes serve a station — a busy interchange (tram + city bus + regional bus + train) is visually indistinguishable from a quiet single-line stop. The popup carries the info but requires a click. At zoom 16 and closer there is enough screen room inside the stop construct to surface a compact per-mode indicator without a click.

## Requirements

### When

The indicators appear starting at **zoom 16** (the same zoom where individual platforms become resolvable). Below z16 they are not rendered. They fade in with the same `appear → appear + 1.0` opacity curve used by other stop family layers (so z16 ramp is `16.0 → 17.0`).

### What an indicator is

A small filled circle drawn inside a stop dot, endpoint disc, or pill. **No border.** Sized so multiple fit comfortably inside the parent feature at every zoom from 16 upward. The indicator's diameter scales with the stop family's `dot/disc/pill diameter` so the relative size is constant across zoom.

### Color groups

One indicator is emitted per **color group** that serves the location. The color groups are:

| Group | Color base | Members |
|---|---|---|
| train | red | mode `train` |
| metro | (metro hue) | mode `metro` |
| tram | purple | mode `tram` |
| bus | blue | modes `bus` and `ferry` (same hue) |
| regional_bus | turquoise | mode `regional_bus` |
| mountain | light yellow | mode `mountain` |

Ferry collapses into the bus group because the two share a color; surfacing them as one indicator avoids two identically-coloured marks at the few stops where both meet. Modes outside this table (none currently) do not contribute an indicator.

### Color picked per group

Within a group, the indicator's color is the **darkest (lowest luminance) hex color** among the lines of that group at the location. The existing per-line color is already saturation-modulated by frequency — darker means higher `f_weighted`, so "darkest" coincides with "fastest" as the user described. Mountain is fixed light yellow (`#ffe566`) by group definition, so the pick is a no-op there.

Color selection uses the same `color_luminance` rule already in `07_extract_stops.py:dominant_line`.

### Indicator emission rule

An indicator is emitted for every color group present at the location, including locations with a **single** group (a single-mode bus stop shows one mini blue dot). The rule is uniform across dots, endpoint discs, and pills — no special-casing single-group locations.

### Placement

- **Stop dot (the `transit_stops_*` circle features)** and **endpoint disc (the `transit_stop_pills` Point features with `feature_type=endpoint`)** — indicators are arranged in a **ring around the parent's center**, equally spaced by angle, on a circle whose radius is half the parent's radius. Up to 6 indicators (the group count) can sit on the ring without overlap. The ring's rotation is fixed per feature (deterministic from the feature's coordinates so the layout is stable across reloads); no attempt is made to align it to compass north. For a single-group location, the one indicator sits at the parent's geometric center rather than offset onto the ring.

- **Pill (the `transit_stop_pills` LineString features with `feature_type=pill`)** — indicators are arranged in a **single horizontal row centered at the pill's midpoint**, packed tightly along the pill's local tangent at the midpoint. The row's direction follows the pill's tangent so the indicators line up along the pill's axis, not the geographic horizontal.

### Sizing

Indicator diameter is **0.35 × parent diameter** at z16, fixed in pixels across zooms 16–20 (i.e., the same multiplier evaluated against the parent's per-zoom width). This is small enough to leave the parent's white field visible around the indicators and large enough to read color at standard map scales.

A new identifier introduced by this concept:

- `INDICATOR_DIAMETER_FRAC = 0.35` — fraction of parent diameter.
- `INDICATOR_RING_RADIUS_FRAC = 0.5` — ring radius as fraction of parent radius, for dot / disc placement.
- `INDICATOR_MIN_ZOOM = 16` — appearance zoom.

### Data model

A new feature type is emitted into `tl_stop_pills.pmtiles` under a new `feature_type = "indicator"` value. Each indicator feature is a Point with properties:

- `color` — the picked hex color for this color group at this location.
- `group` — one of `train`, `metro`, `tram`, `bus`, `regional_bus`, `mountain` (the group key, not the mode).
- `width_base` — the parent's width_base, so the style can size the indicator from the same expression family used by the parent.

Indicators are emitted alongside dot, pill, and endpoint features by the same code paths that build those features today. A single location can yield up to 6 indicator features (one per color group present).

### Style layer

A new style layer `transit-stop-indicator` is added to `build_station_layers`. It renders:

- `circle-color = ["get", "color"]`
- `circle-radius` driven by zoom and `width_base` to track `INDICATOR_DIAMETER_FRAC × parent diameter`.
- No stroke (border).
- Opacity ramp `16 → 17` from 0 to 1.

The layer is drawn **after** every existing stop-family layer (dot fill, pill fill, endpoint fill, connector fill) so the indicators sit on top of the white field.

### Connectors

Connectors are intentionally excluded — they are subordinate links between stops, not stops in their own right, and crowding them with indicators duplicates the info already shown at the connected ends.

## Constraints

- Indicators must not enlarge the parent stop family's hit region or change pill / dot placement; they are decorative.
- Indicators must not change the existing popup payload (`lines_json`); they are read-only consumers of per-location mode info.
- Mountain's fixed light yellow is preserved — no frequency-driven variant on the mountain indicator.
- The 1 px black border on the parent dot / disc / pill stays unchanged; indicators sit inside that border.
- Below z16 indicators are not rendered — no perf cost for far-zoom views.
- Pill indicator placement uses the **simplified pill polyline** the renderer draws, not the raw NN-path, to stay consistent with the rest of `07_extract_stops.py`'s downstream geometry.
