# Transit Pipeline

## Pipeline scripts
- `05_score_and_match.py` — OSM→GTFS matching, stop assignment, outputs `line_stops.json`
- `07_extract_stops.py` — builds pill/connector GeoJSON from `line_stops.json`
- Rebuild command: `./scripts/rebuild_transit.sh --skip-osm`

## Diagnostic output files (written by `05_score_and_match.py` on every run)

| File | Contents |
|------|----------|
| `data/transit/sanity_excluded.json` | OSM routes excluded by the geo sanity check (no valid GTFS candidate passed). Array of objects with `osm_id`, `ref`, `name`, `mode`, and check results. |
| `data/transit/main_loop_dropped.json` | OSM routes that were processed but not drawn. Each entry: `osm_id`, `ref`, `name`, `mode`, `operator`, `matched_line_key` (geo-matched GTFS tuple or null), `freq_score` (null or 0.0), `reason` (`"no_gtfs"` / `"zero_freq"` / `"dedup"`). Dedup entries also have `gtfs_ref`. |
| `data/transit/gtfs_unmatched.json` | GTFS line_keys with non-zero service that were never matched to any drawn OSM feature. Each entry: `short_name`, `long_name`, `bucket`, `freq_score`, `total_trips`. Sorted by `(bucket, short_name, long_name)`. Useful for finding GTFS lines that have no OSM counterpart. |

Use these files to diagnose missing lines without adding one-off debug prints:
- Line absent from map AND absent from `sanity_excluded.json` AND absent from `main_loop_dropped.json` → dropped before the main loop (mountain/TER skip, `osm_to_mode` returns None, etc.)
- Line in `main_loop_dropped.json` with `reason="no_gtfs"` → no GTFS match found at all
- Line in `main_loop_dropped.json` with `reason="zero_freq"` → GTFS match found but service score is 0.0
- GTFS entry present but no OSM route drawn → check `gtfs_unmatched.json` for the line_key

## Transit pipeline config
`scripts/transit/config.yaml` — pipeline-specific settings (separate from the map style `scripts/config.yaml`).

Key: `debug.disable_snap_gate` (bool, default `false`) — when `true`, disables the snap-distance gates that suppress stops too far from their OSM line geometry (rail: 300 m, non-rail: 150 m). **Currently set to `true` intentionally.** The gate is disabled so that misassigned stops (long connectors) are visible on the map and can be diagnosed and fixed at the root (in `05_score_and_match.py`), rather than silently hidden. Do not re-enable it until all known long-connector bugs are fixed in the assignment logic.

---

## OSM→GTFS Matching Architecture

### Key data structure
`_line_canonical_export` keyed by `(short_name_or_long_norm, bucket)` → list of `(line_key, [(stop_id, arr, dep), ...], direction_aware)` tuples. Multiple entries per key exist when: (a) the same line_key spans different geo_buckets (e.g. S6 Bern vs S6 Zürich), or (b) the same line_key+geo_bucket has multiple distinct stop sets (e.g. Maienfeld Bus 14 with 5 stops alongside Feldkirch Bus 14 with 30 stops).

### Low-service filter on `_line_canonical_export`
Lines that would not be drawn (i.e. `compute_freq_score == 0.0`) are excluded from both source dicts (`line_canonical_geo_stops`, `line_variant_counts`) before sections 1 and 2 build the export, right after the 10% variant filter. This prevents zero/near-zero-service lines (e.g. EXT Extrazug) from contaminating the geo-fallback pool.

Filter uses `compute_freq_score(freq, mode_approx)` where `mode_approx` is derived from the GTFS bucket. The "bus" bucket is approximated as `regional_bus` (lower maluses) rather than `bus` — this is intentionally conservative: a city bus with very sparse service might survive the filter here even though it would be dropped at draw time. Mountain bucket is exempt entirely.

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

**Endpoint coverage gate:** `_count_endpoints_covered(osm_pts, best_coords, ENDPOINT_THRESHOLD_KM, osm_stop_nodes)` returns 0/1/2 — how many OSM terminal stations have a GTFS stop within 5 km. Uses **`osm_stop_nodes[0]` and `osm_stop_nodes[-1]`** as the comparison points (falling back to `osm_pts[0]`/`osm_pts[-1]` when stop_nodes is empty). Raw geometry endpoints must not be used — they may extend into tunnels or across borders well past the last passenger stop (e.g. RE1 Bern→Brig: OSM geometry starts inside the Simplon tunnel 10 km past Brig, but `osm_stop_nodes[0]` = Brig correctly). With 0 endpoints covered: geo fallback fires. With 1: geo sanity check runs; fails → geo fallback. With 2: no fallback.

**Switzerland geographic filter (`is_in_switzerland`):** Both GTFS `ccoords` and the inline density candidate list are filtered to stops within Switzerland before use — `is_in_switzerland(lon, lat)` checks a loaded Switzerland border polygon (shapely `Point.within(polygon)`). This prevents Italian/German/French terminal stops from skewing density and endpoint coverage. The polygon is loaded from a Switzerland GeoJSON at pipeline startup. A UIC prefix-`85` filter is insufficient: Iselle di Trasquera (8501952), Varzo (8501951), and Preglia (8501950) are geographically in Italy but carry Swiss UIC IDs because SBB operates trains there.

**Critical:** do NOT feed `find_best_gtfs_candidate`'s canonical stops into stop assignment. Geo-cell-bounded candidates (~40km) cause `_covers_endpoints` to fail more often, triggering the broad geo fallback which pulls in wrong stops. One session: 2 fixes, ~50 regressions.

### Group-level stop assignment (implemented)

**Problem it solved:** A GTFS line often has multiple trip variants. When variants are subsets of each other, all stops collapse into a single union candidate (`dir_aware=False`). Multiple OSM relations for the same line (different directions, short-turns, branches) all competed against this union, with the winning candidate's stops filtered by the OSM relation's sub-bbox — a geographic proxy that cannot distinguish "stop belongs to this OSM variant" from "stop happens to be geographically close." Classic symptom: Glattbrugg (S3-Flughafen branch stop) leaking into S3-Hardbrücke with a 4.5 km connector.

**Implementation — `_group_reassign_stops()` (runs after dedup, before JSON write):**
1. Group all surviving `line_stops_out` entries by `(gtfs_ref, bucket)`.
2. Skip groups with fewer than 2 OSM relations.
3. For each group, pool all canonical GTFS stops from `_line_canonical_export` for that ref.
4. For each stop, filter to relations whose sub-bboxes contain it (coarse geographic gate, margin=0.02°).
5. Compute **full vertex scan** distance (no early exit) from the stop to each nearby relation's polyline.
6. Assign the stop to all relations within `max(d_min + 0.05 km, d_min * 1.1)` of the closest relation — shared stations (both routes within metres) appear on all; branch-specific stops (one route much closer) are pinned to the right branch.
7. Use a dict keyed by `stop_id` to deduplicate stops that appear in multiple canonical entries.

**Critical implementation note — full vertex scan:**
`_min_dist_to_polyline_km` has an early exit at 100 m: it breaks as soon as it finds *any* vertex within 100 m, which may not be the closest vertex. In the group pass this produces wrong d_min values (e.g. a 75 m vertex found before the true 11 m vertex → d_min inflates to 75 m → threshold inflates to 125 m → wrong stops pass). The group pass therefore uses `min(haversine_km(...) for p in geom)` — a full scan with no early exit. Do NOT replace this with `_min_dist_to_polyline_km`.

**Direction filter in `_lookup_canonical_stops`:**
`dir_aware=True` means the two directions of a line have genuinely different stop sets (e.g. Bus 17 Bern: westbound on Effingerstrasse, eastbound on Schwarztorstrasse). The direction filter in `_lookup_canonical_stops` (skips candidates whose first stop is >2× closer to the OSM end than start) is **redundant for multi-relation groups** — the group pass overwrites the per-relation assignment anyway. It is still **load-bearing for single-relation lines** with `dir_aware=True`: without it, the wrong-direction canonical could win and there is no group pass to correct it. Leave it in place.

**What does NOT change:**
- `_lookup_canonical_stops` — still runs per-relation as the initial assignment; group pass overwrites it for multi-relation groups
- `07_extract_stops.py` — connectors snap to the assigned relation's geometry; no changes needed there
- Single-relation groups — group pass skips them (`len(osm_ids) < 2`); direction filter protects them

---

## Post-matching Deduplication

After all OSM→GTFS matching is complete, `05_score_and_match.py` runs a dedup pass over `line_stops_out` before writing `line_stops.json` and `transit_lines.geojson`.

**Rule:** For each `gtfs_ref`, if any OSM line matched it with a **direct ref** (OSM `ref` ≈ `gtfs_ref` after stripping spaces and lowercasing), all **fallback-matched** entries for the same `gtfs_ref` are removed from both outputs. This prevents renamed/legacy OSM routes from appearing alongside their correctly-ref'd successors.

**`gtfs_ref` key:** When a match is made via the geo-fallback and the winning GTFS line has a long_name that adds information beyond the short_name (e.g. `short_name="R"`, `long_name="R 4"`), `gtfs_ref` is set to `long_norm` (`"R4"`) rather than the short_name (`"R"`). This scopes the dedup to the specific line rather than the generic prefix, preventing unrelated lines that share a short_name (e.g. `"R"` used by RhB, SBB, and a French tourist train) from interfering with each other. When long_name adds no information (e.g. `short_name="S8"`, `long_name="S 8"`), the short_name is used as-is.

**Geo-cell scoping:** Each `line_stops_out` entry stores a `_dedup_cell` — the 0.5° grid cell of the matched GTFS stops' centroid (same grid as `GEO_BUCKET_DEG`). Dedup groups by `(gtfs_ref, _dedup_cell)` rather than `gtfs_ref` alone. This prevents unrelated lines that share a name string but serve different geographic regions (e.g. SBB R2 Lausanne–Bex vs RhB R2 Landquart–Davos) from colliding in the same dedup group and incorrectly removing each other.

Implementation: `_refs_match(osm_ref, gtfs_ref)` helper + `dedup_removed` set. Each `line_stops_out` entry stores `osm_ref` (the raw OSM `ref` tag) alongside `gtfs_ref` and `_dedup_cell` to enable the comparison. Console output: `Dedup-removed: N lines superseded by direct-ref match`.

Do not tighten this logic to remove fallback matches unconditionally — they are still valid and needed when no direct-ref match exists for the same `gtfs_ref`.

---

## Geo Sanity Check

Applied in two situations in `05_score_and_match.py` — function `_passes_geo_sanity()`.

### Trigger 1 — name fallback used
If the canonical lookup matched via `matched_gtfs_ref` (not the exact OSM ref), the result is sanity-checked. Tracked via `used_name_fallback = True`. If it fails, `best_coords` is cleared and geo-fallback runs.

### Trigger 2 — geo-fallback
When canonical stops are empty or fail endpoint coverage, all GTFS lines in the bucket are scored as candidates. Scoring: `score = n_stops_in_bbox / n_total_stops`. Candidates with `score < 0.5` are discarded. Remaining candidates are sorted by `(-score, -endpoint_coverage, -len(ccoords))` — highest bbox overlap first, then endpoint coverage (0/1/2 endpoints within 500 m), then absolute stop count. The top 50 candidates are run through `_passes_geo_sanity` in order; the first that passes is used. If nothing passes, `best_coords` is cleared — the line has no stops assigned (appears as EXCLUDED in `check_geo_sanity_rejects.py`). The line is still drawn as a colored line; the sanity check controls stop assignment, not line drawing.

### What the sanity check affects
- It selects which geo-fallback candidate's stops are assigned to the line
- If no candidate passes, the line has no stops AND is removed from the drawn output entirely (EXCLUDED)
- A line can also be in KEPT with wrong stops if a bad candidate passes the check — this is a false positive, not a rejection
- **Rule:** Lines excluded by the geo sanity check (no valid candidate found) must NOT be drawn. Do not draw lines without valid GTFS-backed stops.

### The three checks (cheapest first, returns True on first pass)

**Check 1 — OSM stop name match against GTFS candidate stops**

Input sources:
- OSM side: `stop_nodes` — actual stop member nodes of the OSM route relation, each with `[lon, lat, name]`. Names are extracted from the node's OSM `name` tag by `04_extract_osm.py`.
- GTFS side: the candidate's `ccoords` list (`[lon, lat, stop_id]`), with names looked up via `stop_meta`.

Algorithm:
1. Build a set of normalised GTFS stop names from the candidate's `ccoords` + `stop_meta`.
2. For each OSM stop node, normalise its name. Skip if < 2 chars.
3. Count OSM stops whose normalised name is present in the GTFS name set (whole-token equality, not substring).
4. Pass if `matches >= max(2, round(len(osm_stop_nodes) * 0.9))` — requires ~90% of OSM stops to match.

The 90% threshold rejects partial-corridor matches: e.g. R2 (Landquart–Davos) shares 5/7 stops with RE4 (Landquart–Scuol), scoring 71% → fails. A correct full-route match typically scores 100%. If Check 1 fails due to name format differences, Checks 2/3 still run as fallback.

No outer gate — Check 1 always runs. Missing OSM stop names (< 2 chars after normalisation) are silently skipped; if too many are missing, the threshold is not met and Check 1 fails, which is correct.

**Check 2 — density gate + GTFS stops → OSM geometry proximity**

Density gate (runs first, cheap): if the OSM route has ≥ 2 stop nodes and `osm_line_km > 0`, compare stop densities (stops/km). Gate fails if `ratio = cand_density / osm_density` is outside `[0.5, 2.0]` — prevents a sparse intercity train from matching a dense regional/mountain route.

**Exception — `regional_bus`:** only the lower bound (`ratio >= 0.5`) is enforced; the upper bound is dropped (`skip_upper_density=True`). GTFS maps every village stop for regional buses, while OSM often only maps major interchange stops — a 5:1 ratio is normal (e.g. Julierpass Bus 182: 59 GTFS stops vs 12 OSM stop nodes). The upper bound would incorrectly reject the correct candidate.

Candidate density is computed inline from the **full unfiltered candidate stop list** (not bbox-filtered `ccoords`) — this is critical: using the bbox-filtered span instead inflated the in-corridor density of long-distance trains (e.g. IC6 Basel–Brig scored as dense as RE1 when only the shared Brig–Bern section was measured). Both the inline density candidate stops and `ccoords` are filtered to Switzerland-only stops via `is_in_switzerland()` (see below).

Proximity check (only runs if `density_ok`): sample 5 evenly-spaced GTFS stops from the candidate (always including first and last, using index formula `round(i*(n-1)/4)`). Find the distance from each to the nearest point on the OSM polyline (vertex-based). Require at least 4/5 to be within 100 m.

**Check 3 — OSM stops → GTFS stops proximity**
Sample 6 evenly-spaced OSM stop nodes (always including first and last, using index formula `round(i*(n-1)/5)`). For each, find the nearest GTFS stop in the candidate. Require at least 5/6 to be within 200 m. If the OSM route has fewer than 2 stop nodes, this check is skipped.

The 5/6 threshold and always-include-last sampling close a former gap: with 5 samples and step-based indexing (`[::step][:5]`), routes with < 10 OSM stops would only sample the first 5, missing endpoint stops that expose partial-corridor mismatches (e.g. Scuol-Tarasp on RE4 was never tested against R2).

Note: Check 2 is cheaper (polyline lookup) so it runs first. Check 3 uses OSM stop nodes — actual stop positions on the route — not geometry vertices. Both use distance thresholds generous enough for real stops (which sit within metres of their line).

### Known remaining issues with the sanity check
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

#### S18 (Forchbahn) — two-phase fix (pending)
S18 is actually a **tram** (Forchbahn, operated by FB). It currently appears as `mode=train` because OSM tags it `route=light_rail`. It has no GTFS match under "S18" so it gets a wrong alpha-prefix fallback to "S" (west-shore S-Bahn). New Check 2/3 (200 m threshold) should now reject the wrong west-shore S-Bahn candidate — verify with the diagnostic script. If it still appears, Phase 1 and 2 are still needed.

**Phase 1:** Make S18 vanish — stop drawing lines that only matched via alpha-prefix fallback (no real GTFS match). All 4 OSM relations (2727252, 2727409, 20153407, 20153408) should be hidden.

**Phase 2:** Revive S18 correctly — find its actual GTFS short_name (likely "FB" or similar under Forchbahn agency), map OSM `route=light_rail` with Forchbahn operator to `mode=tram`, and let it match properly.

#### Resolved
- **Train PE (Glacier Express St. Moritz↔Zermatt)** — now KEPT after geo fallback improvements (score ≥ 0.5 filter + better sort). No longer excluded.
- **Train RE42 (Zermatt→Fiesch)** — now KEPT after same geo fallback improvements. No longer excluded.

#### Long_name matching — prefer specific line identity over generic short_name

**Problem:** The `gtfs_index` cascade matches OSM `ref` against GTFS `short_name` only. When short_name is a generic prefix (`"R"`, `"RE"`, `"IC"`), the cascade never sets `matched_gtfs_ref` to a specific value — it either finds nothing or falls through to the alpha-prefix fallback (e.g. `"RE"`). `_lookup_canonical_stops` then has no specific key to use and the geo-fallback fires.

**Context:** GTFS long_names follow two patterns: (1) `short_name` + number suffix — e.g. `short_name="RE"` → `long_name="RE 4"` (~22k routes, useful for matching); (2) type prefix + `short_name` — e.g. `short_name="1"` → `long_name="B 1"` (~529k routes, not useful since OSM ref is `"1"` not `"B1"`). Pattern (1) is exactly where long_name matching helps. Pattern (2) naturally falls through since the long_norm (`"B1"`) won't match the OSM ref_norm (`"1"`).

**Fix:** In the `gtfs_index` cascade (stop assignment loop), try `gtfs_long_index` for `ref_norm` **before** falling through to the alpha-prefix `gtfs_index` lookup. This ensures `ref="RE 4"` → `ref_norm="RE4"` matches `gtfs_long_index[("train", "RE4")]` (→ `matched_gtfs_ref="RE4"`), so `_lookup_canonical_stops` finds the right canonical stops without needing the geo-fallback.

---

#### Still excluded / missing — legitimate lines needing a fix
- **Train RE1 (Bern↔Brig/Domodossola, BLS Lötschberg)** — deduped against IC6 (reason="dedup", gtfs_ref="IC6"). Root cause: `_count_endpoints_covered` uses raw geometry endpoints; OSM RE1 geometry starts inside the Simplon tunnel (~10 km past Brig), so ep_count=0 → geo fallback → IC6 partial trip wins → dedup removes RE1. Fix requires two changes: (1) use `osm_stop_nodes` as effective endpoints in `_count_endpoints_covered`; (2) implement `is_in_switzerland()` polygon check and filter GTFS ccoords + density to Swiss stops. Needs `shapely` dependency and a Switzerland border GeoJSON added to the project.
- **Regional Bus 171 (Chur→Bellinzona)** — PostBus express (EXB 171). Canonical lookup picks a Bellinzona-local B 171 (44 stops, higher count) over the full-corridor EXB 171 (25 stops). Endpoint check fails → geo fallback runs. Geo fallback also ranks Bellinzona-local variants first (more stops, same score=1.0). EXB 171 is buried past the cap of 50. Fix: use 3-level endpoint coverage `(0/1/2 endpoints covered)` as primary geo fallback sort key, so full-corridor candidates float above partial ones.
