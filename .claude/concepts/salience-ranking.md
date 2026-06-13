# Salience Ranking for Zoom-Based Visibility

## Problem

Every transit line and stop currently renders at every zoom level. At low zoom this is illegible — a country-overview of Switzerland with every village PostAuto loop is noise. We need to hide most lines and stops at low zoom and reveal them progressively, keeping the most important ones visible longest.

"Important" cannot be a single global frequency cutoff. A mid-frequency city bus competes with dozens of others for the same screen space, while a low-frequency rural bus or a ferry has no competition at all — the rural one is the salient feature of its area. Importance must be context-aware.

This is purely a visibility decision. Line widths, colors, and styles are unchanged.

## Requirements

### Independent scoring for lines and stops

Lines and stops each get their own salience score, computed independently. A stop's score does not depend on its lines' scores, and vice versa. A stop never inherits visibility from a line passing through it.

If a stop is rated high but every line serving it is rated low, the stop is hidden. This case indicates a parameter mistake and is acceptable as a signal. By the zoom level where pills and connectors render, all lines are visible anyway, so the renderer does not need to handle stop-without-its-line mismatches.

### Line salience score

Combines three signals into one number in `[0, 1]`:

1. **Absolute frequency** — existing `f_weighted` / `freq_score` from `gtfs_groups_full.json`.
2. **Local relative frequency** — how this line's `f_weighted` ranks among competing lines in its neighborhood (see below).
3. **Speed** — faster modes get a salience boost so trains stay visible further out than buses without per-mode hard-coding. Speed is the same value already driving line width.

Component weights live in a new `salience` section in `scripts/transit/config.yaml`. Initial weights are tuned by trial.

### Stop salience score

Combines:

1. **Absolute connection density** — sum of `f_weighted` across all lines serving the stop. (Per direction: a per-direction split line contributes once per direction.)
2. **Local relative connection density** — how this sum ranks among neighbouring stops.
3. **Speed of served lines** — boost using the fastest (or weighted-average) speed across lines serving the stop, same role as for lines.
4. **Terminus boost** — additive, applied only when both:
   - the stop is a first or last stop of at least one line, AND
   - that line is *dominant* at the stop: its `f_weighted` exceeds the next-highest line's `f_weighted` at the same stop by a configurable ratio (`terminus_dominance_ratio` in `config.yaml`).

   Bern HB does not earn the boost — its terminating lines are minor compared to IC passers-through. Ostring does — tram 7 dominates bus 40 there. Terminus boost applies to the stop only, never to the line.

Weights and the dominance ratio live in the same `salience` config section.

### Neighborhood definition (KNN)

"Surrounding" is per-feature, computed via **K nearest neighbors**: for each feature, identify the K closest other features of the appropriate type (Euclidean distance on lon/lat is fine at Swiss scale) and rank or percentile this feature's absolute metric within that set. K is configurable; it may be different for lines and stops, and may be per-mode if needed (a train's relevant neighborhood is larger than a bus's).

- **Stops**: neighbors are the K nearest other stops. The "local relative" component is this stop's percentile in absolute connection density within that set.
- **Lines**: each line's neighborhood is built from the lines passing through (or near) each stop on the line. The local-relative component is aggregated (mean or median percentile) across the line's stops. This avoids sampling polylines geometrically and reuses the per-stop neighborhood work.

Neighborhood size and aggregation choice live in `config.yaml`.

### Mountain handling

Mountain lines and stops participate in salience scoring like everything else, plus a `mountain_boost` (additive, in `config.yaml`) so funiculars, aerials, and rack railways stay visible at low zoom despite low absolute frequency. Mountain's existing visual style (light yellow, fixed width) is unaffected.

### Visibility cutoffs

Each feature carries a `min_zoom` derived from its salience score. The MapLibre style filters features by `min_zoom <= current_zoom`. There is no opacity fade — visibility is binary per zoom level.

The score → `min_zoom` mapping is a list of `(score_threshold, min_zoom)` tuples in `config.yaml`, independent for lines and stops.

The mapping must meet these targets:

- By zoom **5**, essentially all train lines are visible.
- By zoom **12**, all lines are visible.
- Stop visibility ramps to fill in between, calibrated so low zooms are not overcrowded but stops are dense enough by z12.

### New identifiers and outputs

- `salience` section in `scripts/transit/config.yaml`. Sub-keys include component weights (`weight_absolute`, `weight_relative`, `weight_speed`), KNN size (`k_lines`, `k_stops`, optionally per-mode), `terminus_boost`, `terminus_dominance_ratio`, `mountain_boost`, and the two `min_zoom_thresholds` lists (`lines`, `stops`).
- `salience` and `min_zoom` properties on every feature in `transit_lines.geojson` and `transit_stops.geojson`.
- Diagnostic `data/transit/salience.json`: per feature, the raw components (absolute score, local-relative percentile, speed score, terminus boost applied?, mountain boost applied?), the combined score, and the resulting `min_zoom`. Lets us debug "why is line X hidden at z9?" without re-running the pipeline.

## Constraints

- Mountain visual style is unchanged. Salience affects visibility only, never width, color, or saturation.
- Existing `freq_score` continues to drive line width and color saturation. Salience is a separate, additional field.
- Ferry receives no special-case handling; the local-relative component naturally rescues low-frequency ferries because nothing competes for the space on water.
- Pills and connectors must not render at zooms where their stop or line is hidden. The visibility filter applies to the dot, pill, and connector layers consistently.
- Terminus boost must be deterministic across rebuilds: dominance is decided by the configured frequency ratio against the next-highest line's `f_weighted` at the stop, with ties broken explicitly (e.g. by `line_key` lexicographic order).
- The score is computed once per pipeline run and baked into the feature. There is no zoom-time recomputation.

## Possible future extension

Replace the discrete KNN neighborhood with **KDE-style** weighting: instead of "rank within the K nearest", every other feature contributes to this feature's local context with a weight that decays smoothly with distance. Removes the boundary artifact where the last rural stop before a city gets boosted while the first city stop 200 m further in is crushed. Same inputs and outputs, only the neighborhood math changes. Out of scope for the initial implementation; added if the KNN version shows visible artifacts at city edges.
