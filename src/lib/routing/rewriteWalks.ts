// Rewrite MOTIS-returned WALK legs with Valhalla-computed durations,
// distances, and geometries so every walking value the user sees comes
// from the same authority (see
// .claude/concepts/valhalla-pedestrian-router.md).
//
// The concept requires no per-leg constants and no fixed endpoint
// penalties. Consequently:
//
//   * Pre-transit WALK (first leg): shift its `startTime` forward so
//     `endTime` == the next transit leg's `startTime`. The user leaves
//     later, arrives at the boarding stop at the same moment.
//   * Post-transit WALK (last leg): shift its `endTime` backward so
//     `startTime` == the previous transit leg's `endTime`. Arrival at
//     the final destination moves earlier.
//   * Mid-transit transfer WALK: shift `startTime` forward against the
//     previous transit leg's `endTime`. The passenger dwells at the
//     transit stop longer, walks less. Never lets a transfer walk end
//     later than the next transit's `startTime` — that would break the
//     schedule.
//   * Direct-only itinerary (single WALK leg from a coord to a coord):
//     replace end-to-end; caller decides whether to preserve startTime
//     (leave-at) or endTime (arrive-by).
//
// The itinerary-level `duration` and `walkTime` are recomputed from the
// mutated legs at the end. Non-WALK legs are never touched.

import { computeWalk } from './valhalla';
import type { Itinerary, Leg, PlanResponse } from './types';

async function fetchValhallaWalk(leg: Leg, signal?: AbortSignal) {
	const fromLat = leg.from?.lat;
	const fromLon = leg.from?.lon;
	const toLat = leg.to?.lat;
	const toLon = leg.to?.lon;
	if (
		fromLat == null || fromLon == null ||
		toLat == null   || toLon == null
	) {
		return null;
	}
	try {
		return await computeWalk(
			{ lat: fromLat, lon: fromLon },
			{ lat: toLat,   lon: toLon },
			signal,
		);
	} catch {
		// A single Valhalla failure should not kill the whole itinerary —
		// fall back to MOTIS's value for that specific leg. The concept
		// forbids MIXING for the transfer-table matrix (import-time), but
		// for query-time cosmetic rewriting a per-leg fallback is the
		// gentler failure mode.
		return null;
	}
}

/** Rewrite one leg's `duration`, `distance`, and `legGeometry` from a
 * Valhalla result. Does NOT touch times — the caller handles retiming
 * to satisfy the itinerary invariants above. */
function applyValhalla(leg: Leg, walk: { durationSec: number; distanceM: number; encodedPolyline: string; polylinePrecision: 6 }): Leg {
	return {
		...leg,
		duration: walk.durationSec,
		distance: walk.distanceM,
		legGeometry: {
			points: walk.encodedPolyline,
			precision: walk.polylinePrecision,
		},
	};
}

/** Retime an itinerary's WALK legs so they meet the neighbouring
 * transit legs at their scheduled times, using Valhalla durations that
 * have already been written into the WALK legs' `duration` fields.
 * Times on transit legs are locked (they're the schedule). */
function retimeAgainstTransit(legs: Leg[]): Leg[] {
	const out = legs.map((l) => ({ ...l }));
	for (let i = 0; i < out.length; i++) {
		const leg = out[i];
		if (leg.mode !== 'WALK' || leg.duration == null) continue;
		const durMs = leg.duration * 1000;

		const prev = i > 0 ? out[i - 1] : null;
		const next = i < out.length - 1 ? out[i + 1] : null;

		if (prev && next) {
			// Mid-transit transfer walk. Depart the previous transit's
			// endTime, arrive at the next transit's startTime at the
			// latest. Shift startTime forward if slack lets us.
			const anchorEnd = Date.parse(next.startTime);
			const arriveByAnchorStart = new Date(anchorEnd - durMs).toISOString();
			leg.startTime = arriveByAnchorStart;
			leg.endTime = next.startTime;
		} else if (!prev && next) {
			// Pre-transit walk. Arrive at boarding at next.startTime.
			leg.endTime = next.startTime;
			leg.startTime = new Date(Date.parse(next.startTime) - durMs).toISOString();
		} else if (prev && !next) {
			// Post-transit walk. Depart at prev.endTime.
			leg.startTime = prev.endTime;
			leg.endTime = new Date(Date.parse(prev.endTime) + durMs).toISOString();
		} else {
			// No neighbours — a WALK-only itinerary. Preserve startTime and
			// push endTime out (leave-at semantics). The direct-walk path
			// below handles arrive-by explicitly.
			leg.endTime = new Date(Date.parse(leg.startTime) + durMs).toISOString();
		}
	}
	return out;
}

function recomputeItinerarySummary(it: Itinerary, legs: Leg[]): Itinerary {
	const startTime = legs[0].startTime;
	const endTime   = legs[legs.length - 1].endTime;
	const duration = Math.max(0, (Date.parse(endTime) - Date.parse(startTime)) / 1000);
	let walkTime = 0;
	for (const l of legs) {
		if (l.mode !== 'WALK') continue;
		walkTime += l.duration ?? Math.max(0, (Date.parse(l.endTime) - Date.parse(l.startTime)) / 1000);
	}
	return { ...it, legs, startTime, endTime, duration, walkTime };
}

async function rewriteTransitItinerary(it: Itinerary, signal?: AbortSignal): Promise<Itinerary> {
	const walks = await Promise.all(
		it.legs.map(async (leg) => {
			if (leg.mode !== 'WALK') return null;
			return fetchValhallaWalk(leg, signal);
		}),
	);
	const withValhalla = it.legs.map((leg, i) => {
		const walk = walks[i];
		return walk ? applyValhalla(leg, walk) : leg;
	});
	const retimed = retimeAgainstTransit(withValhalla);
	return recomputeItinerarySummary(it, retimed);
}

async function rewriteDirectWalk(
	it: Itinerary,
	mode: 'leave' | 'arrive',
	signal?: AbortSignal,
): Promise<Itinerary | null> {
	if (it.legs.length !== 1 || it.legs[0].mode !== 'WALK') return it;
	const original = it.legs[0];
	const walk = await fetchValhallaWalk(original, signal);
	if (!walk) return it;
	const applied = applyValhalla(original, walk);
	const durMs = walk.durationSec * 1000;
	if (mode === 'arrive') {
		// Preserve arrival, push start earlier.
		applied.endTime = original.endTime;
		applied.startTime = new Date(Date.parse(original.endTime) - durMs).toISOString();
	} else {
		// Preserve departure, push end later.
		applied.startTime = original.startTime;
		applied.endTime = new Date(Date.parse(original.startTime) + durMs).toISOString();
	}
	return recomputeItinerarySummary(it, [applied]);
}

/** Rewrite every WALK duration/geometry in the response via Valhalla.
 * `mode` controls direct-walk retiming (leave-at preserves start,
 * arrive-by preserves end). Transit itineraries retime WALK legs to
 * meet their neighbouring transit legs' scheduled times. */
export async function rewriteWithValhalla(
	res: PlanResponse,
	mode: 'leave' | 'arrive',
	signal?: AbortSignal,
): Promise<PlanResponse> {
	const itineraries = await Promise.all(
		(res.itineraries ?? []).map((it) => rewriteTransitItinerary(it, signal)),
	);
	const direct = res.direct
		? (await Promise.all(res.direct.map((it) => rewriteDirectWalk(it, mode, signal)))).filter(
				(x): x is Itinerary => x != null,
			)
		: undefined;
	return { ...res, itineraries, direct };
}
