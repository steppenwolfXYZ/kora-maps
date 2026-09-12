import maplibregl from 'maplibre-gl';
import type { RouteGlyphSide } from './routeGlyphs';
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
//
// Walk sits ABOVE transit + connector so the dashed walking line reads
// on top wherever it visually crosses transit geometry (matches the user
// design rule "walking dashes always above transit"). Discs sit above
// walk so the neutral transfer / boarding / alighting markers still cap
// the walk endpoints.
export const ROUTE_TRANSIT_CASING_LAYER = 'route-transit-casing';
export const ROUTE_TRANSIT_FILL_LAYER = 'route-transit-fill';
export const ROUTE_CONNECTOR_CASING_LAYER = 'route-connector-casing';
export const ROUTE_CONNECTOR_FILL_LAYER = 'route-connector-fill';
export const ROUTE_WALK_LAYER = 'route-walk';
export const ROUTE_PASSTHROUGH_LAYER = 'route-passthrough';
export const ROUTE_DISC_LAYER = 'route-disc';
export const ROUTE_LABEL_LAYER = 'route-label';

const ROUTE_LAYER_IDS = [
	ROUTE_TRANSIT_CASING_LAYER,
	ROUTE_TRANSIT_FILL_LAYER,
	ROUTE_CONNECTOR_CASING_LAYER,
	ROUTE_CONNECTOR_FILL_LAYER,
	ROUTE_WALK_LAYER,
	ROUTE_PASSTHROUGH_LAYER,
	ROUTE_DISC_LAYER,
	ROUTE_LABEL_LAYER
];

// Match the map's own stop dots: white fill, black outline, stroke width
// 1.0 (scripts/style/transit_stations.py far-zoom + pill-zoom layers).
const STOP_FILL = '#ffffff';
const STOP_STROKE = '#000000';
const STOP_STROKE_WIDTH = 1.0;
// Neutral routing color for the connector body + walking dashes.
const NEUTRAL_DARK = '#1a1a1a';
const NEUTRAL_LIGHT = '#ffffff';
// Pin palette via the app tokens (ux-guidelines.md: brand colors are never
// literals) — the marker SVG is inline in the document, so CSS vars resolve.
const PIN_FILL = 'var(--brand)';
const ICON_FILL = 'var(--white)';

// Label font weights per tier, mirroring scripts/style/transit_stations.py.
// The bold set grows with zoom so the ratio of bold-to-regular labels stays
// balanced at every band. SemiBold is the middle weight; big_station joins
// SemiBold at z12.
const LABEL_BOLD_Z7  = ['major_train', 'main_train'];
const LABEL_BOLD_Z9  = [...LABEL_BOLD_Z7,  'important_train'];
const LABEL_BOLD_Z10 = [...LABEL_BOLD_Z9,  'train_station'];
const LABEL_BOLD_Z11 = [...LABEL_BOLD_Z10, 'major_hub', 'major_mountain'];
const LABEL_BOLD_Z12 = [...LABEL_BOLD_Z11, 'small_train'];
const LABEL_SEMIBOLD_BASE = ['mountain_stop', 'ferry_stop'];
const LABEL_SEMIBOLD_Z12  = [...LABEL_SEMIBOLD_BASE, 'big_station'];

// Per-zoom tier sizes, mirroring the map's LABEL_SIZE_Z* dicts. Route
// passthroughs render these sizes; discs use the fixed disc-size table
// below regardless of tier.
const LABEL_SIZE_Z7:  Record<string, number> = {
	major_train: 11, main_train: 10, important_train: 9,
	major_mountain: 9, ferry_stop: 9
};
const LABEL_SIZE_Z10: Record<string, number> = {
	major_train: 16, main_train: 14, important_train: 12,
	train_station: 11, small_train: 11,
	major_mountain: 11, ferry_stop: 11
};
const LABEL_SIZE_Z12: Record<string, number> = {
	major_train: 20, main_train: 16, important_train: 14,
	train_station: 12, small_train: 12,
	major_mountain: 12, mountain_stop: 10, ferry_stop: 12,
	major_hub: 11, big_station: 10, normal_stop: 10
};
const LABEL_SIZE_Z13: Record<string, number> = {
	major_train: 22, main_train: 18, important_train: 15,
	train_station: 13, small_train: 13,
	major_mountain: 13, mountain_stop: 11, ferry_stop: 13,
	major_hub: 13, big_station: 11, normal_stop: 11
};
const LABEL_SIZE_Z14: Record<string, number> = {
	major_train: 24, main_train: 20, important_train: 17,
	train_station: 15, small_train: 15,
	major_mountain: 15, mountain_stop: 13, ferry_stop: 15,
	major_hub: 15, big_station: 13, normal_stop: 13, small_bus: 12
};
const LABEL_TIERS = [
	'major_train', 'main_train', 'important_train',
	'train_station', 'small_train',
	'major_mountain', 'mountain_stop', 'ferry_stop',
	'major_hub', 'big_station', 'normal_stop', 'small_bus'
];

/** Build a MapLibre `match` expression on stop_tier that returns Saira
 * Bold / SemiBold / Regular per tier. SemiBold overrides come first so
 * they win at zoom bands where the tier also sits in the bold set. */
function fontMatch(bold: string[], semi: string[]): any {
	const cases: any[] = [];
	for (const t of semi) cases.push(t, ['literal', ['Saira SemiBold']]);
	for (const t of bold) cases.push(t, ['literal', ['Saira Bold']]);
	return ['match', ['get', 'stop_tier'], ...cases, ['literal', ['Saira Regular']]];
}

/** Build a MapLibre `match` on stop_tier returning per-tier text-size.
 * Tiers not in the dict get 0 (invisible), matching the map's rule. */
function sizeMatch(sizes: Record<string, number>): any {
	const cases: any[] = [];
	for (const t of LABEL_TIERS) cases.push(t, sizes[t] ?? 0);
	return ['match', ['get', 'stop_tier'], ...cases, 0];
}

/** Disc labels are always Bold (user requirement). Wrap a tier-based
 * expression so discs win regardless of tier. */
function discBoldOr(inner: any): any {
	return ['case', ['==', ['get', 'role'], 'disc'],
		['literal', ['Saira Bold']],
		inner];
}

/** Disc labels get a fixed larger size per zoom band, overriding the
 * tier-based passthrough size. Also hides when the disc has been
 * deduped away (disc_min_zoom > atZoom means the disc isn't yet
 * rendering at this band). */
function discSizeOr(atZoom: number, inner: any): any {
	// Bumped so transfer stops read as important against the passthroughs.
	const discSize =
		atZoom >= 13 ? 17 :
		atZoom >= 12 ? 15 :
		atZoom >= 10 ? 13 :
		atZoom >= 7 ? 12 : 12;
	return ['case',
		['==', ['get', 'role'], 'disc'],
		['case', ['<=', ['get', 'disc_min_zoom'], atZoom], discSize, 0],
		inner
	];
}

/** Piecewise-linear interpolation of a value over zoom anchors. Used to
 * build fine-step-point paint expressions so each disc appears at its
 * exact `disc_min_zoom` (not the next coarse step boundary). */
function lerpOverAnchors(anchors: [number, number][], z: number): number {
	if (z <= anchors[0][0]) return anchors[0][1];
	for (let i = 0; i < anchors.length - 1; i++) {
		const [az, av] = anchors[i];
		const [bz, bv] = anchors[i + 1];
		if (z <= bz) {
			const t = (z - az) / (bz - az);
			return av + t * (bv - av);
		}
	}
	return anchors[anchors.length - 1][1];
}

/** Build a step expression on zoom (integer step points 4..18) where each
 * output is a `case (disc_min_zoom <= z) ? value(z) : 0`. Guarantees a
 * feature becomes visible at exactly its `disc_min_zoom` rather than at
 * the next coarse anchor. */
function fineDiscZoomStep(
	anchors: [number, number][],
	firstZoom = 4,
	lastZoom = 18
): any {
	const steps: any[] = [0];
	for (let z = firstZoom; z <= lastZoom; z++) {
		const v = Math.round(lerpOverAnchors(anchors, z) * 100) / 100;
		steps.push(z, ['case', ['<=', ['get', 'disc_min_zoom'], z], v, 0]);
	}
	return ['step', ['zoom'], ...steps];
}

const DISC_RADIUS_ANCHORS: [number, number][] = [
	[4, 5], [8, 7], [12, 9], [14, 10], [18, 13]
];
const CONNECTOR_CASING_ANCHORS: [number, number][] = [
	[4, 6], [8, 8], [12, 10], [16, 14]
];
const CONNECTOR_FILL_ANCHORS: [number, number][] = [
	[4, 3], [8, 4], [12, 5], [16, 7]
];

export interface RouteMarkerHandles {
	start: maplibregl.Marker | null;
	goal: maplibregl.Marker | null;
	/** One pin per via stop, in journey order (via-stops.md). */
	vias: maplibregl.Marker[];
}

/** Add all route layers + source above the topmost transit layer, so the
 * overlay sits above transit lines and stop symbology. `beforeId` (when
 * present) inserts every route layer directly before that style layer —
 * used to keep close-zoom pill-arrows on top of the route. Idempotent —
 * call again to update; a subsequent `removeRouteLayers` wipes them
 * clean. */
export function installRouteLayers(
	map: maplibregl.Map,
	geo: RouteGeoJSONResult,
	prevMarkers: RouteMarkerHandles | null,
	beforeId?: string
): RouteMarkerHandles {
	if (!map.getSource(ROUTE_SOURCE_ID)) {
		map.addSource(ROUTE_SOURCE_ID, { type: 'geojson', data: geo.features });
	} else {
		(map.getSource(ROUTE_SOURCE_ID) as maplibregl.GeoJSONSource).setData(geo.features);
	}

	const commonLine = { 'line-cap': 'round' as const, 'line-join': 'round' as const };
	// beforeId (first close-zoom pill-arrow layer) applies ONLY to the
	// transit casing + fill — the user's rule is "pill-arrows above the
	// route LINE, but below everything else." Every other route layer
	// (connectors, walk, passthroughs, discs, labels) inserts with no
	// beforeId so it goes to the very top of the layer stack, above the
	// pill-arrows too.
	const beforeTransit = beforeId && map.getLayer(beforeId) ? beforeId : undefined;

	// Widths scale roughly 2× the line-detail highlight so the route reads
	// as the primary content of the map.
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
		}, beforeTransit);
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
		}, beforeTransit);
	}

	// Connectors follow the same disc_min_zoom dedup — connector is only
	// meaningful when both its endpoint discs are visible.
	if (!map.getLayer(ROUTE_CONNECTOR_CASING_LAYER)) {
		map.addLayer({
			id: ROUTE_CONNECTOR_CASING_LAYER,
			type: 'line',
			source: ROUTE_SOURCE_ID,
			filter: ['==', ['get', 'role'], 'connector'],
			layout: commonLine,
			paint: {
				'line-color': NEUTRAL_LIGHT,
				'line-width': fineDiscZoomStep(CONNECTOR_CASING_ANCHORS) as any
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
				'line-width': fineDiscZoomStep(CONNECTOR_FILL_ANCHORS) as any
			}
		});
	}

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

	// Passthrough dots: same white/black scheme as the map's own dots,
	// hidden below the tier's min zoom (radius → 0). Discs are always
	// visible (stop_min_zoom is 0 on those features).
	// MapLibre restricts ["zoom"] to top-level step / interpolate, so the
	// per-feature stop_min_zoom check sits inside each step output.
	if (!map.getLayer(ROUTE_PASSTHROUGH_LAYER)) {
		map.addLayer({
			id: ROUTE_PASSTHROUGH_LAYER,
			type: 'circle',
			source: ROUTE_SOURCE_ID,
			filter: ['==', ['get', 'role'], 'passthrough'],
			paint: {
				'circle-radius': ['step', ['zoom'],
					0,
					7,  ['case', ['<=', ['get', 'stop_min_zoom'], 7],  2.2, 0],
					10, ['case', ['<=', ['get', 'stop_min_zoom'], 10], 2.8, 0],
					12, ['case', ['<=', ['get', 'stop_min_zoom'], 12], 3.2, 0],
					13, ['case', ['<=', ['get', 'stop_min_zoom'], 13], 3.5, 0],
					16, ['case', ['<=', ['get', 'stop_min_zoom'], 13], 4.5, 0]
				] as any,
				'circle-color': STOP_FILL,
				'circle-stroke-color': STOP_STROKE,
				// Stroke also drops to 0 below the tier's zoom band so a
				// radius-0 circle doesn't leave a visible ring at low zooms.
				'circle-stroke-width': ['step', ['zoom'],
					0,
					7,  ['case', ['<=', ['get', 'stop_min_zoom'], 7],  STOP_STROKE_WIDTH, 0],
					10, ['case', ['<=', ['get', 'stop_min_zoom'], 10], STOP_STROKE_WIDTH, 0],
					12, ['case', ['<=', ['get', 'stop_min_zoom'], 12], STOP_STROKE_WIDTH, 0],
					13, ['case', ['<=', ['get', 'stop_min_zoom'], 13], STOP_STROKE_WIDTH, 0]
				] as any
			}
		});
	}

	// Discs: bigger version of the same white/black stop dot. One layer,
	// not casing+fill — matches the map's own dot construction. Discs
	// with a non-zero disc_min_zoom (dedup: a higher-ranked station is
	// visually overlapping at this zoom) collapse to radius 0 until the
	// zoom reaches disc_min_zoom.
	if (!map.getLayer(ROUTE_DISC_LAYER)) {
		// Stroke matches radius visibility (constant width when visible).
		const strokeAnchors: [number, number][] = DISC_RADIUS_ANCHORS.map(
			([z]) => [z, STOP_STROKE_WIDTH]);
		map.addLayer({
			id: ROUTE_DISC_LAYER,
			type: 'circle',
			source: ROUTE_SOURCE_ID,
			filter: ['==', ['get', 'role'], 'disc'],
			paint: {
				'circle-radius': fineDiscZoomStep(DISC_RADIUS_ANCHORS) as any,
				'circle-color': STOP_FILL,
				'circle-stroke-color': STOP_STROKE,
				'circle-stroke-width': fineDiscZoomStep(strokeAnchors) as any
			}
		});
	}

	// Labels for discs + visible passthroughs. Mirrors the map's stop-label
	// styling — anchor 'left' with a 0.55em / -0.11em offset (Saira cap-
	// height correction), tier + zoom-band aware font weight, tier-based
	// size, dark ink on a soft white halo. Transfer discs override tier
	// rules: always Bold, always a larger fixed size (user requirement).
	if (!map.getLayer(ROUTE_LABEL_LAYER)) {
		map.addLayer({
			id: ROUTE_LABEL_LAYER,
			type: 'symbol',
			source: ROUTE_SOURCE_ID,
			filter: ['all',
				['any',
					['==', ['get', 'role'], 'disc'],
					['==', ['get', 'role'], 'passthrough']
				],
				['has', 'stop_name'],
				['!=', ['get', 'stop_name'], '']
			],
			layout: {
				'text-field': ['get', 'stop_name'],
				// Font weight per zoom band; discs always Bold.
				'text-font': ['step', ['zoom'],
					discBoldOr(fontMatch(LABEL_BOLD_Z7,  LABEL_SEMIBOLD_BASE)),
					9,  discBoldOr(fontMatch(LABEL_BOLD_Z9,  LABEL_SEMIBOLD_BASE)),
					10, discBoldOr(fontMatch(LABEL_BOLD_Z10, LABEL_SEMIBOLD_BASE)),
					11, discBoldOr(fontMatch(LABEL_BOLD_Z11, LABEL_SEMIBOLD_BASE)),
					12, discBoldOr(fontMatch(LABEL_BOLD_Z12, LABEL_SEMIBOLD_Z12))
				] as any,
				// Size per zoom band, with a step point at every integer
				// zoom (4–14) so a disc whose `disc_min_zoom` lands between
				// the anchor bands still gets its label at the exact zoom
				// its dot becomes visible. Passthrough tiers below their
				// band get size 0 — same rule the map uses.
				'text-size': ['step', ['zoom'],
					discSizeOr(0, 0),
					4,  discSizeOr(4,  0),
					5,  discSizeOr(5,  0),
					6,  discSizeOr(6,  0),
					7,  discSizeOr(7,  sizeMatch(LABEL_SIZE_Z7)),
					8,  discSizeOr(8,  sizeMatch(LABEL_SIZE_Z7)),
					9,  discSizeOr(9,  sizeMatch(LABEL_SIZE_Z7)),
					10, discSizeOr(10, sizeMatch(LABEL_SIZE_Z10)),
					11, discSizeOr(11, sizeMatch(LABEL_SIZE_Z10)),
					12, discSizeOr(12, sizeMatch(LABEL_SIZE_Z12)),
					13, discSizeOr(13, sizeMatch(LABEL_SIZE_Z13)),
					14, discSizeOr(14, sizeMatch(LABEL_SIZE_Z14))
				] as any,
				'text-anchor': 'left',
				// Passthroughs keep the map's 0.55em clearance from the dot.
				// Discs are much larger — text-offset is measured in ems, so
				// a bigger absolute pixel offset is needed to clear the disc
				// radius (≈7-13px vs the passthrough's 2-4.5px).
				'text-offset': ['case',
					['==', ['get', 'role'], 'disc'], ['literal', [1.0, -0.11]],
					['literal', [0.55, -0.11]]
				] as any,
				'text-justify': 'left',
				'text-max-width': 8,
				'text-padding': 4,
				'text-allow-overlap': false,
				'text-optional': true
			},
			paint: {
				'text-color': '#1a1a1a',
				'text-halo-color': '#ffffff',
				'text-halo-width': 1.5,
				'text-halo-blur': 0.5
			}
		});
	}

	// DOM markers for start / goal — no sprite sheet involved.
	if (prevMarkers) {
		prevMarkers.start?.remove();
		prevMarkers.goal?.remove();
		for (const m of prevMarkers.vias ?? []) m.remove();
	}
	const markers: RouteMarkerHandles = { start: null, goal: null, vias: [] };
	if (geo.startCoord) {
		markers.start = new maplibregl.Marker({
			element: makeStartIconElement(),
			anchor: 'bottom'
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
	for (const coord of geo.viaCoords ?? []) {
		markers.vias.push(
			new maplibregl.Marker({ element: makeViaIconElement(), anchor: 'bottom' })
				.setLngLat(coord)
				.addTo(map)
		);
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
		for (const m of markers.vias ?? []) m.remove();
	}
	for (const id of ROUTE_LAYER_IDS) {
		if (map.getLayer(id)) map.removeLayer(id);
	}
	if (map.getSource(ROUTE_SOURCE_ID)) map.removeSource(ROUTE_SOURCE_ID);
}

// Route pins: a teardrop whose head carries the same dot-and-line
// glyphs as the popup's route buttons and the map context menu
// (routeGlyphs.ts): start `o──`, via `──o──`, goal `──o`. The line is
// clipped to the pin outline so it runs right to the head's edge, as
// it runs to the button edge elsewhere. Same pin shape and palette for
// all three; the glyph is what distinguishes them. Anchored at bottom
// so the tip plants on the coordinate.
const PIN_PATH = 'M12 1 C 7.6 1, 4 4.6, 4 9 C 4 17.5, 11.7 16.2, 12 25 C 12.3 16.2, 20 17.5, 20 9 C 20 4.6, 16.4 1, 12 1 Z';
// Head circle: centre (12, 9), radius 8 → its equator spans x 4..20.
const PIN_HEAD_CX = 12, PIN_HEAD_CY = 9, PIN_HEAD_R = 8;
const PIN_DOT_R = 3.2, PIN_DOT_INSET = 6.2, PIN_LINE_H = 1.3;
let pinClipSeq = 0;

function makePinElement(side: RouteGlyphSide, className: string): HTMLDivElement {
	const wrap = document.createElement('div');
	wrap.className = className;
	wrap.style.cssText = [
		'width: 38px', 'height: 42px', 'pointer-events: none',
		'filter: drop-shadow(0 1px 2px rgba(0,0,0,0.35))'
	].join(';');
	const left = PIN_HEAD_CX - PIN_HEAD_R, right = PIN_HEAD_CX + PIN_HEAD_R;
	const cx = side === 'from' ? left + PIN_DOT_INSET
		: side === 'to' ? right - PIN_DOT_INSET : PIN_HEAD_CX;
	const lineX = side === 'from' ? cx : left;
	const lineEnd = side === 'to' ? cx : right;
	// Clip ids must be unique per document: several pins coexist.
	const clipId = `route-pin-clip-${++pinClipSeq}`;
	wrap.innerHTML = `
		<svg viewBox="0 0 24 26" xmlns="http://www.w3.org/2000/svg" width="38" height="42">
			<defs><clipPath id="${clipId}"><path d="${PIN_PATH}"/></clipPath></defs>
			<path d="${PIN_PATH}" style="fill: ${PIN_FILL}; stroke: ${ICON_FILL}" stroke-width="0.3"/>
			<g style="fill: ${ICON_FILL}" clip-path="url(#${clipId})">
				<rect x="${lineX}" y="${PIN_HEAD_CY - PIN_LINE_H / 2}" width="${lineEnd - lineX}" height="${PIN_LINE_H}"/>
				<circle cx="${cx}" cy="${PIN_HEAD_CY}" r="${PIN_DOT_R}"/>
			</g>
		</svg>
	`;
	return wrap;
}

export function makeStartIconElement(): HTMLDivElement {
	return makePinElement('from', 'route-start-icon');
}

export function makeViaIconElement(): HTMLDivElement {
	return makePinElement('via', 'route-via-icon');
}

export function makeGoalIconElement(): HTMLDivElement {
	return makePinElement('to', 'route-goal-icon');
}
