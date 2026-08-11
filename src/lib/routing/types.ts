// Endpoint = one of three tagged variants (see transit-routing.md § Endpoint
// inputs). `station` and `point` carry the coord MOTIS needs; `current` is
// resolved to a coord at query time from the geolocation API.
export type Endpoint =
	| { type: 'station'; uic: string; name: string; coord: [number, number] }
	| { type: 'point'; coord: [number, number] }
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

export interface Leg {
	mode: LegMode;
	startTime: string;
	endTime: string;
	/** Seconds. Absent on some MOTIS responses; fall back to endTime - startTime. */
	duration?: number;
	from?: { name?: string; lat?: number; lon?: number };
	to?: { name?: string; lat?: number; lon?: number };
	routeShortName?: string;
	routeColor?: string;
	/** MOTIS-prefixed GTFS route id — e.g. "ch_92-12-j26-1". */
	routeId?: string;
	/** GTFS extended route_type — 700/900/1000/… — same field the pipeline
	 * uses to bucket. */
	routeType?: number;
	tripHeadsign?: string;
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
}
