# Kora fork of Valhalla — bicycle costing

Kora-owned bicycle weighting inside the same Valhalla instance that serves
pedestrian routing and the transit stack's walking. Requirements and the
model's rationale: `.claude/concepts/bicycle-costing-fork.md`. Everything
that is not bicycle costing is upstream, byte for byte.

## What is overlaid

| File | Kind | Purpose |
|---|---|---|
| `src/sif/bicyclecost.cc` | full-file overlay of upstream's copy at `VALHALLA_REF` | The whole Kora weighting model: rider-power speed model (the request's flat speed sets the rider's watts, every grade's speed follows — honest hill time; `ebike` / `sbike` add a motor with a 25 / 45 km/h assist cap), quality tiers with speed-priced bare roads, painted-lane / sharrow factors with lane steps on 50 km/h+ through roads, per-metre service-road factor (no entry fee), DEM-artifact grade cap on through roads, official-route bonus, zero destination-only and service-road entry penalties, ferry / car-shuttle pricing (service speed + boarding wait + high on-board cost), pushed-bike access (grade-aware pace, per-section allowance, sac_scale / impassable-surface guards), turn restrictions obeyed only where the maneuver crosses a road posted above 30 km/h, stairs as hauling time + committing fees, per-turn cost, deviation penalty, junction-based lane-scaled crossing rule (turn-direction shares, multi-lane T-junctions), the `exclude_steps` request option, and the `avoidance_scale` / `bonus_scale` ruler scales (bicycle-route-options.md). All tunables sit in the `kora` namespace at the top of the file — the one place to change numbers; every kora-specific line is marked `kora fork:`. Requirements record: `bicycle-costing-fork.md`. |
| `src/thor/triplegbuilder.cc` | full-file overlay of upstream's copy at `VALHALLA_REF` | Pushed-bike sections are reported as pedestrian `travel_mode` maneuvers (upstream already does this for dismount + steps; the overlay extends the condition to not-ridable-but-walkable edges). Maneuvers never merge across a mode change, so the client gets exact shape ranges to draw dotted. |
| `patches/options-proto-kora-bicycle.patch` | `git apply` patch on `proto/descriptors/options.proto` | Adds `bool exclude_steps = 98`, `float avoidance_scale = 99`, `float bonus_scale = 100`, `string surface_profile = 101` and `string route_character = 102` to `Costing.Options` — all in `oneof` wrappers, as upstream does: the `JSON_PBF_*` parser macros check `has_<field>_case()`, which only a oneof member has. Fields 98–102 must stay unused upstream — check on a bump. |

Request API: everything upstream accepts still parses. `use_roads` is
accepted but inert (the tier model replaces what it scaled). Kora
additions under `costing_options.bicycle`:

| Option | Default | Meaning |
|---|---|---|
| `exclude_steps` (bool) | `false` | the avoid-stairs toggle; stairs edges are refused outright instead of priced |
| `route_character` | (empty) | the fast ↔ nice ruler stop, `road` / `fast` / `balanced` / `relaxed` / `quiet`: one bundle per stop in the file's Route character block — traffic-penalty scale, great-tier scale, official-route factor (0.92 Balanced, 0.86 Relaxed, 0.74 Quiet), the quiet boost for narrow unclassified roads / tracks / bike-allowed paths (0.95 Balanced, = great tier at Relaxed / Quiet), the surface profile, the cost-only surface relief (50 % Relaxed, 80 % Quiet), and the share of the turn penalty charged when turning onto a cycle-route edge (25 % Relaxed, 0 Quiet). The client sends this and nothing else |
| `avoidance_scale` (0–5) | `1` | fallback when no `route_character` is sent: multiplies the excess over 1 of every traffic penalty. `0` ignores traffic entirely; the `use_sidepath` factor is never scaled (a signed cycle path is mandatory) |
| `bonus_scale` (0–3) | `1` | fallback when no `route_character` is sent: multiplies the discount of the great tier and the official-route bonus |
| `surface_profile` | `fast` | fallback when no `route_character` is sent: surface tables for the hybrid-based types (bicycle / ebike / sbike): `fast` = upstream's hybrid speeds, surcharge from dirt; `balanced` = same speeds, milder surcharge; `leisure` = gentler speeds on compacted / dirt / gravel (0.9 / 0.8 / 0.7), surcharge on path only. The road type ignores it |
| `bicycle_type` | `hybrid` | upstream's `road` / `cross` / `hybrid` / `mountain` plus `ebike` (25 km/h assist cap, moderate motor) and `sbike` (45 km/h, strong motor; rides with the traffic of 50 zones, so its bare-road curve prices posted 50 at 1.1 and paint on it as plateau). E-bike types ignore `cycling_speed`: the rider pedals at a fixed Normal effort and the flat speed is the cap |
| (turns) | | the flat per-turn seconds (right 3, left 4, U-turn 8 at 25 km/h) scale with the flat speed: leisurely 0.5×, normal 0.75×, fast / e-bike 1×, professional and fast e-bike 1.25× — time and cost. A turn INTO a cycle-route edge pays only the character's share of the whole turn penalty: 25 % at Relaxed, nothing at Quiet |
| `cycling_speed` | per type | pedal types: the rider's flat speed, which sets their sustained power — the whole grade→speed curve follows (see the Hills block in the file) |

The client's mapping of its bike type / pace / roads stops onto these
lives in `src/lib/routing/optionParams.ts` (`bikeCostingOptions`).

## Build

```
docker build -t koramaps/valhalla:bicycle-costing -f valhalla/fork/Dockerfile valhalla/fork
```

`scripts/routing/setup_routing.sh` step 2b does this for you and rebuilds whenever
anything under `valhalla/fork/` is newer than the last build. First build
~30–60 min (full upstream compile, cached per `VALHALLA_REF`); a costing-only
change recompiles one translation unit plus the link (~minutes); a proto
change regenerates `options.pb.h`, which most of the tree includes, so it
costs a near-full rebuild. `--build-arg CONCURRENCY=N` sets the make
parallelism (default 4 — several units peak at 2–3 GB, Docker Desktop's
memory allowance is the constraint on the Mac).

The result is a **drop-in for `ghcr.io/valhalla/valhalla-scripted:<VALHALLA_REF>`**:
the runner stage is upstream's `Dockerfile-scripted` fed with our patched
build — same entrypoint, same environment interface
(`use_tiles_ignore_pbf`, `build_elevation`, `server_threads`, …), same
`/custom_files` layout. `valhalla/docker-compose.yml` and
`docker-compose.prod.yml` reference this tag; nothing else changes.
`valhalla_service --version` and `/usr/local/valhalla_version` carry the
`kora-bicycle` marker so a stock container is recognisable.

## Iterating on the costing

Costing is query-time. The loop is: edit the `kora` constants (or the model)
→ rebuild the image → `cd valhalla && docker compose up -d valhalla` (recreates
the container from the new image; tiles untouched) → run the benchmark:

```
python3 scripts/bicycle_benchmark.py            # every pair in bicycle_benchmark.yaml
python3 scripts/bicycle_benchmark.py --only bern-eichmatt-viktoria --exclude-steps
python3 scripts/bicycle_benchmark.py --roads quiet --bike-type sbike   # a ruler stop (route_character) / e-bike
```

A change ships only when no pair regresses. Every bad route found in hand
testing becomes a new pair in `bicycle_benchmark.yaml` before it is fixed.

## Version pin

`ARG VALHALLA_REF` in the Dockerfile is the single version string for image,
tiles and footpath matrix. `setup_routing.sh` reads it and keys its
tile-staleness check on it (`valhalla/data/.tiles_valhalla_version`), so a
fork iteration never triggers a tile rebuild while a real version bump does.
The tiles were built with 3.8.3; the pin is 3.8.3.

## Deploy

`scripts/deploy/deploy_valhalla.sh` ships the image the way `deploy_motis.sh`
does (`docker save | ssh docker load`, no registry): default = image +
compose from the dev Mac (arm64), `--data-only` = tiles only from the data
machine (its amd64 image must never reach the arm64 server; the script
refuses a non-arm64 image), `--with-data` = both. `update_map.sh` uses
`--data-only`.

## What to check when bumping VALHALLA_REF

A bump means a tile rebuild AND a footpath-matrix rebuild (the graph
changes), plus re-applying the overlay:

1. `git diff <old>..<new> -- src/sif/bicyclecost.cc src/thor/triplegbuilder.cc proto/descriptors/options.proto docker/Dockerfile docker/Dockerfile-scripted scripts/install-linux-deps.sh`
2. Re-copy upstream's `bicyclecost.cc` and `triplegbuilder.cc`, re-apply
   the `kora fork:` blocks (bicyclecost: the tier/crossing/pushed helpers
   in the anonymous namespace, the rider-power solver, the `kora`
   constants, the option members (`exclude_steps_`, `avoidance_scale_`,
   the `TierWeights` / route-character selection, `route_bonus_`,
   `surface_relief_`, `turn_scale_`, `surface_cost_factor_`, the
   `ride_speed_kph_` table built in the constructor),
   `AStarCostFactor`, and the three replaced methods — `EdgeCost`,
   `TransitionCost`, `TransitionCostReverse`; triplegbuilder: the
   pedestrian-mode override condition for pushed edges).
3. Check `git apply --check patches/*.patch` against the new proto; confirm
   field numbers 98–102 are still free, renumber if not (the parser lines
   and the JSON keys stay as they are).
4. Mirror any change in upstream's two Dockerfiles into ours: runtime
   package list, env defaults, the `preserve=` binary list, the locale step.
5. Update `ARG VALHALLA_REF`, rebuild, run `setup_routing.sh` (it wipes the
   tiles on the version change), then `--force-matrix`, then the MOTIS
   import. Re-run the benchmark set before judging anything.
