// Direct cycling / walking route overlay (pedestrian-bicycle-routing.md
// § Query & alternatives): every returned alternative is drawn at once —
// the selected route in full mode color, the others visually muted — and
// tapping a muted line on the map selects its card (the reverse of the
// card click). Reuses the transit route overlay's basemap-focus
// treatment (dim veil, desaturated lines, hidden stop symbology) and its
// start / goal pin markers; the two overlays are mutually exclusive.

import maplibregl from 'maplibre-gl';
import { isNarrow } from './layout';
import { routingState } from './state.svelte';
import { applyBasemapFocus, frameDirectBounds, restoreBasemapFocus } from './routeOverlay';
import { makeGoalIconElement, makeStartIconElement } from './routeLayers';
import type { DirectRoute } from './types';

const DIRECT_SOURCE = 'direct-route';
const DIRECT_CASING_LAYER = 'direct-route-casing';
const DIRECT_LINE_LAYER = 'direct-route-line';

// Mode colors. Walk keeps the neutral dashed language every walking leg
// on this map already speaks (route-display.md § Per-leg rendering);
// bike gets its own solid color with the map's white casing. Muted
// variants are the lighter/desaturated alternates.
const BIKE_COLOR = '#1a7a3c';
const BIKE_MUTED = '#9dbfaa';
const WALK_COLOR = '#1a1a1a';
const WALK_MUTED = '#9a9a9a';
const CASING_COLOR = '#ffffff';

let markers: { start: maplibregl.Marker; goal: maplibregl.Marker } | null = null;
let installedMode: 'bike' | 'walk' | null = null;
let handlersInstalled = false;
let lastRoutes: DirectRoute[] | null = null;

function unionBBox(routes: DirectRoute[]): [number, number, number, number] | null {
	let bb: [number, number, number, number] | null = null;
	for (const r of routes) {
		if (!bb) bb = [...r.bbox];
		else {
			bb = [
				Math.min(bb[0], r.bbox[0]), Math.min(bb[1], r.bbox[1]),
				Math.max(bb[2], r.bbox[2]), Math.max(bb[3], r.bbox[3])
			];
		}
	}
	return bb;
}

function buildData(routes: DirectRoute[], selected: number): GeoJSON.FeatureCollection {
	// Selected route last — within one layer, later features paint on
	// top, so the full-color route always covers the muted alternates.
	const order = routes
		.map((_, i) => i)
		.sort((a, b) => (a === selected ? 1 : 0) - (b === selected ? 1 : 0));
	return {
		type: 'FeatureCollection',
		features: order.map((idx) => ({
			type: 'Feature',
			id: idx,
			geometry: { type: 'LineString', coordinates: routes[idx].coords },
			properties: { idx, sel: idx === selected ? 1 : 0 }
		}))
	};
}

function removeLayers(map: maplibregl.Map) {
	for (const id of [DIRECT_CASING_LAYER, DIRECT_LINE_LAYER]) {
		if (map.getLayer(id)) map.removeLayer(id);
	}
}

function onLineClick(e: maplibregl.MapLayerMouseEvent) {
	const idx = e.features?.[0]?.properties?.idx;
	if (typeof idx === 'number') routingState.selectDirectRoute(idx);
}
function onLineEnter(e: maplibregl.MapLayerMouseEvent) {
	e.target.getCanvas().style.cursor = 'pointer';
}
function onLineLeave(e: maplibregl.MapLayerMouseEvent) {
	e.target.getCanvas().style.cursor = '';
}

const selCase = (selValue: unknown, altValue: unknown) =>
	['case', ['==', ['get', 'sel'], 1], selValue, altValue] as unknown;

// MapLibre allows only one zoom-based subexpression per property, and it
// must be the outermost one — so the zoom interpolate wraps the sel/alt
// case at each stop, never the other way around.
const selWidth = (stops: [number, number, number][]) =>
	[
		'interpolate',
		['linear'],
		['zoom'],
		...stops.flatMap(([z, sel, alt]) => [z, selCase(sel, alt)])
	] as any;

function addLayers(map: maplibregl.Map, mode: 'bike' | 'walk') {
	const color = mode === 'bike' ? BIKE_COLOR : WALK_COLOR;
	const muted = mode === 'bike' ? BIKE_MUTED : WALK_MUTED;
	// Bike routes get the map's white casing like transit legs; walk
	// routes stay the casing-less dashed neutral line every walking leg
	// uses. Widths sit in the transit route overlay's band so a direct
	// route reads as the primary content against the dimmed basemap.
	if (mode === 'bike') {
		map.addLayer({
			id: DIRECT_CASING_LAYER,
			type: 'line',
			source: DIRECT_SOURCE,
			layout: { 'line-cap': 'round', 'line-join': 'round' },
			paint: {
				'line-color': CASING_COLOR,
				'line-width': selWidth([[6, 7, 5], [12, 12, 9], [16, 18, 14]]),
				'line-opacity': selCase(1, 0.7) as any
			}
		});
	}
	map.addLayer({
		id: DIRECT_LINE_LAYER,
		type: 'line',
		source: DIRECT_SOURCE,
		layout: { 'line-cap': 'round', 'line-join': 'round' },
		paint: {
			'line-color': selCase(color, muted) as any,
			'line-width': selWidth([[6, 5, 3.5], [12, 8, 6], [16, 13, 10]]),
			...(mode === 'walk' ? { 'line-dasharray': [1.4, 1.4] as any } : {}),
			'line-opacity': mode === 'walk' ? 0.9 : 1
		}
	});
	if (!handlersInstalled) {
		map.on('click', DIRECT_LINE_LAYER, onLineClick);
		map.on('mouseenter', DIRECT_LINE_LAYER, onLineEnter);
		map.on('mouseleave', DIRECT_LINE_LAYER, onLineLeave);
		handlersInstalled = true;
	}
}

/** Frame the union bbox of all shown alternatives — the direct-mode
 * analogue of frameItinerary (mobile map-mode entry / reframe). */
export function frameDirectRoutes(getMap: () => maplibregl.Map | null) {
	frameDirectBounds(getMap, unionBBox(routingState.directRoutes), 15);
}

/** Install or update the overlay. Fresh route sets (a new query) apply
 * the basemap focus and auto-frame; a selection change only re-orders /
 * re-tags the features. Idempotent. */
export function enterDirectRouteOverlay(
	map: maplibregl.Map,
	routes: DirectRoute[],
	selected: number
) {
	if (routes.length === 0) return;
	const mode = routes[0].mode;
	const fresh = lastRoutes !== routes;
	lastRoutes = routes;

	applyBasemapFocus(map, 'direct');

	const data = buildData(routes, Math.min(selected, routes.length - 1));
	const src = map.getSource(DIRECT_SOURCE) as maplibregl.GeoJSONSource | undefined;
	if (!src) {
		map.addSource(DIRECT_SOURCE, { type: 'geojson', data });
	} else {
		src.setData(data);
	}
	if (installedMode !== mode) {
		removeLayers(map);
		addLayers(map, mode);
		installedMode = mode;
	} else if (!map.getLayer(DIRECT_LINE_LAYER)) {
		addLayers(map, mode);
	}

	// Start / goal pins — same markers the transit route overlay plants.
	// All alternatives share their endpoints, so the primary route's
	// first / last coord serves every card.
	const start = routes[0].coords[0];
	const goal = routes[0].coords[routes[0].coords.length - 1];
	if (!markers) {
		markers = {
			start: new maplibregl.Marker({ element: makeStartIconElement(), anchor: 'bottom' })
				.setLngLat(start).addTo(map),
			goal: new maplibregl.Marker({ element: makeGoalIconElement(), anchor: 'bottom' })
				.setLngLat(goal).addTo(map)
		};
	} else {
		markers.start.setLngLat(start);
		markers.goal.setLngLat(goal);
	}

	// Auto-frame on a fresh query only (desktop; narrow screens defer to
	// the map-mode entry, same rule as the transit overlay).
	if (fresh && !isNarrow()) {
		frameDirectBounds(() => map, unionBBox(routes), 15);
	}
}

export function exitDirectRouteOverlay(map: maplibregl.Map) {
	// Never entered — nothing to tear down (the reactive else-branch
	// calls this unconditionally).
	if (installedMode === null && !markers) return;
	if (markers) {
		markers.start.remove();
		markers.goal.remove();
		markers = null;
	}
	removeLayers(map);
	if (handlersInstalled) {
		map.off('click', DIRECT_LINE_LAYER, onLineClick);
		map.off('mouseenter', DIRECT_LINE_LAYER, onLineEnter);
		map.off('mouseleave', DIRECT_LINE_LAYER, onLineLeave);
		handlersInstalled = false;
	}
	if (map.getSource(DIRECT_SOURCE)) map.removeSource(DIRECT_SOURCE);
	installedMode = null;
	lastRoutes = null;
	restoreBasemapFocus(map, 'direct');
}

/** True while the overlay owns layers on the map — used by the
 * orchestration teardown ordering (line-detail entry). */
export function directOverlayActive(): boolean {
	return installedMode !== null;
}

/** Unmount path: the map is being destroyed — drop DOM markers and
 * bookkeeping without touching the map. */
export function disposeDirectRouteOverlay() {
	markers?.start.remove();
	markers?.goal.remove();
	markers = null;
	installedMode = null;
	handlersInstalled = false;
	lastRoutes = null;
}
