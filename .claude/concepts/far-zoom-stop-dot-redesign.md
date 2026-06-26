# Far-zoom stop dot redesign

## Problem

At zooms 7–12.99 the map shows stops as plain dots (pills take over from z13). Today every dot is the same size — a busy interchange and a once-an-hour halt look identical. The dot should encode how important the stop is, so that hubs read as hubs at a glance.

## Requirements

### Stop score

Each drawn stop gets a single numeric `stop_score`. The score is the **mode-weighted, window-weighted count of scheduled departures** at the stop on an average day.

- **Per departure contribution** = `mode_weight × window_weight`.
- **Sum over all departures** at the stop across the sample dates (then averaged per date type, then combined across windows — i.e. the score is in "weighted trips per average day" units).

**Mode weights** (new config):

| Mode | Weight |
|---|---|
| Bus | 1.0 |
| Tram | 1.5 |
| Metro | 2.0 |
| Train | 3.0 |
| Mountain | 3.0 |
| Ferry | 3.0 |

The mountain weight of 3.0 applies to every `mountain_origin` value (aerial, funicular, rack, rebucketed_rail) uniformly.

**Window weights**: reuse the existing `window_weights` (core 0.6, eve 0.2, we 0.2) and the existing core/eve/we window boundaries used by `f_weighted`.

**Sample dates**: reuse the existing `freq_sampling.weekday_dates` and `freq_sampling.weekend_dates`.

**Reuse, not duplicate**: the existing per-line frequency aggregation logic must be extended so that one pass computes both the per-line frequency metrics (as today) and the per-stop score. Do not introduce a parallel aggregation path.

### Score → size mapping

A new config block carries:

- `score_range.min` — score at which a dot is rendered at minimum size.
- `score_range.max` — score at which a dot is rendered at maximum size.

**Defaults** for `score_range.min` / `.max` are the **20th and 80th percentiles** of `stop_score` across the drawn-stop set. The defaults are derived from the data once, then committed as fixed numbers in config so the visual is stable across rebuilds.

Mapping is **linear** between `score_range.min` and `score_range.max`. Scores below `min` clamp to the minimum size; scores above `max` clamp to the maximum size.

### Size → zoom curve

The minimum and maximum dot diameter (in pixels) are pinned at two zoom anchors:

| Zoom | Min diameter | Max diameter |
|---|---|---|
| z7 | 2 px | 8 px |
| z13 | 8 px | 20 px |

The min and max curves are **linear in zoom** between z7 and z13. The curve grows slower than the map (≈4× over z7→z13 versus the map's 64×), so at high zoom the dots look smaller relative to the basemap than at low zoom — intentional, since at z13 pills take over and dots are at their largest right before that handover.

At any given zoom, a stop's pixel diameter is the linear interpolation between the zoom's min and max according to its score.

## Constraints

- **Visibility logic is unchanged.** Which stops appear at which zoom (e.g. train-only between z7 and z9, all stops from z10) is governed by the existing thresholds and stays as is. The redesign affects only the size of dots that are already drawn.
- **Stop eligibility is unchanged.** Dots are drawn only for stops served by at least one emitted (drawn) line. The score is computed only over those stops, and percentile defaults are taken from that same set.
- **z13+ rendering is unchanged.** Pills, connectors and their casings start at z13 and are out of scope.
- **Design is not final.** The numeric corner values (mode weights, percentiles, px sizes) are placeholders that may move as the look is iterated. The structure — score formula, percentile-anchored linear mapping, linear-in-zoom min/max curve — is the part to lock down.
