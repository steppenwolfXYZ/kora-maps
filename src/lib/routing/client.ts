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
	params.set('searchWindow', String(args.searchWindow ?? 900));

	const url = `${MOTIS_BASE}/api/v1/plan?${params.toString()}`;
	const res = await fetch(url, { signal });
	if (!res.ok) throw new PlanRequestError(res.status, await res.text().catch(() => res.statusText));
	// Every walking duration/geometry in the response is already
	// Valhalla-computed server-side by the MOTIS fork (see
	// valhalla-pedestrian-router.md) — no client-side rewriting.
	return (await res.json()) as PlanResponse;
}
