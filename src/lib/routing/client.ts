import type { Endpoint, PlanResponse, TimeMode } from './types';

// Local MOTIS instance (see motis/docker-compose.yml). Overridable via
// VITE_MOTIS_URL so deployment can point elsewhere without a code change.
const MOTIS_BASE = (import.meta.env.VITE_MOTIS_URL ?? 'http://localhost:8080').replace(/\/$/, '');

const NUM_ITINERARIES = 5;

function formatPlace(ep: Endpoint, resolved: [number, number]): string {
	if (ep.type === 'station') return `${ep.coord[1]},${ep.coord[0]}`;
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
	params.set('arriveBy', args.mode === 'arrive' ? 'true' : 'false');
	if (args.time) params.set('time', args.time);
	params.set('numItineraries', String(NUM_ITINERARIES));
	// Walking caps at 8 h per leg (server-matched):
	// street_routing_max_prepost_transit_seconds and _direct_seconds are
	// 28800 in motis/config.yml. Pre/post transit are SECONDS.
	//
	// `maxTravelTime` is TOTAL itinerary duration (transit + all walking)
	// in MINUTES — a low value here silently drops Bern↔Lötschental-style
	// trips where the walking legs alone approach 8 h. 24 h leaves room
	// for any real cross-CH trip; MOTIS's own limits still cap walking.
	params.set('maxPreTransitTime', '28800');
	params.set('maxPostTransitTime', '28800');
	params.set('maxTravelTime', '1440');
	// directModes controls the non-transit fallback that MOTIS returns in
	// `direct[]`. WALK is the default but set it explicitly so a
	// walk-only itinerary always comes back for merging.
	params.set('directModes', 'WALK');
	// MOTIS caps direct (walk-only) itineraries at 30 min by default —
	// past that, it falls back to weird walking-heavy transit hybrids
	// (WALK 45m + BUS 0m + WALK 1m) instead of just admitting "walk it".
	// Lift to the same 8 h server ceiling as pre/post transit walking.
	params.set('maxDirectTime', '28800');
	// searchWindow is the time span MOTIS considers for transit
	// departures starting from `time`. Default is 900 s (15 min) — far
	// too tight for e.g. a 00:30 query where the first bus is at 06:00,
	// or a Saturday-night query where nothing runs until Monday.
	// 72 h covers weekend gaps; MOTIS still returns only `numItineraries`
	// (5) results after sorting, so a busy midday window doesn't flood
	// the panel. Server hard cap is 96 h (plan_max_search_window_minutes
	// = 5760); staying at 72 h leaves headroom.
	params.set('searchWindow', '259200');

	const url = `${MOTIS_BASE}/api/v1/plan?${params.toString()}`;
	const res = await fetch(url, { signal });
	if (!res.ok) throw new Error(`MOTIS ${res.status}: ${await res.text().catch(() => res.statusText)}`);
	return res.json() as Promise<PlanResponse>;
}
