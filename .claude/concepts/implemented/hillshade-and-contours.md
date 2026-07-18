# Hillshade and Contour Lines

## Problem

The basemap has no terrain relief. Switzerland without hillshade reads as flat green — the Alps, valleys, and the geography that shapes settlement patterns are invisible. Contour lines add precise elevation reading for terrain-focused users (hikers, cyclists).

## Requirements

### Terrain source

- A single raster-DEM source, id `terrain`, referencing Mapterhorn: `https://tiles.mapterhorn.com/{z}/{x}/{y}.webp`. Encoding `terrarium`, tile size 512.

### Hillshade (always on)

- One `hillshade` layer, id `hillshade`, drawn from the `terrain` source.
- Layer order: **directly above the background layer, below all landuse.** Terrain shading shows through only where the basemap is bare background; landuse, water, roads, buildings, and labels are unaffected.
- Illumination direction, exaggeration, shadow/highlight colors, and overall opacity are tokens in `config.yaml` under a new `terrain.hillshade` section.
- Renders at all zooms where DEM tiles are available.

### Contour lines (togglable)

- Generated client-side from the same `terrain` DEM (via `maplibre-contour`). No pipeline step, no PMTiles bake.
- **OFF by default.** Toggled by a single floating button in the bottom-right corner, directly above the attribution (i). Icon + tooltip; no text label.
- Contour lines render **below the transit block** (transit lines, stops, station backdrops) and above the basemap and roads, so transit symbology always draws over them.
- **Adaptive interval by zoom** (metres):

  | Zoom  | Minor | Major |
  |-------|-------|-------|
  | <9    | –     | –     |
  | 9–10  | 200   | 1000  |
  | 11–12 | 100   | 500   |
  | 13–14 | 50    | 250   |
  | 15+   | 10    | 50    |

- Major contours are thicker and labeled with elevation in metres (Saira, matches other map labels) from z12. Minor contours are unlabeled.
- Colors: brown tokens fitting the map's warm palette. Under `terrain.contours`: separate minor/major color, width, opacity, label color, label halo, label size.

### New config keys

Under a new `terrain:` root key in `config.yaml`:

- `source.url`, `source.encoding`, `source.tile_size`, `source.max_zoom`
- `hillshade.opacity`, `hillshade.shadow_color`, `hillshade.highlight_color`, `hillshade.exaggeration`, `hillshade.illumination_direction`
- `contours.color_minor`, `contours.color_major`, `contours.width_minor`, `contours.width_major`, `contours.opacity`, `contours.label_color`, `contours.label_halo`, `contours.label_size`

### New dependency

- `maplibre-contour` in `dependencies`.

## Constraints

- Hillshade must sit **above background, below landuse** — never above roads, buildings, or labels. Relief is a background-tier effect only.
- Mapterhorn only serves Terrarium; the encoding token exists in config but any URL swap must be to another Terrarium source or the config token must change too.
- Contour intervals are strict integer metres (10, 50, 100, 250, 500). No fractional or imperial units.
- Contour toggle state is not persisted across reloads (MVP). URL-param or localStorage persistence is future work.
- 3D terrain (map tilt/pitch driven by the DEM) is **out of scope.** Only hillshade + contours consume the source. Adding `terrain: { source: 'terrain' }` at the style level would enable pitch — do not do it as part of this change.
