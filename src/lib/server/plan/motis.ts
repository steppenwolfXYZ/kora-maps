import { env } from '$env/dynamic/private';
import type { Itinerary, PlanResponse, TimeMode } from '$lib/routing/types';
import type { PlanOptionParams } from '$lib/routing/optionParams';

// Server-side MOTIS /plan call (server-side-transit-planning.md). The
// browser never talks to MOTIS any more — the planning engine
// (cascade.ts) issues every hop from here over the loopback address, and
// share re-verification (share/verify.ts) reuses the same call.
//
// MOTIS_INTERNAL_URL: server-reachable MOTIS base. Dev default matches
// the local MOTIS container; prod: http://127.0.0.1:8080 (loopback-bound
// docker port, never exposed through nginx).

const MOTIS_BASE = (env.MOTIS_INTERNAL_URL || 'http://localhost:8080').replace(/\/$/, '');

const NUM_ITINERARIES = 5;

/** A non-OK response from MOTIS. Carries the HTTP status so the endpoint
 * can classify it (4xx = the query itself was rejected, e.g. a stale stop
 * id; 5xx = engine trouble); the body is for the server log only. */
export class MotisRequestError extends Error {
	status: number;
	body: string;
	constructor(status: number, body: string) {
		super(`MOTIS ${status}: ${body}`);
		this.name = 'MotisRequestError';
		this.status = status;
		this.body = body;
	}
}

/** One via stop as the engine sees it: MOTIS place id + requested
 * minimum stay in whole minutes (0 = may stay on board). */
export interface MotisVia {
	placeId: string;
	waitMin: number;
}

export interface MotisPlanArgs {
	/** MOTIS place strings — "ch_<stop id>" or "lat,lon" (place.ts). */
	fromPlace: string;
	toPlace: string;
	mode: TimeMode;
	/** ISO timestamp; null = MOTIS's own "now". */
	time: string | null;
	vias: MotisVia[];
	/** Walking budget for the walk from FROM to first stop, and last stop
	 * to TO, in SECONDS. Server hard-caps at 28800 (8 h). Narrow default
	 * 1800 = 30 min because every extra kilometre of walking radius costs
	 * real Valhalla matrix time per query; the cascade escalates to 28800
	 * when the narrow search comes up short. */
	maxPreTransitTime: number;
	maxPostTransitTime: number;
	/** Time-window size in seconds. 900 (15 min) for the fast initial
	 * query; the hop cascade widens this to 7200 (2 h). */
	searchWindow: number;
	/** Two-tier transfer table (transfer-point-optimization.md): default
	 * queries search transfers on the capped (30 min) Valhalla table;
	 * `true` selects the full 2-h table. Set whenever the cascade runs
	 * with the wide walking budget — the sparse-service situations where
	 * long transfer-walk connections matter. */
	fullTransfers: boolean;
	/** Routing options as MOTIS parameters (optionParams.ts). */
	options: PlanOptionParams;
}

export async function planMotis(args: MotisPlanArgs, signal?: AbortSignal): Promise<PlanResponse> {
	const params = new URLSearchParams();
	params.set('fromPlace', args.fromPlace);
	params.set('toPlace', args.toPlace);
	params.set('arriveBy', args.mode === 'arrive' ? 'true' : 'false');
	if (args.time) params.set('time', args.time);
	params.set('numItineraries', String(NUM_ITINERARIES));
	params.set('maxPreTransitTime', String(args.maxPreTransitTime));
	params.set('maxPostTransitTime', String(args.maxPostTransitTime));
	// Via stops (via-stops.md). Stop ids only — the engine rejects
	// coordinates here, which is why transit vias are always stations.
	// The per-via minimum stay rides along in the same order; 0 means the
	// traveller may stay on board (no forced vehicle change).
	if (args.vias.length > 0) {
		params.set('via', args.vias.map((v) => v.placeId).join(','));
		params.set('viaMinimumStay', args.vias.map((v) => String(Math.round(v.waitMin))).join(','));
	}
	// `maxTravelTime` is TOTAL itinerary duration (transit + all walking)
	// in MINUTES — a low value here silently drops Bern↔Lötschental-style
	// trips where the walking legs alone approach 8 h. 24 h leaves room
	// for any real cross-CH trip; MOTIS's own limits still cap walking.
	// Planned via waits are part of that total, so the ceiling has to grow
	// by them — otherwise a long errand silently returns nothing at all.
	const dwellMin = args.vias.reduce((s, v) => s + Math.round(v.waitMin), 0);
	params.set('maxTravelTime', String(1440 + dwellMin));
	// directModes controls the non-transit fallback that MOTIS returns in
	// `direct[]`. WALK is the default but set it explicitly so a
	// walk-only itinerary always comes back for merging.
	params.set('directModes', 'WALK');
	// MOTIS caps direct (walk-only) itineraries at 30 min by default —
	// past that, it falls back to weird walking-heavy transit hybrids
	// (WALK 45m + BUS 0m + WALK 1m). Lift to the 8 h server ceiling.
	params.set('maxDirectTime', '28800');
	// Without this flag MOTIS transfers on nigiri's default footpath set
	// (GTFS transfers.txt — sparse, direction-incomplete) instead of the
	// fork's imported Valhalla matrix, producing needlessly long transfer
	// walks (see transfer-point-optimization.md).
	params.set('useRoutedTransfers', 'true');
	// Fork-only flag (upstream MOTIS ignores it): select the full 2-h
	// transfer table instead of the capped default one.
	if (args.fullTransfers) params.set('koraFullTransfers', 'true');
	// Fork-only ε-alternates (near-optimal-endpoint-alternatives.md):
	// besides each Pareto-optimal journey, return egress/access-stop
	// variants arriving within the slack, as ordinary itineraries — the
	// pruning in ranking.ts decides which survive. Default 540 s =
	// ranking.ts's Case-1 overlap window (OVERLAP_TIME_MAX_MS), so the
	// server returns a slight superset of what layer 2 would ever keep;
	// max 3 alternates per Pareto point. Minimize-walking widens both.
	const o = args.options;
	params.set('alternativesEpsilon', String(o.alternativesEpsilon));
	params.set('alternativesMax', String(o.alternativesMax));
	if (o.koraWalkPoints) params.set('koraWalkPoints', o.koraWalkPoints);
	params.set('searchWindow', String(args.searchWindow));
	// Routing options — only sent off their defaults.
	if (o.pedestrianSpeedMs != null)
		params.set('pedestrianSpeed', String(o.pedestrianSpeedMs));
	if (o.transferTimeFactor != null)
		params.set('transferTimeFactor', String(o.transferTimeFactor));
	if (o.additionalTransferMin)
		params.set('additionalTransferTime', String(o.additionalTransferMin));
	if (o.minTransferMin)
		params.set('minTransferTime', String(o.minTransferMin));

	const url = `${MOTIS_BASE}/api/v1/plan?${params.toString()}`;
	const res = await fetch(url, { signal });
	if (!res.ok) throw new MotisRequestError(res.status, await res.text().catch(() => res.statusText));
	// Every walking duration/geometry in the response is Valhalla-computed
	// by the MOTIS fork (see valhalla-pedestrian-router.md) — including
	// `leg.duration` on transfer walks, which the fork reports as
	// Valhalla's own walking seconds rather than the leg's time span.
	// Nothing is rewritten here; every consumer takes leg durations at
	// face value.
	return (await res.json()) as PlanResponse;
}

/** Every itinerary of a response, transit and direct alike. */
export function allItineraries(res: PlanResponse): Itinerary[] {
	return [...(res.itineraries ?? []), ...(res.direct ?? [])];
}
