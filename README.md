# Car-Free Map

A MapLibre GL map focused on walkability and car-free travel (Switzerland).

## First-time setup

Needs `docker`, `node`/`npm`, `osmium-tool`, `tippecanoe`, and Python 3.10+
with `PyYAML` and `osmium`.

```bash
npm install
./scripts/rebuild_transit.sh
```

No arguments = full build from scratch. Takes a few hours (~12 GB OSM
download).

Later:

```bash
./scripts/rebuild_transit.sh --start 6   # re-enter partway (--help lists steps)
./scripts/rebuild_transit.sh --force     # re-download GTFS, atlas, OSM
```

## Routing backend (MOTIS + Valhalla)

Transit routing runs on a local fork of MOTIS; all walking (transfers,
first/last mile, direct walks) comes from a Valhalla pedestrian router.
Once the map pipeline has run at least once:

```bash
./scripts/setup_routing.sh
```

Idempotent — every step skips when its output is already in place, so
re-run it after any pipeline rebuild. The first run is heavy: it
compiles the MOTIS fork image (~30–60 min), builds Valhalla tiles with
elevation (~20–40 min), and computes the stop-to-stop footpath matrix
(hours on a laptop; `.claude/runbooks/matrix_build_remote.md` covers
running that part on a bigger machine). It then imports MOTIS and
serves routing on `:8080` (Valhalla on `:8002`). Per-step `--force-*`
flags: `./scripts/setup_routing.sh --help`.

## Routine data refresh

New timetable → pipeline → routing → deploy, in one go (data only; app
code ships via git push):

```bash
./scripts/update_map.sh          # --osm to refresh OpenStreetMap as well
```

## Run the dev server

```bash
npm run dev
```

Open [http://localhost:5173](http://localhost:5173).

## Iterate on the map style

```bash
python3 scripts/generate_style.py
```

Design tokens live in `scripts/config.yaml`; the transit data pipeline lives
in `scripts/transit/` (see `./scripts/rebuild_transit.sh --help`).
