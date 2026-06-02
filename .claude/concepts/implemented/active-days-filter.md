# Active-Days Line Filter

## Problem

Construction-replacement bus services pass the existing frequency gate because they run intensely during a short window and the frequency sampler's dates happen to fall inside that window. Concrete examples in the current feed: SVB Bern bus 9A runs only on 22 calendar days (a continuous 26-day block, Mar 23 – Apr 17) and bus 3A runs on 44 calendar days scattered across a 154-day window — both shadow the parent trams 3 / 9 with high apparent frequency and reach the rendered map as duplicate corridor lines.

The freq score sees these as full-frequency services because, on the days they do run, they run as full tram-replacement service. There is currently no check on how many calendar days a line is actually active over the feed period, so a service that exists only during a construction shutdown is indistinguishable from one that runs year-round.

Genuine seasonal services (summer-only Bodensee ferries, winter mountain lifts, etc.) must remain visible — the filter has to separate "runs a short window because of construction" from "runs a short window because that is its season".

## Requirements

### Active-day count per line

A new per-line quantity `active_days` is introduced. It is the size of the union, over all trips on that line, of the set of calendar dates on which the trip's service is active. The union is taken across the full feed validity period (`feed_start_date` … `feed_end_date`). Both `calendar.txt` regular weekday patterns and `calendar_dates.txt` exceptions (type 1 add, type 2 remove) are applied when determining whether a service runs on a given date.

The unit "line" here is the same unit at which `freq_score` is currently computed — the partition `(line_key, agency_id, bucket)`. All trip groups under the same line share a single `active_days` value, the same way they currently share `raw_freq` and `freq_score`.

Choosing the line level rather than the trip-group level is deliberate. A line that is rerouted differently for different construction events produces multiple separate trip groups, because the rerouted variants do not share enough merged stops to be union-found into one group. Those separate trip groups stay separate — the filter does not merge them, which would inflate the per-group trip count and make any single rerouting appear larger than it is. Instead, `active_days` is computed once at the line level as the union of calendars across all of the line's trip groups. Even a line with several distinct construction reroutes across the year produces only a small union of dates, well below a year-round line, so the line-level verdict still drops it. The trip-group identity model is left untouched.

### Minimum-active-days drop

A line whose `active_days` falls below a configured threshold is not drawable. None of its trip groups are emitted as features. This is treated as a line-level exclusion in the same place where `low_frequency` is currently decided, alongside the freq-score gate.

### Bucket scope

The filter applies only to lines in the `bus`, `tram`, and `rail` buckets. Lines in the `mountain` and `ferry` buckets are exempt — they are not evaluated against the threshold and are never dropped by this rule, regardless of their active-day count. This protects seasonal lake ferries and seasonal mountain lifts, which legitimately operate for windows comparable to or shorter than long construction projects.

### Configuration

A new key `min_active_days` is added to the pipeline config (`scripts/transit/config.yaml`). It is an integer. The default value is 150. Lowering it (e.g. to 100) loosens the filter; raising it tightens it.

The threshold lives at top level of the config, next to the other line-filtering settings. It is read once when the line filter runs.

### Diagnostic output

`gtfs_groups_full.json` gains the following per-line fields:

- `active_days` — the value defined above (integer).
- `min_active_days_threshold` — the value of `min_active_days` that was applied to this line (the configured threshold for in-scope buckets, or `null` for exempt buckets).

`group_exclusion_reason` gains a new value `short_active_period`, used when the line is dropped by this filter. Existing values (`low_frequency`, etc.) keep their meaning. When a line is dropped by this rule, every trip group under it carries `group_exclusion_reason = "short_active_period"`.

## Constraints

- No name-based filtering. Agency-name substrings ("EV", "Ersatzverkehr", etc.) and route-name patterns (letter suffixes like `3A` / `9A`) are not used as signals by this filter. The only inputs are calendar-day coverage and bucket.
- Mountain and ferry are always exempt, in both code and diagnostic fields. A mountain or ferry line's `min_active_days_threshold` is `null`, and the filter cannot produce `short_active_period` for those buckets.
- `calendar_dates.txt` exceptions are part of the count. A service that has its base pattern in `calendar.txt` plus add/remove rows in `calendar_dates.txt` is counted by the resulting effective active-date set, not by the base pattern alone.
- The frequency-score gate is unchanged. The new filter is additive: a line must satisfy both the existing `MIN_FREQ_SCORE` gate and the new `min_active_days` gate to be drawable (subject to the bucket exemption above).
- `trip_group_id` semantics and the emission key `(line_key, agency_id, trip_group_id)` are unchanged. The filter operates at the line level above trip groups; it does not change how groups or variants are identified.
- Existing exemptions stand. Any bucket-level or always-drawable carve-outs that already exist for mountain / ferry / CC-train continue to apply; this filter does not override them and is not overridden by them (the bucket scope rule above already excludes mountain and ferry; rail CC-train carve-outs at the freq-score layer are independent of this filter and remain in force).
