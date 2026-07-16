# Car-Free Map

A MapLibre GL map focused on walkability and car-free travel (Switzerland).

## Run

```bash
npm install
npm run dev
```

Open [http://localhost:5173](http://localhost:5173).

## Regenerate the map style

```bash
python3 scripts/generate_style.py
```

Design tokens live in `scripts/config.yaml`; the transit data pipeline lives
in `scripts/transit/` (see `./scripts/rebuild_transit.sh`).
