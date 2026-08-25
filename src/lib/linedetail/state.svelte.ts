// Line detail view state + map manipulation (line-detail-view.md).
// Follows the class-instance pattern of routing/state.svelte.ts. The
// map is passed into enter/exit/teardown — this module never owns the
// map lifecycle. Cross-feature coordination (closing the routing panel
// / route overlay before entry) stays in Map.svelte.

import type maplibregl from 'maplibre-gl';
import { pushState, replaceState } from '$app/navigation';
import {
	TRANSIT_LINE_LAYERS,
	TRANSIT_LINE_CASING_LAYERS,
	TRANSIT_STOP_DOT_LAYERS,
	TRANSIT_STOP_LABEL_LAYERS,
	TRANSIT_STOP_PILL_LAYERS
} from '../map/layers';
import {
	loadLineIndex,
	clearLineDeepLinkFromUrl,
	mergeServiceInfo,
	URL_LINE_PARAM,
	type LineDetailSelection,
	type LineServiceInfo
} from './lineIndex';

const LINE_DETAIL_DIM_SOURCE = 'line-detail-dim';
const LINE_DETAIL_DIM_LAYER = 'line-detail-dim';
const LINE_DETAIL_HIGHLIGHT_CASING = 'line-detail-highlight-casing';
const LINE_DETAIL_HIGHLIGHT_FILL = 'line-detail-highlight';
const LINE_DETAIL_WIDTH_ADD_PX = 4;
const LINE_DETAIL_OTHER_OPACITY = 0.5;
const LINE_DETAIL_CASING_OTHER_OPACITY = 0.24;

// Stop-symbology layers filtered to member stations while the view is
// active. Every feature in them carries the `line_keys` membership
// string baked by step 07. Close-zoom (z17+) layers are out of scope.
const LINE_DETAIL_FILTER_LAYERS = [
	...TRANSIT_STOP_DOT_LAYERS,
	...TRANSIT_STOP_PILL_LAYERS,
	...TRANSIT_STOP_LABEL_LAYERS,
	'transit-stop-indicator'
];

/** Add a fixed pixel amount to a line-width value. The style's widths are
 * top-level `["interpolate", …, ["zoom"], …]` expressions — MapLibre
 * forbids nesting those inside `["+", …]`, so the addend is folded into
 * each interpolate output instead. */
function widenLineWidthExpr(expr: unknown, addPx: number): unknown {
	if (typeof expr === 'number') return expr + addPx;
	if (Array.isArray(expr) && expr[0] === 'interpolate') {
		const out = expr.slice(0, 3);
		for (let i = 3; i < expr.length; i += 2) {
			out.push(expr[i], ['+', addPx, expr[i + 1]]);
		}
		return out;
	}
	return expr;
}

class LineDetailState {
	/** Current selection; null while the view is closed. */
	selection = $state<LineDetailSelection | null>(null);
	// Per-line service data baked into line_index.json by the pipeline:
	// weekday mask, first/last departure, per-window trips/hour, seasonal
	// operating period, per-terminus-pair variant rows. Loaded lazily
	// when the detail view opens; absent for indexes built before the
	// feature.
	service = $state<LineServiceInfo | null>(null);
	serviceExpanded = $state(false);

	// History integration: true while the view sits on its own pushed
	// history record (browser back then closes it — see the
	// back/forward $effect in Map.svelte). False for deep-link entries,
	// which have no in-app record to return to.
	enteredViaPush = false;
	// Guards double-firing history.back() while a close is in flight,
	// and suppresses the position-hash camera jump for that back step.
	closingViaBack = false;

	// Original layer state captured on entry and restored on close.
	// Non-reactive — pure bookkeeping.
	private savedLinePaints: Map<string, Record<string, unknown>> | null = null;
	private savedStopFilters: Map<string, unknown> | null = null;

	enter(
		map: maplibregl.Map,
		sel: LineDetailSelection,
		source: 'user' | 'deeplink' | 'history' = 'user'
	) {
		if (!sel.keys?.length || !sel.bbox || sel.bbox.length !== 4) return;

		map.fitBounds(
			[[sel.bbox[0], sel.bbox[1]], [sel.bbox[2], sel.bbox[3]]],
			{ padding: { top: 96, bottom: 48, left: 48, right: 48 }, maxZoom: 15, duration: 900 }
		);

		// Dim veil under the transit block: above basemap / roads / contours,
		// below the station backdrop, lines and stops. Darkens the basemap
		// slightly. Created once, then toggled via visibility.
		if (!map.getSource(LINE_DETAIL_DIM_SOURCE)) {
			map.addSource(LINE_DETAIL_DIM_SOURCE, {
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
		if (!map.getLayer(LINE_DETAIL_DIM_LAYER)) {
			const beforeId = map.getLayer('close-zoom-station-backdrop')
				? 'close-zoom-station-backdrop'
				: TRANSIT_LINE_CASING_LAYERS.find((id) => map.getLayer(id));
			map.addLayer({
				id: LINE_DETAIL_DIM_LAYER,
				type: 'fill',
				source: LINE_DETAIL_DIM_SOURCE,
				paint: { 'fill-color': '#000000', 'fill-opacity': 0.25 }
			}, beforeId);
		} else {
			map.setLayoutProperty(LINE_DETAIL_DIM_LAYER, 'visibility', 'visible');
		}

		// Capture originals on first entry only — switching lines while the
		// view is open re-derives every override from the same originals.
		let savedLinePaints = this.savedLinePaints;
		if (!savedLinePaints) {
			savedLinePaints = new Map();
			for (const id of [...TRANSIT_LINE_LAYERS, ...TRANSIT_LINE_CASING_LAYERS]) {
				if (!map.getLayer(id)) continue;
				savedLinePaints.set(id, {
					'line-color': map.getPaintProperty(id, 'line-color'),
					'line-width': map.getPaintProperty(id, 'line-width'),
					'line-opacity': map.getPaintProperty(id, 'line-opacity')
				});
			}
			this.savedLinePaints = savedLinePaints;
		}
		let savedStopFilters = this.savedStopFilters;
		if (!savedStopFilters) {
			savedStopFilters = new Map();
			for (const id of LINE_DETAIL_FILTER_LAYERS) {
				if (!map.getLayer(id)) continue;
				savedStopFilters.set(id, map.getFilter(id) ?? null);
			}
			this.savedStopFilters = savedStopFilters;
		}

		const isSelected = ['in', ['get', 'line_key'], ['literal', sel.keys]];

		// Base line layers: EVERYTHING switches to the baked desaturated
		// color (same hue/lightness, saturation reduced at build time —
		// pipeline_setup.py) and goes translucent. The selected line is
		// redrawn by the highlight pair on top, so it also sits above lines
		// of higher-ranked modes. Widths stay original. Fallback gray covers
		// tiles built before `color_desat` existed.
		for (const id of TRANSIT_LINE_LAYERS) {
			if (!map.getLayer(id)) continue;
			map.setPaintProperty(id, 'line-color',
				['coalesce', ['get', 'color_desat'], '#c4c4c4'] as any);
			map.setPaintProperty(id, 'line-opacity', LINE_DETAIL_OTHER_OPACITY);
		}
		for (const id of TRANSIT_LINE_CASING_LAYERS) {
			if (!map.getLayer(id)) continue;
			map.setPaintProperty(id, 'line-opacity', LINE_DETAIL_CASING_OTHER_OPACITY);
		}

		// Highlight pair: casing + fill for the selected line only, inserted
		// directly above the topmost transit line layer (below the stop
		// symbology). Width is the style's own interpolate with the fixed
		// widen addend folded into each output (see widenLineWidthExpr).
		if (!map.getLayer(LINE_DETAIL_HIGHLIGHT_CASING)) {
			const styleLayers = map.getStyle().layers ?? [];
			const trainIdx = styleLayers.findIndex((l) => l.id === 'transit-train');
			const beforeId = trainIdx >= 0 ? styleLayers[trainIdx + 1]?.id : undefined;
			const casingOrig = savedLinePaints.get('transit-train-casing');
			const fillOrig = savedLinePaints.get('transit-train');
			const lineLayout = {
				'line-cap': 'round' as const,
				'line-join': 'round' as const,
				'line-sort-key': ['coalesce', ['get', 'speed_kmh'], 0] as any
			};
			map.addLayer({
				id: LINE_DETAIL_HIGHLIGHT_CASING,
				type: 'line',
				source: 'transit_lines',
				'source-layer': 'transit_lines',
				minzoom: 4,
				filter: isSelected as any,
				layout: lineLayout,
				paint: {
					'line-color': '#ffffff',
					'line-width': widenLineWidthExpr(
						casingOrig?.['line-width'], LINE_DETAIL_WIDTH_ADD_PX) as any,
					'line-opacity': 0.9
				}
			}, beforeId);
			map.addLayer({
				id: LINE_DETAIL_HIGHLIGHT_FILL,
				type: 'line',
				source: 'transit_lines',
				'source-layer': 'transit_lines',
				minzoom: 4,
				filter: isSelected as any,
				layout: lineLayout,
				paint: {
					'line-color': ['get', 'color'],
					'line-width': widenLineWidthExpr(
						fillOrig?.['line-width'], LINE_DETAIL_WIDTH_ADD_PX) as any,
					'line-opacity': 1
				}
			}, beforeId);
		} else {
			for (const id of [LINE_DETAIL_HIGHLIGHT_CASING, LINE_DETAIL_HIGHLIGHT_FILL]) {
				map.setFilter(id, isSelected as any);
				map.setLayoutProperty(id, 'visibility', 'visible');
			}
		}

		// Stops: hide stations the line does not serve. Membership is an
		// exact-key substring test against the ";"-padded `line_keys`.
		// At far zoom, an "eater" dot inherits an absorbed member's key
		// only at zooms where absorption is active — so a step-over-zoom
		// expression picks `line_keys_zN` for the current zoom and falls
		// back to base `line_keys` at pill zoom (z ≥ 14, no absorption).
		const keysExprAt = (n: number): unknown =>
			['coalesce', ['get', `line_keys_z${n}`], ['get', 'line_keys'], ''];
		const baseKeysExpr: unknown = ['coalesce', ['get', 'line_keys'], ''];
		const memberAt = (keysExpr: unknown): unknown =>
			['any', ...sel.keys.map((k) => ['in', `;${k};`, keysExpr])];
		const isMember = ['step', ['zoom'],
			memberAt(keysExprAt(7)),
			8,  memberAt(keysExprAt(8)),
			9,  memberAt(keysExprAt(9)),
			10, memberAt(keysExprAt(10)),
			11, memberAt(keysExprAt(11)),
			12, memberAt(keysExprAt(12)),
			13, memberAt(keysExprAt(13)),
			14, memberAt(baseKeysExpr),
		];
		for (const [id, orig] of savedStopFilters) {
			map.setFilter(id, (orig ? ['all', orig, isMember] : isMember) as any);
		}

		const wasOpen = this.selection !== null;
		this.selection = sel;
		this.loadService(sel);

		// History: a fresh user entry pushes ONE record whose state carries
		// the selection, so browser back closes the view (handled by the
		// back/forward $effect in Map.svelte) and forward can restore it.
		// Switching lines while the view is open replaces that record —
		// back always closes fully rather than stepping through previously
		// viewed lines. Deep-link entries replace in place (no prior
		// in-app record), and 'history' reopens write nothing — their
		// record is already current.
		const url = new URL(window.location.href);
		url.searchParams.set(URL_LINE_PARAM, sel.keys.join(','));
		if (source === 'user' && !wasOpen) {
			pushState(url, { lineDetail: sel });
			this.enteredViaPush = true;
		} else if (source === 'history') {
			this.enteredViaPush = true;
		} else {
			replaceState(url, { lineDetail: sel });
		}
	}

	/** Close request from the UI (× button / Escape). When the view sits
	 * on its own pushed history record, consume it via history.back() —
	 * the back/forward $effect performs the teardown — so a later back
	 * press cannot land on the record and reopen the view. Deep-link
	 * entries have no record and clear the param in place. */
	exit(map: maplibregl.Map | null) {
		if (!this.selection || this.closingViaBack) return;
		if (this.enteredViaPush) {
			this.closingViaBack = true;
			history.back();
			return;
		}
		this.teardown(map);
		clearLineDeepLinkFromUrl();
	}

	/** Visual/state teardown shared by every close path. No history or
	 * URL writes. */
	teardown(map: maplibregl.Map | null) {
		this.selection = null;
		this.service = null;
		this.serviceExpanded = false;
		this.enteredViaPush = false;
		this.closingViaBack = false;
		if (!map) {
			this.savedLinePaints = null;
			this.savedStopFilters = null;
			return;
		}
		if (map.getLayer(LINE_DETAIL_DIM_LAYER)) {
			map.setLayoutProperty(LINE_DETAIL_DIM_LAYER, 'visibility', 'none');
		}
		for (const id of [LINE_DETAIL_HIGHLIGHT_CASING, LINE_DETAIL_HIGHLIGHT_FILL]) {
			if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', 'none');
		}
		if (this.savedLinePaints) {
			for (const [id, props] of this.savedLinePaints) {
				for (const [prop, val] of Object.entries(props)) {
					map.setPaintProperty(id, prop as any, val as any);
				}
			}
		}
		if (this.savedStopFilters) {
			for (const [id, orig] of this.savedStopFilters) {
				map.setFilter(id, (orig ?? null) as any);
			}
		}
		this.savedLinePaints = null;
		this.savedStopFilters = null;
	}

	/** Unmount path: the map (and its layers) are being destroyed —
	 * drop all state without touching the map. */
	reset() {
		this.selection = null;
		this.service = null;
		this.serviceExpanded = false;
		this.enteredViaPush = false;
		this.closingViaBack = false;
		this.savedLinePaints = null;
		this.savedStopFilters = null;
	}

	private loadService(sel: LineDetailSelection) {
		this.service = null;
		this.serviceExpanded = false;
		const token = sel.keys.join(',');
		void loadLineIndex().then((index) => {
			if (!index || !this.selection || this.selection.keys.join(',') !== token) return;
			this.service = mergeServiceInfo(
				sel.keys.map((k) => index[k]?.service)
					.filter(Boolean) as LineServiceInfo[]);
		});
	}
}

export const lineDetailState = new LineDetailState();
