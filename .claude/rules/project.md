# Project Overview

Car-Free Map — a MapLibre GL map style focused on walkability and car-free travel.

**Stack:** SvelteKit frontend (`src/routes/`), MapLibre GL JS (`src/routes/Map.svelte`), style generated from `scripts/config.yaml` → `scripts/generate_style.py` → `static/map-assets/style.json`.

**Key files:**
- `scripts/config.yaml` — all design tokens (colors, opacities, zoom levels, widths). Edit here, rerun generator, reload browser.
- `scripts/generate_style.py` — generates MapLibre style JSON from config. Thin driver; the layer-building code lives in the `scripts/style/` package.
- `static/map-assets/style.json` — generated output, gitignored, served at `/map-assets/style.json`. Alongside it live the `tl_*.pmtiles` tile bundles, referenced from the style as `pmtiles:///map-assets/tl_*.pmtiles`.

**Tile source:** OpenMapTiles schema (`openmaptiles` source, OpenFreeMap tiles).

**Basemap design language:**
- Color philosophy: green = nature (background, default), warm yellow/brown = urban human spaces, gray = dead/uninteresting (industry, motorways).
- Road hierarchy: motorway/trunk are "dead space" (gray, dashed when zoomed out, real-width fill when close); primary/secondary gray solid, not inviting; walkable streets carry the walkability color gradient (gray→yellow→orange); paths/cycleways are separate thin brown-orange lines from z14. Real-width streets from z15+ via meter-to-pixel conversion.
- Rendering constraints: `sprite: ""` (no sprite source — never use `icon-image`); fonts are Saira Regular/Bold/Italic/SemiBold/ExtraBold (DIN-inspired grotesque by Omnibus-Type, instantiated from the Google Fonts variable font), self-hosted as pre-built glyph PBFs under `static/map-assets/fonts/`. The color-dot indicator layer is the sole exception — it renders `●` (U+25CF) via `Noto Sans Regular`, which Saira lacks; that folder holds OpenFreeMap's pre-composited "Noto Sans Regular" PBFs (23-font composite that provides the black-circle glyph). Glyphs URL in `config.yaml` points at the local path, not an external server.

---

# Style Architecture

`generate_style.py` builds a MapLibre style JSON via discrete `build_*` functions called in this order in `generate_style()`:

1. `build_background_layer`
2. `build_hillshade_layer` — terrain relief, directly above background so it only shows through on bare land; below everything else
3. `build_landuse_layers`
4. `build_water_layers`
5. `build_building_layers`
6. `build_rail_layers(modes=["tunnel", "normal"])` — rail NOT on bridges
7. `build_road_layers(modes=["tunnel", "normal"])` — roads NOT on bridges
8. `build_path_layers(modes=["tunnel", "normal"])` — paths NOT on bridges
9. `build_bridge_deck_layer` — solid gray deck for all bridge transportation
10. `build_rail_layers(modes=["bridge"])` — rail ON bridges (above deck)
11. `build_road_layers(modes=["bridge"])` — roads ON bridges (above deck)
12. `build_path_layers(modes=["bridge"])` — paths ON bridges (above deck)
13. `build_border_layers`
14. `build_label_layers`

**Why this order:** Bridge deck must render between normal-mode and bridge-mode features so it appears above roads passing below the bridge but below roads on the bridge.

**Road class constants:**
- `MOTORWAY_CLASSES = ["motorway", "trunk"]`
- `MAIN_ROAD_CLASSES = ["primary", "secondary"]`
- `RAIL_CLASSES = ["rail", "transit"]`
- `FERRY_CLASSES = ["ferry"]`
- `WALKABLE_EXCLUDE = MOTORWAY_CLASSES + MAIN_ROAD_CLASSES + RAIL_CLASSES + FERRY_CLASSES`
- `PATH_CLASSES = ["path"]`

**Transit stop architecture:** All stop features (dots, pills, connectors) are `LineString` features in a single PMTile source (`tl_stop_pills.pmtiles`). Dots are `[pos, pos]` zero-length lines rendered as circles via `line-cap: round`. Layer paint order: dot-casing → connector-casing → pill-casing → dot-fill → pill-fill → connector-fill.

**View modes:** The map has two views, `standard` (place labels visible, all stop symbology hidden) and `transit-focus` (place labels hidden, stops visible), toggled client-side in `src/lib/Map.svelte` via layer visibility — one shared `style.json`, no regeneration. Transit lines render identically in both. See `view-modes.md`. Shipped default is `standard`; the code currently carries a `DEFAULT_VIEW` dev override to `transit-focus` during stop-rendering development.

**Terrain:** A `terrain` raster-DEM source (Mapterhorn free API, Terrarium-encoded WebP, tileSize 512, maxzoom 12) feeds two features (see `hillshade-and-contours.md`). Hillshade is always on, generated into the style (tokens under `terrain:` in `config.yaml`; the layer type has no opacity property, so `terrain.hillshade.opacity` is baked into the shadow/highlight colors as alpha). Contour lines are client-side only: `maplibre-contour` in `Map.svelte` builds them from the same DEM tiles, adaptive intervals from z9 (200/1000 m) tightening to z15+ (10/50 m), inserted below the transit block, off by default behind a floating toggle bottom-right. `maplibre-contour` ships a broken `exports` map, so `vite.config.ts` aliases it to its ESM bundle path, and its `DemSource` is constructed lazily because it spawns a Web Worker (crashes SSR at module scope).

---

# Transit Color Scheme

| Mode | Color | Notes |
|---|---|---|
| Train (all rail) | red | one color for all types; speed shown via thickness |
| Tram | purple | |
| City bus | blue | |
| Ferry | blue | same as city bus |
| Long-distance bus | turquoise | |
| Mountain railway | light yellow `#ffe566` | funicular, cable car, gondola (GTFS route_type 5/6/7) plus rack-rail operators in the `mountain_agency_ids` whitelist (WAB, JB, GGB, RB, PB, BRB, MG, DFB, BOB-spb, VerAlp) — fixed color, no freq variance. |

**Speed and frequency encoding:**
- Line thickness = speed (faster = thicker)
- Color saturation = frequency (higher freq = more saturated)

---

# Notable config.yaml Keys

These were added during basemap v1 (previously hardcoded in `generate_style.py`):
- `palette.rail` — rail line color (was hardcoded `#ffffff`)
- `palette.rail_opacity` — rail line opacity (was hardcoded `0.5`)
- `palette.bridge_deck_opacity` — opacity for the bridge deck shape
