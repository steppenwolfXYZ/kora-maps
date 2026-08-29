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
| `src/compute_footpaths.cc` | Full replacement. Import-time stop-to-stop transfer tables loaded from the precomputed Valhalla matrix CSV (`KORA_FOOTPATH_MATRIX_PATH`), not computed via OSR. Aborts if the file is missing. Two-tier split (`transfer-point-optimization.md`): the foot profile gets only rows ≤ `KORA_TRANSFER_CAP_MINUTES` (default 30) — the table default queries search on; the full 2-h set lands in the spare bike profile slot (`kora_valhalla::kFullTransferProfile`) for fallback queries and station-endpoint offsets. |
| `include/motis/kora_valhalla.h`, `src/kora_valhalla.cc` | New. Query-time HTTP client for Valhalla (`KORA_VALHALLA_URL`): `route()` for point-to-point walks, `one_to_many()` for offset matrices, `ensure_reachable_or_abort()` startup probe. Both calls are cached in bounded process-global FIFO caches (the app's query cascade re-sends identical coordinates constantly — a warm re-query costs ~50 ms instead of seconds), and `one_to_many` fires its targets in parallel 600-stop chunks. Costing options mirror `scripts/build_valhalla_footpath_matrix.py` — the two MUST stay in sync (same walker on both sides; changing speed/costing requires a matrix rebuild). Transport errors throw (query fails); "no path" is a normal nullopt. |
| `src/endpoints/routing.cc` | Patched copy, three changes. (1) The WALK branch of `get_offsets` (the offsets that seed RAPTOR's start/destination stops) queries Valhalla one-to-many instead of OSR for coordinate endpoints — candidate radius capped at 20 km, targets capped at 2400 nearest. This is what makes RAPTOR pick boarding stops using real walking times. (2) Station endpoints (stop IDs in fromPlace/toPlace) take their WALK offsets straight from the imported full Valhalla matrix — zero HTTP per query; matrix reach (2 h) is the limit. (3) Two-tier transfer table: the fork-only URL param `koraFullTransfers=true` (invisible to the generated API) routes the search onto the full 2-h table; without it, foot queries with `useRoutedTransfers=true` search the capped default table. Falls back capped→foot→0 on empty tables (pre-two-tier index data). |
| `src/osr/street_routing.cc` | Patched copy. `street_routing()` intercepts `kFoot` and builds the WALK leg (duration, distance, polyline) from Valhalla `/route`. Covers direct walk itineraries, pre/post-transit legs, and transfer legs — all three flow through this function. When the journey has already fixed both leg times, those stay authoritative (they derive from Valhalla numbers anyway) and Valhalla supplies the geometry. |
| `src/server.cc` | Patched copy. Probes Valhalla `/status` at server start and exits when unreachable — docker's `restart: unless-stopped` turns that into a wait-for-valhalla loop. Import is NOT probed (it consumes the CSV, not live Valhalla). |
| `deps/nigiri/src/routing/raptor/optimize_footpaths.cc` | Patched copy (first overlay inside a dependency checkout; nigiri is pinned transitively via MOTIS's `.pkg`). nigiri's post-reconstruction transfer optimizer slides each transfer to the lowest-penalty stop pair, but upstream only scans footpath pairs — and no footpath table carries self-entries — so a zero-walk same-platform transfer was invisible and a longer street walk would win. The patch adds same-stop re-boarding as a candidate (feasibility per the stop's change time, same rule the RAPTOR search applies) and scores the incumbent transfer at its actual transfer stops instead of the journey endpoints. Only effective for queries with `useRoutedTransfers=true`, which the app always sends. See `transfer-point-optimization.md`. |
| `deps/nigiri/include/nigiri/routing/query.h`, `deps/nigiri/include/nigiri/routing/journey.h`, `deps/nigiri/include/nigiri/routing/search.h`, `deps/nigiri/include/nigiri/routing/kora_alternatives.h` (new), `deps/nigiri/src/routing/raptor/reconstruct.cc`, `deps/nigiri/src/routing/raptor/pong.cc` | Patched copies (+ one new header). **ε-alternates** (`near-optimal-endpoint-alternatives.md`): RAPTOR holds a per-stop optimum but only ever reads out the single best (stop arrival + egress offset) combination per Pareto point, silently collapsing equal-or-near journeys over other egress/access stops. `kora_alternatives.h` (new, shared) derives candidate anchors from `round_times[k][stop] + offset` (an upper bound valid for both possible writers of the entry) for every destination offset within the slack, reconstructs each against the live algo state (infeasible guesses throw and are dropped, never emitted), snaps the endpoint walk leg back to the vehicle's real arrival, and controls duplicates purely via quay-blind transit fingerprints (same vehicles between same parent stations) — so different lines at different platforms of one station both survive while quay siblings of the same vehicle collapse. Alternates additionally pass sensibility filters (no same-line re-board — compared by line name, so opposite directions count; no revisiting a parent station already passed; ride-through redundancy: an alternate whose endpoint station is served no later by a kept journey's endpoint vehicle ridden past its own exit is a disguised duplicate) plus endpoint-station dominance (equal-or-worse in both endpoint time and walk vs a same-remainder journey → dropped), and extraction runs once per Pareto point rather than once per PONG cursor rediscovery (~2.5× alternates-off search time). A final fingerprint/dominance/redundancy pass against the primaries runs in each driver. `KORA_ALT_DEBUG=1` (serve-time env) logs per-candidate extraction outcomes to stderr. Hooked into BOTH search drivers: `search.h` in the classic rRAPTOR per-start-time loop, and `pong.cc` after the forward ping pass — MOTIS's default driver is PONG, whose ping state holds the egress side and whose journeys already carry the final arrival/transfers (only the departure gets tightened by the pong pass). `reconstruct.cc` anchors an alternate's dest leg at its synthesized `dest_time_` and forces its egress stop (`journey::kora_alt_egress_`, added in `journey.h`). `query.h` carries the knobs, set from the fork-only URL params `alternativesEpsilon` (seconds, 0 = off = upstream behavior) and `alternativesMax` (cap per Pareto point) parsed in `routing.cc`, which also appends the alternates (`search_state.alternatives_`) to the response as ordinary itineraries. Intermodal-destination, via-free queries only; the search phase itself is untouched. |

## Build

```
docker build -t koramaps/motis:footpath-matrix -f Dockerfile .
```

First build ~60-90 min (full upstream compile, cached); fork iterations
~5-10 min (only overlaid files recompile). When adding a new overlay
file, also add it to the `touch -c` list in the Dockerfile.

## Runtime environment

- `KORA_FOOTPATH_MATRIX_PATH` — import only. Path of the matrix CSV.
- `KORA_TRANSFER_CAP_MINUTES` — import only, optional (default 30).
  Cap of the default transfer table; the full 2-h table is always
  loaded alongside it.

**Import-skip trap:** MOTIS's import is incremental — the transfer-table
task (`osr_footpath`) is fingerprinted on its *inputs* (timetable, OSM,
config), NOT on the fork's code or `KORA_TRANSFER_CAP_MINUTES`. After
changing either, delete `motis/data/meta/osr_footpath.json` before
re-importing, or the task silently keeps the old `tt_ext.bin`.
- `KORA_VALHALLA_URL` — serve only. Default `http://kora-valhalla:8002`
  (resolves over the shared `koramaps` docker network — create once with
  `docker network create koramaps`).
- `KORA_ALT_DEBUG` — serve only, optional. When set, the ε-alternates
  extraction logs one stderr line per candidate with its outcome
  (accepted / reason dropped) — read via `docker logs kora-motis`.
  Off by default; zero cost when unset.

## What to check when bumping MOTIS_REF

`MOTIS_REF` in the Dockerfile is pinned because three overlays are
full-file patched copies — building against a moved upstream would
silently revert upstream's changes in those files. To bump:

1. `git diff <old-pin>..<new-pin> -- src/endpoints/routing.cc src/osr/street_routing.cc src/server.cc src/compute_footpaths.cc include/motis/compute_footpaths.h`
2. Re-copy the new upstream versions, re-apply the kora patches (all
   marked with `kora fork:` comments; the diff hunks are small and
   localized). The nigiri overlay needs the same treatment against
   nigiri's new pinned commit (read it from `.pkg` at the new MOTIS
   ref): diff `src/routing/raptor/optimize_footpaths.cc`,
   `src/routing/raptor/reconstruct.cc`, `src/routing/raptor/pong.cc`,
   `include/nigiri/routing/query.h`, `include/nigiri/routing/journey.h`
   and `include/nigiri/routing/search.h`, re-copy, re-apply
   (`kora_alternatives.h` is fork-only — no upstream counterpart to
   diff, but re-check its raptor_state/journey API usage).
3. Check `nigiri::footpath` / `vector_map` / `build_lb_graph` usage in
   `compute_footpaths.cc` still matches nigiri's API.
4. Update `ARG MOTIS_REF` and rebuild.
