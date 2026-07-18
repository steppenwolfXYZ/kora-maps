# Car-Free Map

A MapLibre GL map focused on walkability and car-free travel (Switzerland).

## First-time setup

```bash
npm install
./scripts/rebuild_transit.sh
```

The rebuild script builds the map's glyph PBFs (Saira + Noto Sans Regular for
the color-dot indicator) as its step 0, then runs the transit pipeline (GTFS
download → pfaedle routing → stop extraction → pmtiles). Step 0 is skipped on
any subsequent `--start N` invocation.

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
