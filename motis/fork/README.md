# MOTIS fork — Valhalla is the sole walking authority

Kora Maps rebuilds MOTIS with a small set of files overlaid so that
**every walking value — durations, distances, geometries, and the walks
RAPTOR uses to choose boarding stops — comes from Valhalla**, never from
MOTIS's OSR pedestrian profile. There is no OSR walking fallback: if
Valhalla is unreachable, the server refuses to start / queries error.
See `.claude/concepts/valhalla-pedestrian-router.md` for the design.

OSR itself stays in the build and the import — it still serves non-foot
profiles (bike / car / wheelchair, none currently surfaced by the app)
and the platform-matching machinery.

## Overlaid files

| File | Change |
|---|---|
| `src/compute_footpaths.cc` | Full replacement. Import-time stop-to-stop transfer table loaded from the precomputed Valhalla matrix CSV (`KORA_FOOTPATH_MATRIX_PATH`), not computed via OSR. Aborts if the file is missing. |
| `include/motis/kora_valhalla.h`, `src/kora_valhalla.cc` | New. Query-time HTTP client for Valhalla (`KORA_VALHALLA_URL`): `route()` for point-to-point walks, `one_to_many()` for offset matrices, `ensure_reachable_or_abort()` startup probe. Both calls are cached in bounded process-global FIFO caches (the app's query cascade re-sends identical coordinates constantly — a warm re-query costs ~50 ms instead of seconds), and `one_to_many` fires its targets in parallel 600-stop chunks. Costing options mirror `scripts/build_valhalla_footpath_matrix.py` — the two MUST stay in sync (same walker on both sides; changing speed/costing requires a matrix rebuild). Transport errors throw (query fails); "no path" is a normal nullopt. |
| `src/endpoints/routing.cc` | Patched copy, two changes. (1) The WALK branch of `get_offsets` (the offsets that seed RAPTOR's start/destination stops) queries Valhalla one-to-many instead of OSR for coordinate endpoints — candidate radius capped at 20 km, targets capped at 2400 nearest. This is what makes RAPTOR pick boarding stops using real walking times. (2) Station endpoints (stop IDs in fromPlace/toPlace) take their WALK offsets straight from the imported Valhalla matrix (the transfer table) — zero HTTP per query; matrix reach (2 h) is the limit. |
| `src/osr/street_routing.cc` | Patched copy. `street_routing()` intercepts `kFoot` and builds the WALK leg (duration, distance, polyline) from Valhalla `/route`. Covers direct walk itineraries, pre/post-transit legs, and transfer legs — all three flow through this function. When the journey has already fixed both leg times, those stay authoritative (they derive from Valhalla numbers anyway) and Valhalla supplies the geometry. |
| `src/server.cc` | Patched copy. Probes Valhalla `/status` at server start and exits when unreachable — docker's `restart: unless-stopped` turns that into a wait-for-valhalla loop. Import is NOT probed (it consumes the CSV, not live Valhalla). |

## Build

```
docker build -t koramaps/motis:footpath-matrix -f Dockerfile .
```

First build ~60-90 min (full upstream compile, cached); fork iterations
~5-10 min (only overlaid files recompile). When adding a new overlay
file, also add it to the `touch -c` list in the Dockerfile.

## Runtime environment

- `KORA_FOOTPATH_MATRIX_PATH` — import only. Path of the matrix CSV.
- `KORA_VALHALLA_URL` — serve only. Default `http://kora-valhalla:8002`
  (resolves over the shared `koramaps` docker network — create once with
  `docker network create koramaps`).

## What to check when bumping MOTIS_REF

`MOTIS_REF` in the Dockerfile is pinned because three overlays are
full-file patched copies — building against a moved upstream would
silently revert upstream's changes in those files. To bump:

1. `git diff <old-pin>..<new-pin> -- src/endpoints/routing.cc src/osr/street_routing.cc src/server.cc src/compute_footpaths.cc include/motis/compute_footpaths.h`
2. Re-copy the new upstream versions, re-apply the kora patches (all
   marked with `kora fork:` comments; the diff hunks are small and
   localized).
3. Check `nigiri::footpath` / `vector_map` / `build_lb_graph` usage in
   `compute_footpaths.cc` still matches nigiri's API.
4. Update `ARG MOTIS_REF` and rebuild.
