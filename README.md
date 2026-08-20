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

Routing backend (MOTIS + Valhalla): `./scripts/setup_routing.sh --help`.

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
