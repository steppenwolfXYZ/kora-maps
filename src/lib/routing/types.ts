// Endpoint = one of three tagged variants (see transit-routing.md § Endpoint
// inputs). `station` and `point` carry the coord MOTIS needs; `current` is
// resolved to a coord at query time from the geolocation API.
//
// `station.mode` and `point.kind` are display-only hints used by the routing
// panel to pick a per-type icon on the selected endpoint pill. Optional
// because URL-restored endpoints won't always have them; the icon falls
// back to a generic transit/pin glyph in that case.
// `point.displayName` is the human-readable label attached by forward or
// reverse geocoding (geocoding-search.md § Display format). Absent when the
// point was set without a name available — the UI then falls back to raw
// coordinates.
export type Endpoint =
	| { type: 'station'; uic: string; name: string; coord: [number, number]; mode?: string }
	| { type: 'point'; coord: [number, number]; displayName?: string; kind?: 'address' | 'poi' }
	| { type: 'current' };

export type TimeMode = 'leave' | 'arrive';

export interface RoutingQuery {
	from: Endpoint;
	to: Endpoint;
	mode: TimeMode;
	/** ISO-8601 timestamp. `null` means "now". */
	time: string | null;
}

// MOTIS itinerary shape (subset of /api/v1/plan response). Only the fields
// the result-card renderer reads are typed — everything else stays as
// `unknown`.
export type LegMode =
	| 'WALK' | 'BIKE' | 'CAR'
	| 'TRANSIT'
	| 'TRAM' | 'SUBWAY' | 'RAIL' | 'BUS' | 'FERRY'
	| 'CABLE_CAR' | 'GONDOLA' | 'FUNICULAR'
	| 'AIRPLANE' | 'COACH'
	| 'HIGHSPEED_RAIL' | 'LONG_DISTANCE' | 'NIGHT_RAIL' | 'REGIONAL_RAIL'
	| 'REGIONAL_FAST_RAIL' | 'METRO';

export interface LegPlace {
	name?: string;
	lat?: number;
	lon?: number;
	/** MOTIS-prefixed platform stop id — e.g. "ch_8500010:0:6". */
	stopId?: string;
	/** MOTIS-prefixed parent station id — e.g. "ch_Parent8500010". */
	parentId?: string;
	/** Platform label (e.g. "6", "12A"). */
	track?: string;
}

export interface IntermediateStop extends LegPlace {
	arrival?: string;
	departure?: string;
}

export interface LegGeometry {
	/** Google-encoded polyline. */
	points: string;
	/** Precision — typically 5 or 6. */
	precision?: number;
	length?: number;
}

export interface Leg {
	mode: LegMode;
	startTime: string;
	endTime: string;
	/** Seconds. Absent on some MOTIS responses; fall back to endTime - startTime. */
	duration?: number;
	from?: LegPlace;
	to?: LegPlace;
	routeShortName?: string;
	routeColor?: string;
	/** MOTIS-prefixed GTFS route id — e.g. "ch_92-12-j26-1". */
	routeId?: string;
	/** GTFS extended route_type — 700/900/1000/… — same field the pipeline
	 * uses to bucket. */
	routeType?: number;
	tripHeadsign?: string;
	agencyId?: string;
	agencyName?: string;
	tripId?: string;
	headsign?: string;
	/** Google-encoded polyline of the leg's geometry. */
	legGeometry?: LegGeometry;
	/** Present on transit legs — stops served between `from` and `to`. */
	intermediateStops?: IntermediateStop[];
}

export interface Itinerary {
	startTime: string;
	endTime: string;
	/** Seconds. */
	duration: number;
	/** Seconds walking across all WALK legs. */
	walkTime?: number;
	transfers?: number;
	legs: Leg[];
}

export interface PlanResponse {
	itineraries: Itinerary[];
	direct?: Itinerary[];
	/** Opaque cursor for fetching later transit departures on the same query
	 * (leave-at mode). Pass back as `pageCursor` on the next /plan call. */
	nextPageCursor?: string;
	/** Same, but for arrive-by mode — earlier departures. */
	previousPageCursor?: string;
}
