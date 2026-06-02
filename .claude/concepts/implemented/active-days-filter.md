# Active-Days Variant Filter

## Problem

Construction-replacement bus services pass the existing frequency gate because they run intensely during a short window and the frequency sampler's dates happen to fall inside that window. Concrete examples in the current feed:

- SVB Bern bus 9A runs only on 22 calendar days (Mar 23 – Apr 17) and bus 3A runs on 44 calendar days scattered across a 154-day window — single agency, single line, isolated construction event.
- The ref `EV4` (Ersatzverkehr 4) is used as a shared replacement-bus label by 6 different Swiss agencies (SBB-EV, BLS-EV, RhB-EV, TPF-EV, MGB-EV, TRAVYS-E). Each agency runs its own short construction events under that ref. Inside SBB-EV alone, `EV4` covers 24 geographically disjoint trip groups across Switzerland (Geneva, Winterthur, Lenzburg–Othmarsingen, Neuhausen–Dachsen, …), each its own physical replacement service with its own calendar window.
- Even within a single trip group, a line that is rerouted differently for several construction phases produces multiple variants (different merged-stop sets) whose calendars are disjoint. Summed across phases they can exceed the active-days threshold while no individual variant runs long enough to be a real service.

The freq score sees these as full-frequency services because, on the days they do run, they run as full replacement service. There is currently no check on how many calendar days a service is actually active over the feed period, so a service that exists only during a construction shutdown is indistinguishable from one that runs year-round.

Genuine seasonal services (summer-only Bodensee ferries, winter mountain lifts, etc.) must remain visible — the filter has to separate "runs a short window because of construction" from "runs a short window because that is its season".

## Requirements

### Active-day count per variant

A new per-variant quantity `active_days` is introduced. The unit is the **emitted-feature unit** — one entry per `(line_key, agency_id, trip_group_id, merged_stop_set)`, the same granularity that produces a feature in the output.

`active_days` for a variant is the size of the union, over every trip belonging to that variant, of the set of calendar dates on which the trip's service is active. The union is taken across the full feed validity period (`feed_start_date` … `feed_end_date`). Both `calendar.txt` regular weekday patterns and `calendar_dates.txt` exceptions (type 1 add, type 2 remove) are applied when determining whether a service runs on a given date.

The variant level is required (not line, not trip group). Two motivating cases:

- A single agency's ref can collect many physically separate services that share only the ref label (e.g. SBB-EV `EV4` = 24 unrelated rail replacements). Their stops do not overlap, so they form different trip groups; their calendars do not overlap, so unioning across the line label produces a misleadingly large `active_days`. A trip-group-level filter handles this case.
- Within a single trip group, multiple construction phases on the same corridor produce different merged-stop sets (different reroutes), each with its own disjoint calendar. They share enough stops to land in the same trip group, but they are not the same service. Unioning across the trip group would let any line with enough construction phases pass; the variant-level filter is required to handle this case.

### Minimum-active-days drop

A variant whose `active_days` falls below the configured threshold is not emitted as a feature. The variant is removed from its trip group at the same stage where the existing rare-variant filter operates, and contributes nothing to downstream emission or to share calculations in the rare-variant filter.

If every variant in a trip group is dropped by this filter, the trip group has no surviving variants and emits nothing.

### Bucket scope

The filter applies only to variants whose bucket is `bus`, `tram`, `train`, or `metro`. Variants in the `mountain` or `ferry` bucket are exempt — they are not evaluated against the threshold and are never dropped by this rule, regardless of their active-day count. This protects seasonal lake ferries and seasonal mountain lifts, which legitimately operate for windows comparable to or shorter than long construction projects.

### Configuration

A new key `min_active_days` is added to the pipeline config (`scripts/transit/config.yaml`). It is an integer. The default value is 150. Lowering it (e.g. to 100) loosens the filter; raising it tightens it.

The threshold lives at top level of the config, alongside the other line-filtering settings.

### Diagnostic output

`gtfs_groups_full.json` is extended as follows.

Per-line (group) fields:

- `min_active_days_threshold` — the value of `min_active_days` that applies to this line (the configured threshold for in-scope buckets, `null` for exempt buckets).

Per-variant fields:

- `active_days` — the variant's active-day count.
- `exclusion_reason` gains a new value `short_active_period`, used when the variant is dropped by this filter. Existing values (`rare_variant`, `pfaedle_unrouted`, `polyline_too_short`, `null`) keep their meaning. A variant with `exclusion_reason = "short_active_period"` is recorded with `kept_by_variant_filter = false`.

Per-line `group_exclusion_reason` gains the value `short_active_period`, used when every variant in the trip group was dropped by this filter and the group therefore has no surviving variants. When only some variants in a group were short-active, the group remains drawable; the `short_active_period` value appears only at the variant level for those dropped variants.

### Filter ordering

The active-days variant filter runs after the trip-group partitioning is complete but before the supergroup formation and rare-group filter. Removing short-active variants first means their weighted trip counts no longer participate in supergroup share calculations or rare-variant share calculations, so a long-window main variant is correctly judged against the surviving real-service variants only.

## Constraints

- No name-based filtering. Agency-name substrings ("EV", "Ersatzverkehr", etc.) and route-name patterns (letter suffixes like `3A` / `9A`) are not used as signals by this filter. The only inputs are per-variant calendar-day coverage and bucket.
- Mountain and ferry are always exempt, in both code and diagnostic fields. A mountain or ferry line's `min_active_days_threshold` is `null`, and the filter never produces `short_active_period` for those buckets at either the variant or group level.
- `calendar_dates.txt` exceptions are part of the count. A service that has its base pattern in `calendar.txt` plus add/remove rows in `calendar_dates.txt` is counted by the resulting effective active-date set, not by the base pattern alone.
- The frequency-score gate is unchanged and continues to operate at the line level. The new filter is additive at variant granularity: a feature is emitted only if its variant has `active_days ≥ min_active_days` (subject to the bucket exemption) **and** its line passes the freq-score gate (subject to its existing exemptions).
- `trip_group_id` semantics, the emission key `(line_key, agency_id, trip_group_id)`, and the per-variant merged-stop identity are unchanged. The filter only removes variants from their trip group; it does not change how groups or variants are identified.
- Existing exemptions stand. Any bucket-level or always-drawable carve-outs that already exist for mountain / ferry / CC-train continue to apply; this filter does not override them and is not overridden by them.
