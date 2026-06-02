# Representative Trip by Platform Popularity

> **Status:** implemented, not yet verified. Verify once prm-platform-positions is implemented.

## Problem

Each drawable line (one feature per merged-stop variant within a trip group) is currently rendered from a single representative trip. That representative is chosen as the trip with the most active service days, ignoring which platforms it uses. As a result, lines are frequently drawn at unusual or atypical platforms — a Sunday-only or extended-service trip can outrank the everyday pattern, and several lines through the same station can collapse onto the same platform even when their normal trips depart elsewhere.

The drawn geometry, the snapped dots, and the per-stop platform identity all derive from this one representative trip, so picking the wrong representative directly determines where the line and its dots appear at every station along the route.

## Requirements

The representative trip for a merged-stop variant must come from the most common platform pattern in that variant, not the trip with the most service days.

Within one merged-stop variant, the trips are sub-grouped by their full platform-suffixed stop set. The weighted count of each sub-group is the sum of its trips' active-day weights (the same weight already used by the rare-variant filter). The sub-group with the highest weighted count is the "popular platform sub-variant". The representative trip is selected from that sub-variant.

Ties in weighted count are resolved deterministically: a stable secondary criterion picks the same sub-variant on every run regardless of dict iteration order.

The pfaedle shape fallback that searches for a routed shape across candidate trips must still scan trips outside the popular sub-variant if no trip inside it has a usable shape. The popular sub-variant is preferred, not exclusive. Without this fallback, a variant where pfaedle routed only the unusual platform would silently drop instead of being drawn.

The drawn variant must be recoverable downstream. The representative trip's platform-suffixed stop_id sequence is the canonical record of which platforms were drawn, and it must continue to appear in `line_stops.json` per emitted feature so that the prm-platform-positions concept (and any future per-platform positioning logic) can look up the correct platform coordinates without re-deriving the selection.

## Constraints

- The merged-stop variant grouping (UIC stations only, platforms ignored) is unchanged. Sub-grouping by platform happens only at the representative-trip selection step.
- The set of drawn features is unchanged. One feature per merged-stop variant is still emitted; this change does not introduce additional features for alternate platform patterns.
- The frequency and speed scoring, the rare-variant filter at the merged-set level, the rare-group / supergroup filter, and the mountain / CC carve-outs are all unchanged.
- The `rep_trip_id` field in `gtfs_groups_full.json` retains its name but now refers to the representative of the popular platform sub-variant. No semantic guarantee about service-day count is implied by this field after the change.
- Mountain and ferry buckets, which bypass pfaedle and emit straight-line geometry between GTFS stops, are unaffected — they have no per-trip platform variation worth selecting between in the same way.
