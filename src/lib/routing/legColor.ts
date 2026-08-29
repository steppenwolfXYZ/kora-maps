// Per-leg badge color for the result cards. Primary path: pipeline-baked
// `route_color_index.json` (route_id → drawn color) so a leg's badge
// matches exactly what the map renders. Fallback: fixed per-bucket mid-tone
// from the MapMenu legend when the leg's route_id isn't in the index (or
// the index isn't published yet).

const INDEX_URL = '/map-assets/route_color_index.json';

let promise: Promise<Map<string, string> | null> | null = null;
export function loadRouteColorIndex(): Promise<Map<string, string> | null> {
	if (!promise) {
		promise = fetch(INDEX_URL)
			.then((r) => (r.ok ? r.json() as Promise<Record<string, string>> : Promise.reject(new Error(`HTTP ${r.status}`))))
			.then((obj) => new Map(Object.entries(obj)))
			.catch(() => null);
	}
	return promise;
}

// MapMenu legend mid-tones (mode.color, per bucket). Kept parallel to
// MODES in MapMenu.svelte — if the legend colors change there, mirror the
// change here.
const BUCKET_COLOR: Record<string, string> = {
	train:        '#c94040',
	metro:        '#40c940',
	tram:         '#40c9c9',
	bus:          '#406dc9',
	regional_bus: '#fc9247',
	ferry:        '#406dc9',
	mountain:     '#b440cb'
};
const NEUTRAL = '#888888';

/** Mid-tone for a pipeline bucket/mode name ("train", "regional_bus", …).
 * Used where only the station's highest-ranked mode is known, no route_id
 * (e.g. the Connect board tiles). */
export function modeMidColor(mode: string | undefined): string | null {
	return (mode && BUCKET_COLOR[mode]) || null;
}

/** GTFS extended route_type → bucket, mirroring the pipeline's
 * `gtfs_type_to_bucket`. Regional-vs-city bus disambiguation needs
 * pipeline state (line index), so 700/702 default to city `bus`; if the
 * route_id is found in the index we take its color instead. */
function bucketFromRouteType(rt: number | undefined): string | null {
	if (rt === undefined) return null;
	if ([100, 101, 102, 103, 105, 106, 107, 109].includes(rt)) return 'train';
	if ([116, 1300, 1303, 1400].includes(rt)) return 'mountain';
	if (rt === 401) return 'metro';
	if (rt === 700 || rt === 702 || rt === 800) return 'bus';
	if (rt === 900) return 'tram';
	if (rt === 1000) return 'ferry';
	return null;
}

/** MOTIS mode → bucket, used when `routeType` is absent. */
function bucketFromMode(mode: string): string | null {
	switch (mode) {
		case 'TRAM': return 'tram';
		case 'SUBWAY': case 'METRO': return 'metro';
		case 'RAIL': case 'HIGHSPEED_RAIL': case 'LONG_DISTANCE':
		case 'NIGHT_RAIL': case 'REGIONAL_RAIL': case 'REGIONAL_FAST_RAIL':
			return 'train';
		case 'BUS': case 'COACH': return 'bus';
		case 'FERRY': return 'ferry';
		case 'CABLE_CAR': case 'GONDOLA': case 'FUNICULAR': return 'mountain';
		default: return null;
	}
}

/** Strip the MOTIS dataset prefix ("ch_") so the id matches the pipeline's
 * raw GTFS route_id. Any single "<letters>_" prefix is stripped. */
function stripDatasetPrefix(routeId: string): string {
	return routeId.replace(/^[a-z]+_/, '');
}

/** Resolve the badge color for one leg: index hit → drawn color, else
 * bucket mid-tone, else neutral gray. */
export function legBadgeColor(
	index: Map<string, string> | null,
	leg: { routeId?: string; routeType?: number; mode: string }
): string {
	if (index && leg.routeId) {
		const hit = index.get(leg.routeId) ?? index.get(stripDatasetPrefix(leg.routeId));
		if (hit) return hit;
	}
	const bucket = bucketFromRouteType(leg.routeType) ?? bucketFromMode(leg.mode);
	return (bucket && BUCKET_COLOR[bucket]) || NEUTRAL;
}
