# maplibre-sveltekit

A minimal SvelteKit + MapLibre GL JS foundation.

## Setup

```bash
npm install
```

## Add your style

Drop your `style.json` into `static/style.json` (replacing the placeholder).  
The app reads `center` and `zoom` directly from the style file.

## Dev server

```bash
npm run dev
```

Open [http://localhost:5173](http://localhost:5173).

## Build

```bash
npm run build
npm run preview   # preview the production build locally
```

## Project layout

```
src/
├── app.css              # global reset
├── app.html             # HTML shell
├── lib/
│   └── Map.svelte       # MapLibre wrapper component
└── routes/
    ├── +layout.svelte   # imports app.css
    ├── +page.ts         # loads /style.json via fetch
    └── +page.svelte     # renders <Map>
static/
└── style.json           # ← replace with your own style
```

## Extending

- **Add a layer at runtime** — call `map.addLayer(…)` inside the `map.on('load', …)` handler in `Map.svelte`, or expose the `map` instance via a Svelte store for use elsewhere.
- **Switch styles** — pass a new `style` prop; add a `map.setStyle(newStyle)` reactive statement.
- **Markers / popups** — create `maplibregl.Marker` / `maplibregl.Popup` instances inside `onMount` after the map loads.
