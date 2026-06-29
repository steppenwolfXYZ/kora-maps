# Far-zoom stop dot redesign

## Problem

At zooms 7–12.99 the map shows stops as plain dots (pills take over from z13). Today every dot is the same size — a busy interchange and a once-an-hour halt look identical. The dot should encode how important the stop is, so that hubs read as hubs at a glance.

## Requirements

### Stop score

Each drawn stop gets a single numeric `stop_score`. The score is a **mode-weighted, frequency-modulated count of the lines departing the stop**. Per emitted feature serving the stop:

```
contribution = mode_weight × (1 + freq_score)
```

with `freq_score ∈ [0, 1]` — the same per-line `freq_score` that already drives line width via `compute_freq_score`. The multiplier `(1 + freq_score)` is `1` at the bucket's `worst_freq` and `2` at its `best_freq`, so a high-frequency line is worth twice as much as a low-frequency line of the same mode, but a low-frequency line still counts.

`stop_score(s) = Σ over emitted features f that depart s of  mode_weight(f) × (1 + freq_score(f))`.

Counting rules:

- **Per direction.** Emitted features are per-direction (one feature per `(line_key, agency, trip_group, merged_stop_set, direction_key)`). A bidirectional line at a stop contributes from both directions — a stop with service in both directions scores higher than a unidirectional stop, all else equal.
- **Only departing lines count.** A feature contributes to a stop iff that stop is not the feature's terminal — i.e. iff at least one trip on that feature actually leaves the stop. A feature whose every trip arrives at the stop and terminates there does not contribute (passengers cannot board).
- **One contribution per feature.** A loop feature that re-visits the same stop within one direction contributes once, not per pass-through. Each feature is a "line at this stop"; loop topology does not multiply the line count.

**Mode weights** (new config; may be retuned):

| Mode | Weight |
|---|---|
| Bus | 1.0 |
| Tram | 1.5 |
| Metro | 2.0 |
| Train | 3.0 |
| Mountain | 3.0 |
| Ferry | 3.0 |

The mountain weight of 3.0 applies to every `mountain_origin` value (aerial, funicular, rack, rebucketed_rail) uniformly.

**Reused inputs**: `freq_score` is already computed per emitted feature in step 06 and stored on the feature's properties. No additional aggregation or window math is needed — the per-stop score is a sum over feature-level numbers that already exist.

### Score → size mapping

A new config block carries:

- `score_range.min` — score at which a dot is rendered at minimum size.
- `score_range.max` — score at which a dot is rendered at maximum size.

Mapping is **linear** in score. With a low `size_px.z13.min` (sub-pixel diameters are still visible at retina) and `score_range.max` pinned near the dataset top, the upper tail spreads visually without needing log compression. Scores below `score_range.min` clamp to the minimum size; scores above `score_range.max` clamp to the maximum size.

Pick `score_range.min` near the lower end of "interesting" stops (around the 20th percentile of the observed distribution). Pick `score_range.max` near the upper end of the actual distribution (close to the dataset maximum, not the p80 or p95) so the few largest hubs spread across the top of the size range. The build prints the distribution percentiles so the values can be re-pinned.

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
