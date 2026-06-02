# Pfaedle Line Geometry

## Problem

Line geometry is currently derived by matching GTFS lines to OSM transit route relations with a custom multi-loop matcher and sanity check stack. The approach is structurally fragile, Switzerland-specific in non-trivial ways (operator tagging conventions, ref formats, generic-prefix collisions), and absorbs disproportionate engineering effort. Pfaedle is the standard tool for the same job — routing GTFS trips over an OSM network to produce GTFS shapes — and resolves these problems by construction.

## Requirements

The pipeline derives line geometry by invoking pfaedle as an external step between GTFS download and stop extraction. Pfaedle consumes an OSM PBF and the GTFS feed; it produces an augmented GTFS folder with `shapes.txt` populated and `shape_dist_traveled` populated on `stop_times.txt`.

### Trip → line aggregation

Pfaedle outputs one shape per GTFS trip. The pipeline aggregates these into per-line features using `trip_group_id` from the gtfs-line-grouping concept as the identity key.

Within each trip group, distinct shapes correspond to different directions (when `dir_aware=True` in the group), Y-branches, short-turns, express/local variants, or minor trip-to-trip routing noise.

The emission rule for this concept is **one feature per distinct shape within a trip group**. Distinct-shape detection uses a geometric similarity rule: two shapes are treated as identical if their endpoint coordinates and a sample of intermediate points agree within a small tolerance. Minor pfaedle output noise on the same physical path must not produce spurious duplicate features.

Each emitted feature carries: `trip_group_id`, `mode`, `width_base`, `color`, `freq_score`, `speed`, `direction_id`, and the set of contributing route_ids.

### Mode classification

Mode is derived entirely from GTFS:
- Standard mapping from `route_type` (0=tram, 1=metro, 2=train, 3=bus, 4=ferry, 5/6/7=mountain).
- Bus subclassification (bus vs regional_bus) by trip distance heuristic, as today.
- Mountain rack railways with `route_type=2` override to the mountain bucket via a new **`mountain_rack_agencies`** denylist of GTFS agency_ids (Jungfraubahn, WAB, BVB, etc., as enumerated in the current `MOUNTAIN_RAIL_OPERATORS` set, ported by agency identity).

The cross-validation guard for the FUN/BOB ref collision is no longer needed and is removed: with pfaedle there is no OSM ref space to collide with.

### Excluded operators

Long-distance bus exclusion uses a new **`excluded_agencies`** denylist of GTFS agency_ids. Operators currently in the OSM `EXCLUDED_OPERATORS` set (Flixbus, BlaBlaCar Bus, OuiBus, etc.) are ported by mapping each operator name to its `agency_id` in `agency.txt`.

### OSM scope

Pfaedle receives a **bbox-complete** OSM PBF: a rectangle around Switzerland with a 1–2 km margin past CH's outermost tips (Ticino in the south, the Bodensee shore and Schaffhausen in the north, Geneva in the west, Engadin in the east). Inside that rectangle, OSM coverage is complete — including the foreign sliver the rectangle catches (Domodossola, Konstanz, Annemasse, Lörrach, Singen, Bregenz, and so on). The PBF therefore cannot be assembled from country extracts of Switzerland and Liechtenstein alone; OSM data from each neighbouring country within the bbox must be present.

Pfaedle may route through that foreign sliver. Cross-border services whose stops all sit inside the bbox are routed normally. Trips with any stop outside the bbox are dropped (see trip filtering). The 1–2 km margin defines the bbox itself, not a separate routable buffer — the bbox, not the margin, bounds where pfaedle may route.

### Trip filtering pre-pfaedle

The following filters apply to the GTFS feed before pfaedle runs, so pfaedle does no work on trips that will not be drawn:
- Foreign-terminus trips: any trip with a stop outside the OSM bbox is dropped.
- Trips of excluded agencies (above).
- Low-frequency lines: per the existing `MIN_FREQ_SCORE` rule.
- Night-line exclusion: per the night-line-exclusion concept.

### Mountain and ferry fallback

Pfaedle cannot route gondolas, funiculars, aerial tramways, or ferries: no continuous routable OSM network exists for them. These modes bypass pfaedle and use straight-line geometry between consecutive GTFS stops, as today.

### Stop assignment

Stop assignment ceases to be a matching problem. Each emitted feature's stops come directly from the contributing trips' `stop_times` sequences. The existing OSM-relation matching machinery is no longer used.

### Pfaedle invocation

Pfaedle runs as a **containerized step**. A Docker image with a pinned pfaedle version is built from a Dockerfile committed to the repo. The pipeline invokes the image with OSM and GTFS mounted as volumes; the augmented GTFS folder is the output. This guarantees identical behaviour on developer laptops and a Linux server deployment.

### Diagnostics

Replacement diagnostic outputs:
- **`pfaedle_unrouted.json`** — trips pfaedle could not route, with reason where available.
- **`gtfs_filtered.json`** — trips dropped at preprocessing (excluded agency, foreign terminus, low frequency, night line).
- **`trip_groups.json`** — trip group composition for inspection.

The current matching-pipeline diagnostics (`sanity_excluded.json`, `main_loop_dropped.json`, `gtfs_unmatched.json`) become irrelevant and are removed.

## Constraints

- The gtfs-line-grouping concept is a hard prerequisite. Pfaedle dedup keys on `trip_group_id`.
- The prm-platform-positions concept is independent and may land before or after this concept. Pre-PRM stops retain their current per-stop accuracy (station centroid + snap to pfaedle shape).
- The pair-centric-transit-model concept is a downstream future step, not part of this concept. Base output is one feature per distinct shape per trip group, with per-line identity.
- Foreign-terminus trips are dropped entirely, not partially rendered. Direct trains to Berlin or Milano and similar long-distance cross-border services are not represented. The Swiss-side portion of such trips, partially rendered today, is also not represented. This is a deliberate regression.
- The OSM PBF remains in the pipeline as pfaedle's routing graph. It is no longer a feature source.
- Visual style is unchanged. Color, width, frequency, and speed encoding stay identical; only the geometry source changes. Per-mode visual rules (mountain yellow, train red, etc.) continue to apply.
- The OSM-relation feature pipeline (route relation extraction, OSM stop node table, the multi-loop matcher, sanity check stack, geo fallback, OSM-stop-name override, snap-distance gates) becomes obsolete and is removed once this concept lands.
- The Switzerland-specific GTFS-side preprocessing already in place (TER prefix filter, Forchbahn S18 ref/bucket remap) continues to apply unchanged.
- Pfaedle's HMM routing is deterministic. Re-runs on the same input produce identical shapes; this matters for reproducible builds.
- Pfaedle output quality depends on OSM coverage of the relevant network. For Swiss rail, road, and tram tracks, coverage is excellent. Modes that depend on poorly-tagged OSM infrastructure may need case-by-case attention.
