# Seasonal regional bus rescue

## Problem

Mountain-feeder bus variants (Theytaz 372 to Le Chargeur, Auto AG Schwyz 506 to Sahli Seilbahnstation, PostAuto 431 to Vals Zervreila, etc.) are dropped by the variant-level `min_active_days >= 150` filter because they run only summer or winter season. The host line is usually drawn (year-round portion), but the alpine destination — which is the point of the line — is missing.

Once a seasonal variant clears the active-days gate, two downstream annual-averaging gates drop it again:

- The **rare-variant 10% filter** computes each variant's share of group-total sample trips over the full year. A 120-day summer variant looks like a tiny share in the annual view even when, on its operating days, it has more trips per day than the year-round dominant variant. Theytaz 372 Le Chargeur and PostAuto 431 Vals Zervreila both die here after active-days rescue.

- The **group-level `worst_freq` gate** divides counted trips by all 25 sample weekdays × window hours, regardless of whether the line ran that day. Wholly seasonal alpine pass lines (PostAuto 681 Furka, 561 Splügen, 162 Susten, 185 Jaun, etc.) are diluted ~3–4× and fail despite running a normal hourly cadence in season.

The 150-day threshold cannot be lowered globally for the bus bucket: it would re-admit city construction-replacement services like SVB Bern 3A / 9A. Those services classify as `bus` (city) at the mode layer; the seasonal feeders classify as `regional_bus`.

## Requirements

### Active-days rescue

- New config key `min_active_days_regional_bus` (default 90). Applied only to the `bus` bucket.
- In the variant-level active-days filter, a bus variant with `active_days < min_active_days` (default 150) but `active_days >= min_active_days_regional_bus` is **rescued**: kept in the variant map, tracked in a `regional_bus_rescued` set keyed by `(tg_key, var_key)`. A bus variant below `min_active_days_regional_bus` is dropped as before (`short_active_period`).
- At feature emission, after the final mode classification, a rescued variant whose emitted mode is `"bus"` (city bus) is dropped without producing a feature. Its emission diagnostic carries the exclusion reason `seasonal_rescue_city_bus`. Rescued variants emitted as `"regional_bus"` are kept normally.
- Diagnostic counter at filter time: a line reporting the number of bus variants tentatively rescued is printed alongside the existing `min_active_days` summary.
- The per-variant block in `gtfs_groups_full.json` carries `regional_bus_rescued: true|false` and the new `exclusion_reason` value `seasonal_rescue_city_bus`.

### Multi-window rare-variant filter

- The 10% rare-variant filter evaluates three windows for every variant in any group with at least one `regional_bus_rescued` variant: **annual** (current behaviour), **Jan–Mar** (winter), **Jun–Aug** (summer). Numerator and denominator are restricted to sample dates inside the window.
- A variant survives if it clears the threshold (10%, with 5% fallback as today) in **any** of the three windows. Each window's 10%/5% fallback runs independently.
- Non-rescued variants in non-rescued groups continue to use the annual window only.
- The per-variant diagnostic carries `rare_variant_window_passed: "annual" | "winter" | "summer" | null` (null = dropped as `rare_variant` in all three windows).

### Multi-window freq gate

- The group-level `worst_freq` check evaluates the same three windows for any group with at least one `regional_bus_rescued` variant. Per-window `f_weighted` uses sample dates restricted to the window and the same window hour counts (07:00–23:00).
- A group passes if `f_weighted > worst_freq` in **any** of the three windows. The `freq_score` written onto emitted features uses the winning window's `f_weighted`, so line thickness and salience reflect in-season cadence rather than annual dilution.
- Groups without any rescued variant continue to use the annual window only.
- The per-group diagnostic carries `freq_gate_window_passed: "annual" | "winter" | "summer" | null`.

## Constraints

- All multi-window logic is scoped to groups that contain at least one `regional_bus_rescued` variant. Groups without rescued variants are evaluated against the annual window only, identical to today.
- Window definitions: Jan–Mar = calendar months 1–3; Jun–Aug = calendar months 6–8. A bus running Dec–Apr is caught via Jan–Mar; one running Jun–Oct via Jun–Aug. A bus active only in December does NOT pass — by design, since December-only is not the alpine-feeder pattern and is more likely construction noise.
- Mountain-origin exemption (`_gate_exempt`) is unchanged. Mountain and ferry stay fully exempt from the active-days gate.
- Night-only services still self-eliminate: the multi-window freq still counts only 07:00–23:00 trips, so any line with no daytime trips has `f_weighted = 0` in every window.
- City-bus construction-replacement services are kept out by the `seasonal_rescue_city_bus` drop at emission, which still runs regardless of which window the group passed. The multi-window gates only protect variants whose final mode classifies as `regional_bus`.
- Config validation: if `min_active_days_regional_bus >= min_active_days`, the rescue range is empty and the multi-window gates have no candidate groups; behaviour collapses to current annual-only.
