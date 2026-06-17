# Seasonal regional bus rescue

## Problem

Mountain-feeder bus variants (Theytaz 372 to Le Chargeur, Auto AG Schwyz 506 to Sahli Seilbahnstation, PostAuto 431 to Vals Zervreila, etc.) are dropped by the variant-level `min_active_days >= 150` filter because they run only summer- or winter-season. The host line is usually drawn (year-round portion), but the alpine destination — which is the point of the line — is missing. The 150-day threshold cannot simply be lowered globally for the bus bucket: it would re-admit city construction-replacement services like SVB Bern 3A / 9A. Those services classify as `bus` (city) at the mode layer; the seasonal feeders classify as `regional_bus`.

## Requirements

- New config key `min_active_days_regional_bus` (default 90). Applied only to the `bus` bucket.
- In the variant-level active-days filter, a bus variant with `active_days < min_active_days` (default 150) but `active_days >= min_active_days_regional_bus` is **rescued**: kept in the variant map, tracked in a `regional_bus_rescued` set keyed by `(tg_key, var_key)`. A bus variant below `min_active_days_regional_bus` is dropped as before (`short_active_period`).
- At feature emission, after the final mode classification, a rescued variant whose emitted mode is `"bus"` (city bus) is dropped without producing a feature. Its emission diagnostic carries the exclusion reason `seasonal_rescue_city_bus`. Rescued variants emitted as `"regional_bus"` are kept normally.
- Diagnostic counter at filter time: a line reporting the number of bus variants tentatively rescued is printed alongside the existing `min_active_days` summary.
- The per-variant block in `gtfs_groups_full.json` carries:
  - `regional_bus_rescued: true|false`
  - the new `exclusion_reason` value `seasonal_rescue_city_bus` for rescued variants that got dropped at emission for being classified as city bus.
- All other gates and emission logic remain identical. Group-level freq-gate, supergroup / rare-variant filter, pfaedle routing, polyline geometry, salience scoring all see rescued variants the same way they see any other kept variant.

## Constraints

- Only the `bus` bucket. Tram, train, metro, ferry, mountain are untouched.
- Mountain-origin exemption logic (`_gate_exempt`) is unchanged. Mountain and ferry remain fully exempt from the active-days gate.
- The freq gate is unchanged. A rescued variant rides on its group's freq score; if the group fails worst_freq, all rescued variants drop with it. Night-only lines (M41, NachtStern N-prefix routes) self-eliminate this way — their `f_weighted` is 0 because the gate's time-of-day window only counts trips between 07:00 and 23:00.
- The rare-variant / supergroup filter is unchanged. A rescued variant can still fail rare_variant.
- The rescue applies regardless of `mountain_origin` (bus bucket has `mountain_origin = None` anyway).
- Config validation: if `min_active_days_regional_bus >= min_active_days`, the rescue range is empty; no special handling required.
