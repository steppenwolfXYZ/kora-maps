// Follow-me camera for bicycle navigation (bicycle-navigation.md
// § Follow-me map): heading up, tilted, close zoom, rider in the lower
// part of the viewport so the screen shows the road ahead. The map is
// created with maxPitch 0 (planning views are flat), so entering raises
// the ceiling and leaving restores it.

import type maplibregl from 'maplibre-gl';

const NAV_ZOOM = 17;
const NAV_PITCH = 55;
const NAV_MAX_PITCH = 60;
/** The rider sits this fraction of the viewport height below centre. */
const RIDER_OFFSET_FRACTION = 0.22;
/** Camera moves at the position update cadence, eased linearly so
 * consecutive fixes chain into one continuous motion. */
const FOLLOW_MS = 900;
const FIRST_MOVE_MS = 1200;

export interface NavCameraSaved {
	maxPitch: number;
}

export function enterNavCamera(map: maplibregl.Map): NavCameraSaved {
	const saved = { maxPitch: map.getMaxPitch() };
	map.setMaxPitch(NAV_MAX_PITCH);
	return saved;
}

export function followRider(
	map: maplibregl.Map,
	coord: [number, number],
	heading: number | null,
	firstMove: boolean
) {
	const h = map.getContainer().clientHeight;
	const opts: maplibregl.EaseToOptions = {
		center: coord,
		bearing: heading ?? map.getBearing(),
		pitch: NAV_PITCH,
		zoom: NAV_ZOOM,
		offset: [0, h * RIDER_OFFSET_FRACTION],
		duration: firstMove ? FIRST_MOVE_MS : FOLLOW_MS,
		essential: true
	};
	if (!firstMove) opts.easing = (t) => t;
	map.easeTo(opts);
}

/** Back to the flat, north-up planning view. The pitch ceiling can only
 * drop once the camera is flat again — setMaxPitch below the current
 * pitch would snap the view. */
export function exitNavCamera(map: maplibregl.Map, saved: NavCameraSaved) {
	map.easeTo({ pitch: 0, bearing: 0, duration: 500 });
	map.once('moveend', () => {
		try { map.setMaxPitch(saved.maxPitch); } catch { /* map gone */ }
	});
}
