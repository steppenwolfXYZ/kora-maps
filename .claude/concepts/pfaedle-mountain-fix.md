# Pfaedle mountain fix

## Problem

When the pipeline migrated to pfaedle, the `mountain` and `ferry` buckets were left on a pre-pfaedle bypass that always emits straight-line geometry between consecutive GTFS stops. That bypass made sense as a last-resort fallback in the old matcher, but in the pfaedle world it is wrong: pfaedle does shape funiculars, cable cars, gondolas, and aerial lifts when the OSM ways exist, and OSM coverage of those modes inside the bbox is generally good. The Harderbahn and several Jungfrau-region cable cars are currently drawn as diagonals across the map while their real `railway=funicular` / `aerialway=*` ways are visible underneath on the basemap.

For genuinely aerial modes (cable car, gondola) OSM coverage is patchier than for rail, so a straight-line fallback on pfaedle failure is acceptable there. For everything else, including funiculars, a missing pfaedle shape should surface as a `pfaedle_unrouted` diagnostic, not be hidden behind a straight line.

## Requirements

1. Pfaedle is the only primary geometry source. No bucket and no GTFS `route_type` is excluded from the pfaedle pass.
2. `pfaedle.modes` in `scripts/transit/config.yaml` is extended so every mode the pipeline emits is shaped by pfaedle. This must include the modes for `route_type` 5 (cable car), 6 (aerial lift / gondola), and 7 (funicular), in addition to today's `tram, subway, rail, bus, ferry`. The exact pfaedle mode tokens are taken from pfaedle's own mode list, not invented.
3. A variant is shaped from pfaedle's `shapes.txt` whenever a shape is produced for its representative trip. This is unchanged from today for rail/road, and now applies equally to mountain and ferry.
4. Straight-line geometry between consecutive GTFS stops is allowed **only** for GTFS `route_type` 5 (cable car) and 6 (aerial lift / gondola), and only when pfaedle produces no shape for the representative trip. For these two route types, the missing shape causes a straight-line feature to be emitted; the line is still drawn.
5. For every other mode — including funicular (`route_type=7`), rail, rack-rail, tram, bus, regional bus, ferry — a missing pfaedle shape behaves as today: no feature is emitted and the rep trip is logged to `data/transit/pfaedle_unrouted.json`. Straight lines are never used as a fallback for these modes; a missing shape is treated as a data problem to surface, not paper over.
6. Aerial features emitted via the straight-line fallback carry a `geometry_source: "straight_line_fallback"` property on the feature and on the corresponding entry in `gtfs_groups_full.json`. Pfaedle-shaped features omit this property (or set `geometry_source: "pfaedle"`).
7. `pfaedle_unrouted.json` is kept and its semantics are unchanged: it lists trips that pfaedle could not shape. Aerial route_type 5/6 trips that were previously listed here are now emitted as straight-line features instead and do not appear in this file.
8. The `_NO_PFAEDLE_BUCKETS` constant and the bucket-keyed bypass branch are removed. The mode-based decision lives in exactly one place: after pfaedle, when deciding what to do about a missing shape.

## Constraints

- Mountain visual style is unchanged: light yellow `#ffe566`, fixed width base 1.0, no frequency-based saturation. Only the geometry source changes.
- Ferry visual style and bucket are unchanged.
- Rack-rail agencies stay in the `train` bucket (their GTFS `route_type=2`); they already pass through pfaedle's rail mode and are unaffected by this concept.
- `deduplicate_mountain()` is restricted to aerial features (`route_type` 5/6) and is unaffected by this concept.
- The bbox cut in step 03 must already include the OSM ways for the targeted aerial / funicular modes. No bbox change is in scope for this concept; if a feature falls back to a straight line because the OSM way is outside the bbox, that is a separate bbox issue, not a defect in this change.
- Rebuild restarts from step 5 because `pfaedle.modes` changes; downstream steps consume the new shapes.
