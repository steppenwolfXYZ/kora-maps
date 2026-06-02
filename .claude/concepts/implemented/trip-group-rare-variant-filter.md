# Trip-Group Rare-Variant Filter

## Problem

Depot runs and other non-revenue short workings survive the rare-variant filter and reach the rendered map. Concrete example: Bern tram 6 emits "Hasler → Brunnhof" (7 trips, 2 stops) and "Kaufmännischer Verband → Eigerplatz" (15 trips, 3 stops) alongside the real ~220-trips-per-variant Worb Dorf ↔ Fischermätteli service.

The reason is structural. The trip-grouping rule requires two trips to share ≥2 merged stops to be unioned into the same trip group. A 2- or 3-stop depot working typically shares only one merged stop with the main service, so it is isolated as its own trip group. The current rare-variant filter applies a percentage threshold (10%, falling back to 5%) within a trip group. Inside an isolated single-variant group the variant is always 100% of the group's trips and can never be dropped, no matter how few absolute trips it represents.

The result is that a filter intended to suppress depot runs cannot see them, because the grouping step has already isolated them from the service they are runs of.

## Requirements

### Supergroup classification

Within each partition `(short_name, agency, bucket)`, trip groups are classified into **supergroups** by union-find with the predicate "share at least one merged stop (transitively)". A supergroup is a transient classification used only for the rare-group drop decision below; it does not change `trip_group_id` or any other identity used downstream.

A new identifier `supergroup_id` is introduced. It is unique within its partition. A trip group belongs to exactly one supergroup.

### Rare-group drop

A trip group is dropped entirely when its share of its supergroup's total trip count is below 10 %. If no trip group in the supergroup clears 10 %, the threshold falls back to 5 %. If nothing clears 5 % either, all trip groups in the supergroup are kept (the supergroup as a whole is sparse and dropping everything would erase a real but rare line). This is the exact same rule the per-variant filter applies within a trip group.

No absolute trip-count floor. A frequently scheduled depot run can have many trips per day, so absolute count is not a reliable signal of "real service"; the share comparison against the supergroup total is what carries the decision.

Dropping a trip group means none of its variants are emitted as features, regardless of what the per-variant filter would say.

### Per-variant filter unchanged in behaviour

The existing per-trip-group rare-variant filter (10 % share within the trip group, falling back to 5 %) continues to operate as today. The new supergroup filter is additive, applied before the per-variant filter. Variants inside a trip group that survives the supergroup filter are then filtered by the per-variant rule as before.

### Trip counting

Both the supergroup filter and the per-variant filter must count trips by a weighting that approximates how often each trip actually runs over the frequency-sampling window, not by raw distinct GTFS `trip_id` count. The weighting per trip is roughly the number of sample dates the trip's service is active on (the same `wd_hits + we_hits` quantity already used by the line-level frequency scoring).

The motivation: a depot run modelled in GTFS as 7 trip_ids active only on a single construction date is a much smaller real-world service than 7 trip_ids active every weekday. Raw trip_id counts cannot distinguish the two; weighted counts can. Since both filters use the same weighted count for numerator and denominator, the share comparison stays well-defined.

### Freq-score / drawable gate unchanged

The existing line-level frequency gate (`MIN_FREQ_SCORE`) and the mountain / CC-train always-drawable carve-outs are unchanged.

### Diagnostic output

`gtfs_groups_full.json` gains the following per-trip-group fields so this class of issue is diagnosable from the file:

- `supergroup_id` — supergroup identifier within the partition.
- `weighted_trip_count` — this trip group's weighted trip count (the quantity used by the supergroup filter; see "Trip counting" above).
- `supergroup_weighted_trip_count` — sum of weighted trip counts across all trip groups in the supergroup.
- `group_share_of_supergroup` — this trip group's `weighted_trip_count / supergroup_weighted_trip_count`.
- `rare_group_share_threshold` — the threshold that was applied (0.10, 0.05, or null if the fall-through "keep all" path was taken).
- `group_exclusion_reason` — gains a new value `rare_group_dropped` when the supergroup filter drops the group. Existing values (`low_frequency`, etc.) keep their meaning.

Per-variant diagnostics additionally gain:

- `weighted_trip_count` — the variant's weighted trip count, same counting method as above.
- `variant_share_of_group` — variant's `weighted_trip_count / sum of weighted trip counts across kept variants in the group at filter time`.

The existing raw `trip_count` (distinct GTFS `trip_id` count) and `total_trip_count` fields stay for backward compatibility.

## Constraints

- Cross-region same-line preservation. Two `(short_name, agency, bucket)` partitions are independent. Within one partition, two trip groups that share no merged stop form two separate supergroups and are evaluated against their own totals. A genuinely low-frequency line in a disconnected part of an agency's network is not dropped because of a high-frequency same-numbered line elsewhere.
- Single-stop-overlap services with <10 % share are dropped. A trip group that lands in the same supergroup as a much larger main service via a single shared stop and represents under 10 % of the supergroup's weighted trips is dropped, regardless of its absolute trip count. This is the intended consequence of using the same rule that already governs the per-variant filter; the design accepts that the rare cases of a legitimate small branch sharing only one stop with the main line are filtered out, in exchange for reliable removal of depot runs that can themselves be frequent.
- Mountain and CC-train exemptions continue to apply. A trip group in the `mountain` bucket or with a CC-train line key is not subject to the rare-group drop.
- The trip-group identity model is unchanged. `trip_group_id` keeps its current semantics (unique within partition, derived from the ≥2-shared-stops union-find). The emission key remains `(line_key, agency_id, trip_group_id)`. The supergroup classification is a transient annotation used for filtering and diagnostics only.
- The per-variant filter is unchanged. Its existing thresholds (10 % with 5 % fallback) and its existing fallback-to-keep-all behaviour when no variant clears either threshold remain as they are.
- Variants dropped because their entire trip group was dropped are reported with `group_exclusion_reason = "rare_group_dropped"` at the group level, and the same `rare_group_dropped` value propagates to each of the group's variants' `exclusion_reason`. This matches the existing behaviour for `low_frequency`: when a group is dropped, the reason surfaces both at the group level and on every variant inside it.
