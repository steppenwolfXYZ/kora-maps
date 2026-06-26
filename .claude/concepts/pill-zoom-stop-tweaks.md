# Pill-Zoom Stop Tweaks

## Problem

At the zoom where the medium-zoom stop family takes over from the far-zoom centroid dot, several small issues compound to make stops at low-frequency lines hard to read:

- Train pills come in at z12 while every other mode comes in at z13. The split feels arbitrary at the boundary, and the network around train hubs (city buses, trams meeting the station) is still represented as a far-zoom centroid dot.
- The white disc / pill diameter is `m(z) × line_width(z)`, so a stop on a very thin (low-frequency) line shrinks to near-illegibility well after pill zoom is supposed to be readable.
- The colored color-group indicators inside the pill / disc have a fixed pixel size and can poke outside the white field — most visible at thin-line stops where the parent is small but the indicator size is the same as at a busy interchange.
- Indicators currently appear at z15 with a short opacity fade. They're absent for the first two zoom levels where pills exist (z13, z14), so the pill-zoom view starts as a featureless white shape and only gains its mode signal two zooms later.

## Requirements

### Uniform pill appear-zoom

Pills appear at **z13 for every mode**. The previous train-at-z12 exception is removed. The cluster-centroid dot's `maxzoom: mz − 1` becomes z12 for every mode (single-line stops, which never get a pill, are unaffected). The MapLibre paint layer's pill-minzoom and every per-feature tippecanoe minzoom for pill-family features (pill bodies, endpoint discs, connectors) move in lockstep.

### Width-base floor for stop sizing

The stop family's diameter formula sees a clamped `width_base`:

`effective_width_base = max(width_base, STOP_WIDTH_BASE_FLOOR[mode])`

`STOP_WIDTH_BASE_FLOOR` is a **per-mode table**, one floor per mode. Starter values are the per-mode 20th percentile of `width_base` projected against the updated `line_width` config:

| Mode | Starter floor |
|---|---:|
| bus | 1.23 |
| regional_bus | 1.62 |
| train | 3.47 |
| tram | 3.00 |
| metro | 3.00 |
| ferry | 2.50 |
| mountain | 2.50 |

The user will tune these values visually after the first rebuild; the implementation must read them from a per-mode config block, not hard-code them in the style or the pipeline.

Note that for tram, metro, ferry, and mountain the p20 starter equals the mode's `line_width.max`, because more than 80 % of those modes' lines sit at the top of their freq curve. Applying those starters as written cancels the within-mode variability — every tram / metro / ferry / mountain stop ends up at its mode's peak width. That is acceptable as a starter (the modes' visual identity stays close to fixed at every stop) but is the first thing to tune downward if mountain's new frequency-driven width variability is to be visible at the stop family rather than only at the line.

The clamp applies to **dot, endpoint disc, and pill diameter only**. The transit line itself keeps its true `width_base` — the floor exists so a stop on a thin line is visibly larger than that line, not to fatten the line. The **connector** width also keeps its true `width_base` derivation: connectors are subordinate links and should stay narrower than the stops at either end.

The clamp applies to **every zoom from pill-minzoom (z13) upward**. There is no zoom-dependent variant of the floor — one number per mode, applied as a floor on the input to the existing per-zoom interpolation curve.

For pills and discs whose cluster mixes modes (e.g. a train + bus interchange), the floor used is the floor of the cluster's **dominant mode** (the mode whose line drives the existing `dominant_line.width_base` pick). This keeps the stop-sizing input consistent with the existing dominant-line rule rather than introducing a competing per-cluster mode pick.

### Color indicators appear at pill-minzoom, no fade

Color-group indicators (the colored dots inside the white field) appear at **z13** instead of z15, with `text-opacity` = `1` at every zoom from z13 upward. The previous `15.0 → 15.3` opacity fade is removed. The indicator's size curve (its `text-size` interpolation) is re-anchored so that its z13 value reads sensibly inside the pill at z13 (where the pill first appears) and continues to grow up to z20 in the same shape as today. The exact z13 anchor and the slope of the curve are tuned visually after the first rebuild; the curve is parameter-driven, not hard-coded across multiple places.

### Indicators must not overflow the parent

The color-indicator row's geometry is checked against the parent's binding inner dimension, and the **entire row** is shrunk uniformly if the actual row at default size would not fit. The check is per-location, against the **actual** number of indicators at that location — not against a worst-case maximum.

Binding inner dimension:

- **Disc** — the disc's diameter (the row sits screen-horizontal across a round field; the row width is what binds).
- **Pill** — the pill's **short** axis (its thickness). The row sits along the pill's long axis and that axis is effectively unbounded; what binds is each indicator's diameter fitting through the pill's thickness.

The available width inside the parent is `binding_dimension × INDICATOR_INNER_MARGIN`. Starter `INDICATOR_INNER_MARGIN = 0.7`, leaving a visible white frame between the indicator row and the parent's border.

For each rendered location:

1. Build the actual row at default indicator size — every indicator in the row is the same size.
2. Compute the row's total span along its row axis (for disc parents) or the indicator's per-dot diameter (for pill parents).
3. If that exceeds `binding_dimension × INDICATOR_INNER_MARGIN`, compute the shrink factor `s = (binding_dimension × INDICATOR_INNER_MARGIN) / actual_span` and multiply every indicator's diameter in that row by `s`. The shrink applies uniformly across the row.

Concrete consequences:

- A 1-indicator stop on a very thin parent can still trigger a shrink (one big dot doesn't fit through pill thickness).
- A 2-indicator stop on a parent wide enough that the row fits at default size stays at default size.
- The 21 known three-indicator Zürich tram/bus interchanges are evaluated individually; only the thinnest parents shrink.
- The shrink-to-fit rule is the **only** exception to "every indicator is the same pixel size at a given zoom." Above the threshold the rule is preserved; below it the rule is necessarily relaxed because the alternative is overflow.

### Diagnostics

The number of distinct color groups at each rendered location is already implicit in the existing indicator-feature emission (one feature per group). No new diagnostic file is required.

For visual tuning of the floor and inner-margin starter values, the **actual** number of stops whose clamp / shrink fires after the first rebuild is the signal — visible in the rendered map at z13.

## Constraints

- The transit-line widths are unchanged. Only stop diameters consult `STOP_WIDTH_BASE_FLOOR`, and only the indicator row's size consults the per-row shrink.
- The connector width derivation is unchanged. Connectors remain subordinate to the stops at either end.
- Below pill-minzoom (z12 and earlier) the cluster-centroid dot is unchanged; only its `maxzoom` shifts to z12 (was z11 for train, z12 for others) so the dot disappears exactly where the pill takes over.
- Indicators are still drawn after every existing stop-family layer so they sit on top of the white field.
- Below z13 indicators are not rendered.
- The 1 px black border on the parent dot / disc / pill stays unchanged. The indicator row sits inside that border, and the `INDICATOR_INNER_MARGIN` leaves a visible white margin between the row and the inside of the border.
- Single-line stops still never get a pill — they keep their cluster-centroid dot at every zoom.
- The pill-collapse fallback (a cluster whose positions all dedup to fewer than two distinct points keeps the centroid dot uncapped) is unchanged.
- Mountain color stays fixed light yellow; mountain stops follow the same `STOP_WIDTH_BASE_FLOOR` clamp as everything else for their stop diameter only.
- All values introduced by this concept (`STOP_WIDTH_BASE_FLOOR`, `INDICATOR_INNER_MARGIN`, indicator appear-zoom, indicator size curve anchors) live in `scripts/transit/config.yaml` so they can be tuned without code changes.
