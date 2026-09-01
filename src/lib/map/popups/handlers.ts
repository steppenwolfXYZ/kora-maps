// Map popup interaction wiring (popups.md): the hover cursor, the click
// orchestration (feature queries in priority order debug → station →
// pill-arrow → line, payload extraction), and the delegated click
// handlers inside the popup HTML ([data-line-detail] badges,
// [data-route-endpoint] buttons). Feature actions are injected as
// callbacks so this module stays free of routing / line-detail state.

import maplibregl from 'maplibre-gl';
import {
	TRANSIT_LINE_LAYERS,
	TRANSIT_STOP_DOT_LAYERS,
	TRANSIT_STOP_LABEL_LAYERS,
	TRANSIT_STOP_PILL_LAYERS,
	TRANSIT_PILL_ARROW_LAYERS,
	DEBUG_STOP_LAYER
} from '../layers';
import { isLabelLayer, isPointInLabelText } from '../labelHitTest';
import {
	buildDebugStopPopupHtml,
	buildStationPopupHtml,
	buildPillArrowPopupHtml,
	buildPlacePopupHtml,
	buildLinePopupHtml,
	type LinePopupGroup,
	type StationPopupData
} from './html';
import { reverseAddress } from '$lib/geocoding/client';
import type { GeocodeResult } from '$lib/geocoding/client';
import type { LineDetailSelection } from '../../linedetail/lineIndex';

/** Parsed payload of a popup's Route from/to button. `uic` / `name`
 * arrive as decoded JSON values — the caller resolves them against the
 * station index. */
export interface RouteEndpointRequest {
	uic?: unknown;
	name?: unknown;
	coord: [number, number];
}

export interface PopupCallbacks {
	onEnterLineDetail: (sel: LineDetailSelection) => void;
	onRouteEndpoint: (side: 'from' | 'to', req: RouteEndpointRequest) => void;
}

/** Pointer cursor when hovering transit lines and stops. Uses a single
 * mousemove handler (not per-layer mouseenter) so the label bbox test
 * can decide per-cursor-position whether the cursor is on actual label
 * text vs the label's placement padding — matches the click behaviour
 * so cursor and click always agree. Call inside map.on('load'). */
export function installHoverCursor(map: maplibregl.Map) {
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
}

// ── Popup ownership ─────────────────────────────────────────────────────
// One popup at a time, held in module scope so the programmatic openers
// (search-bar selection — see openStationPopup / openPlacePopup below)
// share it with the click path: opening either closes whatever was open.

let popup: maplibregl.Popup | null = null;
let callbacks: PopupCallbacks | null = null;

function closePopup() {
	if (popup) { popup.remove(); popup = null; }
}

/** Open a popup, replacing any current one, with both delegated click
 * handlers wired. Handlers are no-ops on popups whose HTML carries no
 * matching elements. */
function showPopup(
	map: maplibregl.Map,
	lngLat: maplibregl.LngLatLike,
	html: string,
	maxWidth = '320px'
): maplibregl.Popup {
	closePopup();
	const p = new maplibregl.Popup({ maxWidth })
		.setLngLat(lngLat)
		.setHTML(html)
		.addTo(map);
	wireLineDetailClicks(p);
	wirePopupRouteClicks(p);
	popup = p;
	return p;
}

/** Delegated click handler for `[data-line-detail]` badges inside a
 * popup: parses the encoded selection payload and enters the view. */
function wireLineDetailClicks(p: maplibregl.Popup) {
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
			callbacks?.onEnterLineDetail(sel);
		} catch { /* malformed payload — ignore */ }
	});
}

/** Delegated click handler for the station popup's Route from/to
 * buttons — hands the parsed endpoint payload to the callback and
 * closes the popup. See transit-routing.md § Entry points / Station
 * popup buttons. */
function wirePopupRouteClicks(p: maplibregl.Popup) {
	const el = p.getElement();
	if (!el) return;
	el.addEventListener('click', (ev) => {
		const target = (ev.target as HTMLElement | null)?.closest?.('[data-route-endpoint]') as HTMLElement | null;
		if (!target) return;
		ev.preventDefault();
		ev.stopPropagation();
		try {
			const payload = JSON.parse(decodeURIComponent(
				target.getAttribute('data-route-endpoint') || ''));
			const side = target.getAttribute('data-route-side') === 'to' ? 'to' : 'from';
			callbacks?.onRouteEndpoint(side, payload);
			closePopup();
		} catch { /* malformed payload — ignore */ }
	});
}

/** Representative coord of a stop feature. Stop dots, pills and
 * connectors are LineStrings — dots are zero-length `[pos, pos]` lines,
 * a pill body runs along the stop's line position (project.md § Transit
 * stop architecture) — so a Point-only extraction would leave every
 * non-label hit without route buttons. Lines collapse to the midpoint of
 * their two ends: exact for a dot, the pill's centre for a pill body. */
function featureCoord(geometry: unknown): [number, number] | null {
	const g = geometry as { type?: string; coordinates?: unknown } | null;
	if (!g || !g.coordinates) return null;
	const pt = (c: unknown): [number, number] | null =>
		Array.isArray(c) && typeof c[0] === 'number' && typeof c[1] === 'number'
			? [c[0], c[1]] : null;
	if (g.type === 'Point') return pt(g.coordinates);
	let line: unknown[] | null = null;
	if (g.type === 'LineString') line = g.coordinates as unknown[];
	else if (g.type === 'MultiLineString') {
		const first = (g.coordinates as unknown[])[0];
		line = Array.isArray(first) ? first : null;
	}
	if (!line || !line.length) return null;
	const a = pt(line[0]);
	const b = pt(line[line.length - 1]);
	if (!a) return null;
	if (!b) return a;
	return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
}

/** Station popup payload for one stop feature. UIC comes from
 * `parent_station` (bare UIC) or the un-suffixed `stop_id`; the coord is
 * the feature's own geometry, never the click position. Far-zoom
 * absorbers carry per-zoom `lines_json_zN` / `dep_hr_zN` reflecting what
 * they fold in at that zoom (stops-far-zoom-dot-redesign.md, popups.md);
 * pills (z ≥ 14) use the base fields. */
function stationPopupData(
	map: maplibregl.Map,
	f: maplibregl.MapGeoJSONFeature
): StationPopupData {
	const p = f.properties as Record<string, unknown>;
	const coord = featureCoord(f.geometry);
	const zoomFloor = Math.max(7, Math.min(12, Math.floor(map.getZoom())));
	const depHrAtZoom = p[`dep_hr_z${zoomFloor}`];
	return {
		stopName: String(p.stop_name ?? ''),
		uic: stationUic(p),
		coord,
		depHr: typeof depHrAtZoom === 'number'
			? depHrAtZoom
			: (typeof p.dep_hr === 'number' ? p.dep_hr as number : null),
		linesRaw: p[`lines_json_z${zoomFloor}`] ?? p.lines_json
	};
}

function stationUic(p: Record<string, unknown>): string {
	return String(p.parent_station ?? String(p.stop_id ?? '').split(':')[0] ?? '');
}

/** Wire the map's click popups. Registers the feature-action callbacks
 * used by every popup this module opens, including the programmatic
 * openers below. */
export function installClickPopups(map: maplibregl.Map, cb: PopupCallbacks) {
	callbacks = cb;
	map.on('click', (e) => {
		closePopup();

		// Debug stop dot takes highest priority — these are the data probe.
		const debugStopFeatures = map.getLayer(DEBUG_STOP_LAYER)
			? map.queryRenderedFeatures(e.point, { layers: [DEBUG_STOP_LAYER] })
			: [];
		if (debugStopFeatures.length) {
			const p = debugStopFeatures[0].properties as Record<string, unknown>;
			const html = buildDebugStopPopupHtml({
				stopName: p.stop_name,
				mode: p.mode,
				stopId: p.stop_id,
				platformLength: p.platform_length,
				linesJson: p.lines_json,
				currentOsmId: p.current_osm_id != null ? String(p.current_osm_id) : ''
			});
			showPopup(map, e.lngLat, html);
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
			showPopup(map, e.lngLat,
				buildStationPopupHtml(stationPopupData(map, stopFeature)));
			return;
		}

		// Pill-arrow popup (z17+). See popups.md § Pill-arrow popup.
		const pillArrowHits = map.queryRenderedFeatures(e.point, {
			layers: TRANSIT_PILL_ARROW_LAYERS.filter((id) => !!map.getLayer(id))
		});
		if (pillArrowHits.length) {
			const pa = pillArrowHits[0].properties as Record<string, unknown>;
			const paGeom = pillArrowHits[0].geometry as { type: string; coordinates?: unknown } | null;
			// Pill-arrow features are polygons — take a representative
			// point (e.g. the first ring's first coord) for the endpoint.
			let paCoord: [number, number] | null = null;
			if (paGeom?.type === 'Polygon' && Array.isArray(paGeom.coordinates)) {
				const ring = (paGeom.coordinates as number[][][])[0];
				if (ring && ring[0] && ring[0].length >= 2) {
					paCoord = [ring[0][0], ring[0][1]];
				}
			}
			const html = buildPillArrowPopupHtml({
				ref: String(pa.ref ?? ''),
				mode: String(pa.mode ?? ''),
				color: String(pa.color ?? '#888888'),
				stopName: String(pa.stop_name ?? ''),
				uic: String(
					pa.parent_station ?? String(pa.stop_id ?? '').split(':')[0] ?? ''
				),
				coord: paCoord,
				firstTerminus: String(pa.first_terminus_name ?? ''),
				lastTerminus: String(pa.last_terminus_name ?? ''),
				lineKey: String(pa.line_key ?? ''),
				lineBbox: String(pa.line_bbox ?? '')
			});
			showPopup(map, e.lngLat, html, '360px');
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
			const key = `${ref} ${mode}`;
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
		const lines: LinePopupGroup[] = Array.from(groups.values())
			.sort((a, b) => {
				const ra = MODE_RANK[a.mode] ?? 99;
				const rb = MODE_RANK[b.mode] ?? 99;
				if (ra !== rb) return ra - rb;
				return a.ref.localeCompare(b.ref, undefined, { numeric: true });
			})
			.map((g) => ({
				ref: g.ref, mode: g.mode, color: g.color, name: g.name,
				termini: Array.from(g.termini),
				lineKeys: Array.from(g.lineKeys),
				bbox: g.bbox
			}));

		showPopup(map, e.lngLat, buildLinePopupHtml(lines), '360px');
	});
}

// ── Programmatic openers (search-bar selection) ─────────────────────────
// The main search bar flies the map to the picked result and then opens
// its popup — see stop-search.md § Selection. Both openers reuse the
// module's single popup slot, so a search popup and a click popup can
// never coexist.

/** Run `fn` once the camera has settled and tiles are loaded, so a
 * feature query right after a flyTo sees the destination. Falls back to
 * a timeout when `idle` never arrives (continuous user interaction). */
function afterCameraSettles(map: maplibregl.Map, fn: () => void) {
	if (!map.isMoving() && map.areTilesLoaded()) { fn(); return; }
	let done = false;
	const finish = () => {
		if (done) return;
		done = true;
		clearTimeout(timer);
		map.off('idle', finish);
		fn();
	};
	const timer = setTimeout(finish, 2500);
	map.on('idle', finish);
}

/** Open the station popup for a search hit. The popup content comes from
 * the rendered stop feature (line badges, departures/h) when one is
 * found at the station's coord; without a hit — the station is filtered
 * out at this zoom, or the transit layers are hidden — it degrades to
 * name + route buttons. */
export function openStationPopup(
	map: maplibregl.Map,
	station: { name: string; uic: string; coord: [number, number] }
) {
	afterCameraSettles(map, () => {
		const layers = [
			...TRANSIT_STOP_PILL_LAYERS,
			...TRANSIT_STOP_DOT_LAYERS
		].filter((id) => !!map.getLayer(id));
		const pt = map.project(station.coord);
		const R = 40;
		let feats: maplibregl.MapGeoJSONFeature[] = [];
		if (layers.length) {
			feats = map.queryRenderedFeatures(
				[[pt.x - R, pt.y - R], [pt.x + R, pt.y + R]], { layers });
		}
		// Only the station's own feature may supply content — a
		// neighbour's badges would be plain wrong.
		let hit: maplibregl.MapGeoJSONFeature | null = null;
		for (const f of feats) {
			const p = f.properties as Record<string, unknown> | null;
			if (!p) continue;
			if (!(p.lines_json || p.dep_hr !== undefined)) continue;
			if (stationUic(p) !== station.uic) continue;
			hit = f;
			break;
		}
		const data: StationPopupData = hit
			? stationPopupData(map, hit)
			: { stopName: station.name, uic: station.uic, coord: station.coord,
			    depHr: null, linesRaw: null };
		showPopup(map, data.coord ?? station.coord, buildStationPopupHtml(data));
	});
}

/** Open the place popup for a geocoded search hit (POI / address).
 * Addresses are their own label, so they render immediately; a POI's
 * street address is not part of the forward-search result and is filled
 * in by a reverse lookup once it returns (popups.md § Place popup). */
export function openPlacePopup(map: maplibregl.Map, place: GeocodeResult) {
	const kind = place.kind;
	const data = {
		title: place.displayName,
		address: null as string | null,
		kind,
		coord: place.coord
	};
	const p = showPopup(map, place.coord, buildPlacePopupHtml(data));
	if (kind === 'address') return;
	void reverseAddress(place.coord[0], place.coord[1]).then((addr) => {
		// Superseded by another popup in the meantime — drop it.
		if (!addr || popup !== p) return;
		// The container element (and its delegated listeners) survives
		// setHTML; only the content node is replaced.
		p.setHTML(buildPlacePopupHtml({ ...data, address: addr }));
	});
}
