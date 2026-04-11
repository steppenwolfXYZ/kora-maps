---
name: Project Overview
description: What this project is, its stack, and key files
type: project
---

Car-Free Map — a MapLibre GL map style focused on walkability/car-free travel.

**Stack:** SvelteKit frontend (`src/routes/`), MapLibre GL JS (`src/routes/Map.svelte`), style generated from `scripts/config.yaml` → `scripts/generate_style.py` → `static/style.json`.

**Key files:**
- `scripts/config.yaml` — all design tokens (colors, opacities, zoom levels, widths). Edit here, rerun generator, reload browser.
- `scripts/generate_style.py` — generates MapLibre style JSON from config. All layer logic lives here.
- `static/style.json` — generated output, committed/served directly.

**Tile source:** OpenMapTiles schema (`openmaptiles` source, OpenFreeMap tiles).

**Status:** Basemap v1 complete as of 2026-04-11.
