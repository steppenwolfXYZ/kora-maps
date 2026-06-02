# Per-Bucket Worst Headway

## Problem

Swiss lake ferries that run only seasonally and only once per day (e.g. BAT 74 Brienz–Giessbach–Oberried–Iseltwald–Bönigen–Interlaken Ost on Lake Brienz, plus the comparable services on Thunersee, Walensee, Vierwaldstättersee Beckenried–Gersau, and the Untersee solar ferry) are dropped by the frequency filter even though they are real, regular public transit during their operating season. The active-days exemption for ferry/mountain already prevents the seasonality filter from cutting them, but the low-frequency gate (`MIN_FREQ_SCORE`) then drops them anyway because the current scoring formula cannot produce a passing score for ~1 trip per day at the ferry bucket's `BEST_HEADWAY`.

Today the score formula has three branches and a hardcoded `0.15` floor that was carried over from the legacy single-date scoring. That floor effectively caps what any sub-2-trip-per-day line can ever score, regardless of mode, so no calibration of `BEST_HEADWAY` alone can rescue once-daily ferries.

## Requirements

Replace the branched score formula with a single per-bucket linear interpolation that has two configurable endpoints: the existing best headway, and a new worst headway.

- Introduce a new mode-keyed table `worst_headway` (minutes) alongside `best_headway`. Every bucket present in `best_headway` must have a corresponding `worst_headway` entry.
- Both `best_headway` and `worst_headway` move from hardcoded Python constants into `scripts/transit/config.yaml` under a single new top-level key (e.g. `headway`). The pipeline reads them from config; there are no Python defaults. A missing bucket entry in either table is a configuration error and the pipeline must fail loudly rather than silently fall back.
- The core-hours frequency score is determined by the line's effective headway `actual_hw = CORE_MINUTES / core_trips`:
  - When `actual_hw <= best_hw`: score = 1.0.
  - When `actual_hw >= worst_hw`: score = 0.0.
  - In between: linear interpolation between the two endpoints.
  - When `core_trips == 0`: score = 0.0 (no draw, same as today).
- The hardcoded `0.15` floor and the separate `>= 1`, `< 2` branch in the current formula are removed. The formula becomes one continuous expression with a single piecewise clamp at 0 and 1.
- Evening and weekend maluses are applied after the core score is computed, the same way they are today. `WORST_HEADWAY` defines the zero point of the pre-malus core score; maluses then multiply on top.
- Initial `WORST_HEADWAY` values per bucket: calibrated so that the new draw cutoff (the lowest `core_trips` value that yields a freq_score at or above `MIN_FREQ_SCORE`) deviates from today's cutoff by no more than ~5% for every non-ferry, non-mountain bucket. Ferry deliberately loosens: its worst headway is set high enough that a line with the seasonal sample-density of BAT 74 clears the gate. Mountain's value is informational only — mountain remains bucket-exempt from the freq gate.

## Constraints

- The `freq_score` output range stays `[0, 1]`. Downstream callers (`freq_to_width_base`, supergroup share calculations, diagnostic dumps) must continue to work unchanged.
- The mountain bucket exemption from the freq gate stays in place.
- The active-days exemption for the ferry and mountain buckets stays in place. This change is in addition to that exemption, not in place of it.
- Non-ferry buckets must not measurably change which lines draw. The acceptance bar is "no more than ~5% drift in the cutoff trips-per-day value per bucket"; marginal lines right at the threshold may flip in either direction, which is acceptable.
- Ferry buckets gain new draws: any seasonal lake ferry whose effective headway is below the ferry `WORST_HEADWAY` will now draw, including lines with `freq_score = 0.000` today.
- The partition key for trip-grouping (`short_name OR long_name, agency_id, bucket`) is not changed by this work. The fact that the five daily Lake Brienz ferries (different ref numbers) live in separate trip groups is a separate concern; this change makes each of them individually drawable instead.
