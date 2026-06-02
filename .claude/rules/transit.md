# Transit Pipeline

## Pipeline scripts

Numbered 1–8 in `scripts/transit/`; the rebuild script runs them in order.

- `01_download_gtfs.py` — fetches the SBB GTFS feed → `data/gtfs/`.
- `02_download_osm.py` — fetches CH + LI + DE + FR + IT + AT country PBFs → `data/osm/`. Neighbour PBFs are required because the bbox extends past CH soil (Domodossola, Konstanz, Annemasse, Lörrach, Bregenz). One-off download ≈ 12 GB.
- `03_bbox_osm.py` — cuts each country PBF to the bbox in `scripts/transit/config.yaml:osm_bbox`, then merges the slices → `data/osm/ch_pfaedle.osm.pbf`. Cut-then-merge avoids a >10 GB intermediate file.
- `04_preprocess_gtfs.py` — drops excluded-agency trips and foreign-terminus trips, repairs `arr > dep` rows → `data/gtfs_filtered/`.
- `05_run_pfaedle.py` — runs pfaedle in Docker (`carfree-pfaedle:latest`, built from `scripts/transit/pfaedle/Dockerfile`) → `data/gtfs_routed/` with `shapes.txt`.
- `06_score_and_match.py` — loads `data/gtfs_routed/`, runs trip-grouping inside `stream_stop_times`, dedupes per `(line_key, agency_id, trip_group_id)` and merged-stop variant, emits `data/transit/transit_lines.geojson` + `line_stops.json`.
- `07_extract_stops.py` — builds stop dots, pills and connectors → `transit_stops.geojson`, `transit_stop_pills.geojson`.
- `08_build_pmtiles.sh` — tippecanoe → `static/tl_*.pmtiles`.

`scripts/generate_style.py` is not numbered — it lives outside `scripts/transit/` because it generates the whole MapLibre style, not transit-only — and runs as a fixed step inside step 7 of the rebuild flow (between extract-stops and pmtiles).

Rebuild: `./scripts/rebuild_transit.sh [--start N]`. Default is `--start 3` (bbox cut and everything after). Each step's output is the next step's input, so middle-skipping is not supported; `--start N` always runs steps N..8 contiguously.

## Diagnostic outputs (in `data/transit/`)

| File | Contents |
|------|----------|
| `gtfs_filtered.json` | Routes affected by 04b's filter, with by-agency / foreign-terminus drop counts. |
| `gtfs_unmatched.json` | GTFS lines with non-zero service that produced no emitted feature. |
| `trip_groups.json` | One row per emitted trip group: short_name, long_name, bucket, agency_id, trip_group_id, trip_count, variant_count. |
| `pfaedle_unrouted.json` | Trips that pfaedle could not shape (rep trip's `shape_id` missing from `shapes.txt`). Aerial GTFS `route_type` 5/6 (cable car / gondola) trips that fail pfaedle do NOT land here — they are emitted with a straight-line fallback instead and tagged `geometry_source: "straight_line_fallback"`. |
| `line_stops.json` | Per emitted feature: ordered list of `[lon, lat, stop_id]`. |
| `gtfs_groups_full.json` | **Comprehensive lookup.** One entry per `(line_key, agency_id, trip_group_id)` including non-drawable ones, with `drawable`, `freq_score`, and a `group_exclusion_reason` (`low_frequency`, etc.). Each group lists every merged-stop variant including dropped ones, with `kept_by_variant_filter`, `exclusion_reason` (`rare_variant`, `pfaedle_unrouted`, `polyline_too_short`, or null when emitted), `first_terminus` / `last_terminus`, and for emitted variants the `feature_id`, `shape_id`, `n_coords`, `line_km`, `rep_trip_id`, and `geometry_source` (`pfaedle` or `straight_line_fallback`). Read this instead of re-running `stream_stop_times` to debug missing lines. |

## Pipeline config (`scripts/transit/config.yaml`)

- `osm_bbox` — bbox for the pfaedle PBF.
- `excluded_agencies` — token list, case-insensitive substring match on `agency_name`. Drops the agencies' trips entirely at 04b. Used to suppress long-distance coaches (Flixbus, BlaBlaCar, etc.).
- `freq_sampling.weekday_dates` / `freq_sampling.weekend_dates` — explicit sample dates the low-frequency filter measures against. The generator that produced them is documented in `.claude/concepts/implemented/multi-date-frequency-sampling.md`; rules + seed live in that generator (deleted after first run; re-create it from the concept doc when the feed period changes).
- `pfaedle.image` / `pfaedle.modes` — image tag and routed modes.
- `debug.disable_snap_gate` — when true, step 07 (`07_extract_stops.py`) keeps stops whose snap distance to the line exceeds the (rail 300 m / non-rail 150 m) threshold. Currently true so misplaced stops stay visible for diagnosis.

## Identity model

`stream_stop_times` partitions trips by `(long_name_norm or short_name, agency_id, bucket)`, runs union-find on merged stop sets within each partition (≥2 shared merged stops → same group), and assigns a `trip_group_id` unique within its partition. **`trip_group_id` is unique only within a partition**, so the emission key in `main()` is `(line_key, agency_id, trip_group_id)`. Forgetting `agency_id` here causes same-numbered lines in different cities to collide (e.g. Bernmobil bus 10 vs Stadtbus Winterthur bus 10).

`_trip_group_export` (`trip_id → (line_key, tg_id, agency_id)`), `_trip_stops_export` (`trip_id → [stop_id, ...]`), and `_trip_merged_export` (`trip_id → frozenset(merged stop ids)`) expose the per-trip info the emission loop needs. They are populated as side effects of `stream_stop_times` and read by `main()`. They are the only export surface; there is no separate `_line_canonical_export` or `CanonEntry` table — the legacy short/long-name dual-index that used to live alongside them was removed once the pre-pfaedle OSM matcher (which was its only consumer) went away.

## Service area filter

`is_in_service_area(stop_id)` filters by UIC prefix 85 (Swiss + Liechtenstein) with hardcoded `_SERVICE_AREA_EXCLUDE` / `_SERVICE_AREA_INCLUDE` overrides at the border. Kept in 05 but no longer called by the active main pipeline; available for stop-side filters if needed later.

## Mountain and ferry

Every variant goes through pfaedle, including mountain (route_type 5/6/7) and ferry (route_type 4). When pfaedle produces a shape, it is used; mountain features keep their visual style (light yellow, fixed width). Straight-line fallback is reserved for aerial GTFS `route_type` 5 (cable car) and 6 (gondola) — these emit a straight line between consecutive stops when pfaedle has no shape, tagged `geometry_source: "straight_line_fallback"`. Funiculars (route_type 7), ferries, and every other mode that pfaedle fails to shape are dropped and logged to `pfaedle_unrouted.json`, same as rail/bus.

Mountain classification comes solely from GTFS route_type; rack railways with route_type=2 stay in the `train` bucket. `deduplicate_mountain()` collapses overlapping **aerial** features (route_type 5/6) that share a ref — funiculars (7) and other modes are not collapsed because a shared bbox typically indicates two different branches off a common stem rather than a duplicate.
