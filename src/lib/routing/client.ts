import { PUBLIC_MOTIS_URL } from '$env/static/public';

import type { Endpoint, PlanResponse, TimeMode } from './types';

// MOTIS base URL — local dev points at the local MOTIS instance
// (motis/docker-compose.yml, http://localhost:8080), production at the
// same-origin nginx proxy (/routing/). Set via PUBLIC_MOTIS_URL in
// .env / .env.production; inlined at build time.
const MOTIS_BASE = PUBLIC_MOTIS_URL.replace(/\/$/, '');

const NUM_ITINERARIES = 5;

/** A non-OK response from the MOTIS /plan endpoint. Carries the HTTP
 * status so state.svelte.ts can pick a user-facing message; the raw
 * server body stays in `body` / `message` for console diagnostics only —
 * it must never be rendered in the UI. */
export class PlanRequestError extends Error {
	status: number;
	body: string;
	constructor(status: number, body: string) {
		super(`MOTIS ${status}: ${body}`);
		this.name = 'PlanRequestError';
		this.status = status;
		this.body = body;
	}
}

// Station endpoints go to MOTIS as stop IDs ("ch_Parent<uic>"), not
// coordinates. The forked MOTIS serves WALK offsets for stop-ID
// endpoints straight from the imported Valhalla footpath matrix — zero
// Valhalla HTTP calls for that side of the query — and MOTIS still
// considers walking to nearby stations (the matrix rows include them).
// Side effect: no spurious first/last WALK leg from the station coord
// to its own platform, which the old stripStationWalks() workaround
// existed to trim.
function formatPlace(ep: Endpoint, resolved: [number, number]): string {
	// pid carries the feed's parent stop id (SLOID scheme); the legacy
	// Parent<uic> shape only exists in pre-migration timetables.
	if (ep.type === 'station') return `ch_${ep.pid ?? `Parent${ep.uic}`}`;
	if (ep.type === 'point')   return `${ep.coord[1]},${ep.coord[0]}`;
	return `${resolved[1]},${resolved[0]}`;
}

export interface PlanArgs {
	from: Endpoint;
	to: Endpoint;
	mode: TimeMode;
	time: string | null;
	/** Coords of `current` endpoints, one per side (undefined if not `current`). */
	currentCoord?: [number, number] | null;
	/** Walking budget for the walk from FROM to first stop, and last stop
	 * to TO, in SECONDS. Server hard-caps at 28800 (8 h). Default is
	 * narrow (1800 = 30 min) because every extra kilometre of walking
	 * radius costs real Valhalla matrix time per query; the cascade in
	 * state.svelte.ts escalates to 7200/28800 when the narrow search
	 * comes up short. */
	maxPreTransitTime?: number;
	maxPostTransitTime?: number;
	/** Time-window size passed to MOTIS in seconds. Defaults to 900 (15 min)
	 * for a fast initial query; the cascade in state.svelte.ts widens this
	 * to 7200 (2 h) once it's advancing `time` forward to accumulate more
	 * results. */
	searchWindow?: number;
	/** Two-tier transfer table (transfer-point-optimization.md): default
	 * queries search transfers on the capped (30 min) Valhalla table;
	 * `true` selects the full 2-h table. The cascade sets it whenever it
	 * runs with the wide walking budget — the sparse-service situations
	 * where long transfer-walk connections matter. */
	fullTransfers?: boolean;
	/** Routing options (routing-options.md). All three omitted at the
	 * defaults so a default query stays byte-identical to the pre-options
	 * behavior. `pedestrianSpeedMs` (m/s) rescales the fork's Valhalla
	 * walking surfaces (offsets + walk legs); `transferTimeFactor` scales
	 * the transfer matrix at query time (walking speed x daring);
	 * `additionalTransferMin` (minutes) is cautious mode's fixed slack. */
	pedestrianSpeedMs?: number | null;
	transferTimeFactor?: number | null;
	additionalTransferMin?: number;
	/** Multiplier that undoes daring's extra halving of the transfer
	 * matrix (2 in daring mode, else 1) — see the response normalization
	 * at the bottom of `plan()`. */
	transferWalkUnscale?: number;
	/** Minimize-walking (routing-options.md § Minimize walking):
	 * `koraWalkPoints` ('minwalk') selects the fork's steeper walk-point
	 * table so walking-light journeys survive as their own Pareto
	 * points; the ε-alternates knobs widen alongside (see below). */
	koraWalkPoints?: 'minwalk' | null;
	alternativesEpsilon?: number;
	alternativesMax?: number;
}

export async function plan(args: PlanArgs, signal?: AbortSignal): Promise<PlanResponse> {
	const fromResolved: [number, number] = args.from.type === 'current'
		? (args.currentCoord ?? [0, 0])
		: args.from.coord;
	const toResolved: [number, number] = args.to.type === 'current'
		? (args.currentCoord ?? [0, 0])
		: args.to.coord;

	const params = new URLSearchParams();
	params.set('fromPlace', formatPlace(args.from, fromResolved));
	params.set('toPlace', formatPlace(args.to, toResolved));
	// fromName/toName: display labels of geocoded point endpoints. MOTIS
	// ignores unknown params — carried purely so the nginx access log
	// (and thus the /stats page) sees the human-readable place names.
	if (args.from.type === 'point' && args.from.displayName)
		params.set('fromName', args.from.displayName);
	if (args.to.type === 'point' && args.to.displayName)
		params.set('toName', args.to.displayName);
	params.set('arriveBy', args.mode === 'arrive' ? 'true' : 'false');
	if (args.time) params.set('time', args.time);
	params.set('numItineraries', String(NUM_ITINERARIES));
	params.set('maxPreTransitTime', String(args.maxPreTransitTime ?? 1800));
	params.set('maxPostTransitTime', String(args.maxPostTransitTime ?? 1800));
	// `maxTravelTime` is TOTAL itinerary duration (transit + all walking)
	// in MINUTES — a low value here silently drops Bern↔Lötschental-style
	// trips where the walking legs alone approach 8 h. 24 h leaves room
	// for any real cross-CH trip; MOTIS's own limits still cap walking.
	params.set('maxTravelTime', '1440');
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
	// max 3 alternates per Pareto point. Minimize-walking widens both
	// (state.svelte.ts passes the values from options.svelte.ts).
	params.set('alternativesEpsilon', String(args.alternativesEpsilon ?? 540));
	params.set('alternativesMax', String(args.alternativesMax ?? 3));
	if (args.koraWalkPoints) params.set('koraWalkPoints', args.koraWalkPoints);
	params.set('searchWindow', String(args.searchWindow ?? 900));
	// Routing options — only sent off their defaults (see PlanArgs).
	if (args.pedestrianSpeedMs != null)
		params.set('pedestrianSpeed', String(args.pedestrianSpeedMs));
	if (args.transferTimeFactor != null)
		params.set('transferTimeFactor', String(args.transferTimeFactor));
	if (args.additionalTransferMin)
		params.set('additionalTransferTime', String(args.additionalTransferMin));

	const url = `${MOTIS_BASE}/api/v1/plan?${params.toString()}`;
	const res = await fetch(url, { signal });
	if (!res.ok) throw new PlanRequestError(res.status, await res.text().catch(() => res.statusText));
	// Every walking duration/geometry in the response is Valhalla-computed
	// server-side by the MOTIS fork (see valhalla-pedestrian-router.md).
	// The one rewrite is the transfer-safety correction below.
	const json = (await res.json()) as PlanResponse;
	normalizeTransferWalks(json, args);
	return json;
}

/** Restate every transfer walk leg at the user's SET walking speed
 * (routing-options.md § Connection safety). MOTIS reports transfer legs
 * as `defaultDuration * transferTimeFactor + additionalTransferTime`;
 * of that factor only the walking-speed part is a real walking time —
 * daring's extra halving and cautious's fixed slack are search knobs,
 * not pace. Left raw, a daring transfer renders as "2 min 310 m" (≈ 9
 * km/h) and contradicts its own "-1 min" tightness chip. Corrected once
 * here, every consumer — leg rows, walked totals, ranking, the tight
 * ladder — speaks the set speed.
 *
 * Scope: WALK legs strictly BETWEEN two transit legs (nigiri footpaths).
 * Access/egress walks come from live Valhalla calls already run at the
 * set speed, and same-stop pseudo walk legs are change-time buffer, not
 * walking — both stay untouched. */
function normalizeTransferWalks(res: PlanResponse, args: PlanArgs): void {
	const unscale = args.transferWalkUnscale ?? 1;
	const slack = (args.additionalTransferMin ?? 0) * 60;
	if (unscale === 1 && slack === 0) return;
	for (const it of [...(res.itineraries ?? []), ...(res.direct ?? [])]) {
		const legs = it.legs ?? [];
		for (let i = 1; i < legs.length - 1; i++) {
			const l = legs[i];
			if (l.mode !== 'WALK') continue;
			if (isWalkMode(legs[i - 1].mode) || isWalkMode(legs[i + 1].mode)) continue;
			if (l.from?.stopId != null && l.from.stopId === l.to?.stopId) continue;
			const raw = l.duration
				?? Math.max(0, (Date.parse(l.endTime) - Date.parse(l.startTime)) / 1000);
			l.duration = Math.max(0, raw - slack) * unscale;
		}
	}
}

function isWalkMode(m: string): boolean {
	return m === 'WALK' || m === 'BIKE' || m === 'CAR';
}
