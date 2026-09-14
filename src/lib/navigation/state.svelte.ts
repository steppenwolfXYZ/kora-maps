// Reactive bicycle navigation state (bicycle-navigation.md). One
// instance shared across the app, following the routingState pattern:
// module-level runes, an exported object of getters and actions. Owns
// the sensors, the rider's projection onto the route, off-route
// detection with its recalculation, arrival, and the local persistence
// that lets a reload resume the ride. Map-side effects (camera, rider
// marker, overlay) live in map/orchestration.svelte.ts and only read
// from here.

import { browser } from '$app/environment';
import { pushState } from '$app/navigation';
import { page } from '$app/state';
import type { DirectRoute } from '../routing/types';
import { fetchNavigationRoute } from '../routing/valhalla';
import { geolocationErrorMessage, markGeolocationDenied } from '../routing/geolocation.svelte';
import { mapUi } from '../map/uiState.svelte';
import {
	blendHeading, buildGeometry, distanceM, projectOntoRoute,
	type LonLat, type RouteGeometry
} from './geometry';
import {
	advanceManeuverIndex, computeGuidance, initialManeuverIndex, type Guidance
} from './guidance';
import {
	getFirstFix, requestCompassPermission, ScreenWakeLock, watchCompass, watchPosition,
	type PositionFix
} from './sensors';

/** Off-route once the projected distance exceeds this for OFF_ROUTE_HOLD_MS
 * (concept § Off-route detection). */
const OFF_ROUTE_DIST_M = 30;
const OFF_ROUTE_HOLD_MS = 5000;
/** Recalculations are rate-limited to one per this interval; failures
 * back off exponentially up to the max. */
const RECALC_MIN_INTERVAL_MS = 10_000;
const RECALC_RETRY_MAX_MS = 60_000;
/** Within this of the destination the ride counts as arrived; the
 * arrival message lingers before navigation ends on its own. */
const ARRIVAL_RADIUS_M = 25;
const ARRIVAL_LINGER_MS = 8000;
/** GPS course is trusted only above this speed; below it the compass
 * takes over once the last good course is older than COURSE_STALE_MS. */
const COURSE_MIN_SPEED_MS = 1.5;
const COURSE_STALE_MS = 4000;
const COMPASS_APPLY_MS = 500;
/** A via counts as passed once progress is this far beyond it. */
const VIA_PASSED_MARGIN_M = 30;
/** A fix older than this makes the banner say so. */
const POSITION_STALE_MS = 30_000;
const STORAGE_KEY = 'kora.navigation';
const RESUME_MAX_AGE_MS = 12 * 3600 * 1000;
const CLOCK_TICK_MS = 15_000;

let active = $state(false);
let starting = $state(false);
let route = $state.raw<DirectRoute | null>(null);
let fix = $state.raw<PositionFix | null>(null);
let heading = $state<number | null>(null);
let following = $state(true);
let progressM = $state(0);
let offRouteM = $state(0);
let offRoute = $state(false);
let recalculating = $state(false);
let updateFailed = $state(false);
let arrived = $state(false);
let maneuverIdx = $state(0);
// Wall clock for the ETA; bumped on every fix and by a slow ticker.
let now = $state(0);

// Non-reactive internals. `geometry` always changes together with
// `route` (installRoute sets it first), so deriveds keyed on `route`
// see a matching geometry.
let geometry: RouteGeometry | null = null;
let stopWatch: (() => void) | null = null;
let stopCompass: (() => void) | null = null;
let wakeLock: ScreenWakeLock | null = null;
let compassHeading: number | null = null;
let lastCourseAt = 0;
let lastCompassApplyAt = 0;
let offSince: number | null = null;
let lastRecalcAt = 0;
let retryDelayMs = RECALC_MIN_INTERVAL_MS;
let nextRetryAt = 0;
let recalcAbort: AbortController | null = null;
let arrivalTimer: ReturnType<typeof setTimeout> | null = null;
let clockTimer: ReturnType<typeof setInterval> | null = null;
let startedAt = 0;
// Whether this ride pushed its history entry (page.state.navigation).
// Browser back pops it → the orchestration effect ends navigation; an
// explicit stop consumes it with history.back() so the entry never
// lingers as a dead forward step.
let pushedEntry = false;

let guidance: Guidance | null = $derived.by(() => {
	if (!route || !geometry) return null;
	return computeGuidance(route, geometry, progressM, maneuverIdx, now);
});

function persist() {
	if (!browser || !route) return;
	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify({ route, startedAt }));
	} catch {
		// Storage full or unavailable — the ride just won't survive a reload.
	}
}

function clearPersisted() {
	if (!browser) return;
	try { localStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
}

/** Swap in a route (start, or a recalculation): rebuild the geometry,
 * reset progress and the off-route judgement, re-project the last fix. */
function installRoute(r: DirectRoute) {
	geometry = buildGeometry(r.coords);
	route = r;
	progressM = 0;
	offRouteM = 0;
	offRoute = false;
	offSince = null;
	maneuverIdx = initialManeuverIndex(r);
	if (fix) applyFix(fix);
}

function updateHeadingFromFix(f: PositionFix) {
	if (f.courseDeg !== null && (f.speedMs ?? 0) >= COURSE_MIN_SPEED_MS) {
		lastCourseAt = f.at;
		heading = heading === null ? f.courseDeg : blendHeading(heading, f.courseDeg, 0.6);
	}
}

/** Compass samples arrive at tens of hertz; they only reach the
 * reactive heading throttled, and only while the GPS course is stale
 * (standing at a light, walking the bike) — course always wins while
 * it is valid, since compasses are so often miscalibrated. */
function onCompass(deg: number) {
	compassHeading = deg;
	const t = Date.now();
	if (t - lastCompassApplyAt < COMPASS_APPLY_MS) return;
	if (t - lastCourseAt <= COURSE_STALE_MS) return;
	lastCompassApplyAt = t;
	const next = heading === null ? deg : blendHeading(heading, deg, 0.3);
	if (heading === null || Math.abs(((next - heading + 540) % 360) - 180) > 2) heading = next;
}

function applyFix(f: PositionFix) {
	fix = f;
	now = f.at;
	updateHeadingFromFix(f);
	if (!route || !geometry) return;
	const proj = projectOntoRoute(geometry, f.coord, progressM);
	progressM = proj.cumM;
	offRouteM = proj.distM;
	maneuverIdx = advanceManeuverIndex(route, geometry, progressM, maneuverIdx);

	if (arrived) return;
	const toGoal = distanceM(f.coord, route.requestedTo);
	const nearEnd = geometry.totalM - progressM < ARRIVAL_RADIUS_M && proj.distM < OFF_ROUTE_DIST_M;
	if (toGoal < ARRIVAL_RADIUS_M || nearEnd) {
		arrive();
		return;
	}

	// Off-route: sustained distance, and only from fixes precise enough
	// to be evidence — a 60 m accuracy circle 40 m off the line says
	// nothing (concept § Off-route detection).
	const evidence = proj.distM > OFF_ROUTE_DIST_M && f.accuracyM < proj.distM;
	if (evidence) {
		if (offSince === null) offSince = f.at;
	} else {
		offSince = null;
		offRoute = false;
		updateFailed = false;
	}
	if (offSince !== null && f.at - offSince >= OFF_ROUTE_HOLD_MS) offRoute = true;
	if (
		offRoute && !recalculating
		&& f.at - lastRecalcAt >= RECALC_MIN_INTERVAL_MS
		&& f.at >= nextRetryAt
	) {
		void recalculate();
	}
}

/** The requested vias still ahead of the rider — judged on the current
 * route, since that is where progress is measured. */
function remainingVias(r: DirectRoute): LonLat[] {
	const g = geometry;
	if (!g) return r.requestedVias;
	return r.requestedVias.filter((v) => {
		const p = projectOntoRoute(g, v, null);
		return progressM <= p.cumM + VIA_PASSED_MARGIN_M;
	});
}

/** New route from the current position to the original destination
 * (concept § Off-route detection). Guidance keeps running on the old
 * route until this succeeds; a failure backs off and leaves a status
 * line for the rider (§ Connection loss). */
async function recalculate() {
	if (!route || !fix) return;
	const r0 = route;
	const f = fix;
	recalculating = true;
	lastRecalcAt = f.at;
	recalcAbort?.abort();
	const ac = new AbortController();
	recalcAbort = ac;
	try {
		const r = await fetchNavigationRoute({
			mode: 'bike',
			from: f.coord,
			to: r0.requestedTo,
			vias: remainingVias(r0)
		}, ac.signal);
		if (ac.signal.aborted || !active) return;
		if (!r) throw new Error('No route found from the current position');
		installRoute(r);
		updateFailed = false;
		retryDelayMs = RECALC_MIN_INTERVAL_MS;
		nextRetryAt = 0;
		persist();
	} catch (e) {
		if ((e as Error).name === 'AbortError' || !active) return;
		console.warn('[navigation] recalculation failed:', e);
		updateFailed = true;
		nextRetryAt = Date.now() + retryDelayMs;
		retryDelayMs = Math.min(retryDelayMs * 2, RECALC_RETRY_MAX_MS);
	} finally {
		if (recalcAbort === ac) {
			recalcAbort = null;
			recalculating = false;
		}
	}
}

function arrive() {
	arrived = true;
	offRoute = false;
	offSince = null;
	updateFailed = false;
	recalcAbort?.abort();
	arrivalTimer = setTimeout(() => stop(), ARRIVAL_LINGER_MS);
}

function onWatchError(err: GeolocationPositionError) {
	if (err.code === 1) {
		markGeolocationDenied();
		mapUi.showToast('Location permission denied — navigation ended.');
		stop();
	}
	// Timeouts and transient unavailability recover on their own; the
	// banner reports a stale position meanwhile.
}

function onVisibility() {
	if (document.visibilityState === 'visible' && active) {
		// Back in the foreground: the wake lock re-acquires itself; the
		// watch delivers a fresh fix; following resumes from it.
		following = true;
		now = Date.now();
	}
}

/** Begin guiding `r`. Needs a location fix first: on denial or timeout
 * nothing changes and the existing location error message shows
 * (concept § Entering and leaving). Call from the user's gesture — the
 * compass permission request must run inside it. */
async function start(r: DirectRoute, resume = false): Promise<void> {
	if (active || starting || r.mode !== 'bike') return;
	starting = true;
	const compassPermission = requestCompassPermission();
	let first: PositionFix;
	try {
		first = await getFirstFix();
	} catch (e) {
		if ((e as { code?: number })?.code === 1) markGeolocationDenied();
		mapUi.showToast(geolocationErrorMessage(e));
		starting = false;
		return;
	}
	if (!resume) startedAt = Date.now();
	active = true;
	following = true;
	arrived = false;
	updateFailed = false;
	heading = null;
	compassHeading = null;
	lastCourseAt = 0;
	lastRecalcAt = 0;
	retryDelayMs = RECALC_MIN_INTERVAL_MS;
	nextRetryAt = 0;
	fix = null;
	installRoute(r);
	applyFix(first);
	// Starting away from the planned route (concept § Entering and
	// leaving): no five-second hold — reroute from where the rider is
	// right now. The persisted route is the planned one until the new
	// route lands (recalculate() persists it).
	if (!arrived && offRouteM > OFF_ROUTE_DIST_M) {
		offRoute = true;
		void recalculate();
	}
	persist();

	wakeLock = new ScreenWakeLock();
	if (!wakeLock.supported) {
		mapUi.showToast('This browser cannot keep the screen on — it may lock during navigation.');
	}
	void wakeLock.acquire();
	stopWatch = watchPosition(applyFix, onWatchError);
	void compassPermission.then((ok) => {
		if (ok && active && !stopCompass) stopCompass = watchCompass(onCompass);
	});
	clockTimer = setInterval(() => { now = Date.now(); }, CLOCK_TICK_MS);
	document.addEventListener('visibilitychange', onVisibility);
	// Same URL, one entry deeper: back leaves navigation and nothing
	// else. Navigation never rides in the URL itself (concept § Entering
	// and leaving). A resume after a reload lands on the entry that
	// already carries the flag — don't stack a second one.
	if (!page.state.navigation) pushState('', { ...page.state, navigation: true });
	pushedEntry = true;
	starting = false;
}

/** End navigation (× button, arrival, permission loss). Every sensor
 * stops here — planning mode keeps its on-demand location behaviour. */
function stop(): void {
	if (!active && !starting) return;
	// Decide before tearing down: an explicit stop while our entry is
	// still on top pops it; a stop triggered BY a back navigation finds
	// the flag already gone and must not step back a second time.
	const consumeEntry = pushedEntry && page.state.navigation === true;
	pushedEntry = false;
	active = false;
	starting = false;
	stopWatch?.();
	stopWatch = null;
	stopCompass?.();
	stopCompass = null;
	wakeLock?.release();
	wakeLock = null;
	recalcAbort?.abort();
	recalcAbort = null;
	recalculating = false;
	if (arrivalTimer) clearTimeout(arrivalTimer);
	arrivalTimer = null;
	if (clockTimer) clearInterval(clockTimer);
	clockTimer = null;
	if (browser) document.removeEventListener('visibilitychange', onVisibility);
	route = null;
	geometry = null;
	fix = null;
	heading = null;
	arrived = false;
	offRoute = false;
	updateFailed = false;
	following = true;
	clearPersisted();
	if (consumeEntry) history.back();
}

/** Resume a ride persisted before a reload (concept § Entering and
 * leaving). Silently does nothing without a fresh, sane record. */
async function tryResume(): Promise<boolean> {
	if (!browser || active || starting) return false;
	let saved: { route?: DirectRoute; startedAt?: number } | null = null;
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (!raw) return false;
		saved = JSON.parse(raw);
	} catch {
		return false;
	}
	const r = saved?.route;
	if (
		!r || typeof saved?.startedAt !== 'number'
		|| Date.now() - saved.startedAt > RESUME_MAX_AGE_MS
		|| r.mode !== 'bike' || !Array.isArray(r.coords) || !Array.isArray(r.maneuvers)
	) {
		clearPersisted();
		return false;
	}
	startedAt = saved.startedAt;
	await start(r, true);
	if (!active) clearPersisted();
	return active;
}

export const navigation = {
	get active() { return active; },
	get starting() { return starting; },
	get route() { return route; },
	get fix() { return fix; },
	get heading() { return heading; },
	get following() { return following; },
	get progressM() { return progressM; },
	get offRoute() { return offRoute; },
	get offRouteM() { return offRouteM; },
	get recalculating() { return recalculating; },
	get updateFailed() { return updateFailed; },
	get arrived() { return arrived; },
	get guidance() { return guidance; },
	get positionStale() { return fix !== null && now - fix.at > POSITION_STALE_MS; },

	start,
	stop,
	tryResume,

	/** A map gesture: keep guiding, stop moving the camera. */
	suspendFollow() {
		if (active) following = false;
	},
	resumeFollow() {
		following = true;
	}
};
