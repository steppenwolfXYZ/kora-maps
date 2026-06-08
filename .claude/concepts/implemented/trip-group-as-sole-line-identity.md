# Trip Group as Sole Line Identity

## Problem

The `gtfs-line-grouping` concept established the trip group as the canonical unit of physical line identity. Trip groups are what the OSM matcher consumes, what the emission loop iterates, and what the comprehensive diagnostic keys by.

But every per-line quantity — frequency, speed, canonical stops, stop-pair frequency — is still computed on a *separate* grouping keyed by `line_key = (short_name, long_name, bucket)`. That parallel grouping is coarser than the trip group: it omits `agency_id`, omits `trip_group_id`, and a secondary index inside `build_gtfs_index` collapses it further by `short_name` alone or normalized `long_name` alone.

The result is that an emitted trip group inherits the summed frequency of every same-named line in the network. SVB Bern's evening-only bus 30 inherits VBL Luzern's and BVB Basel's daytime bus 30 frequency, clears the gate, and draws at maximum width.

## Requirements

### One grouping, used by everything

The trip-group partition built by the `gtfs-line-grouping` pass is the only grouping in the pipeline. There is no parallel per-line dict and no name-based fallback index. Frequency, speed, canonical stops, and stop-pair frequency are all expressed as lookups against the trip-group partition, keyed by `tg_key = (line_key, agency_id, trip_group_id)`. The trip-group partition itself is not recomputed, re-derived, or re-keyed; it is built once and read by everything downstream.

### Per-trip-group frequency

A single dict maps `tg_key → {core_wd, eve_wd, we}`. It is populated in the same pass that already iterates trips by `tg_key` to build the trip-group exports. Each trip's contribution is computed exactly as today (from `frequencies.txt` entries when present, else from the first-departure bucket of its representative trip, weighted by `wd_hits` / `we_hits`) and added to the bucket for that trip's `tg_key`. The freq-score gate, the emission loop, and the diagnostics all read this dict.

### Per-trip-group speed

The canonical trip already chosen per `tg_key` (highest `stop_count * len(active_dates)` within the group) yields the speed. Speed is stored per `tg_key` and read by `speed_to_color` at emission.

### Removal of parallel structures

The following cease to exist:

- `line_freq` (the line_key-keyed frequency dict)
- `line_canonical` (the line_key-keyed canonical-trip dict)
- `line_speed` (the line_key-keyed speed dict)
- `gtfs_index` and `gtfs_long_index` (the name-only fallback indexes built by `build_gtfs_index`)

Their consumers — gate, emission, `gtfs_unmatched.json`, `gtfs_groups_full.json` — read directly from the per-trip-group dicts instead. No emission-time fallback by short_name or long_name exists.

### Diagnostics

`gtfs_unmatched.json` lists individual unmatched trip groups, not line_keys. `gtfs_groups_full.json`'s `raw_freq` and `freq_score` are the trip group's own values.

## Constraints

- The trip-group partition itself (the union-find over shared merged stops within `(long_name_norm or short_name, agency_id, bucket)`) is unchanged. The `gtfs-line-grouping` concept stays the source of truth for how groups are formed.
- Mountain (aerial/funicular) and ferry gate exemptions are unchanged. The mountain `freq_score` floor of 0.4 is unchanged. The CC-train exemption is unchanged.
- The rare-group and rare-variant filters are unchanged; they already operate per trip group.
- If a trip group has no canonical stops or no positive `core_wd`, no fallback recovers it. `compute_freq_score` returns 0, the gate drops it, the diagnostic records the reason. There is no name-based recovery anywhere.
