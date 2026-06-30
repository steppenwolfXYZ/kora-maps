# Far-zoom stop dot redesign

## Problem

At zooms 7–12.99 the map shows stops as plain dots (pills take over from z13). Today every dot is the same size — a busy interchange and a once-an-hour halt look identical. The dot should encode how important the stop is, so that hubs read as hubs at a glance.

## Requirements

### Stop score

Each drawn stop gets a single numeric `stop_score`, aggregated at the **parent UIC** level — platforms of the same physical station combine into one score, and the dot rendered for that station carries it. Stop dots whose feature has no resolvable parent UIC (e.g. mountain straight-line embedded `gtfs_stops` without a `stop_id`) carry no score and fall through to the minimum diameter via the score-range floor.

The score is a **mode-weighted, frequency-modulated count of the lines departing the stop**. Per emitted feature serving the stop:

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

Pick `score_range.min` near the lower end of "interesting" stops (around the 20th percentile of the observed distribution). Pick `score_range.max` near (but **below**) the upper end of the actual distribution so the very top hubs (Zürich HB and Bern HB at the moment) deliberately clamp to the same max diameter, while the next tier (Olten / Basel / Lausanne / etc.) spreads below them. The intent is that "biggest hub" is a single visual category, not a per-station ranking — the eye reads "this is a major interchange" without trying to compare two near-identical giants. The build prints the distribution percentiles so the values can be re-pinned if the data shifts.

### Size → zoom curve

The minimum and maximum dot diameter (in pixels) are pinned at two zoom anchors:

| Zoom | Min diameter | Max diameter |
|---|---|---|
| z7 | 2 px | 8 px |
| z13 | 4 px | 20 px |

The min and max curves are **linear in zoom** between z7 and z13. The curve grows slower than the map, so at high zoom the dots look smaller relative to the basemap than at low zoom — intentional, since at z13 pills take over and dots are at their largest right before that handover.

At any given zoom, a stop's pixel diameter is the linear interpolation between the zoom's min and max according to its score. Sub-pixel diameters at the low end of the score range are accepted — they render visibly at retina without forcing a high floor that flattens the upper tail.

### Dedup of overlapping dots

Stops that visually touch at a given far-zoom level merge into one. The classic case is a bus stop physically next to a train station — different parent UICs, so the parent-UIC aggregation above does not combine them, but visually they should read as one stop and the lines served by the bus stop should add to the train station's apparent importance.

**Touch criterion.** Two dots touch at zoom z when the pixel distance between their centers is ≤ `radius_A(z) + radius_B(z) + min_spacing_px`. `min_spacing_px` is a single config value applied at every zoom — the radii already scale with zoom, so the effective spacing scales naturally.

**Direction.** The higher-score dot absorbs the lower. The absorbed dot disappears at the zoom level where it was touched, and at every lower zoom. Its line contributions are added to the absorber's score from that zoom downward. Tiebreak on equal scores by `stop_id` (deterministic, no ranking by mode).

**Absorber identity is preserved.** The absorber keeps its own mode, color, position, and `stop_id`. Absorbed dots are removed from the far-zoom output entirely — they do not influence the absorber's appearance beyond the score. At z13+ they still render via the pill-zoom layer (dedup does not touch z13+).

**Per-zoom scope.** Dedup runs per integer zoom level z ∈ {7, 8, …, 12}, descending from z12 → z7. Disc radii grow with zoom but real-world distance per pixel grows faster, so absorption is **monotonic downward**: once a dot is eaten at zoom z, it stays eaten at every z′ < z.

At each zoom z (in descending order):
1. Compute the current disc radius for every surviving stop at z, using its score at z and the `size_px` curve.
2. Process survivors in descending score order (tiebreak by `stop_id`). Each absorber finds surviving neighbors within touch range, eats them, and gains their contributions.
3. Iterate within zoom z until stable — an absorber that grew by eating one neighbor may now reach another.
4. Carry the surviving set forward to z−1.

**Per-zoom score on the feature.** A stop that absorbs neighbors at low zoom but not at high zoom carries a different score at each zoom. The feature stores its post-dedup score per zoom (e.g. `score_z7`, `score_z8`, …, `score_z12`) so the dot size grows smoothly as zoom decreases and more neighbors fold in. A stop with no absorption carries the same score at every zoom.

**Visibility encoding.** Absorbed-everywhere stops get `tippecanoe.minzoom: 13` — they disappear from the far-zoom layer entirely and only render via the pill-zoom layer. Partially-absorbed stops (absorbed at z ≤ k, surviving at z > k) get `tippecanoe.minzoom: k + 1`.

**New config block:**

```yaml
stop_dot_dedup:
  min_spacing_px: 2.0
```

## Constraints

- **Visibility logic is unchanged.** Which stops appear at which zoom (e.g. train-only between z7 and z9, all stops from z10) is governed by the existing thresholds and stays as is. The redesign affects only the size of dots that are already drawn, and the dedup pass which removes overlapping ones.
- **Stop eligibility is unchanged.** Dots are drawn only for stops served by at least one emitted (drawn) line. The score is computed only over those stops, and percentile defaults are taken from that same set.
- **Dedup is far-zoom only.** Absorption operates at z7–z12. At z13+ all stops render via the pill-zoom layer, regardless of whether they were absorbed at far-zoom.
- **z13+ rendering belongs to the pill design concept and is not touched here.** The far-zoom redesign owns z7–z12.99 exclusively. The two ranges are drawn as two separate style layers reading the same `transit_stops` PMTiles source: the new far-zoom layer (`-far` suffix, score-driven sizes, `maxzoom: 13`) and the existing dot layer (unchanged expression, now `minzoom: 13` so the two layers do not overlap). At z13+ the rendering is identical to before this redesign.
