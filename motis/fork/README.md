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
| `include/motis/kora_valhalla.h`, `src/kora_valhalla.cc` | New. Query-time HTTP client for Valhalla (`KORA_VALHALLA_URL`): `route()` for point-to-point walks, `one_to_many()` for offset matrices, `ensure_reachable_or_abort()` startup probe. Both calls are cached in bounded process-global FIFO caches (the app's query cascade re-sends identical coordinates constantly — a warm re-query costs ~50 ms instead of seconds), and `one_to_many` fires its targets in parallel 600-stop chunks. Costing options mirror `scripts/build_valhalla_footpath_matrix.py` — the two MUST stay in sync (same walker on both sides; changing speed/costing requires a matrix rebuild). Every `/route` call also requests an elevation profile (`elevation_interval`) and folds it into a noise-filtered ascent / descent pair on `walk_route`. Transport errors throw (query fails); "no path" is a normal nullopt. |
| `src/endpoints/routing.cc` | Patched copy, three changes. (1) The WALK branch of `get_offsets` (the offsets that seed RAPTOR's start/destination stops) queries Valhalla one-to-many instead of OSR for coordinate endpoints — candidate radius capped at 20 km, targets capped at 2400 nearest. This is what makes RAPTOR pick boarding stops using real walking times. (2) Station endpoints (stop IDs in fromPlace/toPlace) take their WALK offsets straight from the imported full Valhalla matrix — zero HTTP per query; matrix reach (2 h) is the limit. (3) Two-tier transfer table: the fork-only URL param `koraFullTransfers=true` (invisible to the generated API) routes the search onto the full 2-h table; without it, foot queries with `useRoutedTransfers=true` search the capped default table. Falls back capped→foot→0 on empty tables (pre-two-tier index data). |
| `src/osr/street_routing.cc` | Patched copy. `street_routing()` intercepts `kFoot` and builds the WALK leg (duration, distance, polyline) from Valhalla `/route`. Covers direct walk itineraries, pre/post-transit legs, and transfer legs — all three flow through this function. When the journey has already fixed both leg times, those stay authoritative (they derive from Valhalla numbers anyway) and Valhalla supplies the geometry. The leg also carries `elevationUp` / `elevationDown` (metres, noise-filtered) from the same Valhalla response. Walking-speed support (`routing-options.md`): durations are rescaled by `osr_params.kora_walk_factor_` (the budget converted to base-speed terms for the Valhalla call) so the coordinate-keyed cache stays shared across speeds. |
| `openapi.yaml` | Patched copy of the API schema. Adds two optional integer properties to `Leg`: `elevationUp` / `elevationDown` — the walk's ascent / descent in metres, set by `street_routing.cc` on WALK legs from the elevation profile Valhalla samples along the shape. Codegen (`openapi_generate`) turns them into `api::Leg` members, so the app reads them like any other leg field. Deliberately NOT in the Dockerfile's `touch -c` list — see the comment there. |
| `include/motis/osr/parameters.h` | Patched copy. Adds `kora_walk_factor_` to `osr_parameters` — the query-scoped walking-speed multiplier (`kWalkSpeedKmh / requested`) that `routing.cc` sets from the standard `pedestrianSpeed` plan param (m/s, upstream validation range mirrored) and the two Valhalla walking surfaces apply to their durations. Absent/invalid param → 1.0 → bit-identical to today. Transfer times are NOT touched by this factor — the client scales them via `transferTimeFactor`. |
| `src/server.cc` | Patched copy. Probes Valhalla `/status` at server start and exits when unreachable — docker's `restart: unless-stopped` turns that into a wait-for-valhalla loop. Import is NOT probed (it consumes the CSV, not live Valhalla). |
| `deps/nigiri/src/routing/raptor/optimize_footpaths.cc` | Patched copy (first overlay inside a dependency checkout; nigiri is pinned transitively via MOTIS's `.pkg`). nigiri's post-reconstruction transfer optimizer slides each transfer to the lowest-penalty stop pair, but upstream only scans footpath pairs — and no footpath table carries self-entries — so a zero-walk same-platform transfer was invisible and a longer street walk would win. The patch adds same-stop re-boarding as a candidate (feasibility per the stop's change time, same rule the RAPTOR search applies) and scores the incumbent transfer at its actual transfer stops instead of the journey endpoints. Only effective for queries with `useRoutedTransfers=true`, which the app always sends. See `transfer-point-optimization.md`. |
| `deps/nigiri/include/nigiri/routing/query.h`, `deps/nigiri/include/nigiri/routing/journey.h`, `deps/nigiri/include/nigiri/routing/search.h`, `deps/nigiri/include/nigiri/routing/kora_alternatives.h` (new), `deps/nigiri/src/routing/raptor/reconstruct.cc`, `deps/nigiri/src/routing/raptor/pong.cc` | Patched copies (+ one new header). **ε-alternates** (`near-optimal-endpoint-alternatives.md`): RAPTOR holds a per-stop optimum but only ever reads out the single best (stop arrival + egress offset) combination per Pareto point, silently collapsing equal-or-near journeys over other egress/access stops. `kora_alternatives.h` (new, shared) derives candidate anchors from `round_times[k][stop] + offset` (an upper bound valid for both possible writers of the entry) for every destination offset within the slack, reconstructs each against the live algo state (infeasible guesses throw and are dropped, never emitted), snaps the endpoint walk leg back to the vehicle's real arrival, and controls duplicates purely via quay-blind transit fingerprints (same vehicles between same parent stations) — so different lines at different platforms of one station both survive while quay siblings of the same vehicle collapse. Alternates additionally pass sensibility filters (no same-line re-board — compared by line name, so opposite directions count; no revisiting a parent station already passed; ride-through redundancy: an alternate whose endpoint station is served no later by a kept journey's endpoint vehicle ridden past its own exit is a disguised duplicate) plus endpoint-station dominance (equal-or-worse in both endpoint time and walk vs a same-remainder journey → dropped), and extraction runs once per Pareto point rather than once per PONG cursor rediscovery (~2.5× alternates-off search time). A final fingerprint/dominance/redundancy pass against the primaries runs in each driver. `KORA_ALT_DEBUG=1` (serve-time env) logs per-candidate extraction outcomes to stderr. **Transfer factors < 1** (`routing-options.md`: fast-walker speed tiers, "daring" safety mode): upstream floors `transferTimeFactor` at 1.0 in `search.h` (the precomputed lower-bound graph uses base transfer times, so a smaller factor would make real costs undercut the pruning bounds); the fork floors at 0.2 instead and multiplies the per-query dijkstra lower bounds by the same factor, which keeps them valid (`scaled_cost >= factor * base_cost >= factor * base_lb`) at the price of weaker pruning on such queries. PONG bypasses `search<>::init()`, so `pong.cc` repeats the sanitize + lb scaling for its own ping/pong bound vectors. Hooked into BOTH search drivers: `search.h` in the classic rRAPTOR per-start-time loop, and `pong.cc` after the forward ping pass — MOTIS's default driver is PONG, whose ping state holds the egress side and whose journeys already carry the final arrival/transfers (only the departure gets tightened by the pong pass). `reconstruct.cc` anchors an alternate's dest leg at its synthesized `dest_time_` and forces its egress stop (`journey::kora_alt_egress_`, added in `journey.h`). `query.h` carries the knobs, set from the fork-only URL params `alternativesEpsilon` (seconds, 0 = off = upstream behavior) and `alternativesMax` (cap per Pareto point) parsed in `routing.cc`, which also appends the alternates (`search_state.alternatives_`) to the response as ordinary itineraries. Intermodal-destination, via-free queries only; the search phase itself is untouched. |

| `deps/nigiri/include/nigiri/routing/kora_walk_points.h` (new), `deps/nigiri/include/nigiri/routing/limits.h`, `deps/nigiri/include/nigiri/routing/raptor/raptor.h`, `deps/nigiri/include/nigiri/routing/transfer_time_settings.h` (+ small additions in `search.h`, `pong.cc`, `reconstruct.cc`, `kora_alternatives.h`) | **Walk-weighted transfer points** (`fix-long-transfer-walks.md`): RAPTOR's discrete Pareto criterion counts POINTS instead of boardings — a boarding costs 1 point plus a class delta for the walk that led to it (`kora_walk_points.h`: ≤5 min +0, ≤10 +1, ≤20 +2, ≤40 +4, >40 +9). Fixes the "walk Oensingen→Balsthal instead of riding the S22" pathology: at equal arrival, the ride variant now has fewer points and dominates in-search. Mechanics: the round index becomes a point level; weighted footpaths / intermodal egress write their target `delta` rounds ahead (per-future-round `pending_marks_` make those labels boardable when their round comes; at consume time a pending label already strictly beaten at a lower level is dropped instead of boarded — without this the level smear costs ~7× route-scan work on sparse rural queries), access walks seed at their class level (`add_start` third arg, fed from the start offsets in `search.h`/`pong.cc`), and reconstruction walks the levels back down consuming the same deltas (loop ends at a start-seed level match, not after a fixed ride count). `kMaxTransfers` (limits.h) becomes the journey points cap (45; upstream 14) — the label arrays grow accordingly (~3×, ≈30 MB per raptor state at CH size). The same-stop transfer buffer is not a walk and always costs the plain 1 point. PING/PONG stay level-symmetric because every walk's delta attaches to the same footpath/offset in both directions; the ε-alternates extraction scans all levels per egress stop and stamps each candidate with its own level. API `transfers` is unaffected (MOTIS derives it from legs). Caveats: a client-sent `maxTransfers` now caps points, not boardings (the app never sends it); the one-to-all endpoint counts levels in its transfer dimension. **Minimize walking** (`routing-options.md` § Minimize walking): the fork-only URL param `koraWalkPoints=minwalk` switches the walk class table per query to the steeper 0/2/3/6/6 variant so walking-light journeys survive as their own Pareto points; the selector rides on `transfer_time_settings::kora_minwalk_points_` (vendored `transfer_time_settings.h`) because the tts already reaches every consumer, and every `kora_walk_delta` call site takes the flag explicitly (no default argument — a missed site would let search and reconstruction disagree on levels). |

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

**Expected log noise:** the server log fills with
`[VERIFY FAIL] intermodal destination reconstruction failed …` lines
(hundreds per alternates-enabled query). These are the ε-alternates
extraction probing candidates and discarding infeasible ones — caught
by design, never lost journeys (zero such lines with
`alternativesEpsilon=0`). A rare
`[VERIFY FAIL] no pong for transfers=…` means the PONG driver hit a
ping/pong level mismatch and the query silently re-ran on rRAPTOR —
correct results, roughly doubled latency for that one request.

## What to check when bumping MOTIS_REF

`MOTIS_REF` in the Dockerfile is pinned because three overlays are
full-file patched copies — building against a moved upstream would
silently revert upstream's changes in those files. To bump:

1. `git diff <old-pin>..<new-pin> -- openapi.yaml src/endpoints/routing.cc src/osr/street_routing.cc src/server.cc src/compute_footpaths.cc include/motis/compute_footpaths.h`
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
3b. Re-save `openapi.yaml` after re-applying its patch so its mtime lands
   after the freshly rebuilt baseline layer — otherwise codegen keeps the
   upstream schema and `Leg.elevationUp` / `elevationDown` vanish
   silently (the build still succeeds; `street_routing.cc` fails to
   compile, which is the intended tripwire).
4. Update `ARG MOTIS_REF` and rebuild.
