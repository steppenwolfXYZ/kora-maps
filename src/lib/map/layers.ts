// Layer-id catalogs for everything Map.svelte and its feature modules
// touch at runtime, plus the view-mode switch that toggles them.
// Single source of truth — no other module re-declares these lists.

import type maplibregl from 'maplibre-gl';

export const TRANSIT_LINE_LAYERS = [
	'transit-mountain', 'transit-regional_bus', 'transit-bus',
	'transit-ferry', 'transit-metro', 'transit-tram', 'transit-train'
];

export const TRANSIT_LINE_CASING_LAYERS = TRANSIT_LINE_LAYERS.map((id) => `${id}-casing`);

export const TRANSIT_STOP_DOT_LAYERS = [
	'transit-stop-fill-transit_stops_rail',
	'transit-stop-fill-transit_stops_tram',
	'transit-stop-fill-transit_stops_regional',
	'transit-stop-fill-transit_stops_bus',
	'transit-stop-fill-transit_stops_rail-far',
	'transit-stop-fill-transit_stops_tram-far',
	'transit-stop-fill-transit_stops_regional-far',
	'transit-stop-fill-transit_stops_bus-far'
];

export const TRANSIT_STOP_LABEL_LAYERS = [
	'transit-stop-label-transit_stops_rail-far-normal',
	'transit-stop-label-transit_stops_rail-far-other',
	'transit-stop-label-transit_stops_tram-far-normal',
	'transit-stop-label-transit_stops_tram-far-other',
	'transit-stop-label-transit_stops_regional-far-normal',
	'transit-stop-label-transit_stops_regional-far-other',
	'transit-stop-label-transit_stops_bus-far-normal',
	'transit-stop-label-transit_stops_bus-far-other',
	'transit-stop-label-pill-normal',
	'transit-stop-label-pill-other'
];

export const TRANSIT_STOP_PILL_LAYERS = [
	'transit-stop-pill-fill',
	'transit-stop-pill-casing',
	'transit-stop-pill-connector',
	'transit-stop-pill-connector-casing',
	'transit-stop-pill-endpoint',
];

// Close-zoom pill-arrow layers (z17+). Any click on the visible arrow
// — the polygon fill, its border, the disc at the number end, the
// number itself, or the destination text — should open the pill-arrow
// popup. Style splits per band (A–E) so exactly one band is visible
// at any given zoom step. Every one of these features carries the
// popup payload (baked via `common` in _writer_render.py), so any hit
// is enough.
const CLOSE_ZOOM_BANDS = ['A', 'B', 'C', 'D', 'E'];
export const TRANSIT_PILL_ARROW_LAYERS = CLOSE_ZOOM_BANDS.flatMap((b) => [
	`close-zoom-pill-arrow-fill-${b}`,
	`close-zoom-pill-arrow-border-${b}`,
	`close-zoom-pill-disc-${b}`,
	`close-zoom-pill-ref-${b}`,
	`close-zoom-pill-dest-${b}`
]);

// Every stop-symbology layer, toggled as one unit by the view switch
// (see .claude/concepts/view-modes.md). Debug layers stay independent.
export const STOP_SYMBOLOGY_LAYERS = [
	...TRANSIT_STOP_DOT_LAYERS,
	...TRANSIT_STOP_PILL_LAYERS,
	...TRANSIT_STOP_LABEL_LAYERS,
	'transit-stop-indicator',
	'close-zoom-station-backdrop',
	'close-zoom-pill-arrow-casing',
	'close-zoom-pill-arrow-fill',
	'close-zoom-pill-arrow-ref',
	'close-zoom-station-label',
];

export const PLACE_LABEL_LAYERS = ['label-place', 'label-state', 'label-country'];

export const DEBUG_STOP_LAYER = 'debug-stop-dot';

export type ViewMode = 'standard' | 'transit-focus';

export function applyViewMode(map: maplibregl.Map, mode: ViewMode) {
	for (const id of STOP_SYMBOLOGY_LAYERS) {
		if (!map.getLayer(id)) continue;
		map.setLayoutProperty(id, 'visibility', mode === 'transit-focus' ? 'visible' : 'none');
	}
	for (const id of PLACE_LABEL_LAYERS) {
		if (!map.getLayer(id)) continue;
		map.setLayoutProperty(id, 'visibility', mode === 'standard' ? 'visible' : 'none');
	}
}

/** Bake a view mode's layer visibilities into the style object before
 * map creation so the first frame already matches — no flash on load. */
export function bakeViewModeVisibility(style: maplibregl.StyleSpecification, mode: ViewMode) {
	for (const layer of style.layers) {
		const l = layer as maplibregl.LayerSpecification & { layout?: object };
		if (STOP_SYMBOLOGY_LAYERS.includes(layer.id)) {
			l.layout = {
				...l.layout,
				visibility: mode === 'transit-focus' ? 'visible' : 'none'
			};
		} else if (PLACE_LABEL_LAYERS.includes(layer.id)) {
			l.layout = {
				...l.layout,
				visibility: mode === 'standard' ? 'visible' : 'none'
			};
		}
	}
}
