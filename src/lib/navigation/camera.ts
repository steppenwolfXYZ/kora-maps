// Follow-me camera for bicycle navigation (bicycle-navigation.md
// § Follow-me map): heading up, tilted, rider in the lower part of the
// viewport so the screen shows the road ahead. Zoom frames the road up
// to the next change of direction — out on the approach so the turn is
// in view, tight at the turn, out again once it is passed — with speed
// as a second term; tilt follows zoom. The map is created with maxPitch
// 0 (planning views are flat), so entering raises the ceiling and
// leaving restores it.

import type maplibregl from 'maplibre-gl';

const ZOOM_NEAR = 18;
const ZOOM_FAR = 15.5;
const PITCH_NEAR = 58;
const PITCH_FAR = 42;
const NAV_MAX_PITCH = 60;
/** While following, the rider is drawn as a fixed screen element this
 * far above the bottom edge (NavigationOverlay's .nav-arrow — keep in
 * sync); the camera places the position exactly there. */
export const RIDER_BOTTOM_PX = 150;
/** Fixed arrow box vs map-marker box — the handover scale. */
export const RIDER_FIXED_PX = 88;
export const RIDER_MARKER_PX = 64;
/** Screen space the banner takes off the top of the road view. */
const BANNER_PX = 130;
/** Metres kept beyond the maneuver point so the turn itself is framed,
 * not just reached. */
const AHEAD_MARGIN_M = 40;
/** Speed term: from SPEED_ZERO_MS upward the camera backs off, up to
 * SPEED_MAX_ZOOM_OUT zoom levels at SPEED_FULL_MS. */
const SPEED_ZERO_MS = 3;
const SPEED_FULL_MS = 9;
const SPEED_MAX_ZOOM_OUT = 0.6;
/** Hysteresis: target moves smaller than this are ignored; larger ones
 * are approached by half each fix, so the zoom glides rather than
 * hunts. */
const ZOOM_DEADBAND = 0.15;
const ZOOM_APPROACH = 0.5;
/** Camera moves at the position update cadence, eased linearly so
 * consecutive fixes chain into one continuous motion. */
const FOLLOW_MS = 900;
/** The entry / re-center move; the handover to the fixed arrow happens
 * when this has elapsed (orchestration). */
export const FIRST_MOVE_MS = 1200;
const EARTH_CIRCUMFERENCE_M = 40075016.686;

export interface NavCameraSaved {
	maxPitch: number;
}

export interface FollowContext {
	/** Metres to the next maneuver point, null when unknown. */
	distanceToNextM: number | null;
	/** Ground speed in m/s, null when the platform gives none. */
	speedMs: number | null;
}

let zoom = ZOOM_NEAR - 1;

export function enterNavCamera(map: maplibregl.Map): NavCameraSaved {
	const saved = { maxPitch: map.getMaxPitch() };
	map.setMaxPitch(NAV_MAX_PITCH);
	zoom = ZOOM_NEAR - 1;
	return saved;
}

/** Zoom at which `metresAhead` of road fits between the rider and the
 * banner on a flat map — the tilt only adds ground beyond that, so the
 * maneuver point always stays inside the view. */
function zoomForAhead(map: maplibregl.Map, lat: number, metresAhead: number): number {
	const h = map.getContainer().clientHeight;
	const pxAhead = Math.max(120, h - RIDER_BOTTOM_PX - BANNER_PX);
	const metresPerPx = metresAhead / pxAhead;
	return Math.log2((EARTH_CIRCUMFERENCE_M * Math.cos((lat * Math.PI) / 180)) / (512 * metresPerPx));
}

function targetZoom(map: maplibregl.Map, lat: number, ctx: FollowContext): number {
	let z = ctx.distanceToNextM === null
		? ZOOM_NEAR - 1
		: zoomForAhead(map, lat, ctx.distanceToNextM + AHEAD_MARGIN_M);
	const v = ctx.speedMs ?? 0;
	const t = Math.min(1, Math.max(0, (v - SPEED_ZERO_MS) / (SPEED_FULL_MS - SPEED_ZERO_MS)));
	z -= t * SPEED_MAX_ZOOM_OUT;
	return Math.min(ZOOM_NEAR, Math.max(ZOOM_FAR, z));
}

/** Eases the camera onto the rider; returns the pitch it is heading
 * for, so the fixed on-screen arrow can wear the same perspective. */
export function followRider(
	map: maplibregl.Map,
	coord: [number, number],
	heading: number | null,
	firstMove: boolean,
	ctx: FollowContext
): number {
	const target = targetZoom(map, coord[1], ctx);
	if (firstMove) zoom = target;
	else if (Math.abs(target - zoom) >= ZOOM_DEADBAND) zoom += (target - zoom) * ZOOM_APPROACH;
	const k = (zoom - ZOOM_FAR) / (ZOOM_NEAR - ZOOM_FAR);
	const pitch = PITCH_FAR + (PITCH_NEAR - PITCH_FAR) * k;
	const h = map.getContainer().clientHeight;
	const opts: maplibregl.EaseToOptions = {
		center: coord,
		bearing: heading ?? map.getBearing(),
		pitch,
		zoom,
		offset: [0, h / 2 - RIDER_BOTTOM_PX],
		duration: firstMove ? FIRST_MOVE_MS : FOLLOW_MS,
		essential: true
	};
	if (!firstMove) opts.easing = (t) => t;
	map.easeTo(opts);
	return pitch;
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
