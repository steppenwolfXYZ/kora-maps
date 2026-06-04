# Pill Rendering

## Problem

The current stop-pill rendering uses uniform pill shapes derived from spatial clustering of stop dots, without regard to actual platform geometry. Real platforms vary from 10 m bus bays to 500 m mainline train platforms, and at multi-line stations the lines run nearly parallel through the area. If each stop's dot is placed naively at the GTFS coordinate, dots sit at staggered points along each line and the pills connecting them become zig-zag rather than clean perpendicular bars.

The `prm-platform-positions` concept provides per-platform attributes: `length` for ~95% of rail platforms (with the GTFS coord at the platform centre) and reliable GTFS coordinates for tram/bus stops (with the coord at the front of the stop in the direction of travel). Combined with the per-line polylines pfaedle already produces, this is enough to give each platform an **allowed range** along its polyline, then place the platform's **dot** anywhere within that range — with the freedom to coordinate placement across a station so that the pills connecting the dots are short and visually clean.

## Multi-zoom-level stop styling

The eventual map will use three distinct stop-style systems, chosen by zoom range:

- **Far zoom** — a single circle per station. Conveys presence and mode, not platform geometry.
- **Medium zoom** — a precise dot-and-pill per platform, faithful to platform extent and clustered cleanly across multi-line stations.
- **Short zoom** — a detailed style emphasising platform-level structure. Vision not yet defined.

This concept covers only the **medium-zoom** layer. Far and short are placeholders; their concepts will be written when their designs are ready.

## Requirements

### Platform extent (the dot's allowed range)

Each platform has an **allowed range**: a contiguous interval along its line's polyline, within which its dot may be placed. The interval is anchored differently per mode, reflecting the GTFS coord's physical meaning:

- **Rail (train, metro)** — interval centred on the GTFS coordinate, extending half of the platform length in each direction along the polyline. The GTFS coordinate is the platform centre.
- **Tram / bus / regional_bus** — interval starts at the GTFS coordinate and extends backwards along the polyline (against the direction of travel) by the platform length. The GTFS coordinate is the front of the stop.

`length` comes from atlas for rail (about 95% coverage); the remaining 5% use a per-mode default. Tram/bus get the per-mode default unconditionally — atlas does not carry `length` for those modes.

Out-of-scope modes (ferry, mountain) render as today.

### Dot placement (the decision variable)

For each platform a single dot is placed somewhere within its allowed range. The position within the range is the decision variable.

- A platform with no neighbouring platforms at the same station has its dot at the GTFS coordinate — no optimisation needed.
- At a station with two or more platforms, dots are placed so the total pill length connecting them is as short as possible, subject to every dot staying within its platform's allowed range. The expected geometric outcome at typical multi-track stations is that dots line up across parallel polylines so pills run perpendicular to the lines, mimicking a real station's cross-platform layout.
- When polylines at a station do not run parallel (cross-junctions, etc.), the optimisation still applies — it minimises total pill length — but the resulting geometry is whatever falls out, not necessarily aligned.

### Pills (connections between dots)

A pill is a line segment joining adjacent dots at the same station cluster. Pill length is a *consequence* of dot placement, not a primitive value. Pill rendering style (thickness, casing, mode-coloured stroke) is unchanged from today; only the endpoint positions change.

### Connectors

Connectors (the joining lines between two physically separated pill groups within one station, e.g. surface tracks ↔ underground tracks at a multi-deck station) are unchanged in topology. Their endpoints are dots, so they automatically benefit from the same dot-placement optimisation.

### Pill grouping (which dots a pill connects)

Which dots a pill connects is determined by the existing clustering — rail by `parent_station`, others spatially within a per-mode radius — and the existing nearest-neighbour path within each cluster. This concept changes only *where* each dot sits, not which dots cluster.

### Per-mode defaults and sanity ranges

The following values live in `config.yaml` and are tuned via configuration only, not code changes:

| Mode | Default length | Sanity min | Sanity max |
|---|---:|---:|---:|
| train        | 100 m | 30 m | 700 m |
| metro        |  60 m | 30 m | 400 m |
| tram         |  35 m | 10 m | 100 m |
| bus          |  30 m |  5 m | 100 m |
| regional_bus |  30 m |  5 m | 100 m |

The default applies when atlas does not provide a length (always for tram / bus / regional_bus; rare for train / metro). The sanity range filters atlas values: anything below the min (e.g. 0 m placeholders) or above the max (e.g. kilometre-scale ferry-route mislabels) falls through to the default. The bus and regional_bus rows are identical: the vehicles are effectively the same, and shared platforms with stacked buses justify a roomy upper bound. The bus default of 30 m is set so a bus platform's allowed range reaches across the typical front-to-front offset between a bus stop and the trams or other buses sharing the same street, letting the dot coordination algorithm pull all three onto a common station axis.

### Fallback chain

For each platform's allowed range:

1. If atlas provides a sane `length` (within the per-mode sanity range), use it, anchored per the mode rule (centred for rail, backwards from front for tram/bus).
2. Otherwise, use the per-mode default length with the same anchor rule.
3. If the polyline at the GTFS coordinate is too short to support the resulting allowed range (degenerate geometry), clip the range to the available polyline.
4. As a last resort, fall back to today's clustering-derived pill shape for that stop only.

### Debug overlay

Two debug elements render on top of the production style, both filtered to the modes in scope (train, metro, tram, bus, regional_bus):

- A **thin black line** tracing each platform's full allowed range along its line's polyline — one per `(line, stop)` pair.
- A **clickable white-filled, black-outlined dot** at the GTFS coordinate snapped onto each line's polyline — 1:1 with the debug lines (every line has a dot, every dot has a line). Clicking opens a popup with the stop name, mode, atlas platform length (or `– (default)` when atlas had none), and mode-coloured badges for each line stopping there. Hovering a badge shows the line's `origin → destination` as a tooltip.

These are development-time visual aids only; not part of the medium-zoom production style.

## Constraints

- Far-zoom and short-zoom stop styles are out of scope.
- `compass_direction` from atlas is intentionally not consumed. The polyline tangent at the dot position is the orientation source for pill geometry.
- Per-mode default lengths and atlas-`length` sanity ranges are configuration values.
- Ferries and mountain modes are out of scope; they render as today.
- Single-platform stops always render as just a dot. The cluster-wide optimisation runs only on clusters of at least two platforms.
- This concept depends on `prm-platform-positions` being implemented and `stop_attributes_sources.json` being emitted.
- The implementation must not regress the rendering of stops without atlas data — the fallback chain guarantees a pill is always producible.

## Algorithm note

Dot placement runs in two stages on each merged cluster (the cluster produced by the existing spatial cluster pass followed by `parent_station` merge — both rail and non-rail). The merged cluster represents one logical station; its sub-groups are physically distinct platform groups within that station.

### Stage 1 — Sub-cluster local axis projection

Pills are drawn **as if each sub-group were a separate cluster**: the axis projection runs per sub-cluster, never across the whole merged cluster.

1. Sub-cluster the merged cluster spatially (the same radius as the initial cluster pass: 300 m rail, 50 m non-rail). Each sub-cluster is a connected component of stops within the radius.
2. For each sub-cluster:
   - Compute the mean polyline tangent across that sub-cluster's stops, with direction canonicalised (so NB and SB versions of the same polyline don't cancel out).
   - Anchor the **local station axis** at the centre of the **intersection of all per-stop range tangent-coords** when that intersection is non-empty (every range covers some common axis position); otherwise fall back to the mean of range midpoints.
   - For each stop, place its dot at the point on its allowed-range polyline closest to that axis line.

The intersection-center preference is what makes the sub-pill align cleanly when extents overlap: every dot lands on the axis line and the bar is perpendicular. For sub-clusters with non-overlapping ranges, the intersection is empty and the mean-midpoint fallback applies — dots clamp to the range end closest to the axis, producing the shortest possible sub-pill given the geometry.

The single-axis-across-the-whole-merged-cluster approach used previously is explicitly **not** done: it muddles the mean tangent across physically distinct platform groups (e.g. Eigerplatz Nord on a N–S street vs Eigerplatz Süd curving east on Eigerstrasse), pulls each sub-pill toward the merged centroid, and stretches sub-pills toward each other along their line direction in order to shorten the connector — a trade-off explicitly disallowed by the rule "neither the angle nor the length of a pill changes to make the connector shorter."

### Stage 2 — Shift toward neighbouring sub-clusters (translation only)

Each sub-pill may then be translated along its mean tangent toward an adjacent sub-cluster, but only by a uniform amount that does not push any of its dots outside their allowed ranges. Because every dot in a sub-pill translates by the same arc length along its own polyline, the perpendicular extent (the pill's length) and orientation (its angle) are preserved: shape is rigid, position is free. The shift is bounded by `min(free range)` across the dots — if any dot is already at the range end on the side it would need to move toward, the shift collapses to zero and the sub-pill stays where stage 1 left it. This is correct: that sub-pill is already as close to its neighbour as it can be without lengthening.

### Connectors

The NN-path-through-all-dots and the split-on-largest-gap mechanism are unchanged. After the two stages above, adjacent sub-pills' inner dots are as close as their ranges allow; the inter-sub-cluster gap remains the largest segment in the NN path and the pill splits there into two pills plus a connector. The connector is the natural geographic gap between sub-pills.

## Status

The two-stage algorithm above plus the debug overlays are implemented in `07_extract_stops.py` (`coordinate_dots_in_cluster`, `shift_sub_pills_toward_target`, `_spatial_subclusters`, `write_debug_platforms`, `write_debug_stops`). Observed behaviour at the stations checked so far:

- **Eigerplatz**: two clean perpendicular sub-pills (Nord, Süd) with a connector between them. Stage 2 shift collapses to zero — both sub-clusters' dots are already at their range ends.
- **Zürich main station**: the approach does not yet produce the desired single-bar look across all rail platforms. Sub-clusters at different parts of the station end up with axes at different tangent-coords even with the intersection-center preference, and the platforms span more than the spatial sub-cluster radius. A different approach to global alignment across the merged cluster will likely be needed.
