# Multi-Date Frequency Sampling

## Problem

`compute_freq_score` measures frequency on **two** sample dates: one Tuesday in April and one Saturday in April. Any line whose service window doesn't cover those exact two dates is invisible to the freq score: construction replacement lines (e.g. 9A) with full weekday service look identical to permanent lines, while seasonal services running outside the sample dates score 0 and get dropped as low-frequency. A single sample slice cannot distinguish "this line runs all year" from "this line runs for four weeks during construction" — the dimension is missing entirely.

## Requirements

Replace the two-date sampling with a list of `N` weekday sample dates and `M` weekend sample dates, drawn from the feed's coverage period and stored explicitly in `scripts/transit/config.yaml`. Code reads the list; no dates remain hardcoded.

### Sample-date generation

- Total samples per year: **3 per month** — 2 weekdays + 1 weekend day. For a 12-month feed period this yields 24 weekday samples + 12 weekend samples.
- Weekday slots cycle Mon/Tue/Wed/Thu/Fri across months so each weekday gets roughly equal representation.
- Weekend slot alternates Sat/Sun across months.
- A fixed random seed governs which week of each month is picked, so the date list is reproducible across runs.
- A **blackout list** of date ranges (Christmas/New Year week, Easter weekend, Pentecost weekend, Ascension, Swiss National Day Aug 1, other federal holidays) is part of config. When the generator's random pick lands inside a blackout:
  - First try the same day-of-week in the next week of the same month.
  - If still in blackout, try the same day-of-week in the prior week of the same month.
  - If both fail (rare; only for multi-week blackouts), shift to the same day-of-week in the adjacent month, preferring later.
- The blackout list is also config-editable: discovering a new problematic date is a config change, not a code change.

### Counter accumulation

`line_freq[line_key]` retains its existing structure (`core_wd`, `eve_wd`, `we` counters). For every trip:

- For each sample weekday the trip is active on, increment `core_wd` / `eve_wd` per the trip's first-departure-time window, exactly as today.
- For each sample weekend day the trip is active on, increment `we` per its window, exactly as today.

After the stream completes, the counters are **normalised to per-sample-day averages**: `core_wd /= N`, `eve_wd /= N`, `we /= M`. The downstream `compute_freq_score` formula and thresholds are unchanged: a line averaging the same per-day counts on the new samples as the old line had on the old samples gets the same score.

### Calendar.txt expansion

`load_calendar_dates` resolves `calendar.txt` weekly-pattern rows for **every** sample date, not just two. As a side effect, `svc_dates[service_id]` becomes a richer set for feeds that lean on `calendar.txt` (other countries; the Swiss feed uses `calendar_dates.txt` for most service definitions and is largely unaffected). This automatically improves the existing 10% variant filter on calendar.txt-heavy feeds, because the filter weights variants by `len(active_dates)`. No separate code change to the 10% rule is required.

### Configuration keys introduced

In `scripts/transit/config.yaml`:

- `freq_sampling.seed` — integer seed for reproducible generation.
- `freq_sampling.weekday_dates` — explicit list of `YYYYMMDD` strings written by the generator.
- `freq_sampling.weekend_dates` — explicit list of `YYYYMMDD` strings.
- `freq_sampling.blackout_ranges` — list of `[start, end]` `YYYYMMDD` pairs.

The generator runs once per feed-period change (or on demand); its output is committed into config so reruns of the main pipeline are deterministic.

## Constraints

- Trip grouping is unaffected. The partition + union-find logic in `stream_stop_times` continues to operate on the full set of buffered trips regardless of dates.
- The 10% variant filter is unaffected as code, but its inputs become richer on calendar.txt-heavy feeds — desirable.
- Mountain bucket exemption from the low-frequency gate is unchanged.
- The CC/train (seasonal rack) exemption is unchanged.
- `MIN_FREQ_SCORE = 0.075` and the per-mode `BEST_HEADWAY` / malus headway constants are unchanged.
- Evening-only-every-day lines (the line-30 case) remain `no_draw="low_frequency"` because `core_wd` stays zero across every weekday sample. This is acceptable; a separate concept can address them later if needed.
- The current behaviour of `frequencies.txt` (headway-defined services) is preserved: integer-divided trip counts per sample day, summed across samples, then normalised the same way.
- Sample-date counts (N=24 weekday, M=12 weekend per year) are starting values; both are tunable in config without a code change. If the feed period is shorter than 12 months, generation scales proportionally.
- The pipeline's runtime cost rises by O(samples × trips) set lookups in `stream_stop_times` — sub-second on the Swiss feed, negligible.
