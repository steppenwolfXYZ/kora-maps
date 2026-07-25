<script lang="ts">
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import { slide } from 'svelte/transition';
	import { replaceState } from '$app/navigation';
	import { Protocol } from 'pmtiles';
	import mlcontour from 'maplibre-contour';
	import StopSearch from './StopSearch.svelte';
	import MapMenu from './MapMenu.svelte';

	// Register the pmtiles:// protocol handler once at module level
	const pmtilesProtocol = new Protocol();
	maplibregl.addProtocol('pmtiles', pmtilesProtocol.tile.bind(pmtilesProtocol));

	// Contour DEM manager — reuses the same Mapterhorn terrain the hillshade
	// layer consumes (source config in scripts/config.yaml → terrain.source).
	// Constructed lazily: DemSource spawns a Web Worker, which doesn't exist
	// during SSR. The $effect below (client-only) triggers creation.
	let demSource: InstanceType<typeof mlcontour.DemSource> | null = null;
	function getDemSource() {
		if (!demSource) {
			demSource = new mlcontour.DemSource({
				url: 'https://tiles.mapterhorn.com/{z}/{x}/{y}.webp',
				encoding: 'terrarium',
				maxzoom: 12,
				worker: true,
				cacheSize: 100,
				timeoutMs: 10_000
			});
			demSource.setupMaplibre(maplibregl);
		}
		return demSource;
	}

	/** Resolved MapLibre style object loaded from /style.json */
	let { style }: { style: maplibregl.StyleSpecification } = $props();

	const CONTOUR_SOURCE_ID = 'contour-source';
	const CONTOUR_LAYERS = ['contour-line-minor', 'contour-line-major', 'contour-label-major'];

	let container: HTMLDivElement;
	let zoom = $state(0);
	let contoursEnabled = $state(false);

	const TRANSIT_LINE_LAYERS = [
		'transit-mountain', 'transit-regional_bus', 'transit-bus',
		'transit-ferry', 'transit-metro', 'transit-tram', 'transit-train'
	];

	const TRANSIT_LINE_CASING_LAYERS = TRANSIT_LINE_LAYERS.map((id) => `${id}-casing`);

	const TRANSIT_STOP_DOT_LAYERS = [
		'transit-stop-fill-transit_stops_rail',
		'transit-stop-fill-transit_stops_tram',
		'transit-stop-fill-transit_stops_regional',
		'transit-stop-fill-transit_stops_bus',
		'transit-stop-fill-transit_stops_rail-far',
		'transit-stop-fill-transit_stops_tram-far',
		'transit-stop-fill-transit_stops_regional-far',
		'transit-stop-fill-transit_stops_bus-far'
	];

	const TRANSIT_STOP_LABEL_LAYERS = [
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

	const TRANSIT_STOP_PILL_LAYERS = [
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
	const TRANSIT_PILL_ARROW_LAYERS = CLOSE_ZOOM_BANDS.flatMap((b) => [
		`close-zoom-pill-arrow-fill-${b}`,
		`close-zoom-pill-arrow-border-${b}`,
		`close-zoom-pill-disc-${b}`,
		`close-zoom-pill-ref-${b}`,
		`close-zoom-pill-dest-${b}`
	]);

	// Every stop-symbology layer, toggled as one unit by the view switch
	// (see .claude/concepts/view-modes.md). Debug layers stay independent.
	const STOP_SYMBOLOGY_LAYERS = [
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

	const PLACE_LABEL_LAYERS = ['label-place', 'label-state', 'label-country'];

	type ViewMode = 'standard' | 'transit-focus';
	// Dev override: transit-focus while stop rendering is under active work.
	// The concept (view-modes.md) specifies 'standard' as the shipped default.
	const DEFAULT_VIEW = 'transit-focus' as ViewMode;
	let viewMode = $state<ViewMode>(DEFAULT_VIEW);
	let mapRef = $state.raw<maplibregl.Map | null>(null);
	// Menu panel state (bound into MapMenu). Non-modal: stays open during
	// map interaction on large screens; on small screens any map move or
	// click closes it (breakpoint matches the .top-controls media query).
	let menuOpen = $state(false);
	const MENU_AUTOCLOSE_MAX_WIDTH = 600;
	function closeMenuOnSmallScreen() {
		if (menuOpen && window.innerWidth <= MENU_AUTOCLOSE_MAX_WIDTH) menuOpen = false;
	}

	function applyViewMode(map: maplibregl.Map, mode: ViewMode) {
		for (const id of STOP_SYMBOLOGY_LAYERS) {
			if (!map.getLayer(id)) continue;
			map.setLayoutProperty(id, 'visibility', mode === 'transit-focus' ? 'visible' : 'none');
		}
		for (const id of PLACE_LABEL_LAYERS) {
			if (!map.getLayer(id)) continue;
			map.setLayoutProperty(id, 'visibility', mode === 'standard' ? 'visible' : 'none');
		}
	}

	function setView(mode: ViewMode) {
		viewMode = mode;
		if (mapRef) applyViewMode(mapRef, mode);
	}

	// ── Line detail view (line-detail-view.md) ──────────────────────────────
	// Entered by clicking a line badge in the station or line popup. "The
	// line" is all variants of its (ref, agency_id, mode) group: `keys` are
	// the canonical line keys baked into badge entries / line features,
	// `bbox` the group's union bbox for the camera fit.
	interface LineDetailSelection {
		keys: string[];
		bbox: [number, number, number, number];
		ref: string;
		mode: string;
		color: string;
		route: string;
	}

	let lineDetail = $state<LineDetailSelection | null>(null);
	// Original layer state captured on entry and restored on close.
	// Non-reactive — pure bookkeeping.
	let savedLinePaints: Map<string, Record<string, unknown>> | null = null;
	let savedStopFilters: Map<string, unknown> | null = null;

	const LINE_DETAIL_DIM_SOURCE = 'line-detail-dim';
	const LINE_DETAIL_DIM_LAYER = 'line-detail-dim';
	const LINE_DETAIL_HIGHLIGHT_CASING = 'line-detail-highlight-casing';
	const LINE_DETAIL_HIGHLIGHT_FILL = 'line-detail-highlight';
	const LINE_DETAIL_WIDTH_ADD_PX = 4;
	const LINE_DETAIL_OTHER_OPACITY = 0.5;
	const LINE_DETAIL_CASING_OTHER_OPACITY = 0.24;

	// Deep link (line-detail-view.md § Deep link). The URL carries the
	// selection as `?line=<key1>[,<key2>...]`. Multiple keys occur when a
	// popup badge merges same-(ref, mode) lines across agencies. The keys
	// resolve against `/map-assets/line_index.json` (baked by step 07:
	// pipeline_setup.py § "write OUT_LINE_INDEX"). replaceState is used so
	// per-interaction updates don't pollute browser history.
	const URL_LINE_PARAM = 'line';
	const LINE_INDEX_URL = '/map-assets/line_index.json';

	interface LineServiceVariant {
		route: string;
		/** 7-char Mo..Su mask, '1' = served */
		days: string;
		/** Average first/last departure of both ends, seconds from midnight
		 * (may exceed 24 h) */
		dep?: [number, number];
		/** Runs per active day (busiest direction) — departures on a day
		 * the line actually runs */
		rpd: number;
		/** Irregular departure pattern (e.g. peak-only service) */
		irr?: boolean;
		/** ISO operating period — present only when the line is seasonal */
		from?: string;
		to?: string;
	}

	interface LineServiceInfo {
		days: string;
		dep?: [number, number];
		rpd: number;
		irr?: boolean;
		from?: string;
		to?: string;
		/** One row per distinct terminus pair, busiest first */
		variants: LineServiceVariant[];
	}

	interface LineIndexEntry {
		ref: string;
		mode: string;
		color: string;
		bbox: [number, number, number, number];
		route: string;
		service?: LineServiceInfo;
	}

	// line_index.json is fetched once per session and shared between the
	// deep-link resolver and the service summary in the title bar.
	let lineIndexPromise: Promise<Record<string, LineIndexEntry> | null> | null = null;
	function loadLineIndex(): Promise<Record<string, LineIndexEntry> | null> {
		if (!lineIndexPromise) {
			lineIndexPromise = fetch(LINE_INDEX_URL)
				.then((res) => (res.ok ? res.json() : null))
				.catch(() => null);
		}
		return lineIndexPromise;
	}

	function readLineDeepLinkFromUrl(): string[] | null {
		if (typeof window === 'undefined') return null;
		const params = new URLSearchParams(window.location.search);
		const raw = params.get(URL_LINE_PARAM);
		if (!raw) return null;
		const keys = raw.split(',').map((s) => s.trim()).filter(Boolean);
		return keys.length ? keys : null;
	}

	function syncLineDeepLinkToUrl(keys: string[] | null) {
		if (typeof window === 'undefined') return;
		const url = new URL(window.location.href);
		if (keys && keys.length) {
			url.searchParams.set(URL_LINE_PARAM, keys.join(','));
		} else {
			url.searchParams.delete(URL_LINE_PARAM);
		}
		// SvelteKit's replaceState, not window.history.replaceState — the
		// raw call wipes the router's history state and trips its dev
		// warning.
		replaceState(url, {});
	}

	// --- URL position hash sync -------------------------------------------
	// Replaces MapLibre's `hash: true`, whose internal writer calls raw
	// window.history.replaceState and so conflicts with SvelteKit's router.
	// Same URL format as MapLibre: #zoom/lat/lng[/bearing[/pitch]], so
	// previously shared links keep working.

	function readPositionHash(): {
		center: [number, number]; zoom: number; bearing: number; pitch: number
	} | null {
		if (typeof window === 'undefined') return null;
		const parts = window.location.hash.slice(1).split('/');
		if (parts.length < 3) return null;
		const [zoom, lat, lng, bearing, pitch] = parts.map(Number);
		if (![zoom, lat, lng].every(Number.isFinite)) return null;
		return {
			center: [lng, lat],
			zoom,
			bearing: Number.isFinite(bearing) ? bearing : 0,
			pitch: Number.isFinite(pitch) ? pitch : 0
		};
	}

	function writePositionHash(map: maplibregl.Map) {
		const zoom = Math.round(map.getZoom() * 100) / 100;
		// MapLibre's precision rule: enough decimals that rounding moves
		// the map by less than half a pixel at this zoom.
		const precision = Math.ceil(
			(zoom * Math.LN2 + Math.log(512 / 360 / 0.5)) / Math.LN10);
		const m = Math.pow(10, Math.max(0, precision));
		const center = map.getCenter();
		const lat = Math.round(center.lat * m) / m;
		const lng = Math.round(center.lng * m) / m;
		const bearing = map.getBearing();
		const pitch = map.getPitch();
		let hash = `#${zoom}/${lat}/${lng}`;
		if (bearing || pitch) hash += `/${Math.round(bearing * 10) / 10}`;
		if (pitch) hash += `/${Math.round(pitch)}`;
		const url = new URL(window.location.href);
		url.hash = hash;
		if (url.href === window.location.href) return;
		replaceState(url, {});
	}

	async function resolveLineDeepLink(keys: string[]): Promise<LineDetailSelection | null> {
		try {
			const index = await loadLineIndex();
			if (!index) return null;
			const resolved: LineIndexEntry[] = [];
			const resolvedKeys: string[] = [];
			for (const k of keys) {
				const e = index[k];
				if (e && Array.isArray(e.bbox) && e.bbox.length === 4
				    && e.bbox.every((v) => Number.isFinite(v))) {
					resolved.push(e);
					resolvedKeys.push(k);
				}
			}
			if (!resolved.length) return null;
			let bb: [number, number, number, number] =
				[resolved[0].bbox[0], resolved[0].bbox[1],
				 resolved[0].bbox[2], resolved[0].bbox[3]];
			for (let i = 1; i < resolved.length; i++) {
				const b = resolved[i].bbox;
				bb = [Math.min(bb[0], b[0]), Math.min(bb[1], b[1]),
				      Math.max(bb[2], b[2]), Math.max(bb[3], b[3])];
			}
			const first = resolved[0];
			return {
				keys: resolvedKeys,
				bbox: bb,
				ref: first.ref,
				mode: first.mode,
				color: first.color,
				route: first.route,
			};
		} catch {
			return null;
		}
	}

	// ── Line service summary (title-bar info) ───────────────────────────────
	// Per-line service data baked into line_index.json by the pipeline:
	// weekday mask, first/last departure, per-window trips/hour, seasonal
	// operating period, per-terminus-pair variant rows. Loaded lazily when
	// the detail view opens; absent for indexes built before the feature.
	let lineService = $state<LineServiceInfo | null>(null);
	let serviceExpanded = $state(false);

	/** Merge the service blocks of a multi-key selection (same ref+mode
	 * across agencies): busiest entry carries the headline cadence, span
	 * and season, days union, variant rows concatenate. */
	function mergeServiceInfo(infos: LineServiceInfo[]): LineServiceInfo | null {
		if (!infos.length) return null;
		if (infos.length === 1) return infos[0];
		const busiest = infos.reduce((a, b) => (b.rpd > a.rpd ? b : a));
		const days = Array.from({ length: 7 }, (_, i) =>
			infos.some((s) => s.days[i] === '1') ? '1' : '0').join('');
		return {
			...busiest,
			days,
			variants: infos.flatMap((s) => s.variants)
				.sort((a, b) => b.rpd - a.rpd)
		};
	}

	function loadLineService(sel: LineDetailSelection) {
		lineService = null;
		serviceExpanded = false;
		const token = sel.keys.join(',');
		void loadLineIndex().then((index) => {
			if (!index || !lineDetail || lineDetail.keys.join(',') !== token) return;
			lineService = mergeServiceInfo(
				sel.keys.map((k) => index[k]?.service)
					.filter(Boolean) as LineServiceInfo[]);
		});
	}

	const SERVICE_DAY_ABBR = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'];

	function fmtServiceDays(mask: string): string {
		if (mask === '1111111') return 'daily';
		const runs: string[] = [];
		let i = 0;
		while (i < 7) {
			if (mask[i] !== '1') { i++; continue; }
			let j = i;
			while (j + 1 < 7 && mask[j + 1] === '1') j++;
			runs.push(j > i
				? `${SERVICE_DAY_ABBR[i]}–${SERVICE_DAY_ABBR[j]}`
				: SERVICE_DAY_ABBR[i]);
			i = j + 1;
		}
		return runs.join(', ') || '–';
	}

	/** Format a departure time rounded to the nearest quarter hour. */
	function fmtDep(secs: number): string {
		const q = Math.round(secs / 900) * 900;
		const h = Math.floor(q / 3600) % 24;
		const m = Math.floor((q % 3600) / 60);
		return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
	}

	/** Cadence on a day the line runs: regular service reads as a rate
	 * (headway / ×-per-hour / every 2 h); rarer than ~every 2 h or an
	 * irregular pattern falls back to runs per day. */
	function fmtCadence(rpd: number, dep?: [number, number], irr?: boolean): string {
		if (rpd <= 0) return '–';
		const perDay = `≈${Math.max(1, Math.round(rpd))}×/day`;
		if (irr || rpd < 3) return perDay;
		const spanMin = dep ? (dep[1] - dep[0]) / 60 : 17 * 60;
		const headway = spanMin / Math.max(1, rpd - 1);
		if (headway > 130) return perDay;
		if (headway < 24) return `every ~${Math.max(1, Math.round(headway))} min`;
		if (headway < 40) return '≈2×/h';
		if (headway < 80) return '≈1×/h';
		return 'every ~2 h';
	}

	function fmtDateShort(iso: string): string {
		return new Date(iso + 'T12:00:00')
			.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
	}

	function serviceSummary(svc: LineServiceInfo): string {
		const parts: string[] = [];
		if (svc.from && svc.to) {
			parts.push(`${fmtDateShort(svc.from)} – ${fmtDateShort(svc.to)}`);
		}
		parts.push(fmtServiceDays(svc.days));
		if (svc.dep) parts.push(`${fmtDep(svc.dep[0])}–${fmtDep(svc.dep[1])}`);
		parts.push(fmtCadence(svc.rpd, svc.dep, svc.irr));
		return parts.join(' · ');
	}

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

	// Stop-symbology layers filtered to member stations while the view is
	// active. Every feature in them carries the `line_keys` membership
	// string baked by step 07. Close-zoom (z17+) layers are out of scope.
	const LINE_DETAIL_FILTER_LAYERS = [
		...TRANSIT_STOP_DOT_LAYERS,
		...TRANSIT_STOP_PILL_LAYERS,
		...TRANSIT_STOP_LABEL_LAYERS,
		'transit-stop-indicator'
	];

	function badgeTextColor(hexColor: string): string {
		const lum = parseInt(hexColor.slice(1, 3), 16) * 0.299
			+ parseInt(hexColor.slice(3, 5), 16) * 0.587
			+ parseInt(hexColor.slice(5, 7), 16) * 0.114;
		return lum > 140 ? '#000' : '#fff';
	}

	// ── Label text-bbox hit test ────────────────────────────────────────────
	// Stop labels use MapLibre's `text-padding` for placement spacing, which
	// also inflates the layer's queryRenderedFeatures / hover collision box.
	// The click / cursor should only hit the actual rendered text — not the
	// padding gap. We rebuild the text bbox client-side (canvas measureText
	// on the same Saira font the map renders) and hit-test against it.
	//
	// Font sizes mirror scripts/style/transit_stations.py LABEL_SIZE_Z*
	// dicts (per stop_tier at zoom stops 7 / 10 / 12 / 13 / 14). Close-zoom
	// station_label features already carry `font_m` and are used directly.
	const LABEL_SIZE_STOPS: Record<number, Record<string, number>> = {
		7:  { major_train: 11, main_train: 10, important_train: 9,
		      major_mountain: 9, ferry_stop: 9 },
		10: { major_train: 16, main_train: 14, important_train: 12,
		      train_station: 11, small_train: 11,
		      major_mountain: 11, ferry_stop: 11 },
		12: { major_train: 20, main_train: 16, important_train: 14,
		      train_station: 12, small_train: 12,
		      major_mountain: 12, mountain_stop: 10, ferry_stop: 12,
		      major_hub: 11, big_station: 10, normal_stop: 10 },
		13: { major_train: 22, main_train: 18, important_train: 15,
		      train_station: 13, small_train: 13,
		      major_mountain: 13, mountain_stop: 11, ferry_stop: 13,
		      major_hub: 13, big_station: 11, normal_stop: 11 },
		14: { major_train: 24, main_train: 20, important_train: 17,
		      train_station: 15, small_train: 15,
		      major_mountain: 15, mountain_stop: 13, ferry_stop: 15,
		      major_hub: 15, big_station: 13, normal_stop: 13, small_bus: 12 }
	};
	const LABEL_SIZE_ZOOMS = [7, 10, 12, 13, 14];

	// Close-zoom station_label features carry `font_m` in metres (world
	// units); the style converts to pixels via _font_px_expr in
	// scripts/style/transit_stations.py, an exponential-base-2 interp
	// between z17 (px = m × PX_PER_M_Z17) and z22 (px = m × PX_PER_M_Z22).
	const PX_PER_M_Z17 = 2.455;
	const PX_PER_M_Z22 = PX_PER_M_Z17 * 32.0;
	function pxPerMAtZoom(zoom: number): number {
		if (zoom <= 17) return PX_PER_M_Z17;
		if (zoom >= 22) return PX_PER_M_Z22;
		const progress = (zoom - 17) / (22 - 17);
		const factor = Math.pow(2, progress) - 1; // exponential base 2
		return PX_PER_M_Z17 + factor * (PX_PER_M_Z22 - PX_PER_M_Z17);
	}

	function labelFontPx(props: Record<string, unknown>, zoom: number): number {
		if (typeof props.font_m === 'number') {
			// Convert font_m (metres) → pixels via the zoom-dependent scale.
			return (props.font_m as number) * pxPerMAtZoom(zoom);
		}
		const tier = String(props.stop_tier ?? 'normal_stop');
		let zLow = LABEL_SIZE_ZOOMS[0];
		let zHigh = LABEL_SIZE_ZOOMS[LABEL_SIZE_ZOOMS.length - 1];
		for (let i = 0; i < LABEL_SIZE_ZOOMS.length - 1; i++) {
			if (zoom <= LABEL_SIZE_ZOOMS[i + 1]) {
				zLow = LABEL_SIZE_ZOOMS[i];
				zHigh = LABEL_SIZE_ZOOMS[i + 1];
				break;
			}
		}
		const sLow = LABEL_SIZE_STOPS[zLow]?.[tier] ?? 10;
		const sHigh = LABEL_SIZE_STOPS[zHigh]?.[tier] ?? 10;
		const t = zHigh === zLow ? 0 : (zoom - zLow) / (zHigh - zLow);
		return sLow + t * (sHigh - sLow);
	}

	// Text-width memo keyed by font+size+text. `measureText` is O(μs) but
	// hover fires it 60+ times per second — the cache keeps repeated
	// mousemoves free.
	const textWidthCache = new Map<string, number>();
	let measureCtx: CanvasRenderingContext2D | null = null;
	function textWidthPx(text: string, fontPx: number, weight = '700'): number {
		const key = `${weight}|${fontPx.toFixed(2)}|${text}`;
		let w = textWidthCache.get(key);
		if (w !== undefined) return w;
		if (!measureCtx) {
			const c = document.createElement('canvas');
			measureCtx = c.getContext('2d');
		}
		if (!measureCtx) return text.length * fontPx * 0.55;
		measureCtx.font = `${weight} ${fontPx}px 'Saira', sans-serif`;
		w = measureCtx.measureText(text).width;
		textWidthCache.set(key, w);
		return w;
	}

	// A label feature that carries popup data (or is `station_label` /
	// `stop_label_anchor` — those don't have data on the anchor itself but
	// still gate clicks upstream). Returns true when the point sits inside
	// the label's rendered text rectangle.
	function isPointInLabelText(
		feat: maplibregl.MapGeoJSONFeature,
		point: { x: number; y: number },
		map: maplibregl.Map
	): boolean {
		const props = feat.properties as Record<string, unknown> | null;
		if (!props) return false;
		const text = String(props.display_name ?? props.name ?? props.stop_name ?? '');
		if (!text) return false;
		const geom = feat.geometry as { type: string; coordinates?: [number, number] } | null;
		if (!geom || geom.type !== 'Point' || !geom.coordinates) return false;
		const anchorPx = map.project(geom.coordinates);
		const zoom = map.getZoom();
		const fontPx = labelFontPx(props, zoom);
		if (!(fontPx > 0)) return false;
		const width = textWidthPx(text, fontPx, '700');
		const height = fontPx; // cap-height approximation
		// Text-anchor + text-offset per feature type. Matches the layer
		// definitions in scripts/style/transit_stations.py.
		const ft = String(props.feature_type ?? '');
		let cx = anchorPx.x;
		let cy = anchorPx.y;
		let leftEdge: number;
		let rightEdge: number;
		if (ft === 'stop_label_anchor') {
			// Pill label: anchor "left", offset [0.5em, -0.11em].
			leftEdge  = cx + 0.5 * fontPx;
			rightEdge = leftEdge + width;
			cy       += -0.11 * fontPx;
		} else if (ft === 'station_label') {
			// Close-zoom station label: anchor default centre, offset
			// [0, -0.11em].
			leftEdge  = cx - width / 2;
			rightEdge = cx + width / 2;
			cy       += -0.11 * fontPx;
		} else {
			// Far-zoom labels: default centre anchor.
			leftEdge  = cx - width / 2;
			rightEdge = cx + width / 2;
		}
		const halo = 2;
		return point.x >= leftEdge - halo
		    && point.x <= rightEdge + halo
		    && point.y >= cy - height / 2 - halo
		    && point.y <= cy + height / 2 + halo;
	}

	// Layer id looks like a stop label. Used to gate the bbox check without
	// touching non-label features (pills, dots, lines).
	function isLabelLayer(layerId: string | undefined): boolean {
		if (!layerId) return false;
		return layerId.startsWith('transit-stop-label-')
		    || layerId === 'close-zoom-station-label';
	}

	function enterLineDetail(sel: LineDetailSelection) {
		const map = mapRef;
		if (!map || !sel.keys?.length || !sel.bbox || sel.bbox.length !== 4) return;

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
		}
		if (!savedStopFilters) {
			savedStopFilters = new Map();
			for (const id of LINE_DETAIL_FILTER_LAYERS) {
				if (!map.getLayer(id)) continue;
				savedStopFilters.set(id, map.getFilter(id) ?? null);
			}
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

		lineDetail = sel;
		loadLineService(sel);
		syncLineDeepLinkToUrl(sel.keys);
	}

	function exitLineDetail() {
		const map = mapRef;
		lineDetail = null;
		lineService = null;
		serviceExpanded = false;
		syncLineDeepLinkToUrl(null);
		if (!map) {
			savedLinePaints = null;
			savedStopFilters = null;
			return;
		}
		if (map.getLayer(LINE_DETAIL_DIM_LAYER)) {
			map.setLayoutProperty(LINE_DETAIL_DIM_LAYER, 'visibility', 'none');
		}
		for (const id of [LINE_DETAIL_HIGHLIGHT_CASING, LINE_DETAIL_HIGHLIGHT_FILL]) {
			if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', 'none');
		}
		if (savedLinePaints) {
			for (const [id, props] of savedLinePaints) {
				for (const [prop, val] of Object.entries(props)) {
					map.setPaintProperty(id, prop as any, val as any);
				}
			}
		}
		if (savedStopFilters) {
			for (const [id, orig] of savedStopFilters) {
				map.setFilter(id, (orig ?? null) as any);
			}
		}
		savedLinePaints = null;
		savedStopFilters = null;
	}

	/** Delegated click handler for `[data-line-detail]` badges inside a
	 * popup: parses the encoded selection payload and enters the view. */
	function wireLineDetailClicks(p: maplibregl.Popup, closePopup: () => void) {
		const el = p.getElement();
		if (!el) return;
		el.addEventListener('click', (ev) => {
			const target = (ev.target as HTMLElement | null)?.closest?.('[data-line-detail]');
			if (!target) return;
			// Badges live inside <summary> — stop the details toggle.
			ev.preventDefault();
			ev.stopPropagation();
			try {
				const sel = JSON.parse(decodeURIComponent(
					target.getAttribute('data-line-detail') || ''));
				closePopup();
				enterLineDetail(sel);
			} catch { /* malformed payload — ignore */ }
		});
	}

	function addContourLayers(map: maplibregl.Map) {
		const meta = (style.metadata as any)?.['carfree:terrain'];
		const c = meta?.contours ?? {};
		const vis = contoursEnabled ? 'visible' : 'none';

		// Register the vector tile source that maplibre-contour serves via
		// its own protocol handler. Thresholds pair [minor_m, major_m] per
		// zoom band; below z9 no contours are generated.
		map.addSource(CONTOUR_SOURCE_ID, {
			type: 'vector',
			tiles: [
				getDemSource().contourProtocolUrl({
					multiplier: 1,
					thresholds: {
						9: [200, 1000],
						11: [100, 500],
						13: [50, 250],
						15: [10, 50]
					},
					elevationKey: 'ele',
					levelKey: 'level',
					contourLayer: 'contours'
				})
			],
			maxzoom: 15
		});

		// Insert contours just below the transit block (station backdrop +
		// lines + stops): above the basemap and roads, below transit.
		const beforeId = map.getLayer('close-zoom-station-backdrop')
			? 'close-zoom-station-backdrop'
			: undefined;

		map.addLayer({
			id: 'contour-line-minor',
			type: 'line',
			source: CONTOUR_SOURCE_ID,
			'source-layer': 'contours',
			minzoom: 9,
			filter: ['==', ['get', 'level'], 0],
			layout: { visibility: vis, 'line-join': 'round' },
			paint: {
				'line-color': c.color_minor ?? '#8a6a3c',
				'line-width': c.width_minor ?? 0.4,
				'line-opacity': c.opacity ?? 0.7
			}
		}, beforeId);

		map.addLayer({
			id: 'contour-line-major',
			type: 'line',
			source: CONTOUR_SOURCE_ID,
			'source-layer': 'contours',
			minzoom: 9,
			filter: ['==', ['get', 'level'], 1],
			layout: { visibility: vis, 'line-join': 'round' },
			paint: {
				'line-color': c.color_major ?? '#6a4a24',
				'line-width': c.width_major ?? 1.0,
				'line-opacity': c.opacity ?? 0.7
			}
		}, beforeId);

		map.addLayer({
			id: 'contour-label-major',
			type: 'symbol',
			source: CONTOUR_SOURCE_ID,
			'source-layer': 'contours',
			minzoom: 12,
			filter: ['==', ['get', 'level'], 1],
			layout: {
				visibility: vis,
				'symbol-placement': 'line',
				'symbol-spacing': 400,
				'text-field': ['concat', ['to-string', ['get', 'ele']], ' m'],
				'text-font': ['Saira Regular'],
				'text-size': c.label_size ?? 10,
				'text-max-angle': 25
			},
			paint: {
				'text-color': c.label_color ?? '#4a3624',
				'text-halo-color': c.label_halo ?? '#f4ecdccc',
				'text-halo-width': 1.2
			}
		}, beforeId);
	}

	function toggleContours() {
		contoursEnabled = !contoursEnabled;
		if (!mapRef) return;
		const vis = contoursEnabled ? 'visible' : 'none';
		for (const id of CONTOUR_LAYERS) {
			if (mapRef.getLayer(id)) mapRef.setLayoutProperty(id, 'visibility', vis);
		}
	}

	const DEBUG_STOP_LAYER = 'debug-stop-dot';

	// Splash screen (rendered in app.html) is faded out and removed on the
	// map's `load` event, once the first tiles for the resolved initial
	// center are rendered. Idempotent — safe to call multiple times.
	let splashHidden = false;
	function hideSplash() {
		if (splashHidden) return;
		splashHidden = true;
		const s = typeof document !== 'undefined'
			? document.getElementById('kora-splash')
			: null;
		if (!s) return;
		s.classList.add('kora-splash-hidden');
		setTimeout(() => s.remove(), 400);
	}

	function setSplashStatus(text: string) {
		const el = typeof document !== 'undefined'
			? document.getElementById('kora-splash-status')
			: null;
		if (el) el.textContent = text;
	}

	$effect(() => {
		// Safety net: if map creation or the load event never completes
		// (tile server down, script error), don't strand the user on the
		// splash forever.
		const safety = setTimeout(hideSplash, 15000);

		// Bake the default view into the style before map creation so the
		// first frame already matches — no flash on load. DEFAULT_VIEW (a
		// plain const, not the reactive viewMode) so this effect never
		// re-runs (and recreates the map) on a view toggle.
		for (const layer of style.layers) {
			const l = layer as maplibregl.LayerSpecification & { layout?: object };
			if (STOP_SYMBOLOGY_LAYERS.includes(layer.id)) {
				l.layout = {
					...l.layout,
					visibility: DEFAULT_VIEW === 'transit-focus' ? 'visible' : 'none'
				};
			} else if (PLACE_LABEL_LAYERS.includes(layer.id)) {
				l.layout = {
					...l.layout,
					visibility: DEFAULT_VIEW === 'standard' ? 'visible' : 'none'
				};
			}
		}

		// A position hash in the URL (shared link / reload) overrides the
		// style default and suppresses geolocation below.
		const initialPos = readPositionHash();
		const hasUrlHash = initialPos !== null;
		const deepLinkKeys = readLineDeepLinkFromUrl();

		const map = new maplibregl.Map({
			container,
			style,
			// Style default center (Swiss overview) unless the URL carries a
			// position; a geolocation fix arriving below re-centers behind
			// the splash.
			center: initialPos?.center ?? (style.center as [number, number]) ?? [0, 0],
			zoom: initialPos?.zoom ?? style.zoom ?? 2,
			bearing: initialPos?.bearing ?? 0,
			pitch: initialPos?.pitch ?? 0,
			attributionControl: false
		});

		// Keep the URL hash in sync with the camera (router-aware
		// replacement for MapLibre's `hash: true`). moveend covers user
		// gestures and programmatic jumps alike; hashchange covers manual
		// URL edits and back/forward (replaceState never fires hashchange,
		// so the two can't feed back into each other).
		map.on('moveend', () => writePositionHash(map));
		map.on('movestart', closeMenuOnSmallScreen);
		map.on('click', closeMenuOnSmallScreen);
		const onHashChange = () => {
			const pos = readPositionHash();
			if (pos) map.jumpTo(pos);
		};
		window.addEventListener('hashchange', onHashChange);

		(window as any).map = map;
		mapRef = map;

		// Deep-link resolution runs in parallel with style load; the fetch
		// runs alongside tile/glyph loading and is awaited inside the
		// map.on('load') handler below.
		const deepLinkPromise: Promise<LineDetailSelection | null> = deepLinkKeys
			? resolveLineDeepLink(deepLinkKeys)
			: Promise.resolve(null);

		// Geolocation runs in PARALLEL with map/tile loading — the map loads
		// at the style default underneath the splash, and a fix arriving
		// re-centers via jumpTo while still hidden. The splash lifts only
		// when both the map has loaded and geolocation has settled (fix,
		// error, or timeout), so the jump is never visible. Skipped when the
		// URL carries an explicit view (#zoom/lat/lng hash or ?line= deep
		// link). maybeHideSplash waits for map.loaded() so a late jump's
		// freshly requested tiles render before the reveal.
		const wantsGeolocation = !hasUrlHash && !deepLinkKeys
			&& typeof navigator !== 'undefined' && !!navigator.geolocation;
		let mapLoaded = false;
		let geoSettled = !wantsGeolocation;
		const maybeHideSplash = () => {
			if (!mapLoaded || !geoSettled) return;
			if (map.loaded()) hideSplash();
			else map.once('idle', hideSplash);
		};
		if (wantsGeolocation) {
			const settleGeo = () => {
				geoSettled = true;
				setSplashStatus('loading map...');
				maybeHideSplash();
			};
			navigator.geolocation.getCurrentPosition(
				(pos) => {
					map.jumpTo({
						center: [pos.coords.longitude, pos.coords.latitude],
						zoom: 13
					});
					settleGeo();
				},
				settleGeo,
				{ timeout: 4000, maximumAge: 5 * 60_000 }
			);
		}

		// Navigation controls (zoom +/-, compass)
		map.addControl(new maplibregl.NavigationControl(), 'top-right');

		// Compact attribution in the corner
		map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');

		// Scale bar (metric) — shows real-world distance for the current zoom
		map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');

		// Keep zoom indicator in sync
		const updateZoom = () => {
			zoom = parseFloat(map.getZoom().toFixed(2));
		};
		map.on('load', updateZoom);
		map.on('zoom', updateZoom);

		// Debug tooltip: click a transit line to see its properties
		let popup: maplibregl.Popup | null = null;

		map.on('load', () => {
			// Splash screen (see app.html) — lift once geolocation has also
			// settled; until then the status shows what we're waiting for.
			mapLoaded = true;
			if (!geoSettled) setSplashStatus('waiting for location...');
			maybeHideSplash();

			// Sync the view in case the user toggled before the style
			// finished loading (the baked default only covers 'standard').
			applyViewMode(map, viewMode);

			addContourLayers(map);

			// Pointer cursor when hovering transit lines and stops.
			// Uses a single mousemove handler (not per-layer mouseenter)
			// so the label bbox test can decide per-cursor-position whether
			// the cursor is on actual label text vs the label's placement
			// padding — matches the click behaviour so cursor and click
			// always agree.
			const hoverLayers = [
				...TRANSIT_LINE_LAYERS,
				...TRANSIT_STOP_DOT_LAYERS,
				...TRANSIT_STOP_PILL_LAYERS,
				...TRANSIT_STOP_LABEL_LAYERS,
				...TRANSIT_PILL_ARROW_LAYERS,
				'close-zoom-station-label',
				DEBUG_STOP_LAYER
			];
			// Filter to layers actually registered in the style —
			// queryRenderedFeatures throws on any unknown id, and the debug
			// stop dot in particular is optional (see popups.md).
			const activeHoverLayers = hoverLayers.filter((id) => !!map.getLayer(id));
			map.on('mousemove', (e) => {
				const feats = map.queryRenderedFeatures(e.point, { layers: activeHoverLayers });
				let hit = false;
				for (const f of feats) {
					if (isLabelLayer(f.layer?.id)) {
						if (isPointInLabelText(f, e.point, map)) {
							hit = true;
							break;
						}
						// Padding hit — keep scanning for a non-label match.
						continue;
					}
					hit = true;
					break;
				}
				map.getCanvas().style.cursor = hit ? 'pointer' : '';
			});

			// Deep link (line-detail-view.md § Deep link): once the style
			// is loaded, apply the pre-fetched selection. Unknown / malformed
			// keys drop the param silently.
			if (deepLinkKeys) {
				deepLinkPromise.then((sel) => {
					if (sel) enterLineDetail(sel);
					else syncLineDeepLinkToUrl(null);
				});
			}
		});

		map.on('click', (e) => {
			if (popup) { popup.remove(); popup = null; }
			const fmt = (v: unknown) => v == null ? '–' : String(v);

			// Debug stop dot takes highest priority — these are the data probe.
			const debugStopFeatures = map.getLayer(DEBUG_STOP_LAYER)
				? map.queryRenderedFeatures(e.point, { layers: [DEBUG_STOP_LAYER] })
				: [];
			if (debugStopFeatures.length) {
				const p = debugStopFeatures[0].properties as Record<string, unknown>;
				const lengthVal = typeof p.platform_length === 'number'
					? `${p.platform_length} m`
					: p.platform_length ? `${p.platform_length} m` : '– (default)';
				let linesHtml = '';
				if (p.lines_json) {
					try {
						const lines: { ref: string; color: string; mode: string;
							origin: string; destination: string; osm_ids?: string[] }[] =
							JSON.parse(String(p.lines_json));
						const currentOsmId = p.current_osm_id != null ? String(p.current_osm_id) : '';
						if (lines.length) {
							const badges = lines.map(l => {
								const label = l.ref || l.mode || '?';
								const c = (l.color || '#888888').replace('#', '');
								const r = parseInt(c.slice(0, 2), 16);
								const g = parseInt(c.slice(2, 4), 16);
								const b = parseInt(c.slice(4, 6), 16);
								const lum = r * 0.299 + g * 0.587 + b * 0.114;
								const fg = lum > 140 ? '#000' : '#fff';
								const route = `${l.origin || '?'} → ${l.destination || '?'}`;
								const titleAttr = ` title="${route.replace(/"/g, '&quot;')}"`;
								const isCurrent = currentOsmId !== '' && Array.isArray(l.osm_ids)
									&& l.osm_ids.includes(currentOsmId);
								const ring = isCurrent
									? 'box-shadow:0 0 0 2px #000, 0 0 0 4px #fff;'
									: '';
								return `<span${titleAttr} style="display:inline-block;background:#${c};color:${fg};border-radius:3px;padding:1px 5px;margin:3px 4px 3px 0;font-size:10px;font-weight:600;letter-spacing:0.03em;cursor:default;${ring}">${label}</span>`;
							}).join('');
							linesHtml = `<div style="margin-top:6px">${badges}</div>`;
						}
					} catch { /* ignore malformed */ }
				}
				const html = `<div style="font-family:'Saira',sans-serif;font-size:12px;line-height:1.5">
					<b>${fmt(p.stop_name) || '(no name)'}</b> &ensp;[${fmt(p.mode)}]<br>
					id: ${fmt(p.stop_id)}<br>
					platform length: ${lengthVal}
					${linesHtml}
				</div>`;
				popup = new maplibregl.Popup({ maxWidth: '320px' })
					.setLngLat(e.lngLat)
					.setHTML(html)
					.addTo(map);
				return;
			}

			// Station click takes priority over line click. Includes label
			// layers so clicking a stop's name text opens its popup too.
			// Pill labels (z14-17) and close-zoom station labels (z17+)
			// don't carry the popup data on the label feature itself —
			// they're dedicated label-anchor / label-only features. Skip
			// past them to the underlying pill/dot; for z17+ where pills
			// stop rendering, fall back to querying the pill source by
			// parent_station.
			const stopFeatures = map.queryRenderedFeatures(e.point, {
				layers: [
					...TRANSIT_STOP_PILL_LAYERS,
					...TRANSIT_STOP_DOT_LAYERS,
					...TRANSIT_STOP_LABEL_LAYERS,
					'close-zoom-station-label',
				]
			});
			// Skip label-anchor / label-only features that carry no popup
			// payload; pick the first hit that actually has lines_json.
			// Pill-label anchors (z14–17) never carry it, but the underlying
			// pill feature is also in the hit list. Close-zoom station
			// labels (z17+) DO carry it (baked at build time), so a click
			// on the text opens the popup even though pills stop rendering.
			let stopFeature: maplibregl.MapGeoJSONFeature | null = null;
			for (const f of stopFeatures) {
				const props = f.properties as Record<string, unknown> | null;
				if (!props) continue;
				if (!(props.lines_json || props.dep_hr !== undefined)) continue;
				// For label features, verify the click landed on the actual
				// rendered text — not the placement-padding zone that
				// MapLibre's own hit test would otherwise accept.
				if (isLabelLayer(f.layer?.id) && !isPointInLabelText(f, e.point, map)) {
					continue;
				}
				stopFeature = f;
				break;
			}
			if (stopFeature) {
				const p = stopFeature.properties as Record<string, unknown>;

				// Per-zoom lookup: far-zoom absorbers carry lines_json_zN
				// and dep_hr_zN reflecting the lines / departures folded in
				// at that zoom (see stops-far-zoom-dot-redesign.md and
				// popups.md). Pills (z ≥ 14) use the base fields.
				const zoomFloor = Math.max(7, Math.min(12, Math.floor(map.getZoom())));
				const linesRaw = (p as Record<string, unknown>)[`lines_json_z${zoomFloor}`]
					?? p.lines_json;
				const depHrAtZoom = (p as Record<string, unknown>)[`dep_hr_z${zoomFloor}`];
				const depHr = typeof depHrAtZoom === 'number'
					? depHrAtZoom
					: (typeof p.dep_hr === 'number' ? p.dep_hr as number : null);

				let depLine = '';
				if (typeof depHr === 'number' && depHr > 0) {
					const disp = depHr < 10 ? depHr.toFixed(1) : String(Math.round(depHr));
					depLine = `<div style="margin-top:2px">Departures: <b>${disp}</b>/h</div>`;
				}

				let linesHtml = '';
				if (linesRaw) {
					try {
						const lines: {
							ref: string; color: string; mode: string;
							name?: string; tooltip?: string;
							keys?: string[]; bbox?: number[]; route?: string;
						}[] = JSON.parse(String(linesRaw));
						if (lines.length) {
							// Flat alternating children: badge, terminus, badge, terminus…
							// Collapsed mode hides termini and lets badges flow with
							// flex-wrap; expanded mode switches to a 2-col grid whose
							// first column is `max-content` — every badge stretches to
							// the widest label width so terminus text aligns.
							const cells = lines.map(l => {
								const label = l.ref || l.mode || '?';
								const lum = parseInt(l.color.slice(1, 3), 16) * 0.299
									+ parseInt(l.color.slice(3, 5), 16) * 0.587
									+ parseInt(l.color.slice(5, 7), 16) * 0.114;
								const fg = lum > 140 ? '#000' : '#fff';
								const tip = l.tooltip || l.name || '';
								const titleAttr = tip
									? ` title="${tip.replace(/"/g, '&quot;')}"`
									: '';
								// Line-detail payload: present only when the tiles carry
								// the baked keys + bbox (line-detail-view.md).
								const canDetail = Array.isArray(l.keys) && l.keys.length > 0
									&& Array.isArray(l.bbox) && l.bbox.length === 4;
								const dataAttr = canDetail
									? ` data-line-detail="${encodeURIComponent(JSON.stringify({
										keys: l.keys, bbox: l.bbox, ref: l.ref || '',
										mode: l.mode || '', color: l.color,
										route: l.route || l.tooltip || ''
									}))}"`
									: '';
								const cursor = canDetail ? 'cursor:pointer' : 'cursor:default';
								const badge = `<span class="popup-badge"${titleAttr}${dataAttr} style="background:${l.color};color:${fg};${cursor}">${label}</span>`;
								const terminus = `<span class="popup-line-terminus">${tip.replace(/</g, '&lt;')}</span>`;
								return badge + terminus;
							}).join('');

							linesHtml = `<details class="popup-lines">
								<summary class="popup-lines-summary">
									<span class="popup-chevron">▸</span>
									<span class="popup-lines-list">${cells}</span>
								</summary>
							</details>`;
						}
					} catch { /* ignore malformed */ }
				}

				const html = `<style>
					.popup-lines { margin-top: 6px; }
					.popup-lines-summary { list-style: none; cursor: pointer; display: flex; align-items: flex-start; gap: 4px; }
					.popup-lines-summary::-webkit-details-marker { display: none; }
					.popup-chevron { display: inline-block; color: #888; font-size: 9px; padding-top: 4px; transition: transform 0.15s ease; flex: 0 0 auto; }
					.popup-lines[open] .popup-chevron { transform: rotate(90deg); }
					.popup-badge { display: inline-block; border-radius: 3px; padding: 2px 6px; font-size: 11px; font-weight: 800; letter-spacing: 0.02em; cursor: default; text-align: center; }
					.popup-lines-list { display: flex; flex-wrap: wrap; gap: 4px 3px; }
					.popup-line-terminus { display: none; }
					.popup-lines[open] .popup-lines-list { display: grid; grid-template-columns: max-content 1fr; column-gap: 8px; row-gap: 3px; align-items: center; max-height: 200px; overflow-y: auto; overflow-x: hidden; }
					.popup-lines[open] .popup-badge { display: block; }
					.popup-lines[open] .popup-line-terminus { display: inline; color: #444; font-size: 12px; }
				</style><div style="font-family:'Saira',sans-serif;font-size:13px;line-height:1.4;color:#222">
					<div style="font-weight:700;font-size:15px">${fmt(p.stop_name) || '(no name)'}</div>
					${linesHtml}
					${depLine}
				</div>`;
				popup = new maplibregl.Popup({ maxWidth: '320px' })
					.setLngLat(e.lngLat)
					.setHTML(html)
					.addTo(map);
				wireLineDetailClicks(popup, () => { if (popup) { popup.remove(); popup = null; } });
				return;
			}

			// Pill-arrow popup (z17+): single-line summary for the specific
			// (station, line) the pill-arrow represents. Rendered as one row
			// in the same visual grid as the line popup (badge + A ↔ B).
			// See popups.md § Pill-arrow popup.
			const pillArrowHits = map.queryRenderedFeatures(e.point, {
				layers: TRANSIT_PILL_ARROW_LAYERS.filter((id) => !!map.getLayer(id))
			});
			if (pillArrowHits.length) {
				const pa = pillArrowHits[0].properties as Record<string, unknown>;
				const ref = String(pa.ref ?? '');
				const mode = String(pa.mode ?? '');
				const color = String(pa.color ?? '#888888');
				const stopName = String(pa.stop_name ?? '');
				const first = String(pa.first_terminus_name ?? '');
				const last  = String(pa.last_terminus_name ?? '');
				let route = '';
				if (first && last) route = first === last ? first : `${first} ↔ ${last}`;
				else if (first) route = first;
				else if (last)  route = last;
				const routeSafe = route.replace(/</g, '&lt;');
				const label = ref || mode || '?';
				const lum = parseInt(color.slice(1, 3), 16) * 0.299
					+ parseInt(color.slice(3, 5), 16) * 0.587
					+ parseInt(color.slice(5, 7), 16) * 0.114;
				const fg = lum > 140 ? '#000' : '#fff';
				// Line-detail-view payload: mirror of the station / line
				// popup badges. Enabled only when the tiles carry line_key
				// + line_bbox on the pill-arrow feature.
				const lineKey = String(pa.line_key ?? '');
				const bboxStr = String(pa.line_bbox ?? '');
				const bboxParts = bboxStr.split(',').map(Number);
				const canDetail = !!lineKey && bboxParts.length === 4
					&& bboxParts.every((n) => Number.isFinite(n));
				const dataAttr = canDetail
					? ` data-line-detail="${encodeURIComponent(JSON.stringify({
						keys: [lineKey],
						bbox: bboxParts,
						ref, mode, color,
						route
					}))}"`
					: '';
				const cursor = canDetail ? 'cursor:pointer' : 'cursor:default';
				const html = `<style>
					.popup-pa-title { font-weight:700; font-size:15px; margin-bottom:6px; }
					.popup-pa-row { display: grid; grid-template-columns: max-content 1fr; column-gap: 8px; align-items: center; }
					.popup-pa-badge { display: block; border-radius: 3px; padding: 2px 6px; font-size: 11px; font-weight: 800; letter-spacing: 0.02em; text-align: center; }
					.popup-pa-route { color: #444; font-size: 12px; }
				</style><div style="font-family:'Saira',sans-serif;font-size:13px;line-height:1.4;color:#222">
					<div class="popup-pa-title">${fmt(stopName) || '(no name)'}</div>
					<div class="popup-pa-row">
						<span class="popup-pa-badge"${dataAttr} style="background:${color};color:${fg};${cursor}">${label}</span>
						<span class="popup-pa-route">${routeSafe}</span>
					</div>
				</div>`;
				popup = new maplibregl.Popup({ maxWidth: '360px' })
					.setLngLat(e.lngLat)
					.setHTML(html)
					.addTo(map);
				wireLineDetailClicks(popup, () => { if (popup) { popup.remove(); popup = null; } });
				return;
			}

			// Line popup: widen the query to a 4-px bbox so parallel lines
			// on shared corridors are all captured (see popups.md § Line
			// popup / Capture set).
			const R = 4;
			const bbox: [maplibregl.PointLike, maplibregl.PointLike] = [
				[e.point.x - R, e.point.y - R],
				[e.point.x + R, e.point.y + R]
			];
			const lineFeatures = map.queryRenderedFeatures(bbox, { layers: TRANSIT_LINE_LAYERS });
			if (!lineFeatures.length) return;

			// Dedup by (ref, mode). Both directions of one line merge; branch
			// variants also merge — their termini fold into a single set that
			// forms the route text.
			const MODE_RANK: Record<string, number> = {
				train: 0, metro: 1, tram: 2, bus: 3, mountain: 4, ferry: 5, regional_bus: 6
			};
			const groups = new Map<string, {
				ref: string; mode: string; color: string; name: string;
				termini: Set<string>;
				lineKeys: Set<string>;
				bbox: [number, number, number, number] | null;
			}>();
			for (const f of lineFeatures) {
				const fp = f.properties as Record<string, unknown>;
				const ref = String(fp.ref ?? '');
				const mode = String(fp.mode ?? '');
				const key = `${ref} ${mode}`;
				let g = groups.get(key);
				if (!g) {
					g = {
						ref, mode,
						color: String(fp.color ?? '#888888'),
						name: String(fp.name ?? ''),
						termini: new Set<string>(),
						lineKeys: new Set<string>(),
						bbox: null,
					};
					groups.set(key, g);
				}
				const first = String(fp.first_terminus_name ?? '');
				const last  = String(fp.last_terminus_name ?? '');
				if (first) g.termini.add(first);
				if (last)  g.termini.add(last);
				// Line-detail identity + camera fit (baked at build time —
				// line-detail-view.md). The group bbox is identical on every
				// variant of one line; the union only matters when two
				// agencies share (ref, mode) in one badge row.
				if (fp.line_key) g.lineKeys.add(String(fp.line_key));
				const bboxRaw = String(fp.line_bbox ?? '');
				if (bboxRaw) {
					const parts = bboxRaw.split(',').map(Number);
					if (parts.length === 4 && parts.every((v) => Number.isFinite(v))) {
						const bb = parts as [number, number, number, number];
						g.bbox = g.bbox
							? [Math.min(g.bbox[0], bb[0]), Math.min(g.bbox[1], bb[1]),
							   Math.max(g.bbox[2], bb[2]), Math.max(g.bbox[3], bb[3])]
							: bb;
					}
				}
			}
			const lines = Array.from(groups.values()).sort((a, b) => {
				const ra = MODE_RANK[a.mode] ?? 99;
				const rb = MODE_RANK[b.mode] ?? 99;
				if (ra !== rb) return ra - rb;
				return a.ref.localeCompare(b.ref, undefined, { numeric: true });
			});

			// Cells: alternating badge + terminus; grid layout in the wrapper
			// gives every badge the same width (widest ref) and left-flushes
			// the terminus text — matches the expanded station popup.
			const cells = lines.map(l => {
				const label = l.ref || l.mode || '?';
				const lum = parseInt(l.color.slice(1, 3), 16) * 0.299
					+ parseInt(l.color.slice(3, 5), 16) * 0.587
					+ parseInt(l.color.slice(5, 7), 16) * 0.114;
				const fg = lum > 140 ? '#000' : '#fff';
				const termini = Array.from(l.termini);
				const route = termini.length === 2
					? `${termini[0]} ↔ ${termini[1]}`
					: termini.join(' · ');
				const routeSafe = route.replace(/</g, '&lt;');
				const canDetail = l.lineKeys.size > 0 && l.bbox !== null;
				const dataAttr = canDetail
					? ` data-line-detail="${encodeURIComponent(JSON.stringify({
						keys: Array.from(l.lineKeys), bbox: l.bbox, ref: l.ref,
						mode: l.mode, color: l.color, route
					}))}"`
					: '';
				const cursor = canDetail ? 'cursor:pointer' : 'cursor:default';
				const badge = `<span class="popup-badge"${dataAttr} style="background:${l.color};color:${fg};${cursor}">${label}</span>`;
				const terminus = `<span class="popup-line-terminus">${routeSafe}</span>`;
				return badge + terminus;
			}).join('');

			const html = `<style>
				.popup-line-list { font-family:'Saira',sans-serif; color:#222; }
				.popup-line-list .popup-badge { display: block; border-radius: 3px; padding: 2px 6px; font-size: 11px; font-weight: 800; letter-spacing: 0.02em; text-align: center; }
				.popup-line-list .popup-cells { display: grid; grid-template-columns: max-content 1fr; column-gap: 8px; row-gap: 3px; align-items: center; max-height: 200px; overflow-y: auto; overflow-x: hidden; }
				.popup-line-list .popup-line-terminus { color: #444; font-size: 12px; }
			</style><div class="popup-line-list">
				<div class="popup-cells">${cells}</div>
			</div>`;
			popup = new maplibregl.Popup({ maxWidth: '360px' })
				.setLngLat(e.lngLat)
				.setHTML(html)
				.addTo(map);
			wireLineDetailClicks(popup, () => { if (popup) { popup.remove(); popup = null; } });
		});

		return () => {
			clearTimeout(safety);
			window.removeEventListener('hashchange', onHashChange);
			lineDetail = null;
			savedLinePaints = null;
			savedStopFilters = null;
			mapRef = null;
			map.remove();
		};
	});
</script>

<svelte:window onkeydown={(e) => { if (e.key === 'Escape' && lineDetail) exitLineDetail(); }} />

<div class="map-wrap">
	<div bind:this={container} class="map"></div>

	{#if lineDetail}
		<div class="line-detail-bar" role="status">
			<div class="line-detail-head">
				<span
					class="line-detail-badge"
					style="background:{lineDetail.color};color:{badgeTextColor(lineDetail.color)}"
				>{lineDetail.ref || lineDetail.mode}</span>
				{#if lineDetail.route}
					<span class="line-detail-route">{lineDetail.route}</span>
				{/if}
				<button
					class="line-detail-close"
					onclick={exitLineDetail}
					aria-label="Close line detail view"
				>×</button>
			</div>
			{#if lineService}
				<div class="line-detail-summary">{serviceSummary(lineService)}</div>
				{#if serviceExpanded}
					<div class="line-detail-details" transition:slide={{ duration: 250 }}>
						<div class="line-detail-variants">
							{#each lineService.variants as v (v.route)}
								<div class="line-detail-variant">
									<span class="line-detail-variant-route">{v.route}</span>
									<span class="line-detail-variant-meta">
										{fmtServiceDays(v.days)}{#if v.dep}
											&nbsp;· {fmtDep(v.dep[0])}–{fmtDep(v.dep[1])}{/if}
										· {fmtCadence(v.rpd, v.dep, v.irr)}{#if v.from && v.to}
											&nbsp;· {fmtDateShort(v.from)} – {fmtDateShort(v.to)}{/if}
									</span>
								</div>
							{/each}
						</div>
					</div>
				{/if}
				{#if lineService.variants.length > 1}
					<button
						class="line-detail-toggle"
						onclick={() => (serviceExpanded = !serviceExpanded)}
						aria-label={serviceExpanded ? 'Hide line details' : 'Show line details'}
						aria-expanded={serviceExpanded}
					><span class="line-detail-chevron" class:flipped={serviceExpanded}>▾</span></button>
				{/if}
			{/if}
		</div>
	{/if}

	{#if !lineDetail}
		<div class="top-controls">
			<MapMenu {viewMode} {setView} {contoursEnabled} {toggleContours} bind:open={menuOpen} />
			{#if viewMode === 'transit-focus'}
				<StopSearch map={mapRef} />
			{/if}
		</div>
	{/if}

	<div class="zoom-badge" aria-label="Current zoom level">
		z&thinsp;{zoom}
	</div>
</div>

<style>
	.map-wrap {
		position: relative;
		width: 100vw;
		height: 100vh;
	}

	.map {
		width: 100%;
		height: 100%;
	}

	.top-controls {
		position: absolute;
		top: 1rem;
		left: 1rem;
		display: flex;
		gap: 0.5rem;
		align-items: flex-start;
	}

	@media (max-width: 600px) {
		.top-controls {
			/* Leave room for MapLibre's top-right NavigationControl
			   (~29 px + 10 px margin) so nothing overlaps its tap area. */
			right: 3.5rem;
		}
	}

	.line-detail-bar {
		position: absolute;
		top: 1rem;
		left: 50%;
		transform: translateX(-50%);
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		max-width: min(85vw, 34rem);
		background: #ffffff;
		border-radius: 1.1rem;
		box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3);
		padding: 0.6rem 0.9rem 0.6rem 1rem;
		font-family: 'Saira', 'Helvetica Neue', Arial, sans-serif;
		z-index: 5;
	}

	.line-detail-head {
		display: flex;
		align-items: center;
		gap: 0.65rem;
	}

	/* Push the action buttons to the right edge even when there is no
	   route text to grow into the gap. */
	.line-detail-head > button:first-of-type {
		margin-left: auto;
	}

	.line-detail-summary {
		font-size: 0.75rem;
		color: #666;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.line-detail-details {
		border-top: 1px solid #eee;
		margin-top: 0.35rem;
		padding-top: 0.45rem;
		max-height: 45vh;
		overflow-y: auto;
		font-size: 0.8rem;
		color: #333;
	}

	.line-detail-variants {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}

	.line-detail-variant {
		display: flex;
		flex-direction: column;
	}

	.line-detail-variant-route {
		font-weight: 600;
		font-size: 0.78rem;
	}

	.line-detail-variant-meta {
		color: #666;
		font-size: 0.72rem;
	}

	.line-detail-toggle {
		border: none;
		background: transparent;
		color: #555;
		font-size: 0.8rem;
		line-height: 1;
		cursor: pointer;
		/* Span the card's horizontal padding so the strip runs edge to
		   edge and closes off the bottom of the bar. */
		margin: 0.25rem -0.9rem -0.6rem -1rem;
		padding: 0.3rem 0 0.4rem;
		border-top: 1px solid #eee;
		border-radius: 0 0 1.1rem 1.1rem;
	}

	.line-detail-toggle:hover {
		background: #f5f5f5;
		color: #000;
	}

	.line-detail-chevron {
		display: inline-block;
		transition: transform 0.25s ease;
	}

	.line-detail-chevron.flipped {
		transform: rotate(180deg);
	}

	.line-detail-badge {
		border-radius: 3px;
		padding: 2px 8px;
		font-size: 0.8rem;
		font-weight: 800;
		letter-spacing: 0.02em;
		white-space: nowrap;
	}

	.line-detail-route {
		font-size: 0.85rem;
		color: #333;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.line-detail-close {
		border: none;
		background: transparent;
		color: #555;
		font-size: 1.1rem;
		line-height: 1;
		cursor: pointer;
		padding: 0.15rem 0.35rem;
		border-radius: 999px;
		flex: 0 0 auto;
	}

	.line-detail-close:hover {
		background: #eee;
		color: #000;
	}

	.zoom-badge {
		position: absolute;
		bottom: 2rem;
		left: 50%;
		transform: translateX(-50%);
		background: rgba(0, 0, 0, 0.55);
		color: #fff;
		font-family: 'ui-monospace', 'SFMono-Regular', 'Menlo', monospace;
		font-size: 0.75rem;
		letter-spacing: 0.05em;
		padding: 0.25rem 0.6rem;
		border-radius: 999px;
		pointer-events: none;
		backdrop-filter: blur(4px);
		-webkit-backdrop-filter: blur(4px);
		user-select: none;
	}
</style>
