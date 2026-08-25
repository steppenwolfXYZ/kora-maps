// Label text-bbox hit test. Stop labels use MapLibre's `text-padding`
// for placement spacing, which also inflates the layer's
// queryRenderedFeatures / hover collision box. The click / cursor should
// only hit the actual rendered text — not the padding gap. We rebuild
// the text bbox client-side (canvas measureText on the same Saira font
// the map renders) and hit-test against it.
//
// Font sizes mirror scripts/style/transit_stations.py LABEL_SIZE_Z*
// dicts (per stop_tier at zoom stops 7 / 10 / 12 / 13 / 14). Close-zoom
// station_label features already carry `font_m` and are used directly.

import type maplibregl from 'maplibre-gl';

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
export function isPointInLabelText(
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
export function isLabelLayer(layerId: string | undefined): boolean {
	if (!layerId) return false;
	return layerId.startsWith('transit-stop-label-')
	    || layerId === 'close-zoom-station-label';
}
