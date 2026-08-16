// Shared geocoding constants (geocoding-search.md).
//
// The provider is Photon's public API in V1. All client requests go through
// the SvelteKit proxy in `src/routes/api/geocode/` — never directly to
// Photon — so a future provider swap (self-hosted Photon on a dedicated
// backend host) leaves the client contract untouched.

/** Upstream provider base URL. */
export const PHOTON_BASE = 'https://photon.komoot.io';

/** User-agent set on the outgoing proxy request, per Photon TOS. */
export const PHOTON_USER_AGENT = 'KoraMaps/0.1 (+https://koramaps.app)';

/** Forward-search bbox: CH+LI plus ~2 km buffer, matching the OSM pipeline's
 * `osm_bbox` (scripts/transit/config.yaml). Hard filter — no location bias.
 * Photon's bbox param format is `minLon,minLat,maxLon,maxLat`. */
export const SEARCH_BBOX = '5.93,45.80,10.52,47.83';

/** Autocomplete result count returned by the search endpoint. */
export const SEARCH_LIMIT = 8;

/** OSM tag filters passed to Photon to keep out features the routing panel
 * has better sources for or the user considers noise. Applied to both
 * forward search and reverse geocoding.
 *
 * Photon's `osm_tag` param syntax: `!key:value` excludes a specific tag,
 * `!key` excludes an entire OSM key. Multiple `osm_tag` params are ANDed. */
export const PHOTON_EXCLUDE_TAGS = [
	// Transit stops — covered by the app's local station index.
	'!railway:station',
	'!railway:halt',
	'!railway:tram_stop',
	'!railway:stop',
	'!railway:service_station',
	'!railway:subway_entrance',
	'!highway:bus_stop',
	'!amenity:bus_station',
	'!amenity:ferry_terminal',
	'!aerialway:station',
	'!public_transport:station',
	'!public_transport:stop_position',
	'!public_transport:platform',
	'!public_transport:stop_area',
	// Cities, cantons and administrative subdivisions — searching for a city
	// as a POI is either redundant (its main station is already in the index)
	// or non-actionable ("Basel-Landschaft" isn't a routing destination).
	// Villages / hamlets / suburbs stay in — they can be legitimate targets.
	'!place:city',
	'!place:state',
	'!place:region',
	'!place:county',
	'!place:district',
	'!boundary:administrative'
];
