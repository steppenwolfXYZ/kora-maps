import type { Endpoint, StationEndpoint } from './types';

// MOTIS place ids — shared by the browser (result cards match legs
// against vias, the /api/plan client formats its endpoints) and the
// server (the planning engine builds the MOTIS query). Pure: no env, no
// browser / node APIs.

// Station endpoints go to MOTIS as stop IDs ("ch_Parent<uic>"), not
// coordinates. The forked MOTIS serves WALK offsets for stop-ID
// endpoints straight from the imported Valhalla footpath matrix — zero
// Valhalla HTTP calls for that side of the query — and MOTIS still
// considers walking to nearby stations (the matrix rows include them).
// Side effect: no spurious first/last WALK leg from the station coord
// to its own platform, which the old stripStationWalks() workaround
// existed to trim.
/** MOTIS place id of a station endpoint. Also the id a via stop is sent
 * as (via-stops.md) and the id an itinerary's leg places carry as
 * `parentId`, so ranking / card code can match legs against vias. */
export function stationPlaceId(ep: StationEndpoint): string {
	// pid carries the feed's parent stop id (SLOID scheme); the legacy
	// Parent<uic> shape only exists in pre-migration timetables.
	return `ch_${ep.pid ?? `Parent${ep.uic}`}`;
}

/** MOTIS place string of any endpoint: station id, or "lat,lon" for a
 * point. A `current` endpoint uses the resolved geolocation coord
 * ([lon, lat]) the caller passes. */
export function formatPlace(ep: Endpoint, resolved: [number, number] | null): string {
	if (ep.type === 'station') return stationPlaceId(ep);
	if (ep.type === 'point') return `${ep.coord[1]},${ep.coord[0]}`;
	const r = resolved ?? [0, 0];
	return `${r[1]},${r[0]}`;
}

/** True for a place string in MOTIS's two accepted shapes: a prefixed
 * stop id or a "lat,lon" coordinate pair. Used by the /api/plan
 * endpoint to reject garbage before it reaches the engine. */
export function isValidPlace(s: string): boolean {
	if (/^ch_[A-Za-z0-9:_.-]{1,80}$/.test(s)) return true;
	const m = s.match(/^(-?\d{1,3}(?:\.\d+)?),(-?\d{1,3}(?:\.\d+)?)$/);
	if (!m) return false;
	const lat = Number(m[1]);
	const lon = Number(m[2]);
	return lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180;
}
