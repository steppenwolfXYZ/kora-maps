# Transit Pipeline

## Pipeline scripts
- `05_score_and_match.py` — OSM→GTFS matching, stop assignment, outputs `line_stops.json`
- `07_extract_stops.py` — builds pill/connector GeoJSON from `line_stops.json`
- Rebuild command: `./scripts/rebuild_transit.sh --skip-osm`

---

## OSM→GTFS Matching Architecture

### Key data structure
`_line_canonical_export` keyed by `(short_name_or_long_norm, bucket)` → list of `(line_key, [(stop_id, arr, dep), ...], direction_aware)` tuples. Multiple entries per key exist when: (a) the same line_key spans different geo_buckets (e.g. S6 Bern vs S6 Zürich), (b) the same line_key+geo_bucket has multiple distinct stop sets (e.g. Maienfeld Bus 14 with 5 stops alongside Feldkirch Bus 14 with 30 stops), or (c) the frequency-weighted canonical differs from the longest-trip canonical.

### Primary matching: `find_best_gtfs_candidate()`
For freq/speed selection ONLY — does NOT drive stop assignment.
1. Builds ref variants (exact, normalised, upper/lower, name-prefix, alpha-prefix)
2. Scores each by precision: `n_inside / n_total` (stops inside OSM bbox, margin=0.05°)
3. Returns highest-scoring candidate — geo is a tiebreaker, not a gate
4. Returns `None` only if no candidates exist at all

### Zero-freq fallback
When the geo match returns a line_key with zero total freq, `gtfs` is NOT set from it — falls through to `gtfs_index` cascade to prevent routes disappearing due to a wrong zero-service match.

### Stop assignment
Scans all ref variants in `_line_canonical_export` and iterates every candidate entry per variant (there can be multiple stop sets per geo_bucket since the fix for Bus 14). Keeps whichever candidate yields the most stops inside the OSM route bbox. Then geo fallback if endpoint coverage fails.

**Critical:** do NOT feed `find_best_gtfs_candidate`'s canonical stops into stop assignment. Geo-cell-bounded candidates (~40km) cause `_covers_endpoints` to fail more often, triggering the broad geo fallback which pulls in wrong stops. One session: 2 fixes, ~50 regressions.

---

## Geo Sanity Check

Applied in two situations in `05_score_and_match.py` — function `_passes_geo_sanity()`.

### Trigger 1 — name fallback used
If the canonical lookup matched via `matched_gtfs_ref` (not the exact OSM ref), the result is sanity-checked. Tracked via `used_name_fallback = True`. If it fails, `best_coords` is cleared and geo-fallback runs.

### Trigger 2 — geo-fallback
When canonical stops are empty or fail endpoint coverage, all GTFS lines in the bucket are scored and each candidate is run through `_passes_geo_sanity`. If nothing passes, `best_coords` is cleared — the line has no stops assigned (appears as EXCLUDED in `check_geo_sanity_rejects.py`). The line is still drawn as a colored line; the sanity check controls stop assignment, not line drawing.

### What the sanity check affects
- It selects which geo-fallback candidate's stops are assigned to the line
- If no candidate passes, the line has no stops (EXCLUDED)
- A line can also be in KEPT with wrong stops if a bad candidate passes the check — this is a false positive, not a rejection

### The three checks (cheapest first, returns True on first pass)

**Check 1 — Terminal name match (string only)**
Counts stops whose normalised name contains the normalised OSM `from` or `to` tag. Requires at least **1/3 of the candidate's stops** to match, with a minimum of **2 stops** (so small lines must have both terminals present). Minimum terminal name length: ≥ 4 chars.

This prevents a long line from passing just because one of its stops happens to share a terminal name — e.g. a 15-stop east-shore line with one stop at Stadelhofen should not pass for a route `from=Zürich Stadelhofen`.

**Check 2 — Endpoint coverage**
At least one stop within 5 km of the OSM geometry start AND one within 5 km of the end.

**Check 3 — Sampled proximity**
5 evenly-sampled stops from the candidate; majority (≥ 50%) must be within 500 m of the nearest OSM polyline vertex.

### Known remaining issues with the sanity check
- `_norm_stop_name` strips `hb`/`hbf`/`bahnhof` — short generic city tokens can still pass Check 1 (e.g. `bern` from `Bern HB`)
- `_min_dist_to_polyline_km` measures distance to nearest vertex, not segment — sparse-vertex lines may falsely fail Check 3
- Exact OSM ref matches (where `used_name_fallback=False`) with good endpoint coverage bypass all sanity checks
- `merge_clusters_by_parent_station` in `07_extract_stops.py` can pull in far-away points via shared parent_station — downstream issue, independent of the sanity check

---

## Mountain Pipeline

### Core principle
GTFS is the authority for mountain lines (type 5/6/7: funicular, gondola, aerial tramway). Every such line gets drawn. OSM geometry is used when available; otherwise a straight line connects GTFS stop coordinates.

### Data flow
1. `osm_mountain_by_ref` built before main loop — OSM routes tagged funicular/cable_car/gondola/aerialway, plus train-tagged rack railways whose ref is in the mountain GTFS bucket
2. Main OSM loop: `mode == "mountain"` routes skipped; rack railways (tracked in `osm_train_refs_in_mountain_gtfs`) skipped
3. GTFS-first mountain loop: iterates `_line_canonical_export` for bucket="mountain", deduplicates direction variants by bbox overlap (0.5 threshold)

### WAB/JB/BOB
WAB and JB are GTFS type=2 (train bucket), short_name="CC". Processed in main OSM loop, overridden to mountain via `MOUNTAIN_PLACE_KEYWORDS`. BOB is type=2 train, short_name="R", drawn as regular train mode.

FUN 311 (Stanserhornbahn) and FUN 312 (VerticAlp) share refs "311"/"312" with BOB/WAB/JB — `osm_train_refs_in_mountain_gtfs` includes a geographic guard to prevent false flagging.

---

## Open Tasks

### Geo sanity filter — excluded lines investigation
Diagnostic script: `scripts/transit/check_geo_sanity_rejects.py`
Run: `python3 scripts/transit/check_geo_sanity_rejects.py [--mode train|bus|...]`

#### Completed / correctly excluded
- **Bus 37 (Mésange/Delle Gare)** — correctly excluded, phantom French border line (both directions)
- **Bus 14 (Maienfeld→Balzers)** — fixed by multi-canonical storage (see above)
- **Bus 625 (Mauborget→Couvet)** — no longer excluded after fixes; resolved
- **S18 (Zürich Stadelhofen→Esslingen)** — no longer excluded after fixes; resolved
- **Regional Bus 108/124 (Flixbus) and Bus 73 (Ouibus/BlaBlaCar Bus)** — now excluded upstream in `osm_to_mode()` by network tag. `EXCLUDED_OPERATORS` extended to include `"blablacar bus"` and `"ouibus"`; `osm_to_mode()` now checks both `operator` and `network` tags (Flixbus subcontractors use their own company name in `operator` but `network="Flixbus"`).
- **Bus 76 (La Plaine → Viry) and Bus X33 (Bellegarde → Ferney)** — correctly excluded by sanity filter; Geneva cross-border lines with no Swiss GTFS coverage. No fix needed.

#### Still excluded — legitimate lines needing a fix
- **Train PE (Glacier Express St. Moritz↔Zermatt)** — legitimate famous tourist train, should be shown. Both directions excluded. Best geo candidate is a wrong 5-stop route near Zermatt (start is 143 km off, only 1/5 stops within 500 m). Root cause: GTFS likely doesn't use "PE" as the short_name for this service, so canonical lookup fails entirely and the geo fallback has no valid candidate.
- **Train RE42 (Zermatt→Fiesch)** — legitimate MGB regional line, should be shown. Identical 5-stop wrong candidate as PE (distances `[0.04, 0.54, 1.40, 3.41, 9.40 km]`), confirming both hit the same spurious Zermatt-area match. Root cause likely the same — GTFS uses a different ref than "RE42".
