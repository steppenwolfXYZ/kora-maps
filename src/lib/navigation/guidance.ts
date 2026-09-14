// Turn-by-turn guidance derived from a route and the rider's progress
// along it (bicycle-navigation.md § Maneuver banner and trip summary).
// Pure functions over the route's maneuver list — the tracker
// (state.svelte.ts) owns the progress and the hysteresis, this module
// turns them into what the banner shows.

import type { DirectRoute, RouteManeuver } from '../routing/types';
import { bearingDeg, type RouteGeometry } from './geometry';

/** Where a roundabout is left, relative to the entry direction. */
export type RoundaboutExit =
	| 'straight' | 'slight-right' | 'right' | 'sharp-right' | 'uturn'
	| 'slight-left' | 'left' | 'sharp-left';

/** Icon vocabulary of the banner. Left/right variants mirror one path;
 * the roundabout family is generated from the exit angle. Plain
 * `roundabout` is the fallback when the shape gives no exit angle. */
export type ManeuverKind =
	| 'start' | 'destination' | 'straight'
	| 'slight-left' | 'slight-right' | 'left' | 'right'
	| 'sharp-left' | 'sharp-right' | 'uturn-left' | 'uturn-right'
	| 'roundabout' | `roundabout-${RoundaboutExit}`
	| 'ferry' | 'shuttle' | 'stairs' | 'elevator';

/** Exit bearing relative to the entry heading, clockwise degrees — the
 * parameter the roundabout glyph is drawn from. */
export const ROUNDABOUT_EXIT_BEARING: Record<RoundaboutExit, number> = {
	straight: 0, 'slight-right': 45, right: 90, 'sharp-right': 135, uturn: 180,
	'slight-left': -45, left: -90, 'sharp-left': -135
};

/** Valhalla's TripLeg_Maneuver_Type enum → banner icon. */
export function maneuverKind(type: number): ManeuverKind {
	switch (type) {
		case 1: case 2: case 3: return 'start';
		case 4: case 5: case 6: return 'destination';
		case 9: case 18: case 20: case 23: case 37: return 'slight-right';
		case 10: return 'right';
		case 11: return 'sharp-right';
		case 12: return 'uturn-right';
		case 13: return 'uturn-left';
		case 14: return 'sharp-left';
		case 15: return 'left';
		case 16: case 19: case 21: case 24: case 38: return 'slight-left';
		case 26: case 27: return 'roundabout';
		case 28: return 'ferry';
		case 39: return 'elevator';
		case 40: case 41: return 'stairs';
		default: return 'straight';
	}
}

/** Metres of shape sampled on either side of a roundabout to read the
 * entry and exit headings. */
const ROUNDABOUT_SAMPLE_M = 12;

function bearingBefore(g: RouteGeometry, idx: number): number | null {
	let j = idx;
	while (j > 0 && g.cum[idx] - g.cum[j] < ROUNDABOUT_SAMPLE_M) j--;
	if (j === idx) return null;
	return bearingDeg(g.coords[j], g.coords[idx]);
}

function bearingAfter(g: RouteGeometry, idx: number): number | null {
	const last = g.coords.length - 1;
	let j = idx;
	while (j < last && g.cum[j] - g.cum[idx] < ROUNDABOUT_SAMPLE_M) j++;
	if (j === idx) return null;
	return bearingDeg(g.coords[idx], g.coords[j]);
}

/** The glyph for maneuver `i`, with roundabouts resolved to the exit
 * direction read off the shape: heading before the enter maneuver
 * versus heading after the exit maneuver. The enter (26) and exit (27)
 * maneuvers are paired either way round, so the banner shows the same
 * glyph while approaching and while inside the roundabout. */
export function maneuverKindAt(route: DirectRoute, g: RouteGeometry, i: number): ManeuverKind {
	const ms = route.maneuvers;
	const m = ms[i];
	if (!m) return 'straight';
	const base = maneuverKind(m.type);
	// Water ferries and car-shuttle trains share the engine's ferry type;
	// the maneuver's ferry flag tells them apart (valhalla.ts).
	if (base === 'ferry') return m.ferry ? 'ferry' : 'shuttle';
	if (base !== 'roundabout') return base;
	let enter = i;
	let exit = i;
	if (m.type === 27 && ms[i - 1]?.type === 26) enter = i - 1;
	else if (m.type === 26 && ms[i + 1]?.type === 27) exit = i + 1;
	const entryIdx = ms[enter].beginIndex;
	const exitIdx = exit !== enter ? ms[exit].beginIndex : ms[enter].endIndex;
	const before = bearingBefore(g, entryIdx);
	const after = bearingAfter(g, exitIdx);
	if (before === null || after === null) return 'roundabout';
	const turn = ((after - before + 540) % 360) - 180;
	const a = Math.abs(turn);
	const side = turn >= 0 ? 'right' : 'left';
	if (a < 22.5) return 'roundabout-straight';
	if (a < 67.5) return `roundabout-slight-${side}`;
	if (a < 112.5) return `roundabout-${side}`;
	if (a < 157.5) return `roundabout-sharp-${side}`;
	return 'roundabout-uturn';
}

export function isDestination(m: RouteManeuver): boolean {
	return m.type === 4 || m.type === 5 || m.type === 6;
}

/** Instruction text for the banner — the engine's sentence without its
 * trailing full stop, with a fallback per kind for routes that came
 * without text. */
export function instructionText(m: RouteManeuver): string {
	const t = m.instruction.replace(/\.\s*$/, '');
	if (t) return t;
	switch (maneuverKind(m.type)) {
		case 'destination': return 'Arrive at your destination';
		case 'roundabout': return 'Enter the roundabout';
		case 'left': return 'Turn left';
		case 'right': return 'Turn right';
		case 'slight-left': return 'Keep left';
		case 'slight-right': return 'Keep right';
		case 'sharp-left': return 'Turn sharp left';
		case 'sharp-right': return 'Turn sharp right';
		case 'uturn-left': case 'uturn-right': return 'Make a U-turn';
		case 'ferry': return 'Take the ferry';
		case 'stairs': return 'Take the stairs';
		case 'elevator': return 'Take the elevator';
		default: return 'Continue';
	}
}

/** Distances a rider can act on: 5 m steps below 100 m, 10 m steps below
 * 1 km, 100 m steps beyond. */
export function fmtNavDistance(metres: number): string {
	const m = Math.max(0, metres);
	if (m < 100) return `${Math.round(m / 5) * 5} m`;
	if (m < 1000) return `${Math.round(m / 10) * 10} m`;
	const km = Math.round(m / 100) / 10;
	return km < 10 ? `${km.toFixed(1)} km` : `${Math.round(km)} km`;
}

/** Metres along the route where maneuver `i` begins. */
export function maneuverStartM(g: RouteGeometry, route: DirectRoute, i: number): number {
	const m = route.maneuvers[i];
	if (!m) return g.totalM;
	return g.cum[Math.min(m.beginIndex, g.cum.length - 1)] ?? 0;
}

/** The maneuver index guidance starts on: the first real turn (index 1);
 * a route with a lone start/destination pair sits on its last entry. */
export function initialManeuverIndex(route: DirectRoute): number {
	return Math.min(1, Math.max(0, route.maneuvers.length - 1));
}

/** Metres past a maneuver point before the banner moves on to the next
 * one — the rider must clearly have made the turn. */
const PASS_MARGIN_M = 8;
/** Backtracking further than this behind the current step re-derives the
 * index from scratch (the rider turned around). */
const BACKTRACK_M = 40;

/** Advance (or rewind) the approached-maneuver index for the new
 * progress. Sticky on purpose: a fix that jitters back across a turn
 * does not flip the banner. */
export function advanceManeuverIndex(
	route: DirectRoute,
	g: RouteGeometry,
	progressM: number,
	idx: number
): number {
	const n = route.maneuvers.length;
	if (n === 0) return 0;
	let i = Math.min(Math.max(idx, 0), n - 1);
	if (i > 0 && progressM < maneuverStartM(g, route, i - 1) - BACKTRACK_M) {
		i = initialManeuverIndex(route);
		while (i > 0 && progressM < maneuverStartM(g, route, i - 1)) i--;
	}
	while (i < n - 1 && progressM > maneuverStartM(g, route, i) + PASS_MARGIN_M) i++;
	return i;
}

export interface Guidance {
	/** The maneuver the rider is approaching, and its glyph. */
	next: RouteManeuver;
	nextKind: ManeuverKind;
	nextIndex: number;
	distanceToNextM: number;
	/** The one after it, previewed when `next` is close. */
	then: RouteManeuver | null;
	thenKind: ManeuverKind | null;
	/** The step currently being ridden (the one before `next`). */
	current: RouteManeuver | null;
	remainingM: number;
	remainingSec: number;
	/** Estimated arrival, epoch ms. */
	etaMs: number;
}

/** Below this distance to the next maneuver the following one is
 * previewed, so two quick turns in a row don't surprise. */
const PREVIEW_WITHIN_M = 80;

export function computeGuidance(
	route: DirectRoute,
	g: RouteGeometry,
	progressM: number,
	idx: number,
	nowMs: number
): Guidance | null {
	const ms = route.maneuvers;
	if (ms.length === 0) return null;
	const i = Math.min(Math.max(idx, 0), ms.length - 1);
	const next = ms[i];
	const nextAtM = maneuverStartM(g, route, i);
	const distanceToNextM = Math.max(0, nextAtM - progressM);
	const current = i > 0 ? ms[i - 1] : null;
	// Remaining time: the unridden fraction of the current step plus every
	// step after it, on the engine's own per-step budgets.
	let remainingSec = 0;
	if (current) {
		const frac = current.lengthM > 0 ? Math.min(1, distanceToNextM / current.lengthM) : 0;
		remainingSec += current.timeSec * frac;
	}
	for (let j = i; j < ms.length; j++) remainingSec += ms[j].timeSec;
	const then = distanceToNextM < PREVIEW_WITHIN_M && i + 1 < ms.length ? ms[i + 1] : null;
	return {
		next,
		nextKind: maneuverKindAt(route, g, i),
		nextIndex: i,
		distanceToNextM,
		then,
		thenKind: then ? maneuverKindAt(route, g, i + 1) : null,
		current,
		remainingM: Math.max(0, g.totalM - progressM),
		remainingSec,
		etaMs: nowMs + remainingSec * 1000
	};
}
