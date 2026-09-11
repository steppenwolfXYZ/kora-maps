import type { Endpoint } from '$lib/routing/types';
import { shareFingerprint, type ShareData } from '$lib/routing/share';
import { DEFAULT_OPTIONS, planOptionParams } from '$lib/routing/optionParams';
import { allItineraries, planMotis } from '$lib/server/plan/motis';

// Server-side share re-verification (connection-sharing.md § Shared view):
// deletion is gated on the SERVER confirming the connection is gone — a
// viewer's DELETE alone must not be able to kill a still-valid share.
// Uses the same MOTIS call as the planning engine (plan/motis.ts).

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
	let fromPlace: string;
	let toPlace: string;
	try {
		fromPlace = formatPlace(share.from);
		toPlace = formatPlace(share.to);
	} catch {
		return 'error';
	}
	try {
		const res = await planMotis({
			fromPlace, toPlace,
			mode: 'leave',
			time: share.itinerary.startTime,
			// Repeat the share's via chain — without it a via-forced
			// connection may not come back at all and the share would read
			// as expired (via-stops.md § Persistence and sharing).
			vias: (share.vias ?? []).map((v) => ({
				placeId: `ch_${v.station.pid ?? `Parent${v.station.uic}`}`,
				waitMin: Math.round(v.wait)
			})),
			maxPreTransitTime: 28800,
			maxPostTransitTime: 28800,
			searchWindow: 3600,
			fullTransfers: false,
			options: planOptionParams(DEFAULT_OPTIONS)
		}, AbortSignal.timeout(20_000));
		return allItineraries(res).some((it) => shareFingerprint(it) === share.fingerprint)
			? 'present'
			: 'gone';
	} catch {
		return 'error';
	}
}
