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
	| { type: 'station'; uic: string; name: string; coord: [number, number]; mode?: string; pid?: string }
	| { type: 'point'; coord: [number, number]; displayName?: string; kind?: 'address' | 'poi' }
	| { type: 'current' };

export type TimeMode = 'leave' | 'arrive';

/** Travel mode of the routing panel (pedestrian-bicycle-routing.md § Mode
 * tabs): the transit connection search (MOTIS) or a direct cycling /
 * walking route (Valhalla). `bike` / `walk` are also the URL `mode`
 * param values; absent means transit. */
export type TravelMode = 'transit' | 'bike' | 'walk';

/** One direct cycling / walking route returned by Valhalla — either the
 * primary route or an alternate (pedestrian-bicycle-routing.md § Query &
 * alternatives). Elevation-derived fields are null when the Valhalla
 * instance served no elevation for the route — the cards then simply
 * omit ascent / descent and the profile graph. */
export interface DirectRoute {
	mode: 'bike' | 'walk';
	/** Seconds. */
	durationSec: number;
	/** Metres. */
	distanceM: number;
	/** Decoded route shape, [lon, lat] pairs. */
	coords: [number, number][];
	/** [minLon, minLat, maxLon, maxLat]. */
	bbox: [number, number, number, number];
	/** Metres climbed / descended, jitter-filtered from the elevation
	 * profile. Null when no elevation came back. */
	ascentM: number | null;
	descentM: number | null;
	/** Elevation samples (metres a.s.l.) at `profileIntervalM` spacing
	 * along the route — the selected card's profile graph. */
	profile: number[] | null;
	profileIntervalM: number;
	/** Metres of stairs on the route (bike: sections to carry / push the
	 * bike over; summed from the steps maneuvers). 0 when none. */
	stairsM: number;
	/** Bike only: [start, end] index ranges into `coords` where the bike
	 * is pushed (walkable-but-not-ridable sections — the fork reports
	 * them as pedestrian-mode maneuvers, bicycle-costing-fork.md). The
	 * map draws these ranges dotted. Empty for walk routes and rides
	 * without pushed sections. */
	pushedRanges: [number, number][];
}

/** The `station` variant of Endpoint, pulled out because via stops can
 * only ever be stations (via-stops.md § Via stops — the routing engine
 * accepts stop ids for vias, never coordinates). */
export type StationEndpoint = Extract<Endpoint, { type: 'station' }>;

/** One via stop of the route (via-stops.md). `station` is null while the
 * row exists in the panel but has not been filled yet — such a row is
 * ignored by the query and never serialised. `wait` is the REQUESTED
 * minimum stay in whole minutes; 0 means "pass through" (the traveller
 * may stay on board and no vehicle change is forced). */
export interface Via {
	station: StationEndpoint | null;
	wait: number;
}

/** A via row whose station is set — the shape everything downstream of the
 * panel (query, URL, ranking, cards) works with. */
export type FilledVia = Via & { station: StationEndpoint };

/** Engine ceiling: nigiri's `kMaxVias`, raised from upstream's 2 to 3 in
 * the Kora MOTIS fork (motis/fork/deps/nigiri/.../limits.h). Keep the two
 * in step — a query with more vias than the engine's constant is rejected
 * outright. */
export const MAX_VIAS = 3;

/** Wait-control presets, in minutes. 0 = pass through. */
export const VIA_WAIT_PRESETS = [0, 5, 10, 15, 30, 45, 60, 90, 120];

/** Ceiling on a custom wait. Low enough that two maxed-out vias still fit
 * comfortably inside the total-travel-time ceiling the query raises by the
 * requested dwell sum (via-stops.md § Constraints). */
export const MAX_VIA_WAIT_MIN = 480;

/** The vias a query actually carries: filled rows only, capped at the
 * engine ceiling. */
export function activeVias(vias: Via[]): FilledVia[] {
	return vias.filter((v): v is FilledVia => v.station !== null).slice(0, MAX_VIAS);
}

/** Sum of the REQUESTED via waits in seconds — the `plannedDwell` of
 * via-stops.md § Planned dwell and time judgement. */
export function plannedDwellSec(vias: Via[]): number {
	return activeVias(vias).reduce((s, v) => s + v.wait, 0) * 60;
}

export interface RoutingQuery {
	from: Endpoint;
	to: Endpoint;
	/** Ordered via stops between from and to (via-stops.md). */
	vias?: Via[];
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
	/** Metres. Present on non-transit legs (WALK/BIKE/CAR) from MOTIS. */
	distance?: number;
	/** Metres climbed / descended along the leg. Kora-fork fields, set on
	 * WALK legs only (Valhalla elevation profile, noise-filtered — see
	 * motis/fork/README.md). Absent when the router had no elevation. */
	elevationUp?: number;
	elevationDown?: number;
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
