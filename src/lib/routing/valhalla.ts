import { PUBLIC_VALHALLA_URL } from '$env/static/public';

import { decodePolyline } from './polyline';
import type { DirectRoute } from './types';

// Valhalla client for the direct cycling / walking routes of the routing
// panel (pedestrian-bicycle-routing.md). Local dev points straight at the
// local Valhalla container (valhalla/docker-compose.yml,
// http://localhost:8002), production at the same-origin nginx proxy
// (/valhalla/). Set via PUBLIC_VALHALLA_URL in .env / .env.production;
// inlined at build time — same pattern as PUBLIC_MOTIS_URL.
//
// Only the /route action is used. The transit connection search is
// untouched: it keeps its single MOTIS request per query — the "no client
// Valhalla calls" constraint in routing-options.md applies to the transit
// search only (see the concept § Constraints).
const VALHALLA_BASE = PUBLIC_VALHALLA_URL.replace(/\/$/, '');

/** Alternates requested on top of the primary route (up to 3 total).
 * Valhalla may return fewer (or none) when the network offers no
 * meaningfully distinct paths. */
const NUM_ALTERNATES = 2;

/** Elevation sample spacing along the route in metres. 30 m matches the
 * SRTM 1-arcsec resolution the Valhalla tiles were built with (the docs'
 * recommended value). */
const ELEVATION_INTERVAL_M = 30;

/** Hysteresis threshold (metres) for the ascent / descent totals: an
 * elevation move only counts once it exceeds this band, so terrain-model
 * jitter never accumulates into invented climb — same idea as the noise
 * filter on the transit WALK legs' elevationUp/Down (motis/fork). */
const ELEVATION_NOISE_M = 5;

/** A non-OK response from the Valhalla /route endpoint. Carries the HTTP
 * status so state.svelte.ts can pick a user-facing message; the raw body
 * stays for console diagnostics only. */
export class DirectRouteError extends Error {
	status: number;
	body: string;
	constructor(status: number, body: string) {
		super(`Valhalla ${status}: ${body}`);
		this.name = 'DirectRouteError';
		this.status = status;
		this.body = body;
	}
}

export interface DirectRouteArgs {
	mode: 'bike' | 'walk';
	/** [lon, lat]. */
	from: [number, number];
	to: [number, number];
	/** Rider / walker pace. For `walk` this is passed as Valhalla's
	 * `walking_speed` so direct-walk times agree with the transit tab's
	 * walking legs at the user's set speed tier. Omitted → engine default
	 * (5.1 km/h, the same base the transit stack uses). */
	walkSpeedKmh?: number | null;
}

// ── Valhalla /route response (subset) ────────────────────────────────────

interface ValhallaManeuver {
	type: number;
	/** Length in the requested units (kilometres here). */
	length?: number;
	/** 'pedestrian' on a bicycle route marks a pushed-bike (or stairs)
	 * section — the fork's triplegbuilder reports walkable-but-not-
	 * ridable edges this way (bicycle-costing-fork.md § pushed-bike). */
	travel_mode?: string;
	/** Indices into the leg's decoded shape. */
	begin_shape_index?: number;
	end_shape_index?: number;
}

interface ValhallaLeg {
	shape: string;
	/** Present when `elevation_interval` was requested and the tiles carry
	 * elevation. Metres (matching `units: kilometers`). Samples with no
	 * data can come back as null. */
	elevation?: (number | null)[];
	elevation_interval?: number;
	maneuvers?: ValhallaManeuver[];
}

interface ValhallaTrip {
	legs: ValhallaLeg[];
	summary: {
		/** Kilometres (units: kilometers). */
		length: number;
		/** Seconds. */
		time: number;
		min_lat: number;
		min_lon: number;
		max_lat: number;
		max_lon: number;
	};
}

interface ValhallaRouteResponse {
	trip?: ValhallaTrip;
	alternates?: { trip?: ValhallaTrip }[];
}

/** Maneuver type kStepsEnter — a flight of stairs begins. Used to sum the
 * stairs metres surfaced on the bike cards (the Kora costing fork prices
 * stairs steeply, uphill more than downhill — bicycle-costing-fork.md — so
 * a route only contains stairs when every alternative is clearly worse;
 * `exclude_steps: true` in the bicycle costing options removes them
 * entirely, which is what the avoid-stairs toggle will send). */
const MANEUVER_STEPS_ENTER = 40;

function costingOptions(args: DirectRouteArgs): Record<string, unknown> {
	if (args.mode === 'bike') {
		// Strong hill avoidance by default (concept § Bicycle costing
		// behavior): use_hills 0.1 leans hard toward flat routes until the
		// user-facing hilliness preference ships. Hybrid bike ≈ everyday
		// utility cycling (18 km/h base).
		return { bicycle: { bicycle_type: 'hybrid', use_hills: 0.1 } };
	}
	const pedestrian: Record<string, unknown> = {};
	// Match the transit tab's walking-speed tier so a direct walk and a
	// transit walking leg of the same length agree on duration.
	if (args.walkSpeedKmh != null) pedestrian.walking_speed = args.walkSpeedKmh;
	return { pedestrian };
}

/** Ascent / descent with hysteresis: only elevation moves beyond the
 * noise band count, measured against the last accepted reference. */
function climbTotals(profile: number[]): { up: number; down: number } {
	let up = 0;
	let down = 0;
	let ref = profile[0];
	for (let i = 1; i < profile.length; i++) {
		const v = profile[i];
		if (v >= ref + ELEVATION_NOISE_M) {
			up += v - ref;
			ref = v;
		} else if (v <= ref - ELEVATION_NOISE_M) {
			down += ref - v;
			ref = v;
		}
	}
	return { up, down };
}

function tripToRoute(trip: ValhallaTrip, mode: 'bike' | 'walk'): DirectRoute | null {
	const legs = trip.legs ?? [];
	if (legs.length === 0) return null;
	// Two break locations → one leg; concat defensively anyway.
	const coords: [number, number][] = [];
	const elevation: number[] = [];
	let elevationComplete = true;
	let intervalM = ELEVATION_INTERVAL_M;
	let stairsM = 0;
	const pushedRanges: [number, number][] = [];
	for (const leg of legs) {
		// Ranges are per-leg shape indices; offset them into the
		// concatenated coords (two break locations → one leg anyway).
		const legStart = coords.length;
		// Valhalla encodes shapes with 6-digit precision.
		coords.push(...decodePolyline(leg.shape, 6));
		if (Array.isArray(leg.elevation) && leg.elevation.length > 0) {
			if (leg.elevation_interval) intervalM = leg.elevation_interval;
			for (const v of leg.elevation) {
				if (typeof v === 'number' && Number.isFinite(v)) elevation.push(v);
				else elevationComplete = false;
			}
		} else {
			elevationComplete = false;
		}
		for (const m of leg.maneuvers ?? []) {
			if (m.type === MANEUVER_STEPS_ENTER && typeof m.length === 'number') {
				stairsM += m.length * 1000;
			}
			// Pushed-bike sections: pedestrian-mode maneuvers on a bike
			// route, drawn dotted on the map. Adjacent ranges merge so a
			// push crossing a maneuver boundary stays one dotted run.
			if (
				mode === 'bike' &&
				m.travel_mode === 'pedestrian' &&
				typeof m.begin_shape_index === 'number' &&
				typeof m.end_shape_index === 'number' &&
				m.end_shape_index > m.begin_shape_index
			) {
				const start = legStart + m.begin_shape_index;
				const end = legStart + m.end_shape_index;
				const last = pushedRanges[pushedRanges.length - 1];
				if (last && start <= last[1]) last[1] = Math.max(last[1], end);
				else pushedRanges.push([start, end]);
			}
		}
	}
	if (coords.length < 2) return null;
	const hasProfile = elevationComplete && elevation.length >= 2;
	const totals = hasProfile ? climbTotals(elevation) : null;
	const s = trip.summary;
	return {
		mode,
		durationSec: s.time,
		distanceM: s.length * 1000,
		coords,
		bbox: [s.min_lon, s.min_lat, s.max_lon, s.max_lat],
		ascentM: totals ? Math.round(totals.up) : null,
		descentM: totals ? Math.round(totals.down) : null,
		profile: hasProfile ? elevation : null,
		profileIntervalM: intervalM,
		stairsM: Math.round(stairsM),
		pushedRanges
	};
}

/** One direct cycling / walking query: a single /route request with
 * `alternates`, so up to 3 routes come back at once (concept § Query &
 * alternatives). The primary route is always index 0. */
export async function fetchDirectRoutes(
	args: DirectRouteArgs,
	signal?: AbortSignal
): Promise<DirectRoute[]> {
	const request = {
		costing: args.mode === 'bike' ? 'bicycle' : 'pedestrian',
		costing_options: costingOptions(args),
		locations: [
			{ lat: args.from[1], lon: args.from[0], type: 'break' },
			{ lat: args.to[1], lon: args.to[0], type: 'break' }
		],
		alternates: NUM_ALTERNATES,
		units: 'kilometers',
		elevation_interval: ELEVATION_INTERVAL_M,
		// Maneuvers only (no instruction text) — needed for the stairs
		// detection; keeps the response small.
		directions_type: 'maneuvers'
	};
	// No explicit Content-Type on purpose: fetch then sends text/plain,
	// which is a CORS "simple request" — no OPTIONS preflight, which the
	// dev Valhalla (prime_server) would not answer. Valhalla parses the
	// body regardless of content type, and its responses carry
	// Access-Control-Allow-Origin: *; in production the call is
	// same-origin via the /valhalla/ nginx proxy anyway.
	const res = await fetch(`${VALHALLA_BASE}/route`, {
		method: 'POST',
		body: JSON.stringify(request),
		signal
	});
	if (!res.ok) {
		throw new DirectRouteError(res.status, await res.text().catch(() => res.statusText));
	}
	const json = (await res.json()) as ValhallaRouteResponse;
	const trips: ValhallaTrip[] = [];
	if (json.trip) trips.push(json.trip);
	for (const a of json.alternates ?? []) if (a?.trip) trips.push(a.trip);
	return trips
		.map((t) => tripToRoute(t, args.mode))
		.filter((r): r is DirectRoute => r !== null);
}
