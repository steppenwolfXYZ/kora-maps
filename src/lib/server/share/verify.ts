import { env } from '$env/dynamic/private';
import type { Endpoint, Itinerary } from '$lib/routing/types';
import { shareFingerprint, type ShareData } from '$lib/routing/share';

// Server-side share re-verification (connection-sharing.md § Shared view):
// deletion is gated on the SERVER confirming the connection is gone — a
// viewer's DELETE alone must not be able to kill a still-valid share.
//
// MOTIS_INTERNAL_URL: server-reachable MOTIS base. The client's
// PUBLIC_MOTIS_URL is '/routing' in production (nginx proxy) — useless for
// a server-side fetch, hence the separate env var. Dev default matches the
// local MOTIS container; prod: http://127.0.0.1:8080 (loopback-bound).

const MOTIS_BASE = (env.MOTIS_INTERNAL_URL || 'http://localhost:8080').replace(/\/$/, '');

/** Mirrors formatPlace in routing/client.ts (kept separate — the client
 * module hard-binds to PUBLIC_MOTIS_URL). */
function formatPlace(ep: Endpoint): string {
	if (ep.type === 'station') return `ch_${ep.pid ?? `Parent${ep.uic}`}`;
	if (ep.type === 'point') return `${ep.coord[1]},${ep.coord[0]}`;
	throw new Error('share endpoints are always concrete');
}

export type VerifyResult = 'present' | 'gone' | 'error';

/** Re-query MOTIS around the share's departure and look for the stored
 * share fingerprint. Uses the wide walking budget so long first/last-mile
 * walks can't fake an expiry. `error` = MOTIS unreachable / bad response —
 * the caller must NOT delete in that case. */
export async function verifyShare(share: ShareData): Promise<VerifyResult> {
	const params = new URLSearchParams();
	try {
		params.set('fromPlace', formatPlace(share.from));
		params.set('toPlace', formatPlace(share.to));
	} catch {
		return 'error';
	}
	params.set('arriveBy', 'false');
	params.set('time', share.itinerary.startTime);
	params.set('numItineraries', '5');
	params.set('maxPreTransitTime', '28800');
	params.set('maxPostTransitTime', '28800');
	params.set('maxTravelTime', '1440');
	params.set('directModes', 'WALK');
	params.set('maxDirectTime', '28800');
	params.set('searchWindow', '3600');

	try {
		const res = await fetch(`${MOTIS_BASE}/api/v1/plan?${params.toString()}`, {
			signal: AbortSignal.timeout(20_000)
		});
		if (!res.ok) return 'error';
		const body = (await res.json()) as { itineraries?: Itinerary[]; direct?: Itinerary[] };
		const all = [...(body.itineraries ?? []), ...(body.direct ?? [])];
		return all.some((it) => shareFingerprint(it) === share.fingerprint)
			? 'present'
			: 'gone';
	} catch {
		return 'error';
	}
}
