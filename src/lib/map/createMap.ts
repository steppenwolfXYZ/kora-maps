// Map plumbing: creates the MapLibre map with its controls, gestures
// (context menu, long-press), URL-hash camera sync, splash lifecycle,
// and the load-time basics (view mode, contours, hover cursor). Feature
// wiring — popups, deep links, route overlay — happens in
// orchestration.svelte.ts, which receives the created map.

import maplibregl from 'maplibre-gl';
import { markGeolocationDenied } from '../routing/geolocation.svelte';
import { applyViewMode, bakeViewModeVisibility } from './layers';
import { readPositionHash, writePositionHash } from './positionHash';
import { addContourLayers } from './contours';
import { installHoverCursor } from './popups/handlers';
import { mapUi, DEFAULT_VIEW } from './uiState.svelte';

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

export function createKoraMap(
	container: HTMLDivElement,
	style: maplibregl.StyleSpecification,
	/** When true, a hashchange (from a feature's history.back() close)
	 * must not snap the camera — the view closes in place. */
	suppressHashJump: () => boolean
): { map: maplibregl.Map; destroy: () => void } {
	// Safety net: if map creation or the load event never completes
	// (tile server down, script error), don't strand the user on the
	// splash forever.
	const safety = setTimeout(hideSplash, 15000);

	// Bake the default view into the style before map creation so the
	// first frame already matches — no flash on load. DEFAULT_VIEW (a
	// plain const, not the reactive viewMode) so the caller's effect
	// never re-runs (and recreates the map) on a view toggle.
	bakeViewModeVisibility(style, DEFAULT_VIEW);

	// A position hash in the URL (shared link / reload) overrides the
	// style default.
	const initialPos = readPositionHash();

	const map = new maplibregl.Map({
		container,
		style,
		// Style default center (Swiss overview) unless the URL carries a
		// position. No startup geolocation — location is only requested
		// on explicit user action (locate button, routing).
		center: initialPos?.center ?? (style.center as [number, number]) ?? [0, 0],
		zoom: initialPos?.zoom ?? style.zoom ?? 2,
		bearing: initialPos?.bearing ?? 0,
		pitch: initialPos?.pitch ?? 0,
		maxPitch: 0,
		attributionControl: false
	});

	// Keep the URL hash in sync with the camera (router-aware
	// replacement for MapLibre's `hash: true`). moveend covers user
	// gestures and programmatic jumps alike; hashchange covers manual
	// URL edits and back/forward (replaceState never fires hashchange,
	// so the two can't feed back into each other).
	map.on('moveend', () => writePositionHash(map));
	map.on('movestart', mapUi.closeMenuOnSmallScreen);
	map.on('movestart', () => (mapUi.contextAnchor = null));
	map.on('click', mapUi.closeMenuOnSmallScreen);

	// Map context menu (transit-routing.md § Entry points / Map
	// context menu). Right-click on desktop opens it; on touch, a
	// long-press (~500 ms) that doesn't move opens it. Any other
	// interaction closes it.
	map.on('contextmenu', (ev) => {
		mapUi.contextAnchor = {
			x: ev.point.x,
			y: ev.point.y,
			lng: ev.lngLat.lng,
			lat: ev.lngLat.lat
		};
	});
	map.on('click', () => { mapUi.contextAnchor = null; });

	const LONG_PRESS_MS = 500;
	const LONG_PRESS_MAX_MOVE = 8;
	let touchTimer: number | null = null;
	let touchStartPx: { x: number; y: number } | null = null;
	function cancelLongPress() {
		if (touchTimer !== null) { clearTimeout(touchTimer); touchTimer = null; }
		touchStartPx = null;
	}
	container.addEventListener('touchstart', (ev) => {
		if (ev.touches.length !== 1) { cancelLongPress(); return; }
		const t = ev.touches[0];
		const rect = container.getBoundingClientRect();
		const px = { x: t.clientX - rect.left, y: t.clientY - rect.top };
		touchStartPx = px;
		touchTimer = window.setTimeout(() => {
			touchTimer = null;
			if (!touchStartPx) return;
			const ll = map.unproject([touchStartPx.x, touchStartPx.y]);
			mapUi.contextAnchor = { x: touchStartPx.x, y: touchStartPx.y, lng: ll.lng, lat: ll.lat };
		}, LONG_PRESS_MS);
	}, { passive: true });
	container.addEventListener('touchmove', (ev) => {
		if (!touchStartPx || !touchTimer) return;
		const t = ev.touches[0];
		if (!t) return;
		const rect = container.getBoundingClientRect();
		const dx = t.clientX - rect.left - touchStartPx.x;
		const dy = t.clientY - rect.top - touchStartPx.y;
		if (Math.hypot(dx, dy) > LONG_PRESS_MAX_MOVE) cancelLongPress();
	}, { passive: true });
	container.addEventListener('touchend', cancelLongPress, { passive: true });
	container.addEventListener('touchcancel', cancelLongPress, { passive: true });

	const onHashChange = () => {
		// A history.back() issued by the × close of the line-detail or
		// route view must not also snap the camera to the previous
		// entry's position hash — the view closes in place. Re-sync the
		// stale hash to the current camera instead. (A user-pressed back
		// keeps the jump: going back restores the previous viewport.)
		if (suppressHashJump()) {
			writePositionHash(map);
			return;
		}
		const pos = readPositionHash();
		if (pos) map.jumpTo(pos);
	};
	window.addEventListener('hashchange', onHashChange);

	(window as any).map = map;
	mapUi.mapRef = map;

	// Navigation controls (zoom +/-, compass)
	map.addControl(new maplibregl.NavigationControl(), 'top-right');

	// Compact attribution in the corner
	map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');

	// Locate button: centers on the device position, shows a dot marker
	// plus a translucent accuracy circle when the fix is imprecise.
	// trackUserLocation keeps following until the user pans away.
	// Added after NavigationControl so it stacks directly below it in
	// the top-right column. This button is the only place (besides a
	// routing query with a "Current location" endpoint) that triggers
	// the browser's location permission prompt.
	const geolocateControl = new maplibregl.GeolocateControl({
		positionOptions: { enableHighAccuracy: true },
		trackUserLocation: true,
		showAccuracyCircle: true,
		fitBoundsOptions: { maxZoom: 15 }
	});
	geolocateControl.on('error', (err: GeolocationPositionError) => {
		if (err.code === 1) markGeolocationDenied();
		mapUi.showToast(
			err.code === 1 ? 'Location permission denied.'
			: err.code === 3 ? 'Location request timed out.'
			: 'Location unavailable.'
		);
	});
	map.addControl(geolocateControl, 'top-right');

	// Scale bar (metric) — shows real-world distance for the current zoom
	map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');

	// Keep zoom indicator in sync
	const updateZoom = () => {
		mapUi.zoom = parseFloat(map.getZoom().toFixed(2));
	};
	map.on('load', updateZoom);
	map.on('zoom', updateZoom);

	map.on('load', () => {
		// Splash screen (see app.html) — lift once the first tiles for
		// the initial view have rendered.
		hideSplash();

		// Sync the view in case the user toggled before the style
		// finished loading (the baked default only covers 'standard').
		applyViewMode(map, mapUi.viewMode);

		addContourLayers(map, style, mapUi.contoursEnabled);

		installHoverCursor(map);
	});

	const destroy = () => {
		clearTimeout(safety);
		window.removeEventListener('hashchange', onHashChange);
		mapUi.contextAnchor = null;
		mapUi.menuOpen = false;
		mapUi.mapRef = null;
		map.remove();
	};

	return { map, destroy };
}
