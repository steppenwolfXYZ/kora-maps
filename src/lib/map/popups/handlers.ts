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
	buildLinePopupHtml,
	type LinePopupGroup
} from './html';
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

export function installClickPopups(map: maplibregl.Map, cb: PopupCallbacks) {
	let popup: maplibregl.Popup | null = null;
	const closePopup = () => { if (popup) { popup.remove(); popup = null; } };

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
				cb.onEnterLineDetail(sel);
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
				cb.onRouteEndpoint(side, payload);
				closePopup();
			} catch { /* malformed payload — ignore */ }
		});
	}

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

			// UIC comes from `parent_station` (bare UIC) or the
			// un-suffixed `stop_id`; coord from the feature's own
			// geometry (not the click position).
			const stopGeom = stopFeature.geometry as {
				type: string; coordinates?: [number, number]
			} | null;
			const stopCoord: [number, number] | null =
				stopGeom?.type === 'Point' && stopGeom.coordinates
					? [stopGeom.coordinates[0], stopGeom.coordinates[1]]
					: null;
			const stopUic = String(
				p.parent_station ?? String(p.stop_id ?? '').split(':')[0] ?? ''
			);

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

			const html = buildStationPopupHtml({
				stopName: String(p.stop_name ?? ''),
				uic: stopUic,
				coord: stopCoord,
				depHr,
				linesRaw
			});
			popup = new maplibregl.Popup({ maxWidth: '320px' })
				.setLngLat(e.lngLat)
				.setHTML(html)
				.addTo(map);
			wireLineDetailClicks(popup);
			wirePopupRouteClicks(popup);
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
			popup = new maplibregl.Popup({ maxWidth: '360px' })
				.setLngLat(e.lngLat)
				.setHTML(html)
				.addTo(map);
			wireLineDetailClicks(popup);
			wirePopupRouteClicks(popup);
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

		popup = new maplibregl.Popup({ maxWidth: '360px' })
			.setLngLat(e.lngLat)
			.setHTML(buildLinePopupHtml(lines))
			.addTo(map);
		wireLineDetailClicks(popup);
	});
}
