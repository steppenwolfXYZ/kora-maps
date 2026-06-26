# Remove EXEMPT_DIRECTION_KEY for ferry / aerial / funicular

## Problem

After `stream_stop_times`, the trips of ferry (route_type 1000), aerial (1300/1303), and funicular (1400) modes have their `(first_uic, last_uic)` direction key overwritten with the canonical `EXEMPT_DIRECTION_KEY = ("*", "*")`. Both directions of such a line collapse into a single variant per merged stop set, and emit one feature.

The original motivation was a pre-pfaedle OSM matching issue: the old matcher would sometimes find a good shape for one direction and a degenerate straight line for the other, so collapsing directions ensured the good shape was used. Pfaedle does not have this problem.

The collapse causes downstream metrics (freq score, thickness, salience) to count both directions' trips as one trip group, overstating the per-stop service level by roughly 2× relative to what a passenger at a stop actually experiences. It also blocks per-direction handling that the rest of the pipeline now supports natively.

## Requirements

- Remove the EXEMPT_DIRECTION_KEY overwrite. Ferry / aerial / funicular trips keep their natural per-trip `(first_uic, last_uic)` direction key.
- Variants are formed per `(merged_stop_set, direction_key)` for these modes too — same as every other bucket.
- The existing `deduplicate_mountain` (aerial only, keyed on `ref`) continues to run. Opposite directions of a cable car typically have identical bboxes and will collapse into one feature via this dedup. That matches today's visual for aerials and is acceptable: if the stops are the same, one feature is fine.
- Funiculars and ferries have no dedup. Symmetric services with identical shapes emit two stacked features (visually equivalent to one). Asymmetric services (different stops per direction, common in ferries) emit two distinct features.

## Constraints

- `_gate_exempt(bucket, mountain_origin)` still exempts ferry + aerial + funicular from the active-days and freq-score gates.
- The rare-variant filter is NOT gate-exempt for these modes and now sees per-direction variants. Asymmetric direction trip counts could drop a small direction below the 10% / 5% share threshold. This is the same exposure that buses and trains have today. Acceptable.
- Aerial dedup key stays as `ref` only. No need to add `direction_key` — opposite directions sharing a bbox is the desired collapse case.
- This is a prerequisite for the per-variant freq work in `seasonal-regional-bus-rescue.md` — once direction is no longer collapsed for these modes, the per-direction freq metric becomes consistent across all buckets.
