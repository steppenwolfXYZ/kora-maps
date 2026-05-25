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
When canonical stops are empty or fail endpoint coverage, all GTFS lines in the bucket are scored as candidates. Scoring: `score = n_stops_in_bbox / n_total_stops`. Candidates with `score < 0.5` are discarded. Remaining candidates are sorted by `(-score, -len(ccoords))` — highest bbox overlap first, absolute stop count as tiebreaker. The top 50 candidates are run through `_passes_geo_sanity` in order; the first that passes is used. If nothing passes, `best_coords` is cleared — the line has no stops assigned (appears as EXCLUDED in `check_geo_sanity_rejects.py`). The line is still drawn as a colored line; the sanity check controls stop assignment, not line drawing.

### What the sanity check affects
- It selects which geo-fallback candidate's stops are assigned to the line
- If no candidate passes, the line has no stops AND is removed from the drawn output entirely (EXCLUDED)
- A line can also be in KEPT with wrong stops if a bad candidate passes the check — this is a false positive, not a rejection
- **Rule:** Lines excluded by the geo sanity check (no valid candidate found) must NOT be drawn. Do not draw lines without valid GTFS-backed stops.

### The three checks (cheapest first, returns True on first pass)

**Check 1 — Terminal name match (string only)**
Counts stops whose normalised name contains the normalised OSM `from` or `to` tag. Requires at least **1/3 of the candidate's stops** to match, with a minimum of **2 stops** (so small lines must have both terminals present). Minimum terminal name length: ≥ 4 chars.

This prevents a long line from passing just because one of its stops happens to share a terminal name — e.g. a 15-stop east-shore line with one stop at Stadelhofen should not pass for a route `from=Zürich Stadelhofen`.

**Check 2 — GTFS stops → OSM geometry proximity**
Sample 5 evenly-spaced GTFS stops from the candidate. Find the distance from each to the nearest point on the OSM polyline (vertex-based). Require at least 3/5 to be within 200 m.

**Check 3 — OSM geometry → GTFS stops proximity**
Sample 5 evenly-spaced points from the OSM geometry (`osm_pts`). For each, find the nearest GTFS stop in the candidate. Require at least 3/5 to be within 200 m.

Note: Check 2 is cheaper (polyline lookup) so it runs first. Check 3 is a nearest-neighbour search over all GTFS stops in the candidate. Both use 200 m threshold — real stops sit within meters of their line, so 200 m is already generous.

### Known remaining issues with the sanity check
- `_norm_stop_name` strips `hb`/`hbf`/`bahnhof` — short generic city tokens can still pass Check 1 (e.g. `bern` from `Bern HB`)
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

#### Still excluded — legitimate lines needing a fix
- **Regional Bus 171 (Chur→Bellinzona)** — PostBus express (EXB 171). Canonical lookup picks a Bellinzona-local B 171 (44 stops, higher count) over the full-corridor EXB 171 (25 stops). Endpoint check fails → geo fallback runs. Geo fallback also ranks Bellinzona-local variants first (more stops, same score=1.0). EXB 171 is buried past the cap of 50. Fix: use 3-level endpoint coverage `(0/1/2 endpoints covered)` as primary geo fallback sort key, so full-corridor candidates float above partial ones.
