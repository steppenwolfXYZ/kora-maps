# Project Overview

Car-Free Map — a MapLibre GL map style focused on walkability and car-free travel.

**Stack:** SvelteKit frontend (`src/routes/`), MapLibre GL JS (`src/routes/Map.svelte`), style generated from `scripts/config.yaml` → `scripts/generate_style.py` → `static/style.json`.

**Key files:**
- `scripts/config.yaml` — all design tokens (colors, opacities, zoom levels, widths). Edit here, rerun generator, reload browser.
- `scripts/generate_style.py` — generates MapLibre style JSON from config. All layer logic lives here.
- `static/style.json` — generated output, committed and served directly.

**Tile source:** OpenMapTiles schema (`openmaptiles` source, OpenFreeMap tiles).

---

# Style Architecture

`generate_style.py` builds a MapLibre style JSON via discrete `build_*` functions called in this order in `generate_style()`:

1. `build_background_layer`
2. `build_landuse_layers`
3. `build_water_layers`
4. `build_building_layers`
5. `build_rail_layers(modes=["tunnel", "normal"])` — rail NOT on bridges
6. `build_road_layers(modes=["tunnel", "normal"])` — roads NOT on bridges
7. `build_path_layers(modes=["tunnel", "normal"])` — paths NOT on bridges
8. `build_bridge_deck_layer` — solid gray deck for all bridge transportation
9. `build_rail_layers(modes=["bridge"])` — rail ON bridges (above deck)
10. `build_road_layers(modes=["bridge"])` — roads ON bridges (above deck)
11. `build_path_layers(modes=["bridge"])` — paths ON bridges (above deck)
12. `build_border_layers`
13. `build_label_layers`

**Why this order:** Bridge deck must render between normal-mode and bridge-mode features so it appears above roads passing below the bridge but below roads on the bridge.

**Road class constants:**
- `MOTORWAY_CLASSES = ["motorway", "trunk"]`
- `MAIN_ROAD_CLASSES = ["primary", "secondary"]`
- `RAIL_CLASSES = ["rail", "transit"]`
- `FERRY_CLASSES = ["ferry"]`
- `WALKABLE_EXCLUDE = MOTORWAY_CLASSES + MAIN_ROAD_CLASSES + RAIL_CLASSES + FERRY_CLASSES`
- `PATH_CLASSES = ["path"]`

**Transit stop architecture:** All stop features (dots, pills, connectors) are `LineString` features in a single PMTile source (`tl_stop_pills.pmtiles`). Dots are `[pos, pos]` zero-length lines rendered as circles via `line-cap: round`. Layer paint order: dot-casing → connector-casing → pill-casing → dot-fill → pill-fill → connector-fill.

---

# Transit Color Scheme

| Mode | Color | Notes |
|---|---|---|
| Train (all rail) | red | one color for all types; speed shown via thickness |
| Tram | purple | |
| City bus | blue | |
| Ferry | blue | same as city bus |
| Long-distance bus | turquoise | |
| Mountain railway | light yellow `#ffe566` | funicular, cable car, rack railway, gondola — fixed color, no freq variance |

**Speed and frequency encoding:**
- Line thickness = speed (faster = thicker)
- Color saturation = frequency (higher freq = more saturated)

---

# Notable config.yaml Keys

These were added during basemap v1 (previously hardcoded in `generate_style.py`):
- `palette.rail` — rail line color (was hardcoded `#ffffff`)
- `palette.rail_opacity` — rail line opacity (was hardcoded `0.5`)
- `palette.bridge_deck_opacity` — opacity for the bridge deck shape
