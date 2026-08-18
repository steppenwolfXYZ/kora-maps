// Client for the Kora Valhalla pedestrian router.
//
// Valhalla is the sole walking authority — every walking duration or
// geometry the user sees comes from here, not from MOTIS's OSM walker
// (see .claude/concepts/valhalla-pedestrian-router.md). This module
// wraps two use cases the frontend needs:
//
//   1. `computeWalk(from, to)` — single-pair walk for a WALK leg the
//      MOTIS response returned (rewriting) or for a direct walk-only
//      itinerary the user asked for.
//
// Elevation, slope, stairs, and surface are already baked into the
// duration Valhalla returns (its pedestrian cost model), so callers
// treat the returned seconds as ground truth.
//
// The `PUBLIC_VALHALLA_URL` env var wires dev vs prod:
//   .env             → http://localhost:8002
//   .env.production  → /valhalla   (nginx reverse-proxied on the VPS)

import { PUBLIC_VALHALLA_URL } from '$env/static/public';

const VALHALLA_BASE = PUBLIC_VALHALLA_URL.replace(/\/$/, '');

// Base walking speed. Kept in sync with WALK_SPEED_KMH in
// scripts/build_valhalla_footpath_matrix.py so the matrix baked into
// MOTIS's transfer table and the query-time walks the app receives
// share one speed profile — the concept requires direct-walk timing to
// match transit-leg walk timing.
const WALKING_SPEED_KMH = 5.1;

const COSTING_OPTIONS = {
	costing: 'pedestrian',
	costing_options: {
		pedestrian: {
			walking_speed: WALKING_SPEED_KMH,
			use_hills: 1.0,
			use_lit: 0.0,
		},
	},
} as const;

export interface WalkResult {
	/** Whole seconds. */
	durationSec: number;
	/** Metres. */
	distanceM: number;
	/** Google-encoded polyline (precision 6 — Valhalla's default). */
	encodedPolyline: string;
	/** Precision matching `encodedPolyline`. Always 6 for the Valhalla
	 * response, but callers hand it to the shared decoder verbatim. */
	polylinePrecision: 6;
}

/** Route a single walk from (lat, lon) to (lat, lon). Returns null on
 * a Valhalla "no path" (isolated island stops etc.) so callers can
 * distinguish it from a network error. */
export async function computeWalk(
	from: { lat: number; lon: number },
	to: { lat: number; lon: number },
	signal?: AbortSignal,
): Promise<WalkResult | null> {
	const body = {
		...COSTING_OPTIONS,
		locations: [
			{ lat: from.lat, lon: from.lon, type: 'break' },
			{ lat: to.lat,   lon: to.lon,   type: 'break' },
		],
		directions_options: { units: 'kilometers' },
	};

	const res = await fetch(`${VALHALLA_BASE}/route`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body),
		signal,
	});
	if (!res.ok) {
		// Valhalla returns 400 with a JSON error body for "no path" — treat
		// that specifically as null. Anything else is a real failure.
		if (res.status === 400) {
			try {
				const err = await res.json();
				if (typeof err?.error_code === 'number') return null;
			} catch {
				// Fall through to throw.
			}
		}
		throw new Error(`Valhalla ${res.status}: ${await res.text().catch(() => res.statusText)}`);
	}
	const json = (await res.json()) as {
		trip?: {
			legs?: Array<{
				summary?: { time?: number; length?: number };
				shape?: string;
			}>;
		};
	};
	const leg = json.trip?.legs?.[0];
	if (!leg?.shape || leg.summary?.time == null || leg.summary?.length == null) {
		return null;
	}
	return {
		durationSec: Math.round(leg.summary.time),
		// Valhalla returns length in the units the caller requested — we
		// asked for kilometers, so convert to metres.
		distanceM: Math.round(leg.summary.length * 1000),
		encodedPolyline: leg.shape,
		polylinePrecision: 6,
	};
}
