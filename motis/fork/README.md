# MOTIS fork — Valhalla footpath matrix

Kora Maps rebuilds MOTIS with a single source file replaced,
[`src/compute_footpaths.cc`](src/compute_footpaths.cc). The fork
swaps MOTIS's OSR-based stop-to-stop transfer-table generator for a
loader that reads a precomputed CSV matrix produced by Valhalla — see
`.claude/concepts/valhalla-pedestrian-router.md` for the design.

Everything else (RAPTOR, transfers.txt handling, dominance rules,
result shape) is upstream MOTIS, verbatim. Query-time WALK legs still
run through MOTIS's OSR router; the app rewrites the surfaced times
and geometries via Valhalla before showing them (see
`src/lib/routing/valhalla.ts`).

## Build

```
docker build -t koramaps/motis:footpath-matrix -f Dockerfile .
```

Overriding the MOTIS ref (defaults to `master`):

```
docker build --build-arg MOTIS_REF=v0.x.y -t koramaps/motis:footpath-matrix -f Dockerfile .
```

First build is ~30-45 min on Apple Silicon (Boost / nigiri / osr all
compile from source). Subsequent builds reuse the `--mount=type=cache`
buildcache layer.

## Run

`motis/docker-compose.yml` references `koramaps/motis:footpath-matrix`.
The env var `KORA_FOOTPATH_MATRIX_PATH` inside the container points
the fork at the matrix file; the compose file wires it to
`/data/data/valhalla_footpath_matrix.csv`. If the env var is unset or
the file cannot be opened, import aborts — the concept forbids a
silent fallback to the OSR walker (mixing Valhalla-quality and OSR-
quality times in one result set is worse than either alone).

## What to check when bumping MOTIS_REF

The fork replaces one file, so the risk surface is:

1. **`compute_footpaths` signature.** If upstream changes the
   parameter list (see `include/motis/compute_footpaths.h`), the
   fork's function definition must match.
2. **`nigiri::footpath` / `nigiri::vector_map` API.** The fork
   constructs `footpath{target_idx, duration_t{minutes}}` and stores
   them in `tt.locations_.footpaths_out_[profile_idx_]`. If nigiri
   restructures these, the fork must follow.
3. **`build_lb_graph` signature.** The fork calls it for both
   directions after populating the transfer table.
4. **`osr_footpath_` config gating.** The upstream task at
   `src/import.cc:505` still needs `c.osr_footpath_` = true to fire
   `compute_footpaths` at all; `motis/config.yml` sets it.

If any of these changed upstream, adjust `src/compute_footpaths.cc`
and rebuild.
