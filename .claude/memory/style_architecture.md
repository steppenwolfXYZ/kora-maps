---
name: Style Architecture
description: How generate_style.py is structured — layer build functions and their render order
type: project
---

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

**Why this order:** Bridge deck must render between normal-mode and bridge-mode features so it appears above roads passing below the bridge but below roads on the bridge. All three transport functions (`rail`, `road`, `path`) accept a `modes` parameter to support this split.

**Road class constants:**
- `MOTORWAY_CLASSES = ["motorway", "trunk"]`
- `MAIN_ROAD_CLASSES = ["primary", "secondary"]`
- `RAIL_CLASSES = ["rail", "transit"]`
- `FERRY_CLASSES = ["ferry"]`
- `WALKABLE_EXCLUDE = MOTORWAY_CLASSES + MAIN_ROAD_CLASSES + RAIL_CLASSES + FERRY_CLASSES`
- `PATH_CLASSES = ["path"]`

**Bridge deck:** Single `bridge-deck` layer, no per-class variants (they cause hollow/donut artifacts). Width: 1.5px flat zoom 8–13, then exponential-2 meter-based (15m) from zoom 14. No maxzoom.
