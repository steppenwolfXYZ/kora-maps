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
const DIRECT_PUSHED_LAYER = 'direct-route-pushed';
const DIRECT_PUSHED_MUTED_LAYER = 'direct-route-pushed-muted';
const DIRECT_CONN_LAYER = 'direct-route-connector';

// Mode colors. Walk keeps the neutral dashed language every walking leg
// on this map already speaks (route-display.md § Per-leg rendering);
// bike gets its own solid color with the map's white casing. Muted
// variants are the lighter/desaturated alternates.
const BIKE_COLOR = '#1a7a3c';
const BIKE_MUTED = '#9dbfaa';
// Pushed-section dashes: black on the selected route, dark gray on
// alternates — set apart from the ridden green.
const PUSHED_COLOR = '#1a1a1a';
const PUSHED_MUTED = '#666666';
// Endpoint connectors: the walk between the pin (the place the user
// actually asked for) and the route's snapped street point. Thin dark
// dashes, slightly bowed (the Google-style "not a routed path" arc) so
// they never read as part of the route.
const CONN_COLOR = '#4d4d4d';
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

/** Split a route's shape at its pushed ranges (bicycle-costing-fork.md
 * § pushed-bike): ridden parts render solid, pushed parts dotted. Slices
 * share their boundary coordinate so the line stays visually continuous. */
function splitByPushed(route: DirectRoute): { coords: [number, number][]; pushed: 0 | 1 }[] {
	if (route.pushedRanges.length === 0) return [{ coords: route.coords, pushed: 0 }];
	const parts: { coords: [number, number][]; pushed: 0 | 1 }[] = [];
	let cursor = 0;
	for (const [start, end] of route.pushedRanges) {
		if (start > cursor) parts.push({ coords: route.coords.slice(cursor, start + 1), pushed: 0 });
		parts.push({ coords: route.coords.slice(start, end + 1), pushed: 1 });
		cursor = end;
	}
	if (cursor < route.coords.length - 1) {
		parts.push({ coords: route.coords.slice(cursor), pushed: 0 });
	}
	return parts.filter((p) => p.coords.length >= 2);
}

/** Quadratic-bezier bow between two points: control point offset
 * perpendicular from the midpoint by 20% of the chord. Sampled densely
 * enough that the dash pattern follows the curve smoothly. The
 * perpendicular is computed in a locally isotropic space (lon scaled by
 * cos lat) so the bow looks circular on screen at any latitude. */
function connectorArc(a: [number, number], b: [number, number]): [number, number][] {
	const kLat = Math.cos((((a[1] + b[1]) / 2) * Math.PI) / 180);
	const ax = a[0] * kLat, ay = a[1], bx = b[0] * kLat, by = b[1];
	const dx = bx - ax, dy = by - ay;
	const cx = (ax + bx) / 2 - dy * 0.2;
	const cy = (ay + by) / 2 + dx * 0.2;
	const pts: [number, number][] = [];
	for (let i = 0; i <= 12; i++) {
		const t = i / 12;
		const u = 1 - t;
		pts.push([
			(u * u * ax + 2 * u * t * cx + t * t * bx) / kLat,
			u * u * ay + 2 * u * t * cy + t * t * by
		]);
	}
	return pts;
}

function buildData(routes: DirectRoute[], selected: number): GeoJSON.FeatureCollection {
	// Selected route last — within one layer, later features paint on
	// top, so the full-color route always covers the muted alternates.
	const order = routes
		.map((_, i) => i)
		.sort((a, b) => (a === selected ? 1 : 0) - (b === selected ? 1 : 0));
	const features: GeoJSON.Feature[] = order.flatMap((idx) =>
		splitByPushed(routes[idx]).map(
			(part): GeoJSON.Feature => ({
				type: 'Feature',
				geometry: { type: 'LineString', coordinates: part.coords },
				properties: { idx, sel: idx === selected ? 1 : 0, pushed: part.pushed }
			})
		)
	);
	// Connector arcs pin → snapped street point, selected route only
	// (alternates share the endpoints; three near-identical stubs would
	// just smear). Skipped when the snap landed on the pin.
	const selRoute = routes[Math.min(selected, routes.length - 1)];
	for (const [pin, snap] of [
		[selRoute.requestedFrom, selRoute.coords[0]],
		[selRoute.requestedTo, selRoute.coords[selRoute.coords.length - 1]]
	] as [[number, number], [number, number]][]) {
		const dM = Math.hypot(
			(pin[0] - snap[0]) * 111320 * Math.cos((pin[1] * Math.PI) / 180),
			(pin[1] - snap[1]) * 111320
		);
		if (dM > 3) {
			features.push({
				type: 'Feature',
				geometry: { type: 'LineString', coordinates: connectorArc(pin, snap) },
				properties: { conn: 1 }
			});
		}
	}
	return { type: 'FeatureCollection', features };
}

function removeLayers(map: maplibregl.Map) {
	for (const id of [DIRECT_CASING_LAYER, DIRECT_LINE_LAYER, DIRECT_PUSHED_LAYER, DIRECT_PUSHED_MUTED_LAYER, DIRECT_CONN_LAYER]) {
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

// One width table for the route line AND the pushed dots, so the dots
// always match the line they continue. [zoom, selected, muted].
const LINE_WIDTH_STOPS: [number, number, number][] = [[6, 5, 3.5], [12, 8, 6], [16, 13, 10]];
const CASING_WIDTH_STOPS: [number, number, number][] = [[6, 7, 5], [12, 12, 9], [16, 18, 14]];
// Pushed-section dash pattern, in line-width units (round caps make the
// short dash read as a rounded pill).
const PUSHED_DASH: [number, number] = [0.3, 1.3];

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
	// Endpoint connectors go in first — under every route layer.
	map.addLayer({
		id: DIRECT_CONN_LAYER,
		type: 'line',
		source: DIRECT_SOURCE,
		filter: ['==', ['get', 'conn'], 1],
		layout: { 'line-cap': 'round', 'line-join': 'round' },
		paint: {
			'line-color': CONN_COLOR,
			'line-width': ['interpolate', ['linear'], ['zoom'], 6, 2, 12, 2.5, 16, 4] as any,
			'line-dasharray': PUSHED_DASH as any
		}
	});
	// Alternates' pushed dashes go in FIRST, below the casing and line
	// layers — a separate layer per selection state is the only way to
	// keep an alternate's dashes from painting over the selected route
	// (within one layer only feature order sorts, and the dashes live
	// in a different layer than the lines they must stay under).
	map.addLayer({
		id: DIRECT_PUSHED_MUTED_LAYER,
		type: 'line',
		source: DIRECT_SOURCE,
		filter: ['all', ['==', ['get', 'pushed'], 1], ['==', ['get', 'sel'], 0]],
		layout: { 'line-cap': 'round', 'line-join': 'round' },
		paint: {
			'line-color': PUSHED_MUTED,
			'line-width': selWidth(LINE_WIDTH_STOPS),
			'line-dasharray': PUSHED_DASH as any
		}
	});
	// Bike routes get the map's white casing like transit legs; walk
	// routes stay the casing-less dashed neutral line every walking leg
	// uses. Widths sit in the transit route overlay's band so a direct
	// route reads as the primary content against the dimmed basemap.
	if (mode === 'bike') {
		map.addLayer({
			id: DIRECT_CASING_LAYER,
			type: 'line',
			source: DIRECT_SOURCE,
			filter: ['all', ['!=', ['get', 'pushed'], 1], ['!=', ['get', 'conn'], 1]],
			layout: { 'line-cap': 'round', 'line-join': 'round' },
			paint: {
				'line-color': CASING_COLOR,
				'line-width': selWidth(CASING_WIDTH_STOPS),
				'line-opacity': selCase(1, 0.7) as any
			}
		});
	}
	map.addLayer({
		id: DIRECT_LINE_LAYER,
		type: 'line',
		source: DIRECT_SOURCE,
		filter: ['all', ['!=', ['get', 'pushed'], 1], ['!=', ['get', 'conn'], 1]],
		layout: { 'line-cap': 'round', 'line-join': 'round' },
		paint: {
			'line-color': selCase(color, muted) as any,
			'line-width': selWidth(LINE_WIDTH_STOPS),
			...(mode === 'walk' ? { 'line-dasharray': [1.4, 1.4] as any } : {}),
			'line-opacity': mode === 'walk' ? 0.9 : 1
		}
	});
	// Pushed-bike sections (bicycle-costing-fork.md § pushed-bike): the
	// transit walking legs' dashed language — short dashes with round
	// caps, nothing painted behind them. A per-dash white border is NOT
	// cleanly possible in MapLibre: each layer advances its dash pattern
	// in units of its own line width, so a wider casing layer drifts out
	// of phase along the line (tried and reverted, like the dash-dot,
	// symbol-glyph and computed-circle attempts before it).
	map.addLayer({
		id: DIRECT_PUSHED_LAYER,
		type: 'line',
		source: DIRECT_SOURCE,
		filter: ['all', ['==', ['get', 'pushed'], 1], ['==', ['get', 'sel'], 1]],
		layout: { 'line-cap': 'round', 'line-join': 'round' },
		paint: {
			'line-color': PUSHED_COLOR,
			'line-width': selWidth(LINE_WIDTH_STOPS),
			'line-dasharray': PUSHED_DASH as any
		}
	});
	if (!handlersInstalled) {
		for (const id of [DIRECT_LINE_LAYER, DIRECT_PUSHED_LAYER, DIRECT_PUSHED_MUTED_LAYER]) {
			map.on('click', id, onLineClick);
			map.on('mouseenter', id, onLineEnter);
			map.on('mouseleave', id, onLineLeave);
		}
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
	// They sit on the REQUESTED locations (the address / POI itself),
	// not on the street points Valhalla snapped to; the connector stubs
	// in the source bridge the gap.
	const start = routes[0].requestedFrom;
	const goal = routes[0].requestedTo;
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
		for (const id of [DIRECT_LINE_LAYER, DIRECT_PUSHED_LAYER, DIRECT_PUSHED_MUTED_LAYER]) {
			map.off('click', id, onLineClick);
			map.off('mouseenter', id, onLineEnter);
			map.off('mouseleave', id, onLineLeave);
		}
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
