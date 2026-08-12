import type { Itinerary, Leg } from './types';

// Itinerary fingerprint — a stable short id derived from the leg breakdown.
// Used to identify a selected itinerary across MOTIS reissues (URL deep
// link with `?route=<fp>`): the panel's from/to/mode/time already fully
// determine the query, and the fingerprint picks which of the returned
// itineraries the user selected. If no returned itinerary hashes to the
// same value, the route is treated as no-longer-valid.
//
// Fingerprint content — the "full leg breakdown" per route-display.md:
// mode, transit line ids, boarding + alighting stop ids, leg start times.
// Walking legs contribute mode + times only (no stop ids).

function legFingerprint(leg: Leg): string {
	const parts: string[] = [leg.mode, leg.startTime, leg.endTime];
	if (leg.mode !== 'WALK' && leg.mode !== 'BIKE' && leg.mode !== 'CAR') {
		parts.push(
			leg.routeId ?? '',
			leg.tripId ?? '',
			leg.from?.stopId ?? '',
			leg.to?.stopId ?? ''
		);
	}
	return parts.join('|');
}

function itineraryPayload(it: Itinerary): string {
	return it.legs.map(legFingerprint).join('||');
}

// Short stable hash — a djb2-derived 32-bit fold expressed as 8 hex
// characters. Not a cryptographic hash; sufficient for indexing 5 or so
// itineraries per query.
function hash8(input: string): string {
	let h = 5381;
	for (let i = 0; i < input.length; i++) {
		h = ((h << 5) + h) + input.charCodeAt(i);
		h |= 0;
	}
	return (h >>> 0).toString(16).padStart(8, '0');
}

export function itineraryFingerprint(it: Itinerary): string {
	return hash8(itineraryPayload(it));
}
