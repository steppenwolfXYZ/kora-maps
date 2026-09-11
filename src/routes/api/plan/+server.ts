import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { MAX_VIAS, MAX_VIA_WAIT_MIN } from '$lib/routing/types';
import { isValidPlace } from '$lib/routing/place';
import {
	DEFAULT_OPTIONS, isSafetyMode, isWalkSpeedTier, type RoutingOptionValues
} from '$lib/routing/optionParams';
import { MotisRequestError, type MotisVia } from '$lib/server/plan/motis';
import { PlanDeadlineError, planCascade, type Extension, type PlanQuery } from '$lib/server/plan/cascade';

// GET /api/plan — the app's transit planning endpoint
// (server-side-transit-planning.md). One request per user action; the
// whole search cascade runs here against MOTIS and the response is the
// final list the panel shows.
//
// Deliberately a GET with the query in the URL: nginx logs the request
// line, and the /stats page counts routing queries and reads the endpoint
// tokens + display names (fromPlace / toPlace / fromName / toName) off
// exactly that line. One log line now equals one user action.
//
// Parameters:
//   fromPlace, toPlace   MOTIS place strings ("ch_<stop id>" | "lat,lon")
//   fromName, toName     display labels — ignored here, read by the stats
//   arriveBy             "true" | "false"
//   time                 ISO timestamp (always concrete; the client pins
//                        "now" before sending)
//   via, viaMinimumStay  comma lists — stop ids and minutes, same order
//   walkSpeed, safety, minWalk   routing options (routing-options.md)
//   extend               comma list of "earlier" | "later" — the history
//                        of the earlier/later clicks on this query
//   share                share fingerprint to verify (connection-sharing.md)

const MAX_EXTENSIONS = 20;

class BadRequest extends Error {}

function parseVias(url: URL): MotisVia[] {
	const ids = url.searchParams.get('via');
	if (!ids) return [];
	const placeIds = ids.split(',').filter(Boolean);
	if (placeIds.length > MAX_VIAS) throw new BadRequest('too many vias');
	const stays = (url.searchParams.get('viaMinimumStay') ?? '').split(',');
	return placeIds.map((placeId, i) => {
		if (!/^ch_[A-Za-z0-9:_.-]{1,80}$/.test(placeId)) throw new BadRequest('bad via');
		const waitMin = stays[i] ? Number(stays[i]) : 0;
		if (!Number.isInteger(waitMin) || waitMin < 0 || waitMin > MAX_VIA_WAIT_MIN)
			throw new BadRequest('bad via stay');
		return { placeId, waitMin };
	});
}

function parseOptions(url: URL): RoutingOptionValues {
	const walk = url.searchParams.get('walkSpeed');
	const safety = url.searchParams.get('safety');
	return {
		walkSpeed: isWalkSpeedTier(walk) ? walk : DEFAULT_OPTIONS.walkSpeed,
		safety: isSafetyMode(safety) ? safety : DEFAULT_OPTIONS.safety,
		minimizeWalking: url.searchParams.get('minWalk') === '1'
	};
}

function parseExtensions(url: URL): Extension[] {
	const raw = url.searchParams.get('extend');
	if (!raw) return [];
	const list = raw.split(',').filter(Boolean);
	if (list.length > MAX_EXTENSIONS) throw new BadRequest('too many extensions');
	return list.map((e) => {
		if (e !== 'earlier' && e !== 'later') throw new BadRequest('bad extension');
		return e;
	});
}

function parseQuery(url: URL): { q: PlanQuery; extensions: Extension[] } {
	const fromPlace = url.searchParams.get('fromPlace') ?? '';
	const toPlace = url.searchParams.get('toPlace') ?? '';
	if (!isValidPlace(fromPlace) || !isValidPlace(toPlace)) throw new BadRequest('bad place');
	const arriveBy = url.searchParams.get('arriveBy');
	if (arriveBy !== 'true' && arriveBy !== 'false') throw new BadRequest('bad arriveBy');
	const time = url.searchParams.get('time') ?? '';
	if (!time || Number.isNaN(Date.parse(time))) throw new BadRequest('bad time');
	const share = url.searchParams.get('share');
	if (share !== null && !/^[0-9a-f]{8}$/.test(share)) throw new BadRequest('bad share');
	return {
		q: {
			fromPlace, toPlace,
			mode: arriveBy === 'true' ? 'arrive' : 'leave',
			time: new Date(time).toISOString(),
			vias: parseVias(url),
			options: parseOptions(url),
			share
		},
		extensions: parseExtensions(url)
	};
}

export const GET: RequestHandler = async ({ url, request }) => {
	let parsed: ReturnType<typeof parseQuery>;
	try {
		parsed = parseQuery(url);
	} catch (e) {
		if (e instanceof BadRequest) return json({ error: 'bad_request' }, { status: 400 });
		throw e;
	}
	try {
		const outcome = await planCascade(parsed.q, parsed.extensions, request.signal);
		return json(outcome, { headers: { 'cache-control': 'no-store' } });
	} catch (e) {
		// The client only ever sees a class of failure, never MOTIS's
		// wording; the raw error goes to the server log.
		if (e instanceof MotisRequestError) {
			console.error('[plan] MOTIS rejected the query:', e.message.slice(0, 500));
			// A 4xx from MOTIS almost always means an endpoint the current
			// timetable doesn't know (e.g. a stale stop id in a bookmark).
			return e.status >= 400 && e.status < 500
				? json({ error: 'rejected' }, { status: 400 })
				: json({ error: 'upstream' }, { status: 502 });
		}
		const name = (e as Error)?.name;
		if (e instanceof PlanDeadlineError || name === 'TimeoutError' || name === 'AbortError') {
			console.error('[plan] search exceeded the time ceiling');
			return json({ error: 'timeout' }, { status: 504 });
		}
		console.error('[plan] search failed:', e);
		return json({ error: 'unavailable' }, { status: 503 });
	}
};
