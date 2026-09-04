# Kora fork of Valhalla — bicycle costing

Kora-owned bicycle weighting inside the same Valhalla instance that serves
pedestrian routing and the transit stack's walking. Requirements and the
model's rationale: `.claude/concepts/bicycle-costing-fork.md`. Everything
that is not bicycle costing is upstream, byte for byte.

## What is overlaid

| File | Kind | Purpose |
|---|---|---|
| `src/sif/bicyclecost.cc` | full-file overlay of upstream's copy at `VALHALLA_REF` | The three-tier quality model (great / fine / bad), official-route bonus, stairs pricing with up/down asymmetry, the crossing rule, and the `exclude_steps` request option. All tunables sit in the `kora` namespace at the top of the file — the one place to change numbers. Every kora-specific line is marked `kora fork:`. |
| `patches/options-proto-exclude-steps.patch` | `git apply` patch on `proto/descriptors/options.proto` | Adds `bool exclude_steps = 98` to `Costing.Options`. Field 98 must stay unused upstream — check on a bump. |

Request API: everything upstream accepts still parses. `use_roads` is
accepted but inert (the tier model replaces what it scaled). New:
`costing_options.bicycle.exclude_steps` (bool, default `false`) — the
avoid-stairs toggle; stairs edges are refused outright instead of priced.

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

1. `git diff <old>..<new> -- src/sif/bicyclecost.cc proto/descriptors/options.proto docker/Dockerfile docker/Dockerfile-scripted scripts/install-linux-deps.sh`
2. Re-copy upstream's `bicyclecost.cc`, re-apply the `kora fork:` blocks
   (the tier/crossing helpers in the anonymous namespace, the `kora`
   constants, `exclude_steps_`, and the three replaced methods —
   `EdgeCost`, `TransitionCost`, `TransitionCostReverse`).
3. Check `git apply --check patches/*.patch` against the new proto; confirm
   field number 98 is still free, renumber if not (and update the parser
   line — the JSON key stays `exclude_steps`).
4. Mirror any change in upstream's two Dockerfiles into ours: runtime
   package list, env defaults, the `preserve=` binary list, the locale step.
5. Update `ARG VALHALLA_REF`, rebuild, run `setup_routing.sh` (it wipes the
   tiles on the version change), then `--force-matrix`, then the MOTIS
   import. Re-run the benchmark set before judging anything.
