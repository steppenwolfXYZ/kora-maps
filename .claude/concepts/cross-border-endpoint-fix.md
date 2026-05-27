# Cross-Border Endpoint Fix

**Status:** Planned — not yet implemented

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

### Part 2: Filter GTFS stops to Switzerland

Both GTFS `ccoords` (used for ep_count and stop assignment) and the full-candidate stop list used for inline density must be filtered to stops geographically within Switzerland.

**Why a geographic filter, not a UIC prefix filter:**
The Swiss GTFS assigns UIC prefix `85` to Iselle di Trasquera, Varzo, and Preglia — three Italian stations on the Simplon south ramp operated by SBB. A prefix-`85` filter would incorrectly keep them. Geographic containment is the correct test.

**Implementation:**
- Load a Switzerland border polygon (GeoJSON) at pipeline startup.
- Implement `is_in_switzerland(lon, lat) -> bool` using `shapely.geometry.Point.within(polygon)`.
- Apply the filter in four places in `05_score_and_match.py`:
  1. `_lookup_canonical_stops` — when building `ccoords` from each GTFS candidate
  2. `_lookup_canonical_stops` Trigger 1 — when building `_cand_coords` for density (name-fallback sanity check)
  3. Geo fallback — when building `ccoords` from each GTFS candidate
  4. Geo fallback — when building `_fc` (the full candidate list used for inline density)

**New dependency:** `shapely` library. Check if already present; if not, add to project requirements.

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
