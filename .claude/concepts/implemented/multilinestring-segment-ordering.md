# MultiLineString Segment Ordering and Noise Reduction

**Status:** Implemented

## Problem

OSM route relations are assembled from member ways that form the track geometry. When exported to GeoJSON as a MultiLineString, the segments are stored in member-list order, which is not necessarily geographic traversal order. For routes with multiple segments (e.g. RhB RE4: 6 segments), consecutive segments in the array may be geographically distant — the segment covering Scuol is followed by a Landquart depot siding, then a Vereinatunnel siding, etc. When `05_score_and_match.py` flattens the MultiLineString into a single point list, these out-of-order segments introduce large teleportation jumps between them (up to 57 km for RE4), inflating the computed route length from the actual 75 km to 205 km. This inflated length makes the density gate in the geo sanity check (Check 2) reject all candidates, preventing correct stop assignment.

Additionally, OSM relations include non-mainline segments: depot sidings, passing loops, tunnel bypass tracks. These add noise to sub-bbox computation and endpoint detection.

## Current workaround

None. The segments are used as-is. `raw_length_km` in the GeoJSON properties is computed correctly (sum of per-segment lengths, no inter-segment jumps), but `05_score_and_match.py` ignores it and recomputes length from the flattened geometry, inheriting the inflation.

## Requirements

### Segment processing in `04_extract_osm.py`

After extracting the raw segments from the OSM relation's way members, reorganise them into a set of merged chains before writing the GeoJSON:

**Step 1 — Chain assembly:**
Repeatedly build chains by following endpoint connections:

1. Find a chain start: a segment whose start point has no predecessor (no other segment ends there). If none, pick the longest unplaced segment.
2. Greedily extend: find a remaining segment whose start or end matches the current chain tip (within tolerance). Reverse it if it connects at its end. Append it to the chain.
3. When no more segments connect to the current chain tip, the chain is complete. Deduplicate shared junction points (the matching endpoint appears only once in the merged result).
4. Repeat from step 1 for any remaining segments, building additional chains as needed (handles Y-shapes and genuinely separate route sections).

Connected segments — those that join the chain — are always merged, regardless of length.

**Step 2 — Noise reduction:**
After chain assembly, a segment is a candidate for removal only if it is fully disconnected: no endpoint (within tolerance) matched any other segment during chain assembly. Apply the following rules to disconnected segments:

**Rule 1 — disconnected and short:**
Drop if length < 5 km.

**Rule 2 — disconnected and stop-less:**
Drop if all of the following hold:
- Length is less than the longest chain
- The relation contains ≥ 3 OSM stop nodes total
- Fewer than 2 OSM stop nodes from the relation are within 50 m of the segment's polyline

**Step 3 — Write output:**
Write the remaining chains and surviving disconnected segments as a MultiLineString. Each entry is a single merged polyline. Recompute `raw_length_km` as the sum of kept segment lengths.

### Endpoint coverage in `05_score_and_match.py`

Replace the two fixed comparison points (`osm_pts[0]` and `osm_pts[-1]`) with the set of all segment endpoints from the MultiLineString: the start and end point of every segment. A GTFS terminal is considered covered if it is within the threshold distance of any segment endpoint.

This works correctly because after merging: each MultiLineString entry is either a meaningful merged chain (whose endpoints are genuine route termini or branch termini) or a surviving disconnected siding. Y-shapes produce multiple chains, all of whose endpoints are considered.

## Out of scope

**U-shape guard:** In theory, a single OSM relation could include ways for both directions of a route, causing the chain assembly to merge them into one U-shaped polyline (outbound + return). In practice this does not occur in this dataset — each direction is a separate OSM relation, and both export as distinct single-segment LineStrings. No guard is implemented. If U-shape problems arise in the future, the fix is to stop extending a chain when the candidate segment's far endpoint matches the chain's start point (would close a loop or reverse back to origin).

## Constraints

- **Endpoint matching tolerance:** Use a small geographic tolerance (< 1 m, approximately 0.00001°) rather than exact float equality, to handle rare floating-point discrepancies while remaining strict enough to avoid false connections.
- **`raw_length_km` integrity:** After noise reduction, `raw_length_km` must be recomputed as the sum of remaining segment lengths only.
- **Y-shapes:** At a fork, the primary chain consumes one branch; the other branch forms a separate chain. Both are kept. Neither is eligible for noise reduction (they are connected at the fork point).
- **Routes with few stops:** Rule 2 is gated on the relation having ≥ 3 OSM stop nodes total, preventing over-filtering on routes where stop membership is sparse.
- **`osm_line_km` in `05_score_and_match.py`:** After this change, `osm_line_km` should be read from the `raw_length_km` GeoJSON property rather than recomputed from the flattened geometry. The property is already correct and avoids any residual inflation from surviving disconnected segments.
