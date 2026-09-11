import type { Endpoint, FilledVia, Itinerary, StationEndpoint, TimeMode } from './types';
import type { RoutingOptionValues } from './optionParams';
import { formatPlace, stationPlaceId } from './place';

// Browser client of the app's own planning endpoint, GET /api/plan
// (server-side-transit-planning.md). The whole search cascade —
// narrow / wide walking budgets, the time-advance hops, dominance
// pruning — runs on the server; this issues exactly one request per user
// action and receives the final list. The browser never talks to MOTIS.

export type Extension = 'earlier' | 'later';

/** A non-OK response from /api/plan. `kind` is the server's failure
 * class (never MOTIS wording); state.svelte.ts maps it to a message. */
export class PlanRequestError extends Error {
	status: number;
	kind: string;
	constructor(status: number, kind: string) {
		super(`plan ${status}: ${kind}`);
		this.name = 'PlanRequestError';
		this.status = status;
		this.kind = kind;
	}
}

export interface PlanArgs {
	from: Endpoint;
	to: Endpoint;
	/** Coord of a `current` endpoint ([lon, lat]); null if neither side
	 * is `current`. The endpoint never sees "current" — it gets a point. */
	currentCoord: [number, number] | null;
	/** Ordered via stops (via-stops.md). Filled rows only — at most the
	 * engine's ceiling. Each carries the REQUESTED minimum stay in
	 * minutes; 0 lets the traveller stay on board. */
	vias: FilledVia[];
	mode: TimeMode;
	/** Concrete ISO timestamp — never null; the caller pins "now". */
	time: string;
	options: RoutingOptionValues;
	/** History of the earlier / later clicks on this query, in order. The
	 * server replays it, so the response is always the whole list. */
	extensions: Extension[];
	/** Share fingerprint to verify (connection-sharing.md § Shared view). */
	share?: string | null;
}

export interface PlanResult {
	itineraries: Itinerary[];
	/** comfort-walk-baseline.md: the query's unavoidable walking in
	 * seconds — a ranking knob the cards need. */
	walkBaselineSec: number;
	budget: 'narrow' | 'wide';
	/** Share mode only: the matching itinerary, or null when gone. */
	shareMatch?: Itinerary | null;
	/** The server's time ceiling cut the search short. */
	truncated: boolean;
}

export async function plan(args: PlanArgs, signal?: AbortSignal): Promise<PlanResult> {
	const params = new URLSearchParams();
	params.set('fromPlace', formatPlace(args.from, args.currentCoord));
	params.set('toPlace', formatPlace(args.to, args.currentCoord));
	// fromName/toName: display labels of geocoded point endpoints. The
	// server ignores them — carried purely so the nginx access log (and
	// thus the /stats page) sees the human-readable place names.
	if (args.from.type === 'point' && args.from.displayName)
		params.set('fromName', args.from.displayName);
	if (args.to.type === 'point' && args.to.displayName)
		params.set('toName', args.to.displayName);
	params.set('arriveBy', args.mode === 'arrive' ? 'true' : 'false');
	params.set('time', args.time);
	// Stop ids only — the engine rejects coordinates here, which is why
	// transit vias are always stations. The station filter is type-level:
	// point vias exist only on the direct tabs, which never call plan().
	const vias = args.vias.filter(
		(v): v is FilledVia & { station: StationEndpoint } => v.station.type === 'station'
	);
	if (vias.length > 0) {
		params.set('via', vias.map((v) => stationPlaceId(v.station)).join(','));
		params.set('viaMinimumStay', vias.map((v) => String(Math.round(v.wait))).join(','));
	}
	// Routing options — only sent off their defaults.
	if (args.options.walkSpeed !== 'normal') params.set('walkSpeed', args.options.walkSpeed);
	if (args.options.safety !== 'balanced') params.set('safety', args.options.safety);
	if (args.options.minimizeWalking) params.set('minWalk', '1');
	if (args.extensions.length > 0) params.set('extend', args.extensions.join(','));
	if (args.share) params.set('share', args.share);

	const res = await fetch(`/api/plan?${params.toString()}`, { signal });
	if (!res.ok) {
		let kind = 'unknown';
		try {
			kind = String(((await res.json()) as { error?: string }).error ?? kind);
		} catch {
			// non-JSON error page (proxy, gateway) — the status is enough
		}
		throw new PlanRequestError(res.status, kind);
	}
	return (await res.json()) as PlanResult;
}
