// Route overlay (route-display.md): a selected itinerary from the
// routing panel renders on the map as polylines + discs + walk dashes +
// pass-through dots + start/goal icons. Everything else on the map
// desaturates (same treatment as line-detail-view) and non-route stops
// hide. The reactive $effects that drive enter/exit live in Map.svelte;
// this module holds the map manipulation and the camera framing rules.

import type maplibregl from 'maplibre-gl';
import { tick } from 'svelte';
import { isNarrow } from './layout';
import { routingState } from './state.svelte';
import { buildRouteGeoJSON, legBounds } from './routeGeoJSON';
import {
	installRouteLayers, removeRouteLayers,
	type RouteMarkerHandles
} from './routeLayers';
import type { StationEntry } from './stationIndex';
import type { Itinerary, Leg } from './types';
import {
	TRANSIT_LINE_LAYERS,
	TRANSIT_LINE_CASING_LAYERS,
	STOP_SYMBOLOGY_LAYERS
} from '../map/layers';

const ROUTE_DIM_SOURCE = 'route-dim';
const ROUTE_DIM_LAYER = 'route-dim';
const ROUTE_LINE_DESAT_OPACITY = 0.5;
const ROUTE_LINE_DESAT_CASING_OPACITY = 0.24;

let routeMarkers: RouteMarkerHandles | null = null;
let savedRouteLinePaints: Map<string, Record<string, unknown>> | null = null;
// While a route is displayed, every map stop-symbology layer is hidden
// so the map's default dots/pills don't misalign against the route's
// own MOTIS-coord discs. Original visibilities restored on exit.
let savedRouteStopVisibilities: Map<string, string> | null = null;

// Camera padding for route framing (routing-map-details-split.md §
// Camera framing). Desktop keeps the left-heavy padding that clears
// the side panel; on narrow screens only the map-mode summary header
// overlays the map, so a modest top clearance is enough.
const ROUTE_HEADER_TOP_CLEARANCE = 96;
function routeFramePadding(): maplibregl.PaddingOptions {
	return isNarrow()
		? { top: ROUTE_HEADER_TOP_CLEARANCE, bottom: 48, left: 32, right: 48 }
		: { top: 96, bottom: 48, left: 380, right: 48 };
}

// Shared route framing for the mobile map-mode entry points. Deferred
// past tick() so the camera math runs after the map-mode layout swap
// (panel hidden, summary header mounted) has been applied — calling
// fitBounds in the same tick as the state change frames against a
// not-yet-settled layout. Deliberately NOT gated on isStyleLoaded():
// fitBounds is pure camera math, and that flag is non-reactive and
// transiently false while tiles stream, which silently turned the old
// map-mode effect into a permanent no-op on mobile.
async function frameRouteBounds(
	getMap: () => maplibregl.Map | null,
	bb: [number, number, number, number] | null,
	maxZoom: number
) {
	await tick();
	const map = getMap();
	if (!bb || !map) return;
	map.fitBounds(
		[[bb[0], bb[1]], [bb[2], bb[3]]],
		{ padding: routeFramePadding(), maxZoom, duration: 600 }
	);
}

/** Camera focus for a clicked leg row in the expanded result card.
 * Frames the leg's bbox, keeping clear of the routing panel on
 * desktop (same framing rule as the whole-route auto-frame). */
/** Merged UICs of the current query's via stops — the drawn route rings
 * them so a stop the traveller chose reads apart from the ones the route
 * merely passes (via-stops.md § Result display). */
function viaUics(): Set<string> {
	return new Set(
		routingState.vias
			.filter((v) => v.station !== null)
			.map((v) => v.station!.uic)
	);
}

export function focusRouteLeg(getMap: () => maplibregl.Map | null, leg: Leg) {
	void frameRouteBounds(getMap, legBounds(leg), 17);
}

/** Frame the whole itinerary. Serves both mobile map-mode entry (the
 * route was never framed on narrow screens — the selection auto-frame
 * is desktop-only) and the reset to the whole-route overview after a
 * leg focus zoomed in. */
export function frameItinerary(
	getMap: () => maplibregl.Map | null,
	it: Itinerary,
	colorIndex: Map<string, string> | null,
	stationIndex: Map<string, StationEntry> | null
) {
	const geo = buildRouteGeoJSON(it, colorIndex, stationIndex, viaUics());
	void frameRouteBounds(getMap, geo.bbox, 15);
}

export function enterRouteOverlay(
	map: maplibregl.Map,
	it: Itinerary,
	colorIndex: Map<string, string> | null,
	stationIndex: Map<string, StationEntry> | null
) {
	const geo = buildRouteGeoJSON(it, colorIndex, stationIndex, viaUics());

	// Auto-frame the route bbox. Desktop frames immediately; on narrow
	// screens the full-width list hides the map anyway, so framing is
	// deferred to the map-mode entry (entering fullscreen map mode
	// reframes against the summary-header padding).
	if (geo.bbox && !isNarrow()) {
		map.fitBounds(
			[[geo.bbox[0], geo.bbox[1]], [geo.bbox[2], geo.bbox[3]]],
			{
				padding: routeFramePadding(),
				maxZoom: 15,
				duration: 900
			}
		);
	}

	// Dim veil — same construction as line-detail but on its own source
	// so the two features never race for layer ownership. Sits just
	// below the transit block so the basemap darkens without pulling
	// the transit / stop symbology down with it.
	if (!map.getSource(ROUTE_DIM_SOURCE)) {
		map.addSource(ROUTE_DIM_SOURCE, {
			type: 'geojson',
			data: {
				type: 'Feature',
				properties: {},
				geometry: {
					type: 'Polygon',
					coordinates: [[[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]]]
				}
			}
		});
	}
	if (!map.getLayer(ROUTE_DIM_LAYER)) {
		const beforeId = map.getLayer('close-zoom-station-backdrop')
			? 'close-zoom-station-backdrop'
			: TRANSIT_LINE_CASING_LAYERS.find((id) => map.getLayer(id));
		map.addLayer({
			id: ROUTE_DIM_LAYER,
			type: 'fill',
			source: ROUTE_DIM_SOURCE,
			paint: { 'fill-color': '#000000', 'fill-opacity': 0.25 }
		}, beforeId);
	} else {
		map.setLayoutProperty(ROUTE_DIM_LAYER, 'visibility', 'visible');
	}

	// Desaturate all map transit lines (same treatment as line-detail).
	// Save originals once on first entry; switching itineraries reuses
	// the same saved set (revert-then-reapply idempotent).
	if (!savedRouteLinePaints) {
		savedRouteLinePaints = new Map();
		for (const id of [...TRANSIT_LINE_LAYERS, ...TRANSIT_LINE_CASING_LAYERS]) {
			if (!map.getLayer(id)) continue;
			savedRouteLinePaints.set(id, {
				'line-color': map.getPaintProperty(id, 'line-color'),
				'line-width': map.getPaintProperty(id, 'line-width'),
				'line-opacity': map.getPaintProperty(id, 'line-opacity')
			});
		}
	}
	for (const id of TRANSIT_LINE_LAYERS) {
		if (!map.getLayer(id)) continue;
		map.setPaintProperty(id, 'line-color',
			['coalesce', ['get', 'color_desat'], '#c4c4c4'] as any);
		map.setPaintProperty(id, 'line-opacity', ROUTE_LINE_DESAT_OPACITY);
	}
	for (const id of TRANSIT_LINE_CASING_LAYERS) {
		if (!map.getLayer(id)) continue;
		map.setPaintProperty(id, 'line-opacity', ROUTE_LINE_DESAT_CASING_OPACITY);
	}

	// Hide every map stop-symbology layer entirely (dots, pills,
	// labels, indicators, close-zoom backdrops). Route stops render
	// from our own source at MOTIS's exact coordinates, and the map's
	// merged-UIC positions don't line up — showing both looks like a
	// doubled, misaligned station. Save current visibility per layer so
	// exit can restore whatever the view mode / dev override had set.
	// Close-zoom pill-arrows stay visible during route mode — they carry
	// the specific line/platform info at z17+ that the route's own
	// discs don't. Everything else in the stop-symbology set hides.
	const layersToHide = STOP_SYMBOLOGY_LAYERS.filter(
		(id) => !id.startsWith('close-zoom-pill-')
	);
	if (!savedRouteStopVisibilities) {
		savedRouteStopVisibilities = new Map();
		for (const id of layersToHide) {
			if (!map.getLayer(id)) continue;
			const vis = map.getLayoutProperty(id, 'visibility');
			savedRouteStopVisibilities.set(id, (vis as string) ?? 'visible');
		}
	}
	for (const id of layersToHide) {
		if (!map.getLayer(id)) continue;
		map.setLayoutProperty(id, 'visibility', 'none');
	}

	// Insert route layers below the first close-zoom pill-arrow layer
	// so pill-arrows always render on top of the route (their platform
	// / line info is finer-grained than the route polyline can show).
	const styleLayers = map.getStyle().layers ?? [];
	const pillArrowBeforeId = styleLayers.find(
		(l) => l.id.startsWith('close-zoom-pill-')
	)?.id;
	routeMarkers = installRouteLayers(map, geo, routeMarkers, pillArrowBeforeId);
}

export function exitRouteOverlay(map: maplibregl.Map) {
	removeRouteLayers(map, routeMarkers);
	routeMarkers = null;
	if (map.getLayer(ROUTE_DIM_LAYER)) {
		map.setLayoutProperty(ROUTE_DIM_LAYER, 'visibility', 'none');
	}
	if (savedRouteLinePaints) {
		for (const [id, props] of savedRouteLinePaints) {
			for (const [prop, val] of Object.entries(props)) {
				map.setPaintProperty(id, prop as any, val as any);
			}
		}
		savedRouteLinePaints = null;
	}
	if (savedRouteStopVisibilities) {
		for (const [id, vis] of savedRouteStopVisibilities) {
			if (!map.getLayer(id)) continue;
			map.setLayoutProperty(id, 'visibility', vis as any);
		}
		savedRouteStopVisibilities = null;
	}
}

/** Unmount path: the map (and its layers) are being destroyed — remove
 * the DOM markers and drop the bookkeeping without touching the map. */
export function disposeRouteOverlay() {
	routeMarkers?.start?.remove();
	routeMarkers?.goal?.remove();
	routeMarkers = null;
	savedRouteLinePaints = null;
	savedRouteStopVisibilities = null;
}
