# Transit Pipeline

## Pipeline scripts
- `05_score_and_match.py` — OSM→GTFS matching, stop assignment, outputs `line_stops.json`
- `07_extract_stops.py` — builds pill/connector GeoJSON from `line_stops.json`
- Rebuild command: `./scripts/rebuild_transit.sh --skip-osm`

## Diagnostic output files (written by `05_score_and_match.py` on every run)

| File | Contents |
|------|----------|
| `data/transit/sanity_excluded.json` | OSM routes excluded by the geo sanity check (no valid GTFS candidate passed). Array of objects with `osm_id`, `ref`, `name`, `mode`, and check results. |
| `data/transit/main_loop_dropped.json` | OSM routes that were processed but not drawn. Each entry: `osm_id`, `ref`, `name`, `mode`, `operator`, `matched_line_key` (settled GTFS tuple or null), `reason` (`"no_draw"` / `"dedup"`). `"no_draw"` entries also have `no_draw_reason` (`"low_frequency"`). Loop 4 exclusions (no passing candidate) have `matched_line_key: null` and are also recorded in `sanity_excluded.json`. |
| `data/transit/gtfs_unmatched.json` | GTFS line_keys with non-zero service that were never matched to any drawn OSM feature. Each entry: `short_name`, `long_name`, `bucket`, `freq_score`, `total_trips`. Sorted by `(bucket, short_name, long_name)`. Useful for finding GTFS lines that have no OSM counterpart. |

Use these files to diagnose missing lines without adding one-off debug prints:
- Line absent from map AND absent from `sanity_excluded.json` AND absent from `main_loop_dropped.json` → dropped in OSM preprocessing pass (non-transit tag, `osm_to_mode` returns None, TER prefix, mountain-tagged, etc.)
- Line in `main_loop_dropped.json` with `reason="no_draw"` and `no_draw_reason="low_frequency"` → 4-loop found a match but it is a low-frequency GTFS line (below `MIN_FREQ_SCORE` 0.075); `matched_line_key` shows which GTFS line was matched
- Line in `sanity_excluded.json` → Loop 4 found no passing candidate; not drawn
- GTFS entry present but no OSM route drawn → check `gtfs_unmatched.json` for the line_key

## Transit pipeline config
`scripts/transit/config.yaml` — pipeline-specific settings (separate from the map style `scripts/config.yaml`).

Key: `debug.disable_snap_gate` (bool, default `false`) — when `true`, disables the snap-distance gates that suppress stops too far from their OSM line geometry (rail: 300 m, non-rail: 150 m). **Currently set to `true` intentionally.** The gate is disabled so that misassigned stops (long connectors) are visible on the map and can be diagnosed and fixed at the root (in `05_score_and_match.py`), rather than silently hidden. Do not re-enable it until all known long-connector bugs are fixed in the assignment logic.

---

## Current behaviour: no_draw entries pre-filtered (TEMP)

**State:** Low-frequency GTFS lines (`no_draw="low_frequency"`) are **removed** from `_line_canonical_export` after `stream_stop_times` and before any matching loop runs. The flag-setting code in `stream_stop_times` still runs and still marks entries with `no_draw`, but a separate block in `main()` strips them from the candidate pool immediately afterwards.

**Why this exists:** The "flag-instead-of-remove" design (originally meant to keep low-freq lines visible to the matcher so their OSM relations would settle on them rather than fall through to Loop 4) caused regressions when a low-freq GTFS entry shared a physical route with a higher-freq sibling stored under a different `short_name`. Classic case: IR-LIX (13 sporadic seasonal trips) alongside PE-LIX (159 regular trips) for the Brünig line. OSM relation 2344785 (`ref="IR 470"`) settled on the IR-LIX no_draw candidate in Loop 3 and never saw PE-LIX. Pre-filtering removes IR-LIX from the pool so Loop 3 produces no candidate, Loop 4 (geo) runs, and PE-LIX matches.

**Where the filter lives:**
- `scripts/transit/05_score_and_match.py:main()` — block tagged `# TEMP: drop no_draw entries…`, immediately after `stream_stop_times()`.
- `scripts/transit/diagnose_transit_line.py` — mirror block tagged the same way, immediately after the script's `_m.stream_stop_times()` call. Required so the diagnostic replays the same candidate pool the real pipeline sees.

**To restore the flag-based behaviour:** delete both `TEMP` blocks. The flag-setting code in `stream_stop_times`, the two-pass logic in `_try_assign`, and the `no_draw` field on `CanonEntry` are all still present and functional — they're just dormant while the filter is in place. The rest of this document still describes the flag-based design as if it were active; treat sections below that mention `no_draw` candidates surviving into matching as the dormant code path, not the current behaviour.

**Consequences for diagnostics:**
- `main_loop_dropped.json` no longer receives `reason="no_draw"` entries (the matcher never settles on a `no_draw` candidate because none reach it). `reason="dedup"` still applies.
- `_try_assign`'s pass-2 (no_draw fallback) iterates an empty list and is effectively a no-op.
- The pre-filter removes whole keys from `_line_canonical_export` if all their entries were `no_draw` — Loop 3 candidate keys can now be empty where they previously held no_draw-only candidates, sending the route to Loop 4 as intended.

---

## OSM→GTFS Matching Architecture

### Key data structure
`_line_canonical_export` keyed by `(short_name_or_long_norm, bucket)` → list of `CanonEntry` namedtuples with fields `(line_key, stops, dir_aware, agency_id, no_draw, trip_group_id)`. Multiple entries per key exist when: (a) the same `line_key` spans multiple physical lines (e.g. SBB S3 Zürich vs SBB S3 Basel vs SBB S3 Luzern — same `line_key=("S3","S 3","train")`, different `trip_group_id`), or (b) the same `(line_key, trip_group_id)` has multiple distinct stop sets emitted as direction-aware variants (`dir_aware=True`).

`no_draw` is `None` for drawable lines; `"low_frequency"` for lines below `MIN_FREQ_SCORE` (0.075). Mountain lines are always `no_draw=None`. The flag is set at GTFS build time and propagated through the 4-loop — it does not affect which candidate the loop settles on. The draw/no-draw decision is applied only at the post-4-loop draw gate.

### GTFS line grouping (trip_group_id)
`trip_group_id` is assigned at GTFS build time inside `stream_stop_times()`. It identifies one physical line within a `(long_name_norm or short_name, agency_id, bucket)` partition. The partition is replaced from the old 0.5° geo-bucket grid; the geo grid is gone.

Algorithm: per partition, deduplicate trips by their **merged stop frozenset** (parent_station from `stops.txt` when non-empty, otherwise the part of `stop_id` before the first colon — collapses platforms). Run union-find over distinct stop patterns: two patterns are connected iff they share ≥2 merged stop identities. Each connected component gets a sequential `trip_group_id` (0, 1, 2, …) unique within the partition.

This separates regional S-Bahn networks (S3 ZH/BS/LU stay apart because they share zero stops), merges short-turn + full-route variants (shared trunk), and groups Y-shapes through the shared trunk. Ersatzverkehr-style numeric labels like SBB `EV1` correctly split into many groups — one per physical replacement route.

`_line_key_full` is now `(short_name, long_name, bucket, agency_id, trip_group_id)` — unique per physical line by construction. Dedup and group-reassignment group by this tuple; the cross-network dedup bug and nationwide stop pool of the geo-bucket era are eliminated by construction.

### Low-service flagging on `_line_canonical_export`
Lines below `MIN_FREQ_SCORE` (0.075) are **kept** in `_line_canonical_export` but flagged `no_draw="low_frequency"` on their `CanonEntry`. This replaces the old deletion that prevented these lines from being matched in the 4-loop.

The `low_freq_keys` set is computed at build time using `compute_freq_score(freq, mode_approx)` where `mode_approx` is derived from the GTFS bucket. The "bus" bucket is approximated as `regional_bus` (higher `BEST_HEADWAY` → higher score) rather than `bus` — intentionally conservative. Mountain bucket is exempt entirely.

### Stop assignment — 4 sequential batch loops

Stop assignment runs as four global batch passes. All routes go through Loop 1 before any route enters Loop 2. A route settles once it finds a match that passes all required checks. See `.claude/concepts/stop-assignment-architecture.md` for the full requirements.

**Loop 1 — simple string:** Try ref against GTFS long_norm first (= `ref_norm` = `ref.replace(" ", "")`), then short_name variants. Known generic prefixes (S, R, RE, IC, …) are excluded — routes whose only match is a generic key pass directly to Loop 3. Helper: `_loop_keys(1, ...)`. Sanity skipped when ep_count (5 km) == 2.

**Loop 2 — string tricks:** RE↔R conversion, name-prefix extraction (normalized segment before ":" + individual tokens), alpha-prefix stripping. Generic results deferred to Loop 3. Helper: `_loop_keys(2, ...)`. Sanity skipped when ep_count (0.5 km) == 2.

**Loop 3 — generic string:** Keys from the `GENERIC_GTFS_PREFIXES` frozenset. Candidates capped at 50, sorted by (-bbox_score, -ep_0.5km, -n_stops). Unconditional sanity check.

**Loop 4 — geo-fallback:** All candidates in bucket scored by bbox overlap (≥0.5 required). Coarse geographic pre-filter applied before per-stop iteration: first in-service-area stop coordinate must fall within the overall OSM bbox ±0.9° (~100 km); candidates outside are skipped at O(1) cost. Capped at 50. Unconditional sanity check. Failing routes excluded (not drawn). Mountain rack railways get a terminal-name lookup fallback before exclusion.

**Shared helpers:** `_stop_candidates()` — collects and scores candidates from specific keys (7-element tuples: `bbox_score, ep_0_5, ccoords, full_density, line_key_full, lk_ref, no_draw`); `_try_assign()` — two-pass iteration: pass 1 tries only drawable candidates (`no_draw is None`); pass 2 tries only `no_draw` candidates as fallback. Within each pass, applies terminal gate (ep_5 ≥ 1) and loop-level sanity logic. This ensures a drawable GTFS match at any rank beats a no_draw match. `_run_stop_loop()` — parameterized batch loop.

**Endpoint coverage gate:** `_count_endpoints_covered(osm_pts, ccoords, ENDPOINT_THRESHOLD_KM, osm_stop_nodes, osm_segs)` returns 0/1/2 — how many OSM terminal stations have a GTFS stop within 5 km. Priority for reference points: (1) `osm_stop_nodes[0]`/`[-1]` — actual passenger stop positions, most accurate; (2) all segment start/end points from MultiLineString geometry (`osm_segs`) — correct for Y-shapes; (3) `osm_pts[0]`/`osm_pts[-1]` as final fallback. ep_count 0 → returned to pool (all loops) or excluded (Loop 4).

**Service area filter (`is_in_service_area`):** Both GTFS `ccoords` and the inline density candidate list are filtered to stops within the service area before use. This prevents foreign terminal stops from skewing density and endpoint coverage. The filter is stop-ID-based: prefix `85` (Swiss + Liechtenstein) is the base rule, overridden by hardcoded `_SERVICE_AREA_EXCLUDE` and `_SERVICE_AREA_INCLUDE` sets in `05_score_and_match.py` for edge cases at the border.

**Critical:** do NOT feed `find_best_gtfs_candidate`'s canonical stops into stop assignment. Geo-cell-bounded candidates (~40km) cause `_covers_endpoints` to fail more often, triggering the broad geo fallback which pulls in wrong stops. One session: 2 fixes, ~50 regressions.

### Group-level stop assignment (implemented)

**Problem it solved:** A GTFS line often has multiple trip variants. When variants are subsets of each other, all stops collapse into a single union candidate (`dir_aware=False`). Multiple OSM relations for the same line (different directions, short-turns, branches) all competed against this union, with the winning candidate's stops filtered by the OSM relation's sub-bbox — a geographic proxy that cannot distinguish "stop belongs to this OSM variant" from "stop happens to be geographically close." Classic symptom: Glattbrugg (S3-Flughafen branch stop) leaking into S3-Hardbrücke with a 4.5 km connector.

**Implementation — `_group_reassign_stops()` (runs after dedup, before JSON write):**
1. Group all surviving `line_stops_out` entries by `(_line_key_full, bucket)`. Because `_line_key_full` includes `trip_group_id`, each group covers one physical line by construction.
2. Skip groups with fewer than 2 OSM relations.
3. For each group, pool all canonical GTFS stops from `_line_canonical_export` filtering by `entry.line_key == target_line_key AND entry.trip_group_id == tg_id`. The `trip_group_id` filter scopes the pool to the one physical line in this group; without it, S3 stops from other regional networks would leak in.
4. For each stop, filter to relations whose sub-bboxes contain it (coarse geographic gate, margin=0.02°).
5. Compute **full vertex scan** distance (no early exit) from the stop to each nearby relation's polyline.
6. Assign the stop to all relations within `max(d_min + 0.05 km, d_min * 1.1)` of the closest relation — shared stations (both routes within metres) appear on all; branch-specific stops (one route much closer) are pinned to the right branch.
7. Use a dict keyed by `stop_id` to deduplicate stops that appear in multiple canonical entries.

**Critical implementation note — full vertex scan:**
`_min_dist_to_polyline_km` has an early exit at 100 m: it breaks as soon as it finds *any* vertex within 100 m, which may not be the closest vertex. In the group pass this produces wrong d_min values (e.g. a 75 m vertex found before the true 11 m vertex → d_min inflates to 75 m → threshold inflates to 125 m → wrong stops pass). The group pass therefore uses `min(haversine_km(...) for p in geom)` — a full scan with no early exit. Do NOT replace this with `_min_dist_to_polyline_km`.

**Direction filter in `_lookup_canonical_stops`:**
`dir_aware=True` means the two directions of a line have genuinely different stop sets (e.g. Bus 17 Bern: westbound on Effingerstrasse, eastbound on Schwarztorstrasse). The direction filter in `_lookup_canonical_stops` (skips candidates whose first stop is >2× closer to the OSM end than start) is **redundant for multi-relation groups** — the group pass overwrites the per-relation assignment anyway. It is still **load-bearing for single-relation lines** with `dir_aware=True`: without it, the wrong-direction canonical could win and there is no group pass to correct it. Leave it in place.

**What does NOT change:**
- `_lookup_canonical_stops` — still exists and is used by the diagnostic script (`check_geo_sanity_rejects.py`); no longer called in the main pipeline (replaced by `_stop_candidates` / `_try_assign`)
- `07_extract_stops.py` — connectors snap to the assigned relation's geometry; no changes needed there
- Single-relation groups — group pass skips them (`len(osm_ids) < 2`); direction filter in `_stop_candidates` protects them

---

## Post-matching Deduplication

After all OSM→GTFS matching is complete, `05_score_and_match.py` runs a dedup pass over `line_stops_out` before writing `line_stops.json` and `transit_lines.geojson`.

**Rule:** Within each `_line_key_full` group, if any OSM entry has a **direct ref** match (OSM `ref` matches `short_name` or `long_name` after stripping spaces and lowercasing), all **fallback-matched** entries in the same group are removed. This prevents renamed/legacy OSM routes from appearing alongside their correctly-ref'd successors.

**`_line_key_full` grouping key:** Each `line_stops_out` entry stores `_line_key_full = (short_name, long_name, bucket, agency_id, trip_group_id)` — uniquely identifies one physical line by construction. Dedup groups by `_line_key_full`. Two lines that share a name string but belong to different operators (e.g. SBB R2 Lausanne–Bex and RhB R2 Landquart–Davos) get different `agency_id` values and are never in the same group; two regional S3s under the same agency get different `trip_group_id` values and are also never in the same group.

**`_is_direct_match(osm_ref, short_name, long_name)`:** Replaces the old `gtfs_ref`/`_refs_match` mechanism. Normalises all three inputs (strip spaces, lowercase). A direct match is when `norm(osm_ref)` equals `norm(short_name)` or `norm(long_name)`. Exception: if the matching name is a `GENERIC_GTFS_PREFIXES` term, it is not a direct match (avoids "S" or "R" being treated as direct).

**`agency_id` provenance:** Comes from GTFS `routes.txt` via `load_routes()`. Propagated through `load_trips()` → `stream_stop_times()` into `CanonEntry.agency_id`. The first-seen agency_id for each `(line_key, trip_group_id)` is stored. `_try_assign()` builds `_line_key_full` from the winning candidate's `(sn, ln, bkt, agency_id, trip_group_id)` tuple.

Implementation: `_is_direct_match()` helper + `dedup_removed` set. Each entry stores `osm_ref` alongside `_line_key_full`. `gtfs_ref` is no longer stored or propagated. Console output: `Dedup-removed: N lines superseded by direct-ref match`.

Do not tighten this logic to remove fallback matches unconditionally — they are still valid and needed when no direct-ref match exists for the same `_line_key_full` group.

---

## Geo Sanity Check

Function `_passes_geo_sanity()` in `05_score_and_match.py`. Called by `_try_assign()` for every candidate that passes the terminal gate (ep_count ≥ 1), unless the loop-level allows skipping (Loop 1: ep_5==2; Loop 2: ep_0_5==2). Loops 3 and 4 always run the sanity check.

Candidates with no valid sanity result are skipped (returned to pool or, in Loop 4, excluded entirely). If Loop 4 finds no passing candidate, the route has no stops assigned and is removed from the drawn output (EXCLUDED, recorded in `sanity_excluded.json`).

**Rule:** Lines excluded by the geo sanity check must NOT be drawn. Do not draw lines without valid GTFS-backed stops.

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

Candidate density is computed inline from the **full unfiltered candidate stop list** (not bbox-filtered `ccoords`) — this is critical: using the bbox-filtered span instead inflated the in-corridor density of long-distance trains (e.g. IC6 Basel–Brig scored as dense as RE1 when only the shared Brig–Bern section was measured). Both the inline density candidate stops and `ccoords` are filtered via `is_in_service_area()` (see above).

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
1. `osm_mountain_by_ref` built before OSM preprocessing pass — OSM routes tagged funicular/cable_car/gondola/aerialway, plus train-tagged rack railways whose ref is in the mountain GTFS bucket
2. OSM preprocessing pass: routes classified as `mode="mountain"` (or train-tagged with ref in `osm_train_refs_in_mountain_gtfs`) are excluded from the 4-loop pool and handled by the GTFS-first mountain loop instead
3. GTFS-first mountain loop: iterates `_line_canonical_export` for bucket="mountain", deduplicates direction variants by bbox overlap (0.5 threshold)

### WAB/JB/BOB
WAB and JB are GTFS type=2 (train bucket), short_name="CC". OSM routes with `MOUNTAIN_RAIL_OPERATORS` are classified `mode="mountain"` in the OSM preprocessing pass. BOB is type=2 train, short_name="R", drawn as regular train mode.

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

#### Resolved
- **Train PE (Glacier Express St. Moritz↔Zermatt)** — now KEPT after geo fallback improvements (score ≥ 0.5 filter + better sort). No longer excluded.
- **Train RE42 (Zermatt→Fiesch)** — now KEPT after same geo fallback improvements. No longer excluded.
- **Train RE1 (Bern↔Brig/Domodossola, BLS Lötschberg)** — fixed by cross-border endpoint fix: `_count_endpoints_covered` now uses `osm_stop_nodes[0]`/`[-1]` instead of raw geometry endpoints (OSM geometry starts inside the Simplon tunnel), and `is_in_service_area` filters out non-Swiss GTFS stops. RE1 (OSM IDs 11612242, 11612421) now draws correctly via the mountain route.
- **Train RE4 (RhB, Landquart→Scuol-Tarasp)** — fixed by the MultiLineString segment ordering fix (see `.claude/concepts/multilinestring-segment-ordering.md`). OSM relation 89792 has 6 disordered segments; `04_extract_osm.py` now chains them into one continuous polyline and drops noise sidings. `05_score_and_match.py` now reads `osm_line_km` from the `raw_length_km` GeoJSON property instead of recomputing from flattened geometry. This corrects `osm_line_km` from 205 km (inflated by inter-segment teleportation jumps) to 75 km, allowing the density gate in Check 2 to pass.
- **Regional Bus 171 (Chur→Bellinzona, EXB 171)** — fixed.
- **S18 (Forchbahn)** — fixed; now draws correctly as tram.

#### 4-loop stop assignment architecture — implemented

Replaced the old single-pass cascade + inline geo-fallback with four sequential batch loops. See `.claude/concepts/stop-assignment-architecture.md` for requirements. Key changes:

- Long_name (`gtfs_long_index`) tried before short_name (`gtfs_index`) in stop assignment.
- Routes with only generic-prefix matches (S, R, RE, IC, …) are deferred to Loop 3 (unconditional sanity check) instead of matching without sanity.
- All loops use uniform scoring: (-bbox_score, -ep_0.5km, -n_stops).
- Routes are not revisited once settled — global ordering is guaranteed.

#### Transit preprocessing architecture — implemented

Replaced the old "main loop" (which prematurely dropped routes via inferior GTFS matching) with two lightweight preprocessing passes. See `.claude/concepts/transit-preprocessing.md` for requirements. Key changes:

- All GTFS lines are kept in `_line_canonical_export` (`no_draw` flag instead of deletion for low-freq lines).
- OSM preprocessing pass: hard exclusions (non-transit, excluded operators, TER, mountain) + classification (mode, bucket, ref remapping) with no GTFS matching.
- Post-4-loop draw gate: filters `no_draw` entries, looks up freq/speed from `gtfs_index` using `_line_key_full`, computes visual properties, writes `transit_lines.geojson`.
- `find_best_gtfs_candidate` removed (was used only by the old main loop for freq/speed).
- `_refs_match`/`gtfs_ref` replaced by `_is_direct_match(osm_ref, short_name, long_name)`.

#### GTFS line grouping — implemented

Replaced the 0.5° geo-bucket partition in `_line_canonical_export` with a trip-graph connectivity merge. See `.claude/concepts/implemented/gtfs-line-grouping.md` for requirements. Key changes:

- `CanonEntry` gains a `trip_group_id` field; `GEO_BUCKET_DEG` removed.
- Trips are partitioned by `(long_name_norm or short_name, agency_id, bucket)`; per partition, connected components are computed over distinct merged-stop patterns (≥2 shared merged stops = connected). Merged identity = `parent_station` from `stops.txt`, fallback to base UIC.
- `_line_key_full` is extended to `(short_name, long_name, bucket, agency_id, trip_group_id)`. Now unique per physical line by construction — dedup and `_group_reassign_stops` no longer cross-contaminate between regional networks under the same agency (e.g. SBB S3 in Zürich vs Basel vs Luzern).
- `_group_reassign_stops` additionally filters canonical pool entries by `entry.trip_group_id == tg_id`.
- `diagnose_transit_line.py` updated for the new `CanonEntry` shape and 5-tuple `_line_key_full`.

---

#### Still excluded / missing — legitimate lines needing a fix
- **Bus 22 (Belprahon↔Moutier)** — OSM IDs 17227287–17227290 (4 directions/variants). Check 1 passes in diagnostic but pipeline excludes — geometry/bbox mismatch.
