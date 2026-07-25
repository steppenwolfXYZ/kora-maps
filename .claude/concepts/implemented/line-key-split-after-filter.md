# Line Key Split After Filter (and Content-Hash Trip-Group Id)

## Problem

The trip-group partition (`gtfs-line-grouping.md`) fuses variants that share ≥2 merged stops within a `(long_name_norm or short_name, agency, bucket)` partition. When the feed labels multiple physically distinct services with the same generic ref (SBB files 8 different IR services under `short_name="IR"` and empty `long_name`; same shape for `IC`, `EC`), all of their trips pool into one partition, and the union-find then fuses everything reachable through low-frequency "sweeper" variants — a Brig ↔ Genève-Aéroport supplementary train shares stops with the Simplon, the Léman, and the Mittelland corridors and welds them together transitively.

The freq / rare-variant / short-active-period filters later drop those bridging variants, but the union-find has already committed the merge. The surviving high-frequency variants — a Genève ↔ Genève-Aéroport shuttle, a Brig ↔ Domodossola pair, a Bern → Zürich-Flughafen backbone — end up sharing one `line_key` and rendering as three geographically disjoint lines under `?line=IR~11~train~0`.

Separately, `trip_group_id` was assigned as a running counter (0, 1, 2…) in insertion order over `stop_times.txt`. Adding or removing any pattern in a feed refresh shifted every id in the affected partition, so URLs published against those ids broke on every SBB timetable rollover.

## Requirements

### Content-hash tg_id

`trip_group_id` is an 8-hex blake2s digest of the sorted union of merged UICs of the (sub-)group. Same UIC set → same id, so the same feed produces the same line_keys across rebuilds, and a small change to the feed only re-hashes the affected groups. The same hashing function names both the partition-level parent groups produced by `stream_stop_times` and the post-split sub-groups; there is no separate "sub id" scheme.

Collisions within one partition are asserted at build time (both in `stream_stop_times` and in the post-split loop) and raise. 8-hex is 32-bit; at typical partition sizes collision probability is negligible.

### Post-emission split

After `drawable_groups` is finalised — i.e. after the active-days filter, the rare-group filter, the rare-variant filter, and the freq gate have all run — every drawable group has union-find re-run on its surviving variants. Two variants merge iff they share ≥2 merged stops (same rule as the initial partition). If the group breaks into ≥2 connected components:

- Each component becomes its own tg_key with a fresh content-hash `trip_group_id` derived from that component's UIC union.
- Per-sub-group frequency is recomputed by summing `var_freq_seasonal` across the sub-group's surviving variants, both annual and per season.
- The freq gate is re-checked at sub-group granularity, using the same window rules the original gate used (rescued-bearing sub-groups sweep all seasons; others check only annual). Sub-groups where recomputed `f_weighted ≤ worst_freq` are dropped.

The parent tg_key disappears from `drawable_groups` and every per-tg map (freq, speed, mountain_origin, route_type, freq_gate_window_passed, supergroup_id, regional_bus_rescued, variant_counts, tg_total_weight, diag_original, diag_filter). Sub-groups replace it entry-by-entry, and per-variant maps keyed by the full 5-tuple (`variant_active_days`, `variant_date_stats`, `variant_runs_full`) are re-keyed onto the new tg_id.

### Diagnostic

`gtfs_groups_full.json` shows each surviving sub-group as its own top-level entry. The rebuild log carries a single summary line reporting how many parent groups were split, how many sub-groups came out, and how many were dropped by the re-check.

## Constraints

- The initial partition is unchanged. `stream_stop_times` still partitions by `(long_name_norm or short_name, agency, bucket)` and runs union-find with the ≥2-shared-stops rule.
- The freq-gate exemption for aerial / funicular / ferry carries through: exempt parent → exempt sub-groups.
- The rare-group / rare-variant filters run before the split, on the parent group. Their outcomes propagate to sub-groups as-is; the split does not re-run these filters.
- `_trip_group_export[tid]` is not rewritten in the split. Downstream code (`_pipeline_emission.py`, etc.) reads trip identity from `drawable_groups` directly, and per-trip metadata (stops, weights, direction) is keyed by `tid` not `tg_id`.
- Line width is unaffected: `compute_freq_score` still consumes per-variant seasonal freq at the winning window. Only the tg-level `f_weighted` shifts, which just gates whether the sub-group renders at all.
- Client `?line=…` URLs published against the old counter-based ids are invalidated by the switch to content hashes. There is no back-compat mapping.
- The tg_id is a URL-facing string; keep it hex (URL-safe) and short enough that pasted keys stay readable.
