import type { Endpoint, FilledVia, Itinerary, Leg } from './types';
import { hash8 } from './fingerprint';
import { isTransitMode } from './itineraryFormat';

// Connection sharing (connection-sharing.md). A share is a server-stored
// snapshot of one itinerary plus the query context needed to re-verify it
// against a live MOTIS query. This module is imported by both the client
// (share button, shared-view verification) and the server (store,
// re-verification before deletion) — keep it free of browser/node-only APIs.

/** Stored share document — one JSON file per share on the server. */
export interface ShareData {
	v: 1;
	id: string;
	createdAt: string;
	/** Share fingerprint of `itinerary` (see shareFingerprint below). */
	fingerprint: string;
	/** Never `current` — resolved to a concrete point at share time. */
	from: Endpoint;
	to: Endpoint;
	/** Via stops the shared query carried (via-stops.md). Absent on shares
	 * created before vias existed — and on any via-less route. The
	 * re-verification query must repeat them: a via-forced connection is
	 * not necessarily Pareto-optimal without its vias, and would read as
	 * expired. */
	vias?: FilledVia[];
	/** Stripped itinerary: no legGeometry / intermediateStops (the shared
	 * view renders the live re-queried itinerary, never this copy). */
	itinerary: Itinerary;
	/** Resolved badge color per leg, parallel to `itinerary.legs` — baked at
	 * share time so the og:image never depends on rebuild-varying pipeline
	 * artifacts (route_color_index.json). */
	legColors: string[];
}

/** Client → POST /api/share body. */
export type SharePayload = Omit<ShareData, 'id' | 'createdAt'>;

// ---------------------------------------------------------------------------
// Share fingerprint — the identity a re-queried itinerary must match for the
// share to count as "still available". Deliberately looser than
// itineraryFingerprint (fingerprint.ts): feed re-releases churn trip ids and
// platform assignments while the human-visible connection (same lines, same
// stations, same times) persists, and Valhalla tile updates jitter walking
// durations by seconds. Transit legs match on mode + times + line number +
// stop names; walk/bike/car legs are excluded entirely. A walk-only direct
// itinerary falls back to its minute-rounded duration.

function shareLegKey(leg: Leg): string {
	return [
		leg.mode, leg.startTime, leg.endTime,
		leg.routeShortName ?? '',
		leg.from?.name ?? '',
		leg.to?.name ?? ''
	].join('|');
}

export function shareFingerprint(it: Itinerary): string {
	const transit = it.legs.filter((l) => isTransitMode(l.mode));
	if (transit.length === 0) {
		return hash8(['WALKONLY', it.startTime, String(Math.round(it.duration / 60))].join('|'));
	}
	return hash8(transit.map(shareLegKey).join('||'));
}

// ---------------------------------------------------------------------------
// Payload construction (client side, share button).

/** Drop bulky fields the share never needs: geometry and intermediate stops.
 * The shared view displays the live re-queried itinerary; the stored copy
 * only feeds the og:image and the verification fingerprint. */
function stripItinerary(it: Itinerary): Itinerary {
	return {
		...it,
		legs: it.legs.map((leg) => {
			const { legGeometry, intermediateStops, ...rest } = leg;
			void legGeometry;
			void intermediateStops;
			return rest;
		})
	};
}

/** A `current` endpoint must not travel — the viewer's location is not the
 * sharer's. Resolve it to the concrete coordinate the itinerary actually
 * used (first/last leg place). */
function concreteEndpoint(ep: Endpoint, it: Itinerary, side: 'from' | 'to'): Endpoint {
	if (ep.type !== 'current') return ep;
	const leg = side === 'from' ? it.legs[0] : it.legs[it.legs.length - 1];
	const place = side === 'from' ? leg?.from : leg?.to;
	const lat = place?.lat ?? 0;
	const lon = place?.lon ?? 0;
	return { type: 'point', coord: [lon, lat] };
}

export function buildSharePayload(
	it: Itinerary,
	from: Endpoint,
	to: Endpoint,
	legColors: string[],
	vias: FilledVia[] = []
): SharePayload {
	const payload: SharePayload = {
		v: 1,
		fingerprint: shareFingerprint(it),
		from: concreteEndpoint(from, it, 'from'),
		to: concreteEndpoint(to, it, 'to'),
		itinerary: stripItinerary(it),
		legColors
	};
	if (vias.length > 0) payload.vias = vias;
	return payload;
}

export interface ShareCreateResult {
	id: string;
	/** Absolute share URL, built from the current origin. */
	url: string;
}

export async function createShare(payload: SharePayload): Promise<ShareCreateResult> {
	const res = await fetch('/api/share', {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(payload)
	});
	if (!res.ok) throw new Error(`share create failed: HTTP ${res.status}`);
	const { id } = (await res.json()) as { id: string };
	return { id, url: `${window.location.origin}/s/${id}` };
}

/** Fire-and-forget: tell the server the shared connection no longer matches
 * a live query. The server re-verifies against MOTIS itself before deleting
 * (a viewer must not be able to delete a still-valid share). */
export function reportShareExpired(id: string): void {
	void fetch(`/api/share/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => {});
}
