import type { Endpoint, RoutingQuery, TimeMode } from './types';

// URL query params (transit-routing.md § Deep link, geocoding-search.md § URL persistence):
//   from, to        — endpoint tokens (uic | lat,lng | 'me')
//   fromName, toName — display label for a coord endpoint (address / POI /
//                     reverse-fallback string). Only carried when the paired
//                     from/to is a coord AND a display name is available.
//   fromKind, toKind — 'address' | 'poi', display hint for the endpoint
//                     pill's icon. Only carried when the paired from/to is
//                     a coord AND a kind is known.
//   mode            — 'leave' | 'arrive'
//   time            — ISO 8601 or 'now'
// A `?from=…&to=…` presence is enough to open the routing panel on cold load.

export const URL_FROM = 'from';
export const URL_TO = 'to';
export const URL_FROM_NAME = 'fromName';
export const URL_TO_NAME = 'toName';
export const URL_FROM_KIND = 'fromKind';
export const URL_TO_KIND = 'toKind';
export const URL_MODE = 'mode';
export const URL_TIME = 'time';
/** Selected itinerary fingerprint (route-display.md § Lifecycle).
 * Independent of the panel query params — presence means one specific
 * itinerary from the current results is being rendered on the map. */
export const URL_ROUTE = 'route';

/** Endpoint serialisation: coord as `lat,lng` (7 fractional digits, ≈1 cm).
 * `station` needs the lookup callback so a UIC round-trips through the
 * search index at parse time. `point` and `current` need no lookup. */
export function endpointToParam(ep: Endpoint): string {
	if (ep.type === 'station') return ep.uic;
	if (ep.type === 'point') return `${ep.coord[1].toFixed(7)},${ep.coord[0].toFixed(7)}`;
	return 'me';
}

export interface StationLookup {
	/** Return the station data for a UIC, or null if unknown. `coord` is
	 *  the station entry's `c` (GTFS centroid) — display/fly-to only;
	 *  routing sends station endpoints to MOTIS as stop IDs, not coords
	 *  (client.ts formatPlace). `mode` is the station's highest-ranked
	 *  mode (`train`, `tram`, …) passed through so the endpoint pill can
	 *  render a mode-specific icon. */
	(uic: string): { name: string; coord: [number, number]; mode?: string; pid?: string } | null;
}

/** Parse a from/to token back into an Endpoint. Unknown UIC → null (caller
 * treats as no endpoint on this side). `lookup` may be omitted if callers
 * only need to detect presence (see readQueryPresence). `displayName` /
 * `kind` are attached only when the parsed endpoint is a `point`. */
export function paramToEndpoint(
	raw: string,
	lookup?: StationLookup,
	displayName?: string | null,
	kind?: string | null
): Endpoint | null {
	if (!raw) return null;
	if (raw === 'me') return { type: 'current' };
	// lat,lng — two floats, possibly negative.
	const m = raw.match(/^(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$/);
	if (m) {
		const lat = Number(m[1]);
		const lng = Number(m[2]);
		if (Number.isFinite(lat) && Number.isFinite(lng)) {
			const ep: Endpoint = { type: 'point', coord: [lng, lat] };
			if (displayName) ep.displayName = displayName;
			if (kind === 'address' || kind === 'poi') ep.kind = kind;
			return ep;
		}
		return null;
	}
	// Otherwise treat as UIC. Without a lookup we cannot fully hydrate the
	// station (no name / coord) — caller retries once the index has loaded.
	if (lookup) {
		const hit = lookup(raw);
		if (!hit) return null;
		const ep: Endpoint = { type: 'station', uic: raw, name: hit.name, coord: hit.coord };
		if (hit.mode) ep.mode = hit.mode;
		if (hit.pid) ep.pid = hit.pid;
		return ep;
	}
	return { type: 'station', uic: raw, name: '', coord: [0, 0] };
}

export function timeToParam(time: string | null): string {
	return time ?? 'now';
}

export function paramToTime(raw: string | null): string | null {
	if (!raw || raw === 'now') return null;
	return raw;
}

export function modeToParam(mode: TimeMode): string {
	return mode;
}

export function paramToMode(raw: string | null): TimeMode {
	return raw === 'arrive' ? 'arrive' : 'leave';
}

/** True when a routing query is present in the current URL — used on cold
 * load to decide whether to open the panel. */
export function urlHasRoutingQuery(url: URL): boolean {
	return url.searchParams.has(URL_FROM) || url.searchParams.has(URL_TO);
}

export function readRoutingQuery(url: URL, lookup?: StationLookup): {
	from: Endpoint | null;
	to: Endpoint | null;
	mode: TimeMode;
	time: string | null;
	route: string | null;
} {
	return {
		from: paramToEndpoint(
			url.searchParams.get(URL_FROM) ?? '',
			lookup,
			url.searchParams.get(URL_FROM_NAME),
			url.searchParams.get(URL_FROM_KIND)
		),
		to: paramToEndpoint(
			url.searchParams.get(URL_TO) ?? '',
			lookup,
			url.searchParams.get(URL_TO_NAME),
			url.searchParams.get(URL_TO_KIND)
		),
		mode: paramToMode(url.searchParams.get(URL_MODE)),
		time: paramToTime(url.searchParams.get(URL_TIME)),
		route: url.searchParams.get(URL_ROUTE)
	};
}

function pointName(ep: Endpoint | null): string | null {
	if (!ep || ep.type !== 'point') return null;
	return ep.displayName ?? null;
}

function pointKind(ep: Endpoint | null): string | null {
	if (!ep || ep.type !== 'point') return null;
	return ep.kind ?? null;
}

/** Write from/to/mode/time onto a URL — pass a URL to `writeRoutingQuery` so
 * callers can preserve other params (line=…, position hash) already there. */
export function writeRoutingQuery(url: URL, q: {
	from: Endpoint | null;
	to: Endpoint | null;
	mode: TimeMode;
	time: string | null;
	route?: string | null;
}) {
	if (q.from) url.searchParams.set(URL_FROM, endpointToParam(q.from));
	else url.searchParams.delete(URL_FROM);
	if (q.to) url.searchParams.set(URL_TO, endpointToParam(q.to));
	else url.searchParams.delete(URL_TO);
	const fromName = pointName(q.from);
	if (fromName) url.searchParams.set(URL_FROM_NAME, fromName);
	else url.searchParams.delete(URL_FROM_NAME);
	const toName = pointName(q.to);
	if (toName) url.searchParams.set(URL_TO_NAME, toName);
	else url.searchParams.delete(URL_TO_NAME);
	const fromKind = pointKind(q.from);
	if (fromKind) url.searchParams.set(URL_FROM_KIND, fromKind);
	else url.searchParams.delete(URL_FROM_KIND);
	const toKind = pointKind(q.to);
	if (toKind) url.searchParams.set(URL_TO_KIND, toKind);
	else url.searchParams.delete(URL_TO_KIND);
	// mode/time only carried when non-default. `leave` + null time = empty.
	if (q.mode === 'arrive') url.searchParams.set(URL_MODE, 'arrive');
	else url.searchParams.delete(URL_MODE);
	if (q.time) url.searchParams.set(URL_TIME, q.time);
	else url.searchParams.delete(URL_TIME);
	if (q.route) url.searchParams.set(URL_ROUTE, q.route);
	else url.searchParams.delete(URL_ROUTE);
}

export function clearRoutingQuery(url: URL) {
	url.searchParams.delete(URL_FROM);
	url.searchParams.delete(URL_TO);
	url.searchParams.delete(URL_FROM_NAME);
	url.searchParams.delete(URL_TO_NAME);
	url.searchParams.delete(URL_FROM_KIND);
	url.searchParams.delete(URL_TO_KIND);
	url.searchParams.delete(URL_MODE);
	url.searchParams.delete(URL_TIME);
	url.searchParams.delete(URL_ROUTE);
}
