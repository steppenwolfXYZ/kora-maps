import type { Endpoint, Itinerary, PlanResponse, TimeMode } from './types';

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
	/** Time-window size passed to MOTIS in seconds. Defaults to 900 (15 min)
	 * for a fast initial query; the cascade in state.svelte.ts widens this
	 * to 7200 (2 h) once it's advancing `time` forward to accumulate more
	 * results. */
	searchWindow?: number;
}

/** Extract the bare UIC from a MOTIS-prefixed stop or parent id
 * ("ch_Parent8500010" / "ch_8500010:0:5" → "8500010"). */
function bareUic(id: string | undefined): string | null {
	if (!id) return null;
	const m = id.match(/(\d+)/);
	return m ? m[1] : null;
}

/** When a From/To endpoint is a station, MOTIS still receives its coord and
 * plans a first/last-mile walk from the coord to the platform — even when
 * the itinerary boards/alights at that exact station. That spurious walk
 * shifts the itinerary's start/end time off the transit schedule. Strip
 * the leading WALK when its arrival stop shares the requested From
 * station's parent UIC, and symmetrically the trailing WALK against To. */
function stripStationWalks(it: Itinerary, fromUic: string | null, toUic: string | null): Itinerary {
	let legs = it.legs;
	if (fromUic && legs.length > 1 && legs[0].mode === 'WALK') {
		const arrivalUic = bareUic(legs[0].to?.parentId ?? legs[0].to?.stopId);
		if (arrivalUic === fromUic) legs = legs.slice(1);
	}
	if (toUic && legs.length > 1 && legs[legs.length - 1].mode === 'WALK') {
		const departureUic = bareUic(legs[legs.length - 1].from?.parentId ?? legs[legs.length - 1].from?.stopId);
		if (departureUic === toUic) legs = legs.slice(0, -1);
	}
	if (legs.length === it.legs.length) return it;
	const startTime = legs[0].startTime;
	const endTime = legs[legs.length - 1].endTime;
	const duration = Math.max(0, (Date.parse(endTime) - Date.parse(startTime)) / 1000);
	let walkTime = 0;
	for (const l of legs) {
		if (l.mode !== 'WALK') continue;
		walkTime += l.duration ?? Math.max(0, (Date.parse(l.endTime) - Date.parse(l.startTime)) / 1000);
	}
	return { ...it, legs, startTime, endTime, duration, walkTime };
}

function stripStationWalksInResponse(res: PlanResponse, from: Endpoint, to: Endpoint): PlanResponse {
	const fromUic = from.type === 'station' ? from.uic : null;
	const toUic   = to.type   === 'station' ? to.uic   : null;
	if (!fromUic && !toUic) return res;
	const map = (its?: Itinerary[]) => its?.map((it) => stripStationWalks(it, fromUic, toUic));
	return { ...res, itineraries: map(res.itineraries) ?? [], direct: map(res.direct) };
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
	params.set('searchWindow', String(args.searchWindow ?? 900));

	const url = `${MOTIS_BASE}/api/v1/plan?${params.toString()}`;
	const res = await fetch(url, { signal });
	if (!res.ok) throw new Error(`MOTIS ${res.status}: ${await res.text().catch(() => res.statusText)}`);
	const json = (await res.json()) as PlanResponse;
	return stripStationWalksInResponse(json, args.from, args.to);
}
