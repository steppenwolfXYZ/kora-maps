# Pill placement: multi-clump GTFS-anchored shortcut

**Status:** deferred. A parallel fix made the concrete case (Worblaufen, June 2026) no longer exhibit the symptom. Keep this document in case the pattern reappears at another station.

## Problem

In multi-clump clusters, the bar-placement tie-break enumerates combinations of one tied sweep option per clump and picks the combination minimising pairwise bar-center distance (proxy for inter-bar connector length). At stations where the GTFS coordinates are already well-aligned across lines, this optimisation can pull bars far from their actual GTFS positions to shave a few metres off connector length — producing bars that sit visually far from the real platforms even though a per-bar GTFS-anchored placement would have produced perfectly short connectors.

Canonical case at the time of writing: Worblaufen, where the optimiser placed the bar near the south end of the lines while the GTFS stops sat well to the north, requiring long connectors that ran the full length of the platforms.

## Requirements

Add a **GTFS-anchored shortcut** to the multi-clump tie-break, applied **before** the combination enumeration:

- Pick each clump's min-`gtfs_dist` tied option independently.
- If this combination passes both existing validity checks (same-tangent-group protection radius, no-double-cover) AND its **maximum single connector length** is ≤ `MULTI_CLUMP_SIMPLE_MAX_CONNECTOR_M` (10 m), accept it as the cluster's placement and skip enumeration.
- Otherwise fall through to the existing validity-checked enumeration.

The threshold is **per-connector max**, not total: a single ugly long connector must not be hidden behind several short ones.

## Constraints

- Scope is multi-clump clusters only. Single-clump clusters are unaffected.
- Validity checks are not bypassed — if the simple option violates them, it falls through to enumeration as normal.
- The shortcut deliberately prefers visual fidelity to GTFS positions over the last few metres of connector-length optimisation. Above the 10 m threshold, the optimiser still wins.
