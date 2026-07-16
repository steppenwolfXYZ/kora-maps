# Cross-Border Endpoint Fix

**Status:** Implemented

## Problem

Routes that cross Switzerland's border are matched incorrectly because endpoint coverage uses raw OSM geometry endpoints and unfiltered GTFS stop lists that include foreign stations.

Root cause chain:
1. `_count_endpoints_covered` uses raw OSM geometry endpoints (`osm_pts[0]`, `osm_pts[-1]`). Cross-border OSM routes often have geometry that extends past the last Swiss passenger stop — into tunnels, across borders, or to foreign terminals. No GTFS stop is within the 5 km threshold of those points.
2. ep_count = 0 → geo fallback fires.
3. Geo fallback may select a wrong GTFS candidate that happens to cover the Swiss portion of the corridor.
4. The wrong match propagates into dedup, potentially removing the correct route.

**Example — BLS RE1 (Bern↔Brig/Domodossola, OSM IDs 11612242/11612421):** The OSM geometry starts inside the Simplon tunnel, ~10 km past Brig station. The correct GTFS RE1 candidate includes Brig. ep_count = 0 (tunnel point is 10 km from Brig, outside the 5 km threshold) → geo fallback → IC6 partial trip (Brig→Bern) wins → RE1 gets `gtfs_ref="IC6"` → dedup removes RE1 because IC6 OSM has a direct ref match.

The correct comparison: the first Swiss passenger stop on the OSM route (Brig) should be compared to the GTFS candidate, not the tunnel geometry endpoint.

Other affected routes include any OSM relation whose geometry or stop list extends into Germany, France, Italy, or Austria.

## Fix — two parts

### Part 1: Use OSM stop nodes as effective endpoints

`_count_endpoints_covered` must use `osm_stop_nodes[0]` and `osm_stop_nodes[-1]` (the first and last OSM route member station nodes) as the comparison points, not the raw geometry endpoints. Fall back to `osm_pts[0]`/`osm_pts[-1]` only when `osm_stop_nodes` is empty or has fewer than 2 entries.

Rationale: OSM route geometry often extends past the last passenger stop (into tunnels, maintenance yards, across borders). Station nodes reflect where passengers actually board — the correct semantic for "does this GTFS candidate cover both ends of this route?"

The ep_count call in the main matching loop must pass `osm_stop_nodes` to `_count_endpoints_covered`.

### Part 2: Filter GTFS stops to the service area

Both GTFS `ccoords` (used for ep_count and stop assignment) and the full-candidate stop list used for inline density must be filtered to stops within the pipeline's service area before use.

**Service area definition:** UIC prefix `85` (Swiss and Liechtenstein) plus an explicit inclusion list of non-85 stops that Swiss operators serve across the border. Liechtenstein uses prefix `85` throughout; it is treated as part of the service area. The filter is implemented as `is_in_service_area(stop_id) -> bool`.

**Why prefix-based, not geographic:**
A polygon approach was investigated but found impractical: the Natural Earth 10m Switzerland polygon misses hundreds of genuine Swiss municipalities in southern Ticino (Chiasso, Pedrinate, Muggio, Arogno, Gandria, etc.) and in the Canton Geneva suburbs (Veyrier, Thônex, etc.). A stop-ID-based filter is more reliable.

**Why prefix `85` is not sufficient alone:**
The Swiss GTFS assigns prefix `85` to stations physically in Italy that SBB or jointly-operated lines serve: Iselle di Trasquera (8501952), Varzo (8501951), Preglia (8501950) on the Simplon south ramp; Tirano (8509369) and Campocologno Li Geri (8581990) on the Bernina line; and the Val Vigezzo/Ossola valley stops on the Centovalli/SSIF line (Colmegna, Maccagno, Pino-Tronzano, and the full Domodossola valley cluster). These must be explicitly excluded.

**Explicit exclude set** (prefix-85 stops physically in Italy or Germany):
- Simplon south ramp (Italy): 8501952, 8501951, 8501950
- Bernina line Italy end: 8509369, 8581990
- Lago Maggiore Italian shore: 8505874, 8505861, 8505862
- Val Vigezzo / Ossola valley (SSIF/Centovalli Italy section): 8505599, 8505597, 8505588, 8505580, 8505590, 8505584, 8505578, 8505593, 8505594, 8505585, 8505589, 8505581
- German enclaves surrounded by Swiss territory: 8503420 (Lottstetten), 8503421 (Jestetten)

**Explicit include set** (non-85 stops Swiss operators serve, within the service area for density purposes):
- Konstanz and surrounds (Thurbo/SBB, DB prefix): 8014586, 8014587, 8014481, 8014491
- Pougny-Chancy (Geneva area, French prefix): 8774538
- Delle (Jura border, French prefix): 8718444

**Apply the filter in four places in `06_score_and_match.py`:**
1. `_lookup_canonical_stops` — when building `ccoords` from each GTFS candidate
2. `_lookup_canonical_stops` Trigger 1 — when building `_cand_coords` for density (name-fallback sanity check)
3. Geo fallback — when building `ccoords` from each GTFS candidate
4. Geo fallback — when building `_fc` (the full candidate list used for inline density)

No new library dependencies. The exception lists were derived by cross-referencing GTFS stop coordinates against OpenStreetMap stop coverage (100 m proximity check) and manual geographic review.

## Why together

Part 1 alone gets RE1 to ep_count = 1 (Brig covered, Bern not covered because the GTFS mountain variant ends at Spiez). This triggers the geo sanity check. Check 2 (density + proximity) should pass since RE1 GTFS mountain stops are on the correct corridor. `needs_fallback = False` → IC6 not selected → RE1 survives dedup.

Part 2 is defense-in-depth: ensures long-distance trains with non-Swiss terminals cannot win the geo fallback for Swiss routes even if Part 1 alone were insufficient. It also correctly scopes density calculations to the Swiss portion of cross-border lines.

## Verification

Run: `./scripts/rebuild_transit.sh --skip-osm`

- RE1 OSM IDs 11612242 and 11612421 must NOT appear in `data/transit/main_loop_dropped.json`
- RE1 must appear on the map as a train line via the Lötschberg mountain route (Bern–Spiez–Kandersteg–Goppenstein–Brig)
- IC6 must still appear correctly
- No new entries in `data/transit/sanity_excluded.json` for previously-passing lines
- No regressions in `data/transit/main_loop_dropped.json` for previously-drawn lines
