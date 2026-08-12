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
	/** Walking budget for the walk from FROM to first stop, and last stop
	 * to TO, in SECONDS. Server hard-caps at 28800 (8 h). Cascade lifts
	 * this from 7200 (2 h) to 28800 (8 h) on trigger. */
	maxPreTransitTime?: number;
	maxPostTransitTime?: number;
	/** MOTIS's paging cursor from a previous PlanResponse. When set, MOTIS
	 * returns the next window of transit departures (leave-at) or earlier
	 * ones (arrive-by). searchWindow is ignored in favour of the cursor's
	 * own window. */
	pageCursor?: string;
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
	params.set('maxPreTransitTime', String(args.maxPreTransitTime ?? 7200));
	params.set('maxPostTransitTime', String(args.maxPostTransitTime ?? 7200));
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
	if (args.pageCursor) {
		params.set('pageCursor', args.pageCursor);
	} else {
		// Fresh query — start with the MOTIS default 15-min window. The
		// caller cascades via `nextPageCursor`/`previousPageCursor` if
		// fewer than NUM_ITINERARIES results come back.
		params.set('searchWindow', '900');
	}

	const url = `${MOTIS_BASE}/api/v1/plan?${params.toString()}`;
	const res = await fetch(url, { signal });
	if (!res.ok) throw new Error(`MOTIS ${res.status}: ${await res.text().catch(() => res.statusText)}`);
	return res.json() as Promise<PlanResponse>;
}
