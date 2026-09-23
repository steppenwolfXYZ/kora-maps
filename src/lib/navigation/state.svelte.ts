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
	bearingDeg, blendHeading, buildGeometry, distanceM, projectOntoRoute,
	type LonLat, type RouteGeometry
} from './geometry';
import {
	advanceManeuverIndex, computeGuidance, initialManeuverIndex, type Guidance
} from './guidance';
import {
	getFirstFix, requestCompassPermission, ScreenWakeLock, watchCompass, watchPosition,
	type PositionFix
} from './sensors';

/** Off-route once the projected distance exceeds the fix's own
 * threshold for OFF_ROUTE_HOLD_MS (concept § Off-route detection).
 * The threshold rides on the fix's reported accuracy — a precise fix
 * is evidence a short way off the line, a vague one only far off it —
 * clamped into the min/max band. A gap that is visibly widening is a
 * rider already riding away from the route, so it confirms after the
 * shorter FAST hold once it has grown by GROWTH metres. */
const OFF_ROUTE_DIST_MIN_M = 15;
const OFF_ROUTE_DIST_MAX_M = 45;
const OFF_ROUTE_ACCURACY_FACTOR = 1.6;
const OFF_ROUTE_HOLD_MS = 2500;
const OFF_ROUTE_FAST_HOLD_MS = 1200;
const OFF_ROUTE_GROWTH_M = 10;
/** Recalculations are rate-limited to one per this interval and to one
 * per RECALC_MIN_MOVE_M of ground covered since the last successful
 * one — the short holds above make the trigger reactive, the move gate
 * keeps a rider standing still off the route from re-requesting the
 * same route. A failed recalculation is exempt from the move gate and
 * backs off exponentially up to the max instead. */
const RECALC_MIN_INTERVAL_MS = 10_000;
const RECALC_MIN_MOVE_M = 25;
const RECALC_RETRY_MAX_MS = 60_000;
/** Within this of the destination the ride counts as arrived; the
 * banner then offers Finish — navigation never ends on its own. */
const ARRIVAL_RADIUS_M = 25;
/** Heading sources, in order of trust (concept § Follow-me map). On the
 * route (fix within the off-route threshold) the marker locks to the projected
 * point and the arrow takes the route's own bearing there — turns show
 * the instant the projection passes the corner. Off it: the platform's
 * course when it reports a speed of at least COURSE_MIN_SPEED_MS;
 * otherwise the bearing of the rider's own movement over the last
 * MOVE_WINDOW_MS — time, not distance, so walking and riding resolve a
 * turn equally fast — gated only against jitter; the compass while both
 * are stale. Standing still off-route without a compass for
 * STOP_HOLD_MS drops the heading — the marker becomes the dot again. */
const COURSE_MIN_SPEED_MS = 1.0;
const COURSE_STALE_MS = 4000;
const COMPASS_APPLY_MS = 500;
const COMPASS_FRESH_MS = 3000;
const TRACK_WINDOW_MS = 15_000;
const MOVE_WINDOW_MS = 2000;
const MOVE_MIN_M = 2;
const MOVE_ACCURACY_FACTOR = 0.25;
const STOP_WINDOW_MS = 5000;
const STOP_MIN_MOVE_M = 3;
const STOP_HOLD_MS = 4000;
/** Route bearing is read from the projected point to this far ahead
 * along the route, so a jagged shape doesn't twitch the arrow. */
const ROUTE_BEARING_AHEAD_M = 6;
/** Route lock is tighter than off-route detection: within this of the
 * line the marker snaps onto it — widened only when the fix's own
 * accuracy is worse, and never beyond the cap. */
const LOCK_DIST_M = 8;
const LOCK_ACCURACY_FACTOR = 0.8;
const LOCK_DIST_MAX_M = 20;
/** A via counts as passed once progress is this far beyond it. */
const VIA_PASSED_MARGIN_M = 30;
/** Ground speed for the readout (concept § Speed readout): the
 * platform's own figure when it reports one, otherwise the rider's
 * displacement over this window. */
const SPEED_WINDOW_MS = 3000;
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
 * parting point by this much, within the off-route threshold of it, and
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
/** No alternatives within the last stretch before the goal — parting
 * there is pointless, and the fetches would be too. */
const ALT_GOAL_CUTOFF_M = 300;
/** Merging a refresh into the shown set: a candidate this close in
 * parting point and duration to a shown one is the same alternative. */
const ALT_DUP_DIVERGE_M = 25;
const ALT_DUP_TIME_SEC = 20;

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
// Where the ride goes, as the rider named it — the bottom island shows
// it throughout, the banner large on arrival.
let destinationName = $state('');
let fix = $state.raw<PositionFix | null>(null);
// What the marker and camera use: the projected point while locked to
// the route, the raw fix otherwise.
let displayCoord = $state.raw<LonLat | null>(null);
let onRoute = $state(false);
// True while the camera is still travelling to the rider after a
// re-center (or the start): the marker rides the map into place and
// the fixed on-screen arrow takes over only once the camera is there.
let followTransition = $state(false);
// The camera's pitch while following (set by the orchestration): the
// fixed on-screen arrow squashes by cos(pitch), like a marker lying on
// the tilted map, so it is the same shape as the map marker.
let viewPitch = $state(0);
let heading = $state<number | null>(null);
let following = $state(true);
let progressM = $state(0);
// Ground speed in m/s for the on-screen readout; 0 while standing.
let speedMs = $state(0);
let offRouteM = $state(0);
let offRoute = $state(false);
let recalculating = $state(false);
let updateFailed = $state(false);
let arrived = $state(false);
let maneuverIdx = $state(0);
// Wall clock for the ETA; bumped on every fix and by a slow ticker.
let now = $state(0);
let alternatives = $state.raw<NavAlternative[]>([]);

// Non-reactive internals. `geometry` always changes together with
// `route` (installRoute sets it first), so deriveds keyed on `route`
// see a matching geometry.
let geometry: RouteGeometry | null = null;
let stopWatch: (() => void) | null = null;
let stopCompass: (() => void) | null = null;
let wakeLock: ScreenWakeLock | null = null;
let compassHeading: number | null = null;
let lastCourseAt = 0;
let lastCompassAt = 0;
let lastCompassApplyAt = 0;
// Recent fixes (TRACK_WINDOW_MS) for the movement bearing and the
// standing-still judgement.
let track: PositionFix[] = [];
let lastMoveAt = 0;
let offSince: number | null = null;
// Projected distance when the current off-route streak began — the
// growth the fast hold looks for is measured against it.
let offSinceDistM = 0;
let lastRecalcAt = 0;
let lastRecalcCoord: LonLat | null = null;
let retryDelayMs = RECALC_MIN_INTERVAL_MS;
let nextRetryAt = 0;
let recalcAbort: AbortController | null = null;
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
// The motion-sensor hint shows once per session, not on every start.
let sensorHintShown = false;

let guidance: Guidance | null = $derived.by(() => {
	if (!route || !geometry) return null;
	return computeGuidance(route, geometry, progressM, maneuverIdx, now);
});

function persist() {
	if (!browser || !route) return;
	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify({ route, startedAt, destinationName }));
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
	// Shown alternatives are sticky (concept § Live alternatives): a new
	// navigated route re-measures them rather than dropping them; only
	// one that no longer parts from it (it IS the route now) goes.
	if (alternatives.length > 0) {
		const kept: NavAlternative[] = [];
		for (const a of alternatives) {
			const d = divergence(geometry, a.route);
			if (!d || progressM > d.divergeM + ALT_SPENT_MARGIN_M) continue;
			kept.push({ ...a, divergeM: d.divergeM, altDivergeM: d.altDivergeM, bubbleCoord: d.bubbleCoord });
		}
		alternatives = kept;
	}
}

/** Bearing of the route at metres `cumM` along it, read over a short
 * stretch ahead. */
function routeBearingAt(g: RouteGeometry, cumM: number, from: LonLat): number | null {
	const target = cumM + ROUTE_BEARING_AHEAD_M;
	let j = 0;
	while (j < g.cum.length - 1 && g.cum[j] < target) j++;
	const ahead = g.coords[j];
	if (distanceM(from, ahead) < 0.5) {
		// At the very end of the route: use the last segment's direction.
		const n = g.coords.length;
		return n >= 2 ? bearingDeg(g.coords[n - 2], g.coords[n - 1]) : null;
	}
	return bearingDeg(from, ahead);
}

/** How far off the line this fix has to be to mean anything: at least
 * OFF_ROUTE_ACCURACY_FACTOR × its own accuracy radius, inside the
 * min/max band. Used for the off-route judgement, for the arrival's
 * on-the-line test and for the switch onto a ridden alternative, so all
 * three read "off the route" the same way. */
function offRouteDistM(f: PositionFix): number {
	return Math.min(
		OFF_ROUTE_DIST_MAX_M,
		Math.max(OFF_ROUTE_DIST_MIN_M, f.accuracyM * OFF_ROUTE_ACCURACY_FACTOR)
	);
}

/** Ground speed for the readout: the platform's own figure when it
 * reports one, otherwise the displacement over SPEED_WINDOW_MS. A
 * standstill reads zero rather than the last speed. */
function updateSpeedFromFix(f: PositionFix) {
	if (f.speedMs !== null && f.speedMs >= 0) {
		speedMs = f.speedMs;
		return;
	}
	const ref = [...track].reverse().find((t) => f.at - t.at >= SPEED_WINDOW_MS);
	if (!ref) return;
	const dt = (f.at - ref.at) / 1000;
	speedMs = dt > 0 ? distanceM(ref.coord, f.coord) / dt : 0;
}

function updateHeadingFromFix(f: PositionFix) {
	track.push(f);
	while (track.length > 0 && f.at - track[0].at > TRACK_WINDOW_MS) track.shift();

	let course: number | null = null;
	let trust = 0.6;
	if (f.courseDeg !== null && f.courseDeg >= 0 && (f.speedMs ?? 0) >= COURSE_MIN_SPEED_MS) {
		course = f.courseDeg;
	} else {
		// Movement bearing over the last MOVE_WINDOW_MS: the newest fix
		// at least that old, gated against jitter only.
		const ref = [...track].reverse().find((t) => f.at - t.at >= MOVE_WINDOW_MS);
		if (ref) {
			const minMove = Math.max(MOVE_MIN_M, f.accuracyM * MOVE_ACCURACY_FACTOR);
			if (distanceM(ref.coord, f.coord) >= minMove) {
				course = bearingDeg(ref.coord, f.coord);
				trust = 0.5;
			}
		}
	}
	if (course !== null) {
		lastCourseAt = f.at;
		lastMoveAt = f.at;
		heading = heading === null ? course : blendHeading(heading, course, trust);
		return;
	}
	// No course: still moving at all? Small displacements within the
	// stop window keep the last heading; a true standstill without a
	// live compass drops it, and the marker shows the dot.
	const stopMove = Math.max(STOP_MIN_MOVE_M, f.accuracyM * 0.5);
	const moved = (f.speedMs ?? 0) >= COURSE_MIN_SPEED_MS
		|| track.some((t) => f.at - t.at <= STOP_WINDOW_MS && distanceM(t.coord, f.coord) >= stopMove);
	if (moved) lastMoveAt = f.at;
	if (isStill(f.at)) heading = null;
}

/** Standing still with nothing to tell the direction from: no
 * movement within the hold and no live compass. */
function isStill(at: number): boolean {
	return at - lastMoveAt > STOP_HOLD_MS && at - lastCompassAt > COMPASS_FRESH_MS;
}

/** Compass samples arrive at tens of hertz; they only reach the
 * reactive heading throttled, and only while the GPS course is stale
 * (standing at a light, walking the bike) — course always wins while
 * it is valid, since compasses are so often miscalibrated. */
function onCompass(deg: number) {
	compassHeading = deg;
	const t = Date.now();
	lastCompassAt = t;
	// Locked to the route, the arrow follows the route, not the phone —
	// except from a standstill, where the compass is the only direction
	// there is (the next fix re-locks the arrow to the route).
	if (onRoute && heading !== null) return;
	if (t - lastCompassApplyAt < COMPASS_APPLY_MS) return;
	if (t - lastCourseAt <= COURSE_STALE_MS) return;
	lastCompassApplyAt = t;
	const next = heading === null ? deg : blendHeading(heading, deg, 0.3);
	if (heading === null || Math.abs(((next - heading + 540) % 360) - 180) > 2) heading = next;
}

function applyFix(f: PositionFix) {
	fix = f;
	now = f.at;
	// Movement / course / stop bookkeeping runs on every fix, so the
	// fallback heading is current the moment the route lock lets go.
	updateHeadingFromFix(f);
	updateSpeedFromFix(f);
	if (!route || !geometry) {
		displayCoord = f.coord;
		onRoute = false;
		return;
	}
	const proj = projectOntoRoute(geometry, f.coord, progressM);
	progressM = proj.cumM;
	offRouteM = proj.distM;
	// One threshold per fix, from its own accuracy (see offRouteDistM).
	const offDist = offRouteDistM(f);
	maneuverIdx = advanceManeuverIndex(route, geometry, progressM, maneuverIdx);

	// Route lock (concept § Follow-me map): on the route, the marker
	// sits on the projected point and the arrow takes the route's
	// bearing there. Decisions below keep using the raw fix.
	const lockDist = Math.min(LOCK_DIST_MAX_M, Math.max(LOCK_DIST_M, f.accuracyM * LOCK_ACCURACY_FACTOR));
	if (proj.distM <= lockDist) {
		onRoute = true;
		displayCoord = proj.point;
		// The route's bearing only while there is a direction at all —
		// standing still on the route shows the dot, not an arrow.
		const rb = isStill(f.at) ? null : routeBearingAt(geometry, proj.cumM, proj.point);
		heading = rb;
	} else {
		onRoute = false;
		displayCoord = f.coord;
	}

	if (arrived) return;

	// Taking an alternative is done by riding it (concept § Live
	// alternatives): off the navigated route but on a shown alternative
	// past its parting point → that alternative is the route now. No
	// hold, no request.
	const precise = f.accuracyM < proj.distM;
	if (proj.distM > offDist && precise) {
		const taken = alternatives.find((a) => {
			const ap = projectOntoRoute(a.geometry, f.coord, null);
			return ap.distM <= offDist
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
	const nearEnd = geometry.totalM - progressM < ARRIVAL_RADIUS_M && proj.distM < offDist;
	if (toGoal < ARRIVAL_RADIUS_M || nearEnd) {
		arrive();
		return;
	}

	// Off-route: sustained distance past this fix's own threshold, and
	// only from fixes precise enough to be evidence at all — a 60 m
	// accuracy circle 40 m off the line says nothing, which the band's
	// upper clamp would otherwise let through (concept § Off-route
	// detection).
	const evidence = proj.distM > offDist && f.accuracyM < proj.distM;
	if (evidence) {
		if (offSince === null) {
			offSince = f.at;
			offSinceDistM = proj.distM;
		}
		const heldMs = f.at - offSince;
		// A widening gap needs no full hold: the rider is riding away
		// from the line, and every second of waiting is metres to undo.
		const widening = proj.distM - offSinceDistM >= OFF_ROUTE_GROWTH_M;
		if (heldMs >= OFF_ROUTE_HOLD_MS || (widening && heldMs >= OFF_ROUTE_FAST_HOLD_MS)) {
			offRoute = true;
		}
	} else {
		offSince = null;
		offRoute = false;
		updateFailed = false;
	}
	// Rate limit: the interval always, plus — once a recalculation has
	// actually landed — a minimum distance covered since it, so a rider
	// held up off the route does not re-request the route they have.
	const movedSinceRecalc = lastRecalcCoord === null
		|| distanceM(lastRecalcCoord, f.coord) >= RECALC_MIN_MOVE_M;
	if (
		offRoute && !recalculating
		&& f.at - lastRecalcAt >= RECALC_MIN_INTERVAL_MS
		&& f.at >= nextRetryAt
		&& (updateFailed || movedSinceRecalc)
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
	// Shown ones stay (sticky); new ones join up to ALT_MAX.
	const kept: NavAlternative[] = [...alternatives];
	const goalCutoffM = geometry.totalM - ALT_GOAL_CUTOFF_M;
	let earliestRejectedM: number | null = null;
	let tooClose = false;
	for (const c of cands) {
		const d = divergence(geometry, c);
		if (!d) continue;
		if (d.divergeM > goalCutoffM) continue;
		const ahead = d.divergeM - progressM;
		if (ahead < ALT_MIN_AHEAD_M) {
			tooClose = true;
			continue;
		}
		if (ahead > ALT_EARLY_MAX_M) {
			if (earliestRejectedM === null || d.divergeM < earliestRejectedM) earliestRejectedM = d.divergeM;
			continue;
		}
		const deltaSec = c.durationSec - refSec;
		const dup = kept.some((k) =>
			Math.abs(k.divergeM - d.divergeM) < ALT_DUP_DIVERGE_M
			&& Math.abs(k.deltaSec - deltaSec) < ALT_DUP_TIME_SEC);
		if (dup) continue;
		kept.push({
			route: c, geometry: d.geometry, divergeM: d.divergeM, altDivergeM: d.altDivergeM,
			bubbleCoord: d.bubbleCoord, deltaSec
		});
	}
	alternatives = kept
		.slice(0, Math.max(alternatives.length, ALT_MAX))
		.sort((a, b) => a.divergeM - b.divergeM);
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
	// Nothing to look for within the last stretch before the goal.
	if (geometry.totalM - progressM < ALT_GOAL_CUTOFF_M) return;
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
			mode: 'bike', from: fix.coord, to: route.requestedTo, vias: remainingVias(route),
			bike: route.requestedBike ?? undefined,
			fromHeading: heading
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
	lastRecalcCoord = f.coord;
	recalcAbort?.abort();
	const ac = new AbortController();
	recalcAbort = ac;
	altAbort?.abort();
	try {
		const routes = await fetchNavigationRoutes({
			mode: 'bike',
			from: f.coord,
			to: r0.requestedTo,
			vias: remainingVias(r0),
			// The same rider model the planned route used (bicycle-
			// route-options.md § 6): bike type, pace, ruler stop, stairs.
			bike: r0.requestedBike ?? undefined,
			// From the direction of travel: turning back is a U-turn the
			// engine prices and reports, not a silent reversal.
			fromHeading: heading
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

/** Arrived: the banner switches to the destination and offers Finish;
 * the ride ends only on that button (or the ×). */
function arrive() {
	arrived = true;
	offRoute = false;
	offSince = null;
	updateFailed = false;
	alternatives = [];
	nextAltRefreshM = null;
	recalcAbort?.abort();
	altAbort?.abort();
}

function onWatchError(err: GeolocationPositionError) {
	if (err.code === 1) {
		markGeolocationDenied();
		mapUi.showToast('Location permission was revoked. Pick a start manually to plan again.', 'error', 'Navigation ended');
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
async function start(
	r: DirectRoute,
	resume = false,
	planned: DirectRoute[] = [],
	destination = ''
): Promise<void> {
	if (active || starting || r.mode !== 'bike') return;
	starting = true;
	const compassPermission = requestCompassPermission();
	let first: PositionFix;
	try {
		first = await getFirstFix();
	} catch (e) {
		if ((e as { code?: number })?.code === 1) markGeolocationDenied();
		mapUi.showToast(geolocationErrorMessage(e), 'error', 'Navigation needs your location');
		starting = false;
		return;
	}
	if (!resume) startedAt = Date.now();
	if (!resume) destinationName = destination;
	active = true;
	following = true;
	followTransition = true;
	arrived = false;
	updateFailed = false;
	heading = null;
	compassHeading = null;
	lastCourseAt = 0;
	lastCompassAt = 0;
	lastMoveAt = 0;
	track = [];
	lastRecalcAt = 0;
	lastRecalcCoord = null;
	retryDelayMs = RECALC_MIN_INTERVAL_MS;
	nextRetryAt = 0;
	speedMs = 0;
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
	if (!arrived && offRouteM > offRouteDistM(first)) {
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
	void compassPermission.then((outcome) => {
		// A refused permission (iOS prompt, Brave) or events with their
		// values stripped (Brave with motion sensors blocked) both mean
		// no compass: say so once, since the fix is a browser setting.
		const hint = () => {
			if (sensorHintShown || !active) return;
			sensorHintShown = true;
			mapUi.showToast(
				'Allow them in the browser\'s site settings so the map can show your '
				+ 'direction while standing still.',
				'error',
				'Motion sensors are blocked'
			);
		};
		if (outcome === 'denied') hint();
		// Listen whenever the API exists: only iOS withholds events
		// without a grant, and a listener that never fires costs nothing.
		if (outcome !== 'no-api' && active && !stopCompass) stopCompass = watchCompass(onCompass, hint);
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
	if (clockTimer) clearInterval(clockTimer);
	clockTimer = null;
	if (browser) document.removeEventListener('visibilitychange', onVisibility);
	route = null;
	geometry = null;
	fix = null;
	displayCoord = null;
	onRoute = false;
	heading = null;
	speedMs = 0;
	arrived = false;
	offRoute = false;
	updateFailed = false;
	following = true;
	followTransition = false;
	clearPersisted();
	if (consumeEntry) history.back();
}

/** Resume a ride persisted before a reload (concept § Entering and
 * leaving). Silently does nothing without a fresh, sane record. */
async function tryResume(): Promise<boolean> {
	if (!browser || active || starting) return false;
	let saved: { route?: DirectRoute; startedAt?: number; destinationName?: string } | null = null;
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
	destinationName = typeof saved.destinationName === 'string' ? saved.destinationName : '';
	await start(r, true);
	if (!active) clearPersisted();
	return active;
}

export const navigation = {
	get active() { return active; },
	get starting() { return starting; },
	get route() { return route; },
	get destinationName() { return destinationName; },
	get fix() { return fix; },
	/** Marker / camera position: projected onto the route while locked
	 * to it, the raw fix otherwise. */
	get displayCoord() { return displayCoord; },
	get onRoute() { return onRoute; },
	get heading() { return heading; },
	/** Ground speed in m/s — the readout's own value, 0 while standing. */
	get speedMs() { return speedMs; },
	get following() { return following; },
	get followTransition() { return followTransition; },
	get viewPitch() { return viewPitch; },
	setViewPitch(p: number) { viewPitch = p; },
	get progressM() { return progressM; },
	get offRoute() { return offRoute; },
	get offRouteM() { return offRouteM; },
	get recalculating() { return recalculating; },
	get updateFailed() { return updateFailed; },
	get arrived() { return arrived; },
	get guidance() { return guidance; },
	get alternatives() { return alternatives; },
	get positionStale() { return fix !== null && now - fix.at > POSITION_STALE_MS; },

	/** Start from the planning view: `planned` = the other shown
	 * alternatives, which seed the live alternatives; `destination` =
	 * the destination as the rider named it. */
	start(r: DirectRoute, planned: DirectRoute[] = [], destination = '') {
		return start(r, false, planned, destination);
	},
	stop,
	tryResume,

	/** A map gesture: keep guiding, stop moving the camera. */
	suspendFollow() {
		if (active) following = false;
	},
	resumeFollow() {
		if (!following) followTransition = true;
		following = true;
	},
	/** The camera has arrived at the rider (orchestration, on moveend). */
	endFollowTransition() {
		followTransition = false;
	}
};
