import { PUBLIC_VALHALLA_URL } from '$env/static/public';

import { decodePolyline } from './polyline';
import type { DirectRoute } from './types';

// Valhalla client for the direct cycling / walking routes of the routing
// panel (pedestrian-bicycle-routing.md). Local dev points straight at the
// local Valhalla container (valhalla/docker-compose.yml,
// http://localhost:8002), production at the same-origin nginx proxy
// (/valhalla/). Set via PUBLIC_VALHALLA_URL in .env / .env.production;
// inlined at build time. The only engine the browser still calls
// directly — the transit search goes through the app's own /api/plan
// (server-side-transit-planning.md).
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

/** Endpoints must never snap onto a ferry / car-shuttle edge: a station
 * coord sitting beside the tracks (Brig) otherwise snaps to the shuttle's
 * rail way and the route "boards" it mid-line, producing a shuttle loop
 * before the real route starts. Snapping only — routes still use shuttles
 * mid-route by entering at their terminals. */
const SNAP_FILTER = { exclude_ferry: true };

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
	/** Ordered via points, [lon, lat] — sent as `through` locations, so
	 * the route passes through each without a modelled stop or a leg
	 * split. With vias the engine's alternates search is unavailable
	 * (two-location queries only), so a via query returns one route. */
	vias?: [number, number][];
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
	street_names?: string[];
	/** Present and true on water-ferry maneuvers; absent on car-shuttle
	 * (rail ferry) maneuvers — the two kinds share the maneuver type. */
	ferry?: boolean;
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
/** Maneuver type kFerryEnter — boarding a water ferry or a car-shuttle
 * train. Its length is the on-board distance. The maneuver's `ferry`
 * flag tells the two apart: set for ships, absent for rail shuttles. */
const MANEUVER_FERRY_ENTER = 28;

function costingOptions(args: DirectRouteArgs): Record<string, unknown> {
	// A border crossing costs a couple of minutes on a bike or on foot —
	// slow down, maybe show ID — not the engine's default 10 (sized for
	// cars queueing at customs; it made an 11-minute descent to Paglino
	// read as 24).
	const COUNTRY_CROSSING_COST_SEC = 120;
	if (args.mode === 'bike') {
		// Hills price themselves through the fork's everyday grade→speed
		// curve (bicycle-costing-fork.md § Hills); use_hills only scales
		// the steep-discomfort penalty for pushing-territory grades, and
		// 0.1 keeps that near full strength until the hilliness
		// preference ships. Hybrid bike ≈ everyday utility cycling
		// (18 km/h base).
		return {
			bicycle: {
				bicycle_type: 'hybrid',
				use_hills: 0.1,
				country_crossing_cost: COUNTRY_CROSSING_COST_SEC
			}
		};
	}
	// Stairs are a normal part of walking: zero the engine's default 30 s
	// per-flight penalty (pedestrian-bicycle-routing.md § Pedestrian
	// costing). The later wheelchair / stroller mode avoids them via its
	// own costing options instead.
	//
	// Ferries: walking is slow and ships are fast, so at the neutral
	// default every slightly-viable ferry wins. use_ferry 0.17 prices a
	// crossing at ~4 h of boarding cost plus ~7x on board (cost only —
	// displayed times stay honest), so a ship wins only against a truly
	// disproportionate walk; the per-crossing walk-around variants still
	// appear when one does. use_rail_ferry 0 pins car-shuttle trains at
	// the engine's maximum avoidance — walking through a rail tunnel
	// aboard an Autoverlad is never a sensible pedestrian route. These
	// options ride ONLY on the walking tab's queries: MOTIS's transit
	// walk legs and the footpath matrix never see them, keeping the
	// pedestrian costing byte-identical for the transit stack.
	const pedestrian: Record<string, unknown> = {
		step_penalty: 0,
		use_ferry: 0.17,
		use_rail_ferry: 0,
		country_crossing_cost: COUNTRY_CROSSING_COST_SEC
	};
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

/** Replace the profile samples inside each crossing's span with a
 * straight line between its shores/portals. Sample i sits at distance
 * i × intervalM along the shape, so spans convert via cumulative
 * distance (equirectangular metres — ample at this precision). */
function flattenCrossings(
	profile: number[],
	coords: [number, number][],
	ranges: [number, number][],
	intervalM: number
) {
	const cum: number[] = [0];
	for (let i = 1; i < coords.length; i++) {
		const kLat = Math.cos((((coords[i - 1][1] + coords[i][1]) / 2) * Math.PI) / 180);
		const dx = (coords[i][0] - coords[i - 1][0]) * 111320 * kLat;
		const dy = (coords[i][1] - coords[i - 1][1]) * 111320;
		cum.push(cum[i - 1] + Math.hypot(dx, dy));
	}
	for (const [b, e] of ranges) {
		if (e >= cum.length) continue;
		const i0 = Math.max(1, Math.ceil(cum[b] / intervalM));
		const i1 = Math.min(profile.length - 2, Math.floor(cum[e] / intervalM));
		if (i1 <= i0) continue;
		const v0 = profile[i0 - 1];
		const v1 = profile[i1 + 1];
		for (let i = i0; i <= i1; i++) {
			profile[i] = v0 + ((v1 - v0) * (i - i0 + 1)) / (i1 - i0 + 2);
		}
	}
}

function tripToRoute(trip: ValhallaTrip, args: DirectRouteArgs): DirectRoute | null {
	const mode = args.mode;
	const legs = trip.legs ?? [];
	if (legs.length === 0) return null;
	// Two break locations → one leg; concat defensively anyway.
	const coords: [number, number][] = [];
	const elevation: number[] = [];
	let elevationComplete = true;
	let intervalM = ELEVATION_INTERVAL_M;
	let stairsM = 0;
	let ferryM = 0;
	let shuttleM = 0;
	const ferryCrossings: [number, number][] = [];
	// Crossing spans as global shape-index ranges — the elevation profile
	// is flattened across them (the DEM samples the massif ABOVE a
	// tunnel, so an on-board section would otherwise draw a mountain the
	// rider never climbs and inflate the ascent totals).
	const crossingRanges: [number, number][] = [];
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
			if (m.type === MANEUVER_FERRY_ENTER && typeof m.length === 'number') {
				if (m.ferry === true) {
					ferryM += m.length * 1000;
				} else {
					shuttleM += m.length * 1000;
				}
				if (typeof m.begin_shape_index === 'number' && typeof m.end_shape_index === 'number') {
					const b = legStart + m.begin_shape_index;
					const e = legStart + m.end_shape_index;
					const mid = coords[Math.floor((b + e) / 2)];
					if (mid) ferryCrossings.push(mid);
					crossingRanges.push([b, e]);
				}
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
	if (crossingRanges.length > 0 && elevation.length >= 2) {
		flattenCrossings(elevation, coords, crossingRanges, intervalM);
	}
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
		ferryM: Math.round(ferryM),
		shuttleM: Math.round(shuttleM),
		ferryCrossings,
		pushedRanges,
		requestedFrom: args.from,
		requestedTo: args.to,
		requestedVias: args.vias ?? []
	};
}

async function requestRoutes(
	args: DirectRouteArgs,
	excludePolygons: [number, number][][] | null,
	alternates: number,
	signal?: AbortSignal
): Promise<DirectRoute[]> {
	const costing = args.mode === 'bike' ? 'bicycle' : 'pedestrian';
	const options = costingOptions(args);
	const request = {
		costing,
		costing_options: options,
		...(excludePolygons ? { exclude_polygons: excludePolygons } : {}),
		locations: [
			{ lat: args.from[1], lon: args.from[0], type: 'break', search_filter: SNAP_FILTER },
			...(args.vias ?? []).map(([lon, lat]) => ({
				lat,
				lon,
				type: 'through',
				search_filter: SNAP_FILTER
			})),
			{ lat: args.to[1], lon: args.to[0], type: 'break', search_filter: SNAP_FILTER }
		],
		alternates,
		units: 'kilometers',
		elevation_interval: ELEVATION_INTERVAL_M,
		// Maneuvers only (no instruction text) — needed for the stairs /
		// ferry / pushed-section detection; keeps the response small.
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
		.map((t) => tripToRoute(t, args))
		.filter((r): r is DirectRoute => r !== null);
}

/** Avoid-this-crossing variants are judged by DISTANCE ratio against
 * the crossing route — deliberately not time: a mountain pass instead
 * of a car shuttle rides similar kilometres in many more hours and is
 * the sporting default, while circumnavigating a lake (or the whole
 * Lötschberg massif) multiplies the kilometres. Three bands:
 * ratio ≤ PROMOTE → the land route becomes the suggested route and the
 * crossing an alternative; ≤ SHOW → offered as an alternative; above →
 * not offered at all. */
const AVOID_FERRY_PROMOTE_RATIO = 1.25;
const AVOID_FERRY_SHOW_RATIO = 1.5;

/** ~100 m exclusion box around a crossing's midpoint — enough to sever
 * that ferry / shuttle line without touching nearby land routes. */
function crossingPolygon([lon, lat]: [number, number]): [number, number][] {
	const d = 0.001;
	return [
		[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d],
		[lon - d, lat + d], [lon - d, lat - d]
	];
}

/** One direct cycling / walking query: a single /route request with
 * `alternates`, so up to 3 routes come back at once (concept § Query &
 * alternatives). The primary route is always index 0. For each ferry /
 * car-shuttle crossing aboard the primary, one extra query avoids that
 * single crossing (the others stay available) and joins the cards when
 * it passes the distance rule — the crossing may be the honest optimum,
 * but the sporting choice belongs to the rider (bicycle-costing-fork.md
 * § Ferries). Failures of the extra queries are swallowed: the main
 * result stands on its own. */
export async function fetchDirectRoutes(
	args: DirectRouteArgs,
	signal?: AbortSignal
): Promise<DirectRoute[]> {
	// Alternates exist only for two-location queries — a via query
	// returns the single best route through the chain. The ferry /
	// car-shuttle avoidance variants below still apply.
	const alternates = (args.vias?.length ?? 0) > 0 ? 0 : NUM_ALTERNATES;
	const routes = await requestRoutes(args, null, alternates, signal);
	const primary = routes[0];
	if (!primary) return routes;
	let promoted: DirectRoute | null = null;
	for (const crossing of primary.ferryCrossings.slice(0, 3)) {
		try {
			const variant = (await requestRoutes(args, [crossingPolygon(crossing)], 0, signal))[0];
			if (variant === undefined) continue;
			const ratio = variant.distanceM / primary.distanceM;
			if (ratio > AVOID_FERRY_SHOW_RATIO) continue;
			const dup = routes.some(
				(r) =>
					Math.abs(r.distanceM - variant.distanceM) < 10 &&
					Math.abs(r.durationSec - variant.durationSec) < 10
			);
			if (dup) continue;
			if (ratio <= AVOID_FERRY_PROMOTE_RATIO && (!promoted || variant.distanceM < promoted.distanceM)) {
				if (promoted) routes.push(promoted);
				promoted = variant;
			} else {
				routes.push(variant);
			}
		} catch {
			// Avoid-this-crossing variants are a bonus — never fail the query.
		}
	}
	if (promoted) routes.unshift(promoted);
	return routes;
}
