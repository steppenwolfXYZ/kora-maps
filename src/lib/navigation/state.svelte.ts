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
import { fetchNavigationRoutes } from '../routing/valhalla';
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
	type CompassSample, type PositionFix
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
/** Live alternatives (concept § Live alternatives): at most this many
 * shown; only ones parting from the navigated route between MIN_AHEAD
 * and EARLY_MAX metres ahead count as early enough; a candidate point
 * further than DIVERGE_M from the navigated route marks the parting;
 * an alternative is spent SPENT_MARGIN past its parting point; the
 * bubble sits BUBBLE_AHEAD metres down the alternative from there. */
const ALT_MAX = 2;
const ALT_MIN_AHEAD_M = 40;
const ALT_EARLY_MAX_M = 1500;
const ALT_DIVERGE_M = 25;
const ALT_SPENT_MARGIN_M = 30;
const ALT_BUBBLE_AHEAD_M = 70;
/** Switching by riding: the rider must be past the alternative's own
 * parting point by this much, within OFF_ROUTE_DIST_M of it, and
 * clearly nearer to it than to the navigated route — near the parting
 * point both routes are close, and a jittery fix must not switch. */
const ALT_SWITCH_PAST_M = 20;
const ALT_SWITCH_NEARER_FACTOR = 0.5;
const ALT_FETCH_MIN_INTERVAL_MS = 10_000;
/** Refresh scheduling when nothing was kept: after a failed fetch;
 * when candidates parted too close ahead to act on; when no candidate
 * parted at all (the engine offered nothing) — retry further on. */
const ALT_RETRY_AFTER_M = 300;
const ALT_RETRY_TOO_CLOSE_M = 150;
const ALT_RETRY_NONE_M = 1000;

/** An alternative shown beside the navigated route. */
export interface NavAlternative {
	route: DirectRoute;
	geometry: RouteGeometry;
	/** Metres along the navigated route where the alternative parts. */
	divergeM: number;
	/** The same point measured along the alternative itself. */
	altDivergeM: number;
	/** Where the time-difference bubble sits: on the alternative, a
	 * little past the parting point. */
	bubbleCoord: LonLat;
	/** Seconds slower (+) or faster (−) than staying on the navigated
	 * route. */
	deltaSec: number;
}

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
let alternatives = $state.raw<NavAlternative[]>([]);
// TEMPORARY compass diagnostic shown in the banner: how the permission
// resolved, how many raw events arrived and what the last one carried.
let compassDebug = $state<{ permission: string; samples: number; last: CompassSample | null }>({
	permission: 'not asked', samples: 0, last: null
});

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
// Progress (metres along the navigated route) at which the alternatives
// are fetched again; null = nothing scheduled. Set when the shown ones
// are spent, when the earliest rejected candidate comes within reach,
// or after a switch.
let nextAltRefreshM: number | null = null;
let altFetchAt = 0;
let altAbort: AbortController | null = null;
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

/** Swap in a route (start, a recalculation, a switch to an
 * alternative): rebuild the geometry, reset progress and the off-route
 * judgement, re-project the last fix. */
function installRoute(r: DirectRoute, g?: RouteGeometry) {
	geometry = g ?? buildGeometry(r.coords);
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

	// Taking an alternative is done by riding it (concept § Live
	// alternatives): off the navigated route but on a shown alternative
	// past its parting point → that alternative is the route now. No
	// hold, no request.
	const precise = f.accuracyM < proj.distM;
	if (proj.distM > OFF_ROUTE_DIST_M && precise) {
		const taken = alternatives.find((a) => {
			const ap = projectOntoRoute(a.geometry, f.coord, null);
			return ap.distM <= OFF_ROUTE_DIST_M
				&& ap.distM < proj.distM * ALT_SWITCH_NEARER_FACTOR
				&& ap.cumM > a.altDivergeM + ALT_SWITCH_PAST_M;
		});
		if (taken) {
			switchToAlternative(taken);
			return;
		}
	}
	// Spent alternatives (parting point behind the rider) go; with none
	// left a refresh is due.
	if (alternatives.some((a) => progressM > a.divergeM + ALT_SPENT_MARGIN_M)) {
		alternatives = alternatives.filter((a) => progressM <= a.divergeM + ALT_SPENT_MARGIN_M);
		if (alternatives.length === 0) nextAltRefreshM = progressM;
	}
	if (nextAltRefreshM !== null && progressM >= nextAltRefreshM && !recalculating) {
		nextAltRefreshM = null;
		void refreshAlternatives();
	}
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

/** Where `cand` parts from the navigated route: walk the candidate
 * from its start while it stays on the navigated route; the first
 * point clearly off it marks the parting. Null when it never parts
 * (it IS the navigated route) or when it is off from its very first
 * point (no shared start — nothing to part from). */
function divergence(
	main: RouteGeometry,
	cand: DirectRoute
): { divergeM: number; altDivergeM: number; bubbleCoord: LonLat; geometry: RouteGeometry } | null {
	const cg = buildGeometry(cand.coords);
	let prevCum: number | null = null;
	let lastOn = -1;
	for (let i = 0; i < cand.coords.length; i += 2) {
		const p = projectOntoRoute(main, cand.coords[i], prevCum);
		if (p.distM > ALT_DIVERGE_M) {
			if (lastOn < 0 || prevCum === null) return null;
			const target = cg.cum[lastOn] + ALT_BUBBLE_AHEAD_M;
			let j = lastOn;
			while (j < cg.coords.length - 1 && cg.cum[j] < target) j++;
			return {
				divergeM: prevCum, altDivergeM: cg.cum[lastOn],
				bubbleCoord: cg.coords[j], geometry: cg
			};
		}
		prevCum = p.cumM;
		lastOn = i;
	}
	return null;
}

/** Turn engine candidates into the shown alternatives (concept § Live
 * alternatives): keep those parting early enough ahead of the rider,
 * earliest first, at most ALT_MAX. `refSec` is the time the navigated
 * route needs from the candidates' common start, so a candidate's
 * delta compares like with like. Candidates parting too far ahead
 * schedule a refresh for when the earliest of them comes within reach. */
function adoptAlternatives(cands: DirectRoute[], refSec: number, fromPlanning = false) {
	if (!geometry) return;
	const kept: NavAlternative[] = [];
	let earliestRejectedM: number | null = null;
	let tooClose = false;
	for (const c of cands) {
		const d = divergence(geometry, c);
		if (!d) continue;
		const ahead = d.divergeM - progressM;
		if (ahead < ALT_MIN_AHEAD_M) {
			tooClose = true;
			continue;
		}
		if (ahead > ALT_EARLY_MAX_M) {
			if (earliestRejectedM === null || d.divergeM < earliestRejectedM) earliestRejectedM = d.divergeM;
			continue;
		}
		kept.push({
			route: c, geometry: d.geometry, divergeM: d.divergeM, altDivergeM: d.altDivergeM,
			bubbleCoord: d.bubbleCoord, deltaSec: c.durationSec - refSec
		});
	}
	kept.sort((a, b) => a.divergeM - b.divergeM);
	alternatives = kept.slice(0, ALT_MAX);
	// Nothing kept → when to look again. Planned candidates that are
	// already behind / too close say nothing about the road ahead: fetch
	// from the current position right away. Fetched ones parting too
	// close are worth a retry a little further on; ones parting too far
	// become early enough once the rider approaches; none at all — the
	// engine may offer some later on.
	if (alternatives.length > 0) nextAltRefreshM = null;
	else if (tooClose) nextAltRefreshM = fromPlanning ? progressM : progressM + ALT_RETRY_TOO_CLOSE_M;
	else if (earliestRejectedM !== null) nextAltRefreshM = earliestRejectedM - ALT_EARLY_MAX_M;
	else nextAltRefreshM = progressM + ALT_RETRY_NONE_M;
	console.debug('[navigation] alternatives', {
		candidates: cands.length, kept: alternatives.length, tooClose,
		earliestRejectedM, nextAltRefreshM, progressM: Math.round(progressM)
	});
}

/** Fetch alternatives from the current position without touching the
 * navigated route: every returned route is a candidate, the one that
 * coincides with the navigated route drops out in `divergence`. Silent
 * on failure — alternatives are a bonus; a retry is due further on. */
async function refreshAlternatives() {
	if (!route || !fix || !geometry || recalculating) return;
	const t = Date.now();
	if (t - altFetchAt < ALT_FETCH_MIN_INTERVAL_MS) {
		nextAltRefreshM = progressM;
		return;
	}
	altFetchAt = t;
	altAbort?.abort();
	const ac = new AbortController();
	altAbort = ac;
	const remainingSec = computeGuidance(route, geometry, progressM, maneuverIdx, t)?.remainingSec
		?? route.durationSec;
	try {
		const routes = await fetchNavigationRoutes({
			mode: 'bike', from: fix.coord, to: route.requestedTo, vias: remainingVias(route)
		}, ac.signal);
		if (ac.signal.aborted || !active) return;
		adoptAlternatives(routes, remainingSec);
	} catch (e) {
		if ((e as Error).name === 'AbortError' || !active) return;
		nextAltRefreshM = progressM + ALT_RETRY_AFTER_M;
	} finally {
		if (altAbort === ac) altAbort = null;
	}
}

/** The rider is on `alt`: it becomes the navigated route. The other
 * alternatives were judged against the old route and go; a refresh is
 * due on the next fix (rate-limited like every fetch). */
function switchToAlternative(alt: NavAlternative) {
	alternatives = [];
	altAbort?.abort();
	installRoute(alt.route, alt.geometry);
	updateFailed = false;
	nextAltRefreshM = 0;
	persist();
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
	altAbort?.abort();
	try {
		const routes = await fetchNavigationRoutes({
			mode: 'bike',
			from: f.coord,
			to: r0.requestedTo,
			vias: remainingVias(r0)
		}, ac.signal);
		if (ac.signal.aborted || !active) return;
		const r = routes[0];
		if (!r) throw new Error('No route found from the current position');
		installRoute(r);
		updateFailed = false;
		retryDelayMs = RECALC_MIN_INTERVAL_MS;
		nextRetryAt = 0;
		// The alternates came with the same request (concept § Live
		// alternatives: refreshed on every recalculation).
		altFetchAt = Date.now();
		adoptAlternatives(routes.slice(1), r.durationSec);
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
	alternatives = [];
	nextAltRefreshM = null;
	recalcAbort?.abort();
	altAbort?.abort();
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
 * compass permission request must run inside it. `planned` are the
 * planning view's other alternatives to `r`: they start the ride's
 * live alternatives; without them (a resume) the first fetch is due
 * on the next fix. */
async function start(r: DirectRoute, resume = false, planned: DirectRoute[] = []): Promise<void> {
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
	alternatives = [];
	nextAltRefreshM = null;
	altFetchAt = 0;
	installRoute(r);
	applyFix(first);
	// Starting away from the planned route (concept § Entering and
	// leaving): no five-second hold — reroute from where the rider is
	// right now. The persisted route is the planned one until the new
	// route lands (recalculate() persists it, and brings alternatives).
	if (!arrived && offRouteM > OFF_ROUTE_DIST_M) {
		offRoute = true;
		void recalculate();
	} else if (planned.length > 0) {
		adoptAlternatives(planned, r.durationSec, true);
	} else {
		nextAltRefreshM = progressM;
	}
	persist();

	wakeLock = new ScreenWakeLock();
	if (!wakeLock.supported) {
		mapUi.showToast('This browser cannot keep the screen on — it may lock during navigation.');
	}
	void wakeLock.acquire();
	stopWatch = watchPosition(applyFix, onWatchError);
	compassDebug = { permission: 'pending', samples: 0, last: null };
	void compassPermission.then((ok) => {
		compassDebug = { ...compassDebug, permission: ok ? 'granted' : 'denied' };
		if (ok && active && !stopCompass) {
			stopCompass = watchCompass(onCompass, (s) => {
				compassDebug = { ...compassDebug, samples: compassDebug.samples + 1, last: s };
			});
		}
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
	altAbort?.abort();
	altAbort = null;
	alternatives = [];
	nextAltRefreshM = null;
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
	get alternatives() { return alternatives; },
	/** TEMPORARY diagnostic. */
	get compassDebug() { return compassDebug; },
	get positionStale() { return fix !== null && now - fix.at > POSITION_STALE_MS; },

	/** Start from the planning view: `planned` = the other shown
	 * alternatives, which seed the live alternatives. */
	start(r: DirectRoute, planned: DirectRoute[] = []) { return start(r, false, planned); },
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
