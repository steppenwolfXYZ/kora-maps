# Frequency-Weighted Line Scoring

## Problem

The current `freq_score` formula has structural problems:

- It is **linear in headway**, which compresses the high-frequency end — a 7-minute and a 15-minute bus look almost identical (`width_base` 2.6 vs 2.4).
- It treats the **evening and weekend windows as maluses on a core score**, so a line with no core service but real evening or weekend service short-circuits to score 0 regardless of how good those windows are.
- It uses a hardcoded `MIN_FREQ_SCORE = 0.075` gate left over from before the `bucket-worst-headway` concept; under that concept's new `worst_headway` calibration, this floor is redundant.
- The width range `[1.1, 2.6]` is too narrow to express "very thin marginal line" — the thinnest drawn line is barely thinner than a moderate one.

The result is that almost every urban bus and tram saturates at the same width, and lines with unusual service patterns either look the same as well-served lines or get dropped entirely.

## Requirements

### Time windows

Three non-overlapping windows define when a trip's first departure counts:

- **Core**: weekday, 07:00–19:00 (12 hours).
- **Evening**: weekday, 19:00–23:00 (4 hours).
- **Weekend**: weekend sample dates, 07:00–20:00 (13 hours).

A trip whose first departure falls outside all three windows (notably night buses departing at 23:00 or later) contributes to no window and effectively has zero weight on the line's score.

### Per-window frequency

For each emitted trip group, compute three frequencies in **trips per hour**:

- `f_core` = (sum of trips active in the core window across weekday sample dates) ÷ (n_weekday_samples · 12 hours)
- `f_eve`  = (… eve window …) ÷ (n_weekday_samples · 4 hours)
- `f_we`   = (… weekend window …) ÷ (n_weekend_samples · 13 hours)

Each per-window value is the trip group's average trips-per-hour during that window, on the sample dates that window applies to.

### Weighted frequency

A line's overall service level is captured by a single weighted frequency, in trips per hour:

```
f_weighted = w_core · f_core + w_eve · f_eve + w_we · f_we
```

The weights `w_core`, `w_eve`, `w_we` live in `scripts/transit/config.yaml` under a new top-level key `window_weights`. Initial values: `core: 0.6`, `eve: 0.2`, `we: 0.2`. They represent the relative importance of each window for the visual prominence of a line and must sum to 1.0. A line with no service in a window contributes zero from that window.

### Configuration: trips-per-hour endpoints

Per-mode endpoints move from `best_headway` / `worst_headway` (minutes) to `best_freq` / `worst_freq` (trips per hour) in `scripts/transit/config.yaml`. Initial values:

| mode | `best_freq` | `worst_freq` |
|---|---|---|
| train | 4 | 0.25 |
| tram | 8.6 | 0.6 |
| metro | 12 | 0.8 |
| bus | 10 | 0.75 |
| regional_bus | 2 | 0.17 |
| ferry | 1.33 | 0.029 |
| mountain | 1 | 0.05 |

The mountain row is informational only — mountain bucket remains exempt from the freq gate. Every bucket present in either table must appear in both; a missing entry is a fatal config error.

### Score

```
score_log = (log(f_weighted) − log(worst_freq)) / (log(best_freq) − log(worst_freq))
score = clamp(score_log, 0, 1) ** 2.5
```

The power `2.5` bends the curve so that:

- A perfect line (`f_weighted = best_freq`) scores 1.0.
- An evening-only-perfect line (`f_weighted = 0.2 · best_freq`) scores about 0.1.
- A line with `f_weighted = worst_freq` scores 0.

### Width range

Per-mode width endpoints live in `scripts/transit/config.yaml` under a new top-level key `line_width`. Each mode has a `min` and `max` value:

```
width_base = min_mode + (max_mode − min_mode) · score
```

Initial values:

| mode | `min` | `max` |
|---|---|---|
| train | 0.45 | 3.75 |
| tram | 0.30 | 2.50 |
| metro | 0.30 | 2.50 |
| bus | 0.30 | 2.50 |
| regional_bus | 0.30 | 2.50 |
| ferry | 0.30 | 2.50 |
| mountain | 0.75 | 0.75 |

Train's `1.5×` thickness is now expressed directly via its `max = 3.75` (= `1.5 × 2.5`); no separate multiplier exists. Mountain's fixed `width_base = 0.75` is expressed by `min = max = 0.75`; mountain's width is constant regardless of frequency, consistent with the rule that mountain is colored and sized by mode, not by service level.

Every mode that draws must appear in `line_width`; a missing entry is a fatal config error.

### Gate

A line draws iff `f_weighted > worst_freq`. Equivalently, `score > 0`. There is no separate `MIN_FREQ_SCORE` constant — `worst_freq` per mode is the only cutoff.

Mountain (aerial / funicular) and ferry buckets remain exempt from the gate, as today.

### Removed mechanisms

- `MIN_FREQ_SCORE` is removed entirely.
- `LOW_EVE_HEADWAY`, `LOW_WE_HEADWAY`, `MALUS_LOW`, `MALUS_NO` are removed. Per-window maluses are redundant once each window contributes its own positive score to the weighted average.
- The legacy `best_headway` and `worst_headway` config tables are removed in favour of `best_freq` and `worst_freq`.

### Corridor stop-pair frequency

The corridor stop-pair frequency table (used for the per-feature corridor boost) is built from the same per-trip-group `f_weighted` value. Each trip group contributes its `f_weighted` to every consecutive (UIC, UIC) pair on its canonical stop sequence. The corridor-boost rule in the emission loop applies the weighted frequency, not raw per-window counts.

### Diagnostics

`gtfs_groups_full.json` reports per trip group:

- `f_core`, `f_eve`, `f_we` (trips/hour per window)
- `f_weighted` (trips/hour)
- `freq_score` (the final `score` value after the curve and power)
- `width_base` (the final width input)

The legacy `raw_freq` shape (`core_wd` / `eve_wd` / `we`) is removed from the diagnostic. The `gtfs_unmatched.json` schema changes accordingly — it reports `f_weighted` instead of `raw_freq`.

## Constraints

- The trip-group partition (`gtfs-line-grouping` concept) and the rule that the trip group is the only line identity (`trip-group-as-sole-line-identity` concept) are unchanged. All per-window frequencies and the weighted frequency are computed per `tg_key`, with no parallel aggregation.
- The freq-sampling weekday/weekend sample dates are unchanged.
- Mountain bucket's fixed `width_base = 0.75` is unchanged (mountain is colored by mode, not by frequency).
- Mountain's exemption from the gate is unchanged. Aerial / funicular mountain origins remain fully exempt; rebucketed rail and rack mountain are gated like normal train.
- The CC-train exemption from the gate is unchanged.
- The active-days gate (`min_active_days`) and the rare-variant / rare-group filters are unchanged. They operate on trip-group structure, not on the freq score.
- Pair-freq corridor aggregation continues to span trip groups (trunk reinforcement is the point), with each trip group contributing its correctly-scoped `f_weighted`.
- The pipeline must fail loudly if any mode is missing a `best_freq` or `worst_freq` entry in config; there are no Python defaults.
- Trips with first departure outside all three windows are simply not counted in any window. They are not separately flagged or excluded; they just contribute nothing to the score.
