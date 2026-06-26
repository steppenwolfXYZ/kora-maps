# Seasonal regional bus rescue

## Problem

Mountain-feeder bus variants (Theytaz 372 to Le Chargeur, Auto AG Schwyz 506 to Sahli Seilbahnstation, PostAuto 431 to Vals Zervreila, etc.) are dropped by the variant-level `min_active_days >= 150` filter because they run only summer or winter season. The host line is usually drawn (year-round portion), but the alpine destination — which is the point of the line — is missing.

Once a seasonal variant clears the active-days gate, two downstream gates drop it again:

- The **rare-variant 10% filter** computes each variant's share of group-total sample trips. A variant with a small share is treated as garage noise. But on lines fragmented into many variants (e.g. Theytaz 372 with 22 variants), even the alpine destination's share stays small — both annually and in a season-restricted view — because the year-round variants also run during the season. Seasonal concentration alone is not enough to lift such a variant over 10%.

- The **group-level `worst_freq` gate** divides counted trips by all sample weekdays × window hours regardless of whether the line ran that day. Wholly seasonal alpine pass lines (PostAuto 681 Furka, 561 Splügen, 162 Susten, 185 Jaun, etc.) are diluted ~3-4× and fail despite running a normal hourly cadence in season.

The 150-day threshold cannot be lowered globally for the bus bucket: it would re-admit city construction-replacement services like SVB Bern 3A / 9A. Those services classify as `bus` (city) at the mode layer; the seasonal feeders classify as `regional_bus`.

## Requirements

### Active-days rescue

- New config key `min_active_days_regional_bus` (default 90). Applied only to the `bus` bucket.
- In the variant-level active-days filter, a bus variant with `active_days < min_active_days` (default 150) but `active_days >= min_active_days_regional_bus` is **rescued**: kept in the variant map, tracked in a `regional_bus_rescued` set keyed by `(tg_key, var_key)`. A bus variant below `min_active_days_regional_bus` is dropped as before (`short_active_period`).
- At feature emission, after the final mode classification, a rescued variant whose emitted mode is `"bus"` (city bus) is dropped without producing a feature. Its emission diagnostic carries the exclusion reason `seasonal_rescue_city_bus`. Rescued variants emitted as `"regional_bus"` are kept normally.
- Diagnostic counter at filter time: number of bus variants tentatively rescued, printed alongside the existing `min_active_days` summary.
- The per-variant block in `gtfs_groups_full.json` carries `regional_bus_rescued: true|false` and the new `exclusion_reason` value `seasonal_rescue_city_bus`.

### Rare-variant filter for rescued-bearing groups

A group containing at least one `regional_bus_rescued` variant uses the rules below instead of the legacy 10% / 5%-fallback gate. Non-rescued-bearing groups keep the legacy gate unchanged.

- **Per-window share test.** For each of three windows — annual, winter (Jan-Mar), summer (Jun-Aug) — compute each variant's weighted-share of the group's window-restricted total. A variant clears the share test in a window if its share ≥ 10%. The 5% fallback that the legacy gate applies when nobody clears 10% is **not** used in any window of this branch.
- **Kept-by-share set.** A variant passes if it clears the share test in any one of the three windows. The union of those passers forms the "kept-by-share" set.
- **Unique-stop rescue.** A variant outside the kept-by-share set is **rescued** if it serves at least one parent station such that:
  - the parent station is not served by any kept-by-share variant **across the entire dataset** — this matters because a depot run often shares part of another line's route on its way to the depot, so the "no other line serves this stop" check has to be global to filter those out, AND
  - the parent station's coordinates are at least `unique_stop_min_distance_m` away from every parent station served by **this trip group's** kept-by-share variants — the global "served by" check already filters depots co-located with foreign passenger stops, so the distance check only needs to handle depots co-located with this group's own kept stops, AND
  - the variant has a weighted-share ≥ `unique_stop_min_share_pct` in at least one of the three windows.
- The rescue is mutual: multiple variants below the share threshold that each contribute the same unique parent station all pass — the kept-by-share set is fixed at evaluation time and does not grow as variants are rescued.
- The per-variant diagnostic carries `rare_variant_window_passed: "annual" | "winter" | "summer" | "unique_stop" | null` (null = dropped).
- New config keys: `unique_stop_min_distance_m` (default 1000) and `unique_stop_min_share_pct` (default 0.02).

### Multi-window freq gate

- The group-level `worst_freq` check evaluates three windows for any group with at least one `regional_bus_rescued` variant: annual, winter (Jan-Mar), summer (Jun-Aug). Per-window `f_weighted` uses sample dates restricted to the window and the same window hour counts (07:00-23:00).
- A group passes if `f_weighted > worst_freq` in any of the three windows. The `freq_score` written onto emitted features uses the winning window's `f_weighted`, so line thickness and salience reflect in-season cadence rather than annual dilution.
- Groups without any rescued variant continue to use the annual window only.
- The per-group diagnostic carries `freq_gate_window_passed: "annual" | "winter" | "summer" | null`.

### Per-variant freq for line thickness

The motivating bug: a unique-stop-rescued variant (e.g. Theytaz 372 → Le Chargeur) inherits its group's high freq_score from the year-round dominant variants and renders at full thickness, despite running ~1 trip/day in season. The correct metric is per-direction trips/hour — what a passenger at a stop in that direction actually experiences.

- The `freq_score` written onto each emitted feature is computed from **that variant's trips only**, not the trip group's total.
- The per-variant `f_weighted` uses the same sample-date / window-hour normalisation as the group-level one. For groups that passed the freq gate via a seasonal window (above), the same window is used for the per-variant freq.
- **Inclusion is unchanged.** The group-level `worst_freq` gate still uses the group's combined freq. Per-variant freq drives only `freq_score` → `width_base` → thickness, and `salience_absolute` → `min_zoom`.
- Applies to every emitted feature in every bucket — bus, regional_bus, train, tram, metro, ferry, mountain. All buckets are now per-direction-split (see `.claude/concepts/remove-exempt-direction-key.md`), so per-variant freq equals per-direction freq across the board. Mountain has fixed width so the value is moot for the visual, but it still flows through salience scoring.
- The per-variant block in `gtfs_groups_full.json` gains its own `raw_freq` and `f_weighted` fields for debugging.

## Constraints

- All multi-window logic — both the rare-variant rules and the freq gate — is scoped to groups that contain at least one `regional_bus_rescued` variant. Non-rescued-bearing groups are evaluated against the annual window only with the legacy 10% / 5%-fallback gate, identical to today.
- The unique-stop rescue and the seasonal windows are bus-bucket-only consequences of `regional_bus_rescued`. Rail and tram are not subject to either rule — permanent infrastructure implies regular service, so the assumptions behind the rescue do not apply.
- Window definitions: winter = calendar months 1-3; summer = calendar months 6-8. A bus running Dec-Apr is caught via winter, one running Jun-Oct via summer. A bus active only in December does not pass — by design, since December-only is not the alpine-feeder pattern and is more likely construction noise.
- Mountain-origin exemption (`_gate_exempt`) is unchanged. Mountain and ferry stay fully exempt from the active-days gate.
- Night-only services still self-eliminate: the multi-window freq still counts only 07:00-23:00 trips, so any line with no daytime trips has `f_weighted = 0` in every window.
- City-bus construction-replacement services are kept out by the `seasonal_rescue_city_bus` drop at emission, which still runs regardless of which window or rescue rule kept the variant. The rare-variant rules only protect variants whose final mode classifies as `regional_bus`.
- The unique-stop rescue's distance check uses the parent station's GTFS coordinates. The kept-by-share set is computed once per group and frozen before the unique-stop test runs, so two sub-share variants that each add the same unique station both pass.
- Config validation: if `min_active_days_regional_bus >= min_active_days`, the rescue range is empty and the multi-window gates have no candidate groups; behaviour collapses to current annual-only.
- The width curve (`best_freq` / `worst_freq` per mode in `config.yaml`) was calibrated assuming group-level freq. Per-variant freq is roughly half for balanced bidirectional services, so existing lines will render thinner. Expect a follow-up retune of these endpoints once the visual result is reviewed.
