// URL position hash sync. Replaces MapLibre's `hash: true`, whose
// internal writer calls raw window.history.replaceState and so conflicts
// with SvelteKit's router. Same URL format as MapLibre:
// #zoom/lat/lng[/bearing[/pitch]], so previously shared links keep
// working.

import { replaceState } from '$app/navigation';
import { page } from '$app/state';
import type maplibregl from 'maplibre-gl';

export function readPositionHash(): {
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

export function writePositionHash(map: maplibregl.Map) {
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
	// Preserve page.state — wiping it would drop the line-detail
	// view's history marker on every camera move (see the
	// back/forward $effect in Map.svelte).
	replaceState(url, page.state);
}
