# Salience Ranking for Zoom-Based Visibility

## Problem

Every transit line and stop currently renders at every zoom level. At low zoom this is illegible — a country-overview of Switzerland with every village PostAuto loop is noise. We need to hide most lines and stops at low zoom and reveal them progressively, keeping the most important ones visible longest.

"Important" cannot be a single global frequency cutoff. A mid-frequency city bus competes with dozens of others for the same screen space, while a low-frequency rural bus or a ferry has no competition at all — the rural one is the salient feature of its area. Importance must be context-aware.

Two further constraints complicate a pure local-frequency ranking:

- **Asymmetric competition between modes.** A train should not be hidden because of dense bus traffic around it; a tram should not be hidden because of dense bus traffic. Modes have an order of importance, and lower-tier modes do not suppress higher-tier ones.
- **Network coherence.** The visible set must look like a connected network at every zoom. A line in isolation, disconnected from the rest of the visible network, is more misleading than useful. The system should reveal short branches together with the connectors that link them to the main network.

This is purely a visibility decision. Line widths, colors, and styles are unchanged.

## Requirements

### Independent scoring for lines and stops

Lines and stops each get their own salience score, computed independently. A stop's score does not depend on its lines' scores, and vice versa. The visibility decision (zoom at which the feature first renders) is a follow-up step that may adjust the stop's zoom to keep it consistent with its lines — see "Stops follow lines" below.

### Mode-tier competition

The local-relative components for lines and stops use a mode-tier hierarchy so that each mode is ranked only against itself and modes considered equal or more important. The hierarchy (most to least important):

1. train
2. metro
3. tram
4. bus

Ferry and mountain are isolated pools — they do not interact with the hierarchy or with each other.

For each mode, the set of **comparator modes** used when computing the local-relative component:

- train: {train}
- metro: {metro, train}
- tram: {tram, metro, train}
- bus: {bus, tram, metro, train}
- ferry: {ferry}
- mountain: {mountain}

In line scoring: at each stop the line serves, the line's relative percentile considers only competing lines whose mode is in the line's comparator set. A train serving a stop adjacent to dozens of city buses is not pushed down by the bus mass.

In stop scoring: each stop has a **tier** equal to the highest-importance mode that serves it. Its absolute connection density sums `f_weighted` only over lines whose mode equals the stop's tier. Its KNN neighborhood is the K nearest other stops of the same tier. A train stop in a city is compared only against other train stops, which may be much farther away than the surrounding tram and bus stops.

### Line salience score

Combines three signals into one number in `[0, 1]`:

1. **Absolute frequency** — existing `f_weighted` / `freq_score`.
2. **Local relative frequency** — how this line's `f_weighted` ranks among competing lines in its neighborhood, where competition is restricted to the comparator-set rule above.
3. **Speed** — faster modes get a salience boost so trains stay visible further out than buses without per-mode hard-coding. Speed is the same value already driving line width.

Component weights live in a new `salience` section in `scripts/transit/config.yaml`. Initial weights are tuned by trial.

### Stop salience score

Combines:

1. **Absolute connection density** — sum of `f_weighted` across lines of the stop's tier serving the stop. (Per direction: a per-direction split line contributes once per direction.)
2. **Local relative connection density** — how this sum ranks among the K nearest other stops of the same tier.
3. **Speed of served lines** — boost using the fastest (or weighted-average) speed across lines serving the stop, same role as for lines.
4. **Terminus boost** — additive, applied only when both:
   - the stop is a first or last stop of at least one line, AND
   - that line is *dominant* at the stop: its `f_weighted` exceeds the next-highest line's `f_weighted` at the same stop by a configurable ratio (`terminus_dominance_ratio` in `config.yaml`).

   Bern HB does not earn the boost — its terminating lines are minor compared to IC passers-through. Ostring does — tram 7 dominates bus 40 there. Terminus boost applies to the stop only, never to the line.

Weights and the dominance ratio live in the same `salience` config section.

### Neighborhood definition (KNN)

"Surrounding" is per-feature, computed via **K nearest neighbors** within the tier:

- **Stops**: neighbors are the K nearest other stops of the same tier. The "local relative" component is this stop's percentile in tier-restricted connection density within that set.
- **Lines**: each line's neighborhood is built from the competing lines at each stop it serves (filtered by the line's comparator set). The local-relative component is aggregated (mean or median percentile) across the line's stops.

K is configurable, may be different for lines and stops, and may be per-tier if needed (a train's relevant neighborhood spans much greater distance than a bus's). All KNN distances use Euclidean lon/lat with cosine-latitude scaling — sufficient at Swiss extent.

### Mountain and ferry handling

Mountain and ferry are isolated comparator pools (see "Mode-tier competition") with their own dedicated entries in `mode_zoom_range_lines` and `mode_zoom_range_stops`. Their visibility is therefore controlled entirely by these per-mode ranges and the standard min-max stretch — no additive boost is applied. Mountain's existing visual style (light yellow, fixed width) is unaffected.

### Visibility cutoffs

Each feature carries a **float** `min_zoom` derived from its salience score and subsequent adjustments (connectivity, stops-follow-lines). The MapLibre style filters features by `min_zoom <= current_zoom`. There is no opacity fade — visibility is binary per zoom level, but the exact zoom at which a feature appears is fractional (e.g. 6.43), so features spread continuously over the zoom scale instead of banding at integer zoom transitions.

The mapping from salience score to `own_min_zoom` is **per-mode**, with separate tables for lines and stops in `config.yaml`:

- `mode_zoom_range_lines` — used by line scoring; lookup key is the line's mode.
- `mode_zoom_range_stops` — used by stop scoring; lookup key is the stop's tier (the highest-priority mode serving it).

Each table maps a mode to a `[z_low, z_high]` range. Within each mode, salience scores are normalised by **min-max stretch**: the highest-salience feature of the mode lands at `z_low` (visible earliest), the lowest at `z_high` (visible latest), and the rest are linearly spaced between:

```
own_min_zoom = z_low + ((s_max - salience) / (s_max - s_min)) * (z_high - z_low)
```

Where `s_max` and `s_min` are the highest and lowest raw salience scores observed within the mode in the current pipeline run. This guarantees the configured range is fully used regardless of how raw scores cluster. A mode with a single feature, or all-equal salience scores, collapses to `z_low` (treated as "best of its mode").

`own_min_zoom` is the value from min-max stretch alone; the final `min_zoom` is computed after connectivity and stops-follow-lines adjustments and is what the style filter compares against.

Because tile boundaries are at integer zoom levels, two zoom values are baked into each feature:

- `tippecanoe.minzoom = floor(min_zoom)` — the first integer zoom whose tiles must contain the feature.
- `min_zoom` (float, in properties) — enforced at render time via the layer filter `["zoom"] >= ["get", "min_zoom"]`.

Separate stop ranges intentionally lag their line counterparts (e.g. train lines `[4, 6]` vs. train stops `[6, 8]`) so a line is visible alone for a zoom band before the stops decorate it. The stops-follow-lines rule then clamps: a stop never appears before any of its serving lines, even if its tier's range would say so.

Mode ranges (first-pass values):

| Mode         | Lines       | Stops          |
| ------------ | ----------- | -------------- |
| train        | `[4, 6]`    | `[6, 8]`       |
| metro        | `[8, 10]`   | `[9, 10.5]`    |
| tram         | `[8.5, 10]` | `[9, 11.5]`    |
| regional_bus | `[7, 10]`   | `[9, 11.5]`    |
| bus          | `[9.5, 11]` | `[10.5, 12.5]` |
| ferry        | `[7.5, 9]`  | `[10.5, 12.5]` |
| mountain     | `[7.5, 9.5]`| `[8.5, 10]`    |

These achieve the original target order (most trains visible by z6, everything visible by z11–z12 for lines and z12–z13 for stops) and are tunable per mode without changing code.

### Network connectivity

After per-feature salience is computed, the visible set must form a single connected component at every zoom. The base of that network is the train network; everything else attaches to it via shared stops, possibly through a chain of intermediate "connector" lines.

#### Line graph

Two lines share an edge in the line graph if they share at least one stop. For this purpose, stops are first clustered: any two stops within `cluster_threshold_m` (default 250 m, config key) are treated as the same node, regardless of `parent_station`. This catches transfer points where the bus stop and the train station have different GTFS parents but are physically the same interchange, and is loose enough to cover slightly remote ferry harbours.

#### Base set

The base is the set of train lines tied at the lowest `own_min_zoom` among trains. The set is rebuilt on every pipeline run; no manual base list. A line is "connected to base" if it has a path in the line graph to any base line. For algorithmic convenience the base set is treated as a single virtual super-node.

#### Connector promotion via min-bottleneck spanning tree

Form a minimum bottleneck spanning tree (MBST) of the line graph rooted at the base super-node, where edge weight between two lines is the larger of their two `own_min_zoom` values. In the MBST every non-base line has a unique chain of ancestors leading to the base.

For each line C that is the ancestor of at least one other line in the MBST (a **connector**):

- **branch_weight(C)** = sum of station counts of every descendant line of C in the MBST (the sub-tree rooted at C, excluding C itself).
- **earliest_demand(C)** = the smallest `own_min_zoom` among C's descendants.
- **s_C** = station count of C itself.

If `branch_weight(C) >= s_C * promotion_ratio` (gate; `promotion_ratio` defaults to 0.5, config key), C is promotable. Its **meet zoom** is:

```
meet_zoom(C) = ceil((branch_weight(C) * earliest_demand(C) + s_C * own_min_zoom(C))
                    / (branch_weight(C) + s_C))
```

C's `effective_min_zoom(C) = min(own_min_zoom(C), meet_zoom(C))`.

If the gate fails (the sub-tree is too small relative to the connector), C is not promoted: `effective_min_zoom(C) = own_min_zoom(C)`.

For each line L, its final `effective_min_zoom`:

```
effective_min_zoom(L) = max(own_min_zoom(L),
                            max effective_min_zoom(C) over C on L's path to base)
```

Both endpoints move: the sub-tree pulls the connector forward; the connector pulls the branch back. A long branch with a short connector pulls the connector close to the branch's own zoom; a short branch with a long connector is held at the connector's own zoom (because the connector resists promotion).

#### Isolated lines

A line not in the MBST (no path to base in the line graph at all) is **truly isolated**. In practice in CH only a few mountain lines fall in this set. They are assigned `isolated_mountain_min_zoom` (config key, default z13) — visible alongside everything else at high zoom but not contributing to network visibility at lower zooms. Non-mountain truly-isolated lines are not expected; any that appear are written to the diagnostic and given the same fallback zoom.

### Stops follow lines

After line `effective_min_zoom` is finalised, every stop's final `min_zoom`:

```
min_zoom(stop) = max(own_min_zoom(stop),
                     min over serving lines' effective_min_zoom)
```

A stop is therefore never visible before at least one of its lines. This subsumes the original "a stop with no visible line is silently hidden" caveat — it is hidden because the formula sets its `min_zoom` to the earliest line's, which in extreme cases pushes it to the isolated fallback zoom.

### New identifiers and outputs

- `salience` section in `scripts/transit/config.yaml`. Sub-keys:
  - Component weights: `weight_absolute`, `weight_relative`, `weight_speed`.
  - KNN: `k_lines`, `k_stops` (optionally per-tier).
  - Boosts: `terminus_boost`, `terminus_dominance_ratio`.
  - Per-mode score → zoom mapping: `mode_zoom_range_lines` and `mode_zoom_range_stops` (separate `[z_low, z_high]` tables for lines vs. stops; within each table, min-max stretch ensures the full range is used per mode).
  - Mode hierarchy: `mode_tiers` (the comparator-set table per mode, including ferry and mountain pools).
  - Connectivity: `cluster_threshold_m`, `promotion_ratio`, `isolated_mountain_min_zoom`.
- Per-feature properties on every line in `transit_lines.geojson` and every stop in `transit_stops.geojson`:
  - `salience` — the raw [0, 1] score.
  - `own_min_zoom` — the zoom from salience alone, before any adjustment.
  - `min_zoom` — the final rendered value (after connector promotion and stops-follow-lines).
- Diagnostic `data/transit/salience.json` — per feature: raw components, `salience`, `own_min_zoom`, final `min_zoom`. For lines additionally: the MBST parent chain to base, the connector's `branch_weight`, `earliest_demand`, gate outcome, `meet_zoom`, and whether the line was promoted (and by which sub-tree's demand) or held back (and by which connector).

## Constraints

- Mountain visual style is unchanged. Salience affects visibility only, never width, color, or saturation.
- Existing `freq_score` continues to drive line width and color saturation. Salience is a separate, additional field.
- Mode tier hierarchy is fixed (train > metro > tram > bus, with ferry and mountain in their own pools). Cross-tier comparison only goes downward in the hierarchy — a tram does not push down trains; trains do push down nothing because their comparator set is `{train}`.
- The MBST is rebuilt on every pipeline run from current data; no manual base list, no manual promotion overrides.
- Connector promotion produces a single `effective_min_zoom` per line per run; no per-zoom recomputation at render time.
- The stops-follow-lines rule is applied after connector promotion so stops inherit promoted zooms.
- Pills and connectors must not render at zooms where their stop or line is hidden. The `min_zoom` field applies to dot, pill, and connector layers consistently.
- Terminus boost must be deterministic across rebuilds: dominance is decided by the configured frequency ratio against the next-highest line's `f_weighted` at the stop, with ties broken explicitly (e.g. by `line_key` lexicographic order).

## Possible future extension

Replace the discrete KNN neighborhood with **KDE-style** weighting: instead of "rank within the K nearest", every other feature contributes to this feature's local context with a weight that decays smoothly with distance. Removes the boundary artifact where the last rural stop before a city gets boosted while the first city stop 200 m further in is crushed. Same inputs and outputs, only the neighborhood math changes. Out of scope for the initial implementation; added if the KNN version shows visible artifacts at city edges.
