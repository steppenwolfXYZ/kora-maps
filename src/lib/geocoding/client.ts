// Photon geocoding client (geocoding-search.md). Talks to the SvelteKit proxy
// endpoints, never Photon directly. Normalises the returned features into a
// small `GeocodeResult` shape with a pre-formatted `displayName` string.

export interface GeocodeResult {
	/** [lon, lat] — matches the map convention used throughout Endpoint. */
	coord: [number, number];
	/** Ready-to-render label. Follows geocoding-search.md § Display format. */
	displayName: string;
	/** Coarse category — the UI uses it to pick an icon. */
	kind: 'poi' | 'address' | 'place';
	osmKey?: string;
	osmValue?: string;
}

interface PhotonFeature {
	geometry: { coordinates: [number, number]; type: string };
	properties: {
		name?: string;
		street?: string;
		housenumber?: string;
		city?: string;
		state?: string;
		county?: string;
		postcode?: string;
		osm_key?: string;
		osm_value?: string;
		osm_type?: string;
		osm_id?: number;
		type?: string;
		/** Photon convention: `[minLon, maxLat, maxLon, minLat]`. Only present
		 * for multi-point features (streets, buildings). Absent for POI nodes,
		 * house nodes, etc. */
		extent?: [number, number, number, number];
	};
}

interface PhotonResponse {
	features?: PhotonFeature[];
}

// OSM keys whose features are POIs — a `name` in one of these represents the
// POI itself, not an address. Reverse geocoding must strip these names per
// concept § Reverse geocoding.
const POI_KEYS = new Set([
	'amenity',
	'shop',
	'tourism',
	'leisure',
	'historic',
	'office',
	'craft',
	'healthcare',
	'sport',
	'man_made',
	'emergency',
	'aeroway',
	'aerialway',
	'railway' // stations, halts, tram_stops — stations should be found via the transit index
]);

function isPoi(f: PhotonFeature): boolean {
	const k = f.properties.osm_key;
	return !!k && POI_KEYS.has(k);
}

function cityOf(f: PhotonFeature): string {
	return f.properties.city || f.properties.county || f.properties.state || '';
}

/** Approximate metre distance between two lon/lat points — equirectangular,
 * accurate enough at the scales we care about (tens of metres). */
function distanceMeters(a: [number, number], b: [number, number]): number {
	const R = 6371000;
	const dLat = ((b[1] - a[1]) * Math.PI) / 180;
	const dLon = ((b[0] - a[0]) * Math.PI) / 180;
	const lat0 = (((a[1] + b[1]) / 2) * Math.PI) / 180;
	const x = dLon * Math.cos(lat0);
	return Math.sqrt(dLat * dLat + x * x) * R;
}

/** Whether the query coord is close enough to `f` that the feature can be
 * called the address of the query point.
 *
 *   - Within `TIGHT_M` metres of the centroid → definitely on-feature (works
 *     for POI nodes, house nodes, short streets).
 *   - OR extent bbox contains the query AND centroid ≤ `GENEROUS_M` metres.
 *     The centroid cap defends against long roads / valleys whose bbox spans
 *     kilometres — a mountain-pass road's bbox happily contains points 500m
 *     off the actual road.
 *   - Otherwise → not on this feature; caller falls through to the "Nähe"
 *     branch. */
function isAtQueryPoint(f: PhotonFeature, query: [number, number]): boolean {
	const TIGHT_M = 60;
	const GENEROUS_M = 200;
	const centroidDist = distanceMeters(f.geometry.coordinates, query);
	if (centroidDist <= TIGHT_M) return true;
	const ext = f.properties.extent;
	if (!ext) return false;
	const [minLon, maxLat, maxLon, minLat] = ext;
	const inBbox =
		query[0] >= minLon && query[0] <= maxLon &&
		query[1] >= minLat && query[1] <= maxLat;
	return inBbox && centroidDist <= GENEROUS_M;
}

function joinWithCity(label: string, city: string): string {
	if (!label) return city;
	if (!city || city === label) return label;
	return `${label}, ${city}`;
}

/** Forward-search classification for display formatting. */
function formatForward(f: PhotonFeature): GeocodeResult | null {
	const p = f.properties;
	const coord: [number, number] = [f.geometry.coordinates[0], f.geometry.coordinates[1]];
	const city = cityOf(f);

	// POI — "[POI name], [city]"
	if (isPoi(f) && p.name) {
		return {
			coord,
			displayName: joinWithCity(p.name, city),
			kind: 'poi',
			osmKey: p.osm_key,
			osmValue: p.osm_value
		};
	}

	// Address with street — "[street] [num?], [city]"
	if (p.street) {
		const streetPart = p.housenumber ? `${p.street} ${p.housenumber}` : p.street;
		return {
			coord,
			displayName: joinWithCity(streetPart, city),
			kind: 'address',
			osmKey: p.osm_key,
			osmValue: p.osm_value
		};
	}

	// Highway feature — the road IS the label
	if (p.osm_key === 'highway' && p.name) {
		return {
			coord,
			displayName: joinWithCity(p.name, city),
			kind: 'address',
			osmKey: p.osm_key,
			osmValue: p.osm_value
		};
	}

	// Named place (suburb, town, hamlet, …)
	if (p.name) {
		return {
			coord,
			displayName: joinWithCity(p.name, city),
			kind: 'place',
			osmKey: p.osm_key,
			osmValue: p.osm_value
		};
	}

	// Unnamed and streetless — no useful label
	return null;
}

/** Forward search. Empty / short queries short-circuit to []. Failures are
 * swallowed and return [] — the search box is best-effort UX, not critical
 * path. */
export async function searchPlaces(
	query: string,
	signal?: AbortSignal
): Promise<GeocodeResult[]> {
	const q = query.trim();
	if (q.length < 2) return [];
	const url = `/api/geocode/search?q=${encodeURIComponent(q)}`;
	let data: PhotonResponse;
	try {
		const res = await fetch(url, { signal });
		if (!res.ok) return [];
		data = (await res.json()) as PhotonResponse;
	} catch {
		return [];
	}
	// Photon frequently returns visual duplicates: same POI mapped as both a
	// node and a way, or two adjacent OSM buildings sharing name + coord.
	// They render as identical rows to the user, so keep only the first per
	// unique displayName.
	const out: GeocodeResult[] = [];
	const seen = new Set<string>();
	for (const f of data.features ?? []) {
		const r = formatForward(f);
		if (!r) continue;
		if (seen.has(r.displayName)) continue;
		seen.add(r.displayName);
		out.push(r);
	}
	return out;
}

/** Reverse geocoding: coord → address string, never a POI name.
 *
 *   - If the closest addressable feature has a street, format the street +
 *     housenumber (if any) + city — no "Nähe" prefix.
 *   - If it's an unnumbered street (highway=* with name), use that + city.
 *   - If none of the nearby features is addressable but at least one has a
 *     name, fall back to "Nähe [name], [city]" without housenumber.
 *   - If nothing usable at all, return null (caller shows raw coords).
 */
export async function reverseAddress(
	lon: number,
	lat: number,
	signal?: AbortSignal
): Promise<string | null> {
	const url = `/api/geocode/reverse?lon=${lon}&lat=${lat}`;
	let data: PhotonResponse;
	try {
		const res = await fetch(url, { signal });
		if (!res.ok) return null;
		data = (await res.json()) as PhotonResponse;
	} catch {
		return null;
	}
	const feats = data.features ?? [];
	if (!feats.length) return null;

	const query: [number, number] = [lon, lat];

	// Prefer any feature that (a) carries actual address content AND (b) sits
	// at the query point (extent contains it, or centroid within threshold).
	// The at-query check is what prevents "Nähe X" being falsely dropped when
	// the closest returned road is 500 m away in a lake.
	for (const f of feats) {
		const p = f.properties;
		if (!isAtQueryPoint(f, query)) continue;
		const city = cityOf(f);
		if (p.street) {
			// POI-on-street or house-on-street — drop any POI name; keep street context.
			const streetPart = p.housenumber ? `${p.street} ${p.housenumber}` : p.street;
			return joinWithCity(streetPart, city);
		}
		if (p.osm_key === 'highway' && p.name) {
			return joinWithCity(p.name, city);
		}
	}

	// No address candidate at the query point — fall back to nearest named
	// feature with "Nähe" prefix. Concept: no house number in the fallback.
	// Prefix is in the app's UI language (currently German).
	for (const f of feats) {
		const p = f.properties;
		const label = p.name || p.street || '';
		if (!label) continue;
		const city = cityOf(f);
		return `Nähe ${joinWithCity(label, city)}`;
	}

	return null;
}
