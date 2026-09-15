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
import { makeGoalIconElement, makeStartIconElement, makeViaIconElement } from './routeLayers';
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
const WALK_MUTED = '#6a6a6a';
const CASING_COLOR = '#ffffff';

let markers: {
	start: maplibregl.Marker;
	goal: maplibregl.Marker;
	/** One pin per requested via, in route order (direct-mode vias). */
	vias: maplibregl.Marker[];
} | null = null;
let installedMode: 'bike' | 'walk' | null = null;
let handlersInstalled = false;
let lastRoutes: DirectRoute[] | null = null;
// Navigation shows its alternatives muted but not clickable — a
// rider's hands are on the bars (bicycle-navigation.md § Live
// alternatives); the line click then does nothing.
let interactive = true;

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

function buildData(
	routes: DirectRoute[],
	selected: number,
	startPin: boolean
): GeoJSON.FeatureCollection {
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
	const stubs: [[number, number], [number, number]][] = [
		[selRoute.requestedTo, selRoute.coords[selRoute.coords.length - 1]]
	];
	if (startPin) stubs.unshift([selRoute.requestedFrom, selRoute.coords[0]]);
	for (const [pin, snap] of stubs) {
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
	if (!interactive) return;
	const idx = e.features?.[0]?.properties?.idx;
	if (typeof idx === 'number') routingState.selectDirectRoute(idx);
}
function onLineEnter(e: maplibregl.MapLayerMouseEvent) {
	if (!interactive) return;
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
	// Anchor the whole overlay below the place-label block: bike/walk
	// mode shows the standard view, and city/town names must stay
	// readable above the route. label-country is the first of the three
	// place-label layers (country → state → place), so anchoring there
	// keeps all of them on top while POI and street labels stay beneath
	// the route. Each addLayer below inserts before the same anchor, so
	// their relative order is their add order, unchanged.
	const beforeId = map.getLayer('label-country') ? 'label-country' : undefined;
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
	}, beforeId);
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
	}, beforeId);
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
		}, beforeId);
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
	}, beforeId);
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
	}, beforeId);
	if (!handlersInstalled) {
		for (const id of [DIRECT_LINE_LAYER, DIRECT_PUSHED_LAYER, DIRECT_PUSHED_MUTED_LAYER]) {
			map.on('click', id, onLineClick);
			map.on('mouseenter', id, onLineEnter);
			map.on('mouseleave', id, onLineLeave);
		}
		handlersInstalled = true;
	}
}

/** Camera padding for direct-route framing. Narrow screens dock the
 * result cards as a bottom sheet (max 46dvh — keep in sync with
 * .routing-panel.sheet in RoutingPanel.svelte), so the bottom clearance
 * covers it. With the sheet expanded to the full editing panel
 * (content-height, top-anchored) the map only peeks out below it —
 * frame into that measured strip, or return null when the strip is too
 * small for a camera move to be worth anything. Desktop (undefined)
 * keeps the shared left-panel padding. */
const MIN_EXPANDED_STRIP_PX = 140;
function directFramePadding(): maplibregl.PaddingOptions | null | undefined {
	if (!isNarrow()) return undefined;
	const panel = document.querySelector('.routing-panel');
	const ph = panel ? Math.round(panel.getBoundingClientRect().height) : 0;
	if (routingState.directSheetExpanded) {
		if (window.innerHeight - ph < MIN_EXPANDED_STRIP_PX) return null;
		return { top: ph + 16, bottom: 24, left: 40, right: 40 };
	}
	// Collapsed sheet: measured too — the drag handle can have grown it
	// past the default 46dvh. The default is the floor (during a query
	// the loading sheet is shorter than the results will be).
	const bottom = Math.max(ph, Math.round(window.innerHeight * 0.46));
	if (window.innerHeight - bottom < MIN_EXPANDED_STRIP_PX) return null;
	return { top: 64, bottom: bottom + 24, left: 40, right: 40 };
}

/** Frame the union bbox of all shown alternatives — the direct-mode
 * analogue of frameItinerary (card map icon / sheet expand / desktop
 * reframe). */
export function frameDirectRoutes(getMap: () => maplibregl.Map | null) {
	const padding = directFramePadding();
	if (padding === null) return;
	frameDirectBounds(
		getMap, unionBBox(routingState.directRoutes), 15, padding);
}

/** Frame the selected alternative's own bbox — a card click re-centers
 * the route it picked (or re-picked). */
export function frameSelectedDirectRoute(getMap: () => maplibregl.Map | null) {
	const r = routingState.directRoutes[routingState.directSelected];
	if (!r) return;
	const padding = directFramePadding();
	if (padding === null) return;
	frameDirectBounds(getMap, [...r.bbox], 15, padding);
}

export interface DirectOverlayOptions {
	/** Frame a fresh route set (default). Navigation passes false — the
	 * follow-me camera owns the view (bicycle-navigation.md). */
	autoFrame?: boolean;
	/** Plant the start pin + its connector stub (default). Navigation
	 * passes false: a recalculated route "starts" wherever the rider
	 * was, which is no place for a pin. */
	startPin?: boolean;
	/** Lines select their card on click (default). Navigation passes
	 * false. */
	interactive?: boolean;
}

/** Install or update the overlay. Fresh route sets (a new query) apply
 * the basemap focus and auto-frame; a selection change only re-orders /
 * re-tags the features. Idempotent. */
export function enterDirectRouteOverlay(
	map: maplibregl.Map,
	routes: DirectRoute[],
	selected: number,
	opts: DirectOverlayOptions = {}
) {
	if (routes.length === 0) return;
	const autoFrame = opts.autoFrame ?? true;
	const startPin = opts.startPin ?? true;
	interactive = opts.interactive ?? true;
	const mode = routes[0].mode;
	const fresh = lastRoutes !== routes;
	lastRoutes = routes;

	applyBasemapFocus(map, 'direct');

	const data = buildData(routes, Math.min(selected, routes.length - 1), startPin);
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
				.setLngLat(goal).addTo(map),
			vias: []
		};
	} else {
		markers.start.setLngLat(start);
		markers.goal.setLngLat(goal);
	}
	markers.start.getElement().style.display = startPin ? '' : 'none';
	// Via pins — same teardrop family as the transit route overlay's via
	// markers. The count can change between queries, so surplus pins drop
	// and missing ones are added; existing ones just move.
	const vias = routes[0].requestedVias;
	while (markers.vias.length > vias.length) markers.vias.pop()!.remove();
	vias.forEach((coord, i) => {
		if (markers!.vias[i]) markers!.vias[i].setLngLat(coord);
		else {
			markers!.vias.push(
				new maplibregl.Marker({ element: makeViaIconElement(), anchor: 'bottom' })
					.setLngLat(coord).addTo(map)
			);
		}
	});

	// Auto-frame on a fresh query only. Narrow screens frame too — the
	// bottom sheet leaves the map visible, so the new routes must land
	// in the strip above it (padding accounts for the sheet; a fresh
	// query always collapses it, so the null expanded case can't occur,
	// but guard anyway).
	if (fresh && autoFrame) {
		const padding = directFramePadding();
		if (padding !== null) {
			frameDirectBounds(() => map, unionBBox(routes), 15, padding);
		}
	}
}

export function exitDirectRouteOverlay(map: maplibregl.Map) {
	// Never entered — nothing to tear down (the reactive else-branch
	// calls this unconditionally).
	if (installedMode === null && !markers) return;
	if (markers) {
		markers.start.remove();
		markers.goal.remove();
		for (const m of markers.vias) m.remove();
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
	for (const m of markers?.vias ?? []) m.remove();
	markers = null;
	installedMode = null;
	handlersInstalled = false;
	lastRoutes = null;
}
