# Pill Rendering

## Problem

The current stop-pill rendering uses uniform pill shapes derived from spatial clustering of stop dots, without regard to actual platform geometry. Real platforms vary from a 10 m bus bay to a 500 m mainline train platform, and run in directions that have nothing to do with the cluster's bounding box. The visual result is generic — pills do not communicate platform position, length, or orientation.

The atlas data wired in by `prm-platform-positions` (`stop_attributes_sources.json`) carries `length` for ~95% of rail platforms and `compass_direction` for tram/bus stops, keyed by GTFS `stop_id`. Combined with the per-line polyline geometry pfaedle already produces, this is enough to drive a more faithful per-platform pill design — at least for the common modes.

## Multi-zoom-level stop styling

The eventual map will use three distinct stop-style systems, chosen by zoom range:

- **Far zoom** — a single circle per station. Conveys presence and mode, not platform geometry.
- **Medium zoom** — a precise pill per platform, faithful to length and orientation but slightly simplified.
- **Short zoom** — a detailed style emphasising platform-level structure. Vision not yet defined.

This concept covers only the **medium-zoom pill**. Far and short are placeholders; their concepts will be written when their designs are decided.

## Requirements

### Per-mode interpretation of the GTFS stop coordinate

The GTFS coordinate's relationship to the physical platform differs by mode, and the pill construction follows that interpretation:

- **Rail (train, metro)** — the GTFS coordinate is the **centre** of the platform. The pill extends half of the atlas `length` in each direction along the polyline tangent at the stop.
- **Tram / bus / regional_bus** — the GTFS coordinate is the **front** of the stop in the direction of travel. The pill extends backwards along the polyline from the stop by the per-mode default length. Atlas does not carry `length` for these modes.

### Orientation source

Orientation is derived from the per-line polyline tangent at the stop coordinate, on every pill, every mode. The atlas `compass_direction` field is not consumed — the polyline already runs through the platform area, and its local tangent gives a more line-faithful orientation than an absolute geographic bearing.

### Per-mode default length

A configurable default length per non-rail mode is used wherever atlas does not provide `length` (always for tram/bus, occasionally for rail). The default values live in `config.yaml` so they can be tuned without code changes. Rail also gets a default for the rare unmatched cases.

### Atlas-data sanitisation

Atlas `length` outliers (0 m placeholders, kilometre-scale entries from ferry-route mislabels, etc.) are clipped or treated as missing before being consumed for pill geometry. The cutoff is a configurable sensible range per mode (rail roughly 30–600 m, others smaller); out-of-range values fall through to the default-length path.

### Fallback chain

For every stop:

1. If atlas provided a sane `length`, use it (per the mode-specific interpretation above).
2. Otherwise, use the per-mode default length.
3. If the polyline tangent at the stop cannot be computed (e.g. degenerate geometry), fall back to today's clustering-derived pill shape for that stop only.

### Clustering and grouping

Which pills exist — i.e. how stops are grouped into pills — is unchanged from the current pipeline. This concept changes only how each pill is shaped, not the set of pills.

## Constraints

- Far-zoom and short-zoom stop styles are out of scope. They will get their own concept docs when their visual designs are ready.
- `compass_direction` from atlas is intentionally not used. Polyline tangent is the orientation source on every pill.
- Per-mode default lengths and the atlas-length sanity ranges are configuration values; tuning them is not a code change.
- The concept depends on `prm-platform-positions` being implemented and emitting `stop_attributes_sources.json`.
- Ferries and mountain modes are out of scope for medium-zoom pill redesign; they render as today.
- The implementation must not regress the rendering of stops without atlas data — the fallback chain above guarantees a pill is always producible.
