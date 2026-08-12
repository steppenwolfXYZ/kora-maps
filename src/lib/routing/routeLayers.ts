import maplibregl from 'maplibre-gl';
import type { RouteGeoJSONResult } from './routeGeoJSON';

// Install / update / remove the MapLibre source, layers and DOM markers
// that render a selected route. All layers filter the single source by
// `role` — the GeoJSON builder tags each feature with one of:
//   walk | transit | connector | disc | passthrough
// (endpoint markers ride on the returned start/goal coords, not on the
// source itself).

export const ROUTE_SOURCE_ID = 'route-overlay';

// Layer ids in paint order (bottom → top). All under `route-` so a
// wildcard sweep can toggle / remove them together.
export const ROUTE_WALK_LAYER = 'route-walk';
export const ROUTE_TRANSIT_CASING_LAYER = 'route-transit-casing';
export const ROUTE_TRANSIT_FILL_LAYER = 'route-transit-fill';
export const ROUTE_CONNECTOR_CASING_LAYER = 'route-connector-casing';
export const ROUTE_CONNECTOR_FILL_LAYER = 'route-connector-fill';
export const ROUTE_PASSTHROUGH_LAYER = 'route-passthrough';
export const ROUTE_DISC_CASING_LAYER = 'route-disc-casing';
export const ROUTE_DISC_FILL_LAYER = 'route-disc-fill';

const ROUTE_LAYER_IDS = [
	ROUTE_WALK_LAYER,
	ROUTE_TRANSIT_CASING_LAYER,
	ROUTE_TRANSIT_FILL_LAYER,
	ROUTE_CONNECTOR_CASING_LAYER,
	ROUTE_CONNECTOR_FILL_LAYER,
	ROUTE_PASSTHROUGH_LAYER,
	ROUTE_DISC_CASING_LAYER,
	ROUTE_DISC_FILL_LAYER
];

const NEUTRAL_DARK = '#1a1a1a';
const NEUTRAL_LIGHT = '#ffffff';

export interface RouteMarkerHandles {
	start: maplibregl.Marker | null;
	goal: maplibregl.Marker | null;
}

/** Add all route layers + source above the topmost transit layer, so the
 * overlay sits above transit lines and stop symbology. Idempotent — call
 * again to update; a subsequent `removeRouteLayers` wipes them clean. */
export function installRouteLayers(
	map: maplibregl.Map,
	geo: RouteGeoJSONResult,
	prevMarkers: RouteMarkerHandles | null
): RouteMarkerHandles {
	if (!map.getSource(ROUTE_SOURCE_ID)) {
		map.addSource(ROUTE_SOURCE_ID, { type: 'geojson', data: geo.features });
	} else {
		(map.getSource(ROUTE_SOURCE_ID) as maplibregl.GeoJSONSource).setData(geo.features);
	}

	// Add above every existing layer so the route reads on top of the
	// map's own transit lines / stops (which are being desaturated by
	// the caller in parallel).
	const commonLine = { 'line-cap': 'round' as const, 'line-join': 'round' as const };

	if (!map.getLayer(ROUTE_WALK_LAYER)) {
		map.addLayer({
			id: ROUTE_WALK_LAYER,
			type: 'line',
			source: ROUTE_SOURCE_ID,
			filter: ['==', ['get', 'role'], 'walk'],
			layout: commonLine,
			paint: {
				'line-color': NEUTRAL_DARK,
				'line-width': ['interpolate', ['linear'], ['zoom'], 8, 3, 14, 5, 18, 8],
				'line-dasharray': [1.4, 1.4],
				'line-opacity': 0.9
			}
		});
	}

	// Widths: sized to dominate the desaturated basemap around them.
	// Roughly 2× the line-detail-view highlight so the selected route
	// reads unambiguously as the primary content of the map.
	if (!map.getLayer(ROUTE_TRANSIT_CASING_LAYER)) {
		map.addLayer({
			id: ROUTE_TRANSIT_CASING_LAYER,
			type: 'line',
			source: ROUTE_SOURCE_ID,
			filter: ['==', ['get', 'role'], 'transit'],
			layout: commonLine,
			paint: {
				'line-color': NEUTRAL_LIGHT,
				'line-width': ['interpolate', ['linear'], ['zoom'], 6, 7, 12, 12, 16, 18]
			}
		});
	}
	if (!map.getLayer(ROUTE_TRANSIT_FILL_LAYER)) {
		map.addLayer({
			id: ROUTE_TRANSIT_FILL_LAYER,
			type: 'line',
			source: ROUTE_SOURCE_ID,
			filter: ['==', ['get', 'role'], 'transit'],
			layout: commonLine,
			paint: {
				'line-color': ['coalesce', ['get', 'color'], '#888888'],
				'line-width': ['interpolate', ['linear'], ['zoom'], 6, 5, 12, 8, 16, 13]
			}
		});
	}

	if (!map.getLayer(ROUTE_CONNECTOR_CASING_LAYER)) {
		map.addLayer({
			id: ROUTE_CONNECTOR_CASING_LAYER,
			type: 'line',
			source: ROUTE_SOURCE_ID,
			filter: ['==', ['get', 'role'], 'connector'],
			layout: commonLine,
			paint: {
				'line-color': NEUTRAL_LIGHT,
				'line-width': ['interpolate', ['linear'], ['zoom'], 6, 6, 12, 10, 16, 14]
			}
		});
	}
	if (!map.getLayer(ROUTE_CONNECTOR_FILL_LAYER)) {
		map.addLayer({
			id: ROUTE_CONNECTOR_FILL_LAYER,
			type: 'line',
			source: ROUTE_SOURCE_ID,
			filter: ['==', ['get', 'role'], 'connector'],
			layout: commonLine,
			paint: {
				'line-color': NEUTRAL_DARK,
				'line-width': ['interpolate', ['linear'], ['zoom'], 6, 3, 12, 5, 16, 7]
			}
		});
	}

	if (!map.getLayer(ROUTE_PASSTHROUGH_LAYER)) {
		map.addLayer({
			id: ROUTE_PASSTHROUGH_LAYER,
			type: 'circle',
			source: ROUTE_SOURCE_ID,
			filter: ['==', ['get', 'role'], 'passthrough'],
			paint: {
				'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 2, 14, 3.5, 18, 5.5],
				'circle-color': NEUTRAL_DARK,
				'circle-stroke-color': NEUTRAL_LIGHT,
				'circle-stroke-width': 1.5
			}
		});
	}

	if (!map.getLayer(ROUTE_DISC_CASING_LAYER)) {
		map.addLayer({
			id: ROUTE_DISC_CASING_LAYER,
			type: 'circle',
			source: ROUTE_SOURCE_ID,
			filter: ['==', ['get', 'role'], 'disc'],
			paint: {
				'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 9, 14, 12, 18, 16],
				'circle-color': NEUTRAL_LIGHT
			}
		});
	}
	if (!map.getLayer(ROUTE_DISC_FILL_LAYER)) {
		map.addLayer({
			id: ROUTE_DISC_FILL_LAYER,
			type: 'circle',
			source: ROUTE_SOURCE_ID,
			filter: ['==', ['get', 'role'], 'disc'],
			paint: {
				'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 6, 14, 9, 18, 12],
				'circle-color': NEUTRAL_DARK
			}
		});
	}

	// DOM markers for start / goal — no sprite sheet involved (the style
	// disables sprites; icon-image is off-limits).
	if (prevMarkers) {
		prevMarkers.start?.remove();
		prevMarkers.goal?.remove();
	}
	const markers: RouteMarkerHandles = { start: null, goal: null };
	if (geo.startCoord) {
		markers.start = new maplibregl.Marker({
			element: makeStartIconElement(),
			anchor: 'center'
		})
			.setLngLat(geo.startCoord)
			.addTo(map);
	}
	if (geo.goalCoord) {
		markers.goal = new maplibregl.Marker({
			element: makeGoalIconElement(),
			anchor: 'bottom'
		})
			.setLngLat(geo.goalCoord)
			.addTo(map);
	}
	return markers;
}

/** Remove all route layers, the source, and the DOM markers. Safe to call
 * on an already-empty map. */
export function removeRouteLayers(
	map: maplibregl.Map,
	markers: RouteMarkerHandles | null
): void {
	if (markers) {
		markers.start?.remove();
		markers.goal?.remove();
	}
	for (const id of ROUTE_LAYER_IDS) {
		if (map.getLayer(id)) map.removeLayer(id);
	}
	if (map.getSource(ROUTE_SOURCE_ID)) map.removeSource(ROUTE_SOURCE_ID);
}

// Start icon: small filled dark disc with a white outer ring — reads as
// "you are here" / "start line" at all zooms.
function makeStartIconElement(): HTMLDivElement {
	const el = document.createElement('div');
	el.className = 'route-start-icon';
	el.style.cssText = [
		'width: 18px', 'height: 18px', 'border-radius: 50%',
		`background: ${NEUTRAL_DARK}`,
		`box-shadow: 0 0 0 3px ${NEUTRAL_LIGHT}, 0 1px 3px rgba(0,0,0,0.35)`,
		'pointer-events: none'
	].join(';');
	return el;
}

// Goal icon: checkered flag on a stick. Inline SVG keeps us free of any
// sprite / icon-image dependency. Anchor is bottom so the pole plants on
// the goal coordinate.
function makeGoalIconElement(): HTMLDivElement {
	const wrap = document.createElement('div');
	wrap.className = 'route-goal-icon';
	wrap.style.cssText = [
		'width: 22px', 'height: 28px', 'pointer-events: none',
		'filter: drop-shadow(0 1px 2px rgba(0,0,0,0.35))'
	].join(';');
	wrap.innerHTML = `
		<svg viewBox="0 0 22 28" xmlns="http://www.w3.org/2000/svg" width="22" height="28">
			<rect x="9" y="4" width="2" height="24" fill="${NEUTRAL_DARK}"/>
			<g transform="translate(11,4)">
				<rect x="0" y="0" width="10" height="8" fill="${NEUTRAL_LIGHT}"/>
				<rect x="0" y="0" width="2.5" height="2" fill="${NEUTRAL_DARK}"/>
				<rect x="5" y="0" width="2.5" height="2" fill="${NEUTRAL_DARK}"/>
				<rect x="2.5" y="2" width="2.5" height="2" fill="${NEUTRAL_DARK}"/>
				<rect x="7.5" y="2" width="2.5" height="2" fill="${NEUTRAL_DARK}"/>
				<rect x="0" y="4" width="2.5" height="2" fill="${NEUTRAL_DARK}"/>
				<rect x="5" y="4" width="2.5" height="2" fill="${NEUTRAL_DARK}"/>
				<rect x="2.5" y="6" width="2.5" height="2" fill="${NEUTRAL_DARK}"/>
				<rect x="7.5" y="6" width="2.5" height="2" fill="${NEUTRAL_DARK}"/>
				<rect x="0" y="0" width="10" height="8" fill="none" stroke="${NEUTRAL_DARK}" stroke-width="0.7"/>
			</g>
		</svg>
	`;
	return wrap;
}
