import { pushState, replaceState } from '$app/navigation';
import { browser } from '$app/environment';
import { page } from '$app/state';
import { plan, PlanRequestError, type Extension } from './client';
import { DirectRouteError, fetchDirectRoutes } from './valhalla';
import { itineraryFingerprint } from './fingerprint';
import {
	geolocationDenied, geolocationErrorMessage, hasGeolocation,
	invalidateCurrent, resolveCurrent
} from './geolocation.svelte';
import { routingOptions, type RoutingOptionValues } from './options.svelte';
import { stationPlaceId } from './place';
import { connectStations } from './connect.svelte';
import { recentRoutes } from './recents.svelte';
import { reportShareExpired, type ShareData } from './share';
import { reverseAddress } from '$lib/geocoding/client';
import {
	activeVias, MAX_VIAS, MAX_VIA_WAIT_MIN, plannedDwellSec,
	type DirectRoute, type Endpoint, type FilledVia, type Itinerary,
	type StationEndpoint, type TimeMode, type TravelMode, type Via
} from './types';
import { endpointToParam, writeRoutingQuery } from './url';

// Reactive routing state (Svelte 5 runes). One instance shared across the
// app — Map.svelte and RoutingPanel read from it, entry-point handlers
// mutate it. See transit-routing.md § Routing panel / § Entry points.

let panelOpen = $state(false);
// Which endpoint input the panel should focus right after opening —
// consumed once by RoutingPanel on mount. Plain (non-reactive) on purpose.
let focusRequest: 'from' | 'to' | null = null;
let from = $state<Endpoint | null>(null);
let to = $state<Endpoint | null>(null);
// Ordered via stops between From and To (via-stops.md). Rows with a null
// station exist in the panel but are invisible to the query, the URL and
// every judgement — filling one is what makes it real.
let vias = $state<Via[]>([]);
let mode = $state<TimeMode>('leave');
let time = $state<string | null>(null);

// Travel mode of the panel (pedestrian-bicycle-routing.md § Mode tabs):
// transit (MOTIS connection search) / bike / walk (direct Valhalla
// routes). The last user choice persists across visits; a deep link's
// mode overrides it for that visit only (hydrate → session-only).
const TRAVEL_MODE_KEY = 'kora.routing.travelMode';
function readStoredTravelMode(): TravelMode {
	if (!browser) return 'transit';
	try {
		const v = localStorage.getItem(TRAVEL_MODE_KEY);
		return v === 'bike' || v === 'walk' ? v : 'transit';
	} catch {
		return 'transit';
	}
}
let travelMode = $state<TravelMode>(readStoredTravelMode());

// Direct cycling / walking results (pedestrian-bicycle-routing.md
// § Query & alternatives): all alternatives of the latest query, and the
// index of the selected one (drawn in full color; the others muted).
// $state.raw — routes are plain immutable data, replaced wholesale.
let directRoutes = $state.raw<DirectRoute[]>([]);
let directSelected = $state(0);
// Bumped on every `setTime` call so consumers re-run even when `time`
// itself is unchanged (refresh-to-now while already at null — the wall
// clock has moved but the value hasn't).
let timeVersion = $state(0);

let results = $state<Itinerary[]>([]);
let loading = $state(false);
// Non-null while a loadMoreEarlier / loadMoreLater is in flight; the
// direction lets the panel disable / label the matching button.
let loadingMore = $state<'earlier' | 'later' | null>(null);
let error = $state<string | null>(null);
let hasQueried = $state(false);

// route-display.md § Lifecycle. When one of the current `results`
// itineraries has been selected for map rendering, it lives here.
// `selectedFingerprint` mirrors `itineraryFingerprint(selectedItinerary)`
// and is what the URL carries as `?route=…` — pulled out so a pending
// fingerprint from a cold-load restore can wait for `runQuery` to return.
let selectedItinerary = $state.raw<Itinerary | null>(null);
let selectedFingerprint = $state<string | null>(null);
let pendingFingerprint: string | null = null;
let selectionInvalid = $state(false);

// routing-map-details-split.md: expansion (details open in the list) and
// selection (rendered on the map) are independent per-connection states.
// Expansion is an accordion — at most one card open — keyed by the same
// fingerprint so the map-mode header's details button can reopen the card
// back in the list. Not serialised; not restored on cold load.
let expandedFingerprint = $state<string | null>(null);
// Mobile fullscreen map mode: list/panel hidden, route + summary header
// own the viewport. Entered only via a card's map icon on narrow screens,
// left via the header's back / details buttons or by the selection
// clearing (browser back, ×, input change).
let mapModeFlag = $state(false);
// Direct-mode bottom sheet on narrow screens: with cycling / walking
// results the map is the primary content, so the panel docks at the
// bottom as a compact sheet. `true` = the user expanded it back to the
// full panel to edit the query; collapses again on every fresh query.
// Only meaningful while the direct tab has queried — CSS scopes the
// sheet layout to narrow viewports.
let directSheetExpanded = $state(false);

// Shared-connection view (connection-sharing.md § Shared view). `sharedShare`
// holds the share document while a /s/<id> landing drives the panel;
// `sharedOnly` filters the visible list down to the one shared connection
// (earlier/later exit it); `sharedExpired` shows the gone-error after the
// re-query found no share-fingerprint match. `pendingShareFingerprint` is
// the shared analogue of `pendingFingerprint`, resolved against the raw
// (unpruned) cascade results because share matching must never be defeated
// by the dominance pruning of the display list.
let sharedShare = $state.raw<ShareData | null>(null);
let sharedOnly = $state(false);
let sharedExpired = $state(false);
let pendingShareFingerprint: string | null = null;

let pendingAbort: AbortController | null = null;

// Dedup guard for runQuery — set on successful completion, cleared whenever
// query inputs change or the panel closes. Prevents the RoutingPanel $effect
// from re-running the cascade when the panel simply remounts (e.g. mobile
// map-mode toggle) with unchanged inputs.
let lastQueryKey: string | null = null;

// Whether the current history entry was pushed by `selectItinerary` for
// the active selection. Only then does `dismissSelectedItinerary` consume
// it via history.back() — an auto-selected or URL-restored selection
// lives on an entry it never pushed, so × must clear in place instead
// (back() on a single-entry history is a silent no-op and the selection
// would survive). Replace-stamping doesn't change the flag: a replaced
// entry is still the pushed one.
let pushedEntry = false;

// The search cascade (narrow / wide walking budgets, time-advance hops,
// dominance pruning) runs on the server — one GET /api/plan per user
// action returns the final list (server-side-transit-planning.md). What
// the client keeps is the request's history: the earlier / later clicks
// on the current query, replayed by the server on every extension so the
// endpoint stays stateless.
let extensions: Extension[] = [];
let resolvedCurrentCoord: [number, number] | null = null;

function abortInFlight() {
	if (!pendingAbort) return;
	pendingAbort.abort();
	pendingAbort = null;
}

/** The vias a query actually carries (filled rows, capped at the engine
 * ceiling) and the derived shapes ranking / cards need. */
function queryVias(): FilledVia[] {
	return activeVias(vias);
}

/** via-stops.md § Planned dwell: parent-stop id → requested wait in
 * seconds, so ranking can tell deliberate stop-time from dead time.
 * `null` when no via asks for a wait — nothing downstream has to branch.
 * The station guard is type-level: waits exist on the transit tab only,
 * where vias are always stations. */
function viaWaitByStop(): Map<string, number> | null {
	const withWait = queryVias().filter(
		(v): v is FilledVia & { station: StationEndpoint } =>
			v.wait > 0 && v.station.type === 'station'
	);
	if (withWait.length === 0) return null;
	return new Map(withWait.map((v) => [stationPlaceId(v.station), v.wait * 60]));
}

/** comfort-walk-baseline.md: the query's unavoidable walking (seconds),
 * summed from the fork's per-endpoint minima. A property of the query,
 * reported by the server with every result list. Reset with the cascade
 * state. */
let walkBaselineSec = $state(0);

/** Ranking knobs shared by publishResults and the panel's card states. */
export function rankOptionsFor(): {
	minimizeWalking: boolean; plannedDwellSec: number;
	viaWaitByStop: Map<string, number> | null; walkBaselineSec: number;
} {
	return {
		minimizeWalking: routingOptions.minimizeWalking,
		plannedDwellSec: plannedDwellSec(vias),
		viaWaitByStop: viaWaitByStop(),
		walkBaselineSec
	};
}

/** Signature of everything about the vias the query can see — used to
 * decide whether a via edit actually invalidates the shown results.
 * Adding or dropping an EMPTY row changes nothing and must not wipe the
 * result list. endpointToParam covers both via kinds (station → UIC,
 * point → coord token). */
function viaSignature(): string {
	return queryVias().map((v) => `${endpointToParam(v.station)}:${v.wait}`).join(',');
}

/** Shared tail of every via edit: only an edit the QUERY can see drops the
 * shown results — adding or removing an empty row must not. */
function commitViaEdit(before: string) {
	if (viaSignature() === before) {
		syncUrl();
		return;
	}
	abortInFlight();
	results = [];
	directRoutes = [];
	directSelected = 0;
	hasQueried = false;
	error = null;
	lastQueryKey = null;
	invalidateSelection();
	syncUrl();
}

function resetCascadeState() {
	extensions = [];
	walkBaselineSec = 0;
	resolvedCurrentCoord = null;
}

// Recents never store a live "current location" endpoint — it can't
// reproduce the shown result later. The resolved query coordinate is
// recorded as a point endpoint instead, reverse-geocoded to an address
// like the map right-click (nameless coord fallback). See
// routing-persistence.md § Recent routes list.
const RECENT_REVERSE_TIMEOUT_MS = 2000;

async function materializeCurrent(
	ep: Endpoint, coord: [number, number] | null
): Promise<Endpoint | null> {
	if (ep.type !== 'current') return ep;
	if (!coord) return null;
	const ac = new AbortController();
	const timer = setTimeout(() => ac.abort(), RECENT_REVERSE_TIMEOUT_MS);
	let name: string | null = null;
	try {
		name = await reverseAddress(coord[0], coord[1], ac.signal);
	} catch {
		// Geocoder down / timed out — record with raw coords.
	} finally {
		clearTimeout(timer);
	}
	return { type: 'point', coord, displayName: name ?? undefined, kind: 'address' };
}

async function recordRecentRoute(
	from: Endpoint, to: Endpoint, viaList: FilledVia[],
	mode: TimeMode, time: string | null
) {
	// Snapshot before the awaits — a follow-up query may reset it.
	const coord = resolvedCurrentCoord;
	const [f, t] = await Promise.all([
		materializeCurrent(from, coord),
		materializeCurrent(to, coord)
	]);
	// A current endpoint without a resolved coord can't be reproduced —
	// skip the entry rather than store a dead one.
	if (f && t) {
		recentRoutes.record(f, t, viaList, mode, time);
		// Connect tiles ride on the same materialized endpoints, so a route
		// run from the current location tiles as the resolved address
		// rather than being dropped.
		connectStations.record(f);
		connectStations.record(t);
	}
}


/** Map a failed plan request to a short user-facing message. The server
 * reports only a failure class (server-side-transit-planning.md); the raw
 * error goes to the console. */
function userFacingError(e: unknown): string {
	console.error('[routing] query failed:', e);
	if (e instanceof PlanRequestError) {
		// The engine rejected the query itself — almost always an endpoint
		// the current timetable doesn't know (e.g. a stale stop id in a
		// bookmarked URL).
		if (e.kind === 'rejected' || e.status === 400)
			return 'Sorry — an error on our side prevented finding the locations for this route.';
		return 'Sorry — the route search is temporarily unavailable on our side. Please try again later.';
	}
	if (e instanceof TypeError) return 'Could not reach the route search. Please check your connection.';
	return 'Sorry — the route search failed due to an error on our side. Please try again.';
}

function currentUrl(): URL {
	return new URL(window.location.href);
}

// The concrete timestamp substituted for a null `time` ("now") in the
// URL. Stamped by runQuery, so the address always carries the time the
// shown results were computed for — a shared / reloaded URL reproduces
// them and never silently re-resolves to a new "now" (refresh-to-now is
// the panel's explicit button). Reset whenever the panel time changes.
let resolvedNowTime: string | null = null;

/** Every routing URL write goes through here: substitutes the stamped
 * query timestamp for a null time and rides the current routing options
 * along (url.ts writes only their non-default values). */
function writeUrl(url: URL, q: {
	from: Endpoint | null; to: Endpoint | null;
	mode: TimeMode; time: string | null; route?: string | null;
	/** Omitted = "the current vias"; pass [] explicitly to clear them
	 * (close / clear-route, which blank the whole query). */
	vias?: FilledVia[];
}) {
	writeRoutingQuery(url, {
		...q,
		// The travel mode always reflects the panel state — url.ts drops
		// the transit-only params for bike / walk. A cleared query (both
		// endpoints null) writes no mode param either way.
		travel: travelMode,
		vias: q.vias ?? queryVias(),
		time: q.time ?? resolvedNowTime,
		options: routingOptions.snapshot()
	});
}

function syncUrl() {
	const url = currentUrl();
	writeUrl(url, {
		from, to, mode, time,
		route: selectedFingerprint
	});
	if (url.href === window.location.href) return;
	// Preserve SvelteKit page state (line-detail marker etc.) — wiping it
	// would drop other views' history markers on every routing edit.
	replaceState(url, page.state);
}

/** Whenever the query inputs (from / to / mode / time) change the current
 * selection is no longer meaningful. Drop it and clear the URL param;
 * clear pending too so a stale fingerprint doesn't re-attach when new
 * results come back. */
function invalidateSelection() {
	// Editing the query leaves the shared context behind — the share only
	// describes the original from/to/time.
	sharedShare = null;
	sharedOnly = false;
	sharedExpired = false;
	pendingShareFingerprint = null;
	if (!selectedItinerary && !selectedFingerprint && !pendingFingerprint) return;
	selectedItinerary = null;
	selectedFingerprint = null;
	pendingFingerprint = null;
	selectionInvalid = false;
	pushedEntry = false;
	expandedFingerprint = null;
	mapModeFlag = false;
	// Drop the history marker too. syncUrl() preserves page.state verbatim,
	// so a leftover `routeSelection` would survive the input change and
	// Map.svelte's back/forward effect would re-select that old connection
	// the moment a matching itinerary shows up in the new result set —
	// pre-empting the fresh auto-select (leave-at first / arrive-by last).
	if (page.state?.routeSelection) {
		replaceState(currentUrl(), { ...page.state, routeSelection: undefined });
	}
}

/** The /api/plan request for the current query and its click history.
 * `queryTime` is always the pinned concrete timestamp (see
 * resolvedNowTime). */
function planArgs(queryTime: string, share: string | null = null) {
	return {
		from: from!, to: to!, currentCoord: resolvedCurrentCoord,
		vias: queryVias(), mode, time: queryTime,
		options: routingOptions.snapshot(),
		extensions: [...extensions],
		share
	};
}

/** Extend the result set in one chronological direction: one more
 * "earlier" / "later" in the request's history, replayed and extended by
 * the server. The response replaces the whole list. Called only when an
 * initial query has completed with at least one result — the history is
 * reset by resetCascadeState() when a fresh runQuery starts. */
async function loadMoreInDirection(direction: 'earlier' | 'later') {
	if (loading || loadingMore) return;
	if (!from || !to || results.length === 0) return;
	const queryTime = time ?? resolvedNowTime;
	if (!queryTime) return;
	abortInFlight();
	const ac = new AbortController();
	pendingAbort = ac;
	loadingMore = direction;
	extensions = [...extensions, direction];
	try {
		const res = await plan(planArgs(queryTime), ac.signal);
		if (ac.signal.aborted) return;
		walkBaselineSec = res.walkBaselineSec;
		results = res.itineraries;
	} catch (e) {
		if ((e as Error).name !== 'AbortError') {
			// The click didn't land — drop it from the history so the next
			// request doesn't replay a step the list never showed.
			extensions = extensions.slice(0, -1);
			error = userFacingError(e);
		}
	} finally {
		if (pendingAbort === ac) {
			pendingAbort = null;
			loadingMore = null;
		}
	}
}

/** Map a failed Valhalla request to a short user-facing message —
 * mirror of userFacingError for the direct modes. */
function directUserFacingError(e: unknown, m: TravelMode): string {
	console.error('[routing] direct query failed:', e);
	const what = m === 'bike' ? 'cycling route' : 'walking route';
	if (e instanceof DirectRouteError) {
		// Valhalla 400s when a location can't be matched to the network
		// (e.g. a point in a lake) or the path exceeds engine limits.
		if (e.status >= 400 && e.status < 500)
			return `Sorry — no ${what} could be found between these places.`;
		return `Sorry — the ${what} search is temporarily unavailable on our side. Please try again later.`;
	}
	if (e instanceof TypeError) return `Could not reach the ${what} search. Please check your connection.`;
	return `Sorry — the ${what} search failed due to an error on our side. Please try again.`;
}

/** The bike / walk counterpart of the transit cascade: one Valhalla
 * /route call with alternates (pedestrian-bicycle-routing.md § Query &
 * alternatives). The primary route auto-selects; recents / Connect
 * record the pair like any shown route. */
async function runDirectQuery(key: string) {
	const m = travelMode as 'bike' | 'walk';
	error = null;
	loading = true;
	hasQueried = true;
	abortInFlight();
	const ac = new AbortController();
	pendingAbort = ac;
	resetCascadeState();
	directRoutes = [];
	directSelected = 0;
	// A fresh query always lands collapsed — the map with the new routes
	// is what the user asked for.
	directSheetExpanded = false;
	try {
		if (from!.type === 'current' || to!.type === 'current') {
			try { resolvedCurrentCoord = await resolveCurrent(); }
			catch (e) {
				if (ac.signal.aborted) return;
				error = geolocationErrorMessage(e);
				return;
			}
		}
		const coordOf = (ep: Endpoint): [number, number] =>
			ep.type === 'current' ? (resolvedCurrentCoord ?? [0, 0]) : ep.coord;
		const vias = queryVias();
		const routes = await fetchDirectRoutes({
			mode: m,
			from: coordOf(from!),
			to: coordOf(to!),
			// Via points ride as coordinates — stations use their (platform-
			// snapped) coord, points their own (direct-mode vias accept
			// both; see ViaEndpoint in types.ts).
			vias: vias.map((v) => v.station.coord),
			// Walking pace follows the transit tab's speed tier so the same
			// walk shows the same duration on both tabs (null at the normal
			// tier → engine default 5.1 km/h, identical to the transit base).
			walkSpeedKmh: m === 'walk'
				? (routingOptions.pedestrianSpeedMs != null ? routingOptions.walkSpeedKmh : null)
				: null
		}, ac.signal);
		if (ac.signal.aborted) return;
		directRoutes = routes;
		directSelected = 0;
		if (routes.length > 0 && from && to) {
			void recordRecentRoute(from, to, vias, mode, null);
			connectStations.record(from);
			connectStations.record(to);
		}
		lastQueryKey = key;
	} catch (e) {
		if ((e as Error).name === 'AbortError') return;
		error = directUserFacingError(e, m);
		directRoutes = [];
	} finally {
		if (pendingAbort === ac) {
			pendingAbort = null;
			loading = false;
		}
	}
}

export const routingState = {
	get open() { return panelOpen; },
	get from() { return from; },
	get to() { return to; },
	get vias() { return vias; },
	/** Whether another via row may be added — the engine takes two. */
	get canAddVia() { return vias.length < MAX_VIAS; },
	get mode() { return mode; },
	get travelMode() { return travelMode; },
	get directRoutes() { return directRoutes; },
	get directSelected() { return directSelected; },
	get selectedDirectRoute(): DirectRoute | null {
		if (travelMode === 'transit') return null;
		return directRoutes[directSelected] ?? null;
	},
	get directSheetExpanded() { return directSheetExpanded; },
	get time() { return time; },
	get timeVersion() { return timeVersion; },
	get results() { return results; },
	get loading() { return loading; },
	get loadingMore() { return loadingMore; },
	get error() { return error; },
	get hasQueried() { return hasQueried; },
	get selectedItinerary() { return selectedItinerary; },
	get selectedFingerprint() { return selectedFingerprint; },
	get selectionInvalid() { return selectionInvalid; },
	get expandedFingerprint() { return expandedFingerprint; },
	// Effective only while something is on the map — the flag alone never
	// surfaces map mode on its own. Direct modes always have a selection
	// while routes exist (index-based), so they qualify via the routes.
	get mapMode() {
		return mapModeFlag && (
			selectedItinerary !== null ||
			(travelMode !== 'transit' && directRoutes.length > 0)
		);
	},
	get sharedOnly() { return sharedOnly; },
	get sharedExpired() { return sharedExpired; },
	/** What the panel renders: in shared-only mode just the verified shared
	 * connection; otherwise the normal pruned result list. */
	get displayedResults(): Itinerary[] {
		if (sharedOnly && selectedItinerary) return [selectedItinerary];
		return results;
	},

	openPanel(opts?: { prefillCurrent?: boolean; focus?: 'from' | 'to' }) {
		if (panelOpen) return;
		panelOpen = true;
		// Fresh open with no state: prefill From with current location (concept
		// § Endpoint inputs). If URL restoration filled `from` first, skip.
		// Skipped when geolocation is unavailable or already denied — the
		// prefill would only produce a dead endpoint that errors on query.
		// "Route from/to here" entry points pass prefillCurrent: false — the
		// user picked an explicit point, current location shouldn't ride along.
		if (opts?.prefillCurrent !== false && !from && !to && hasGeolocation() && !geolocationDenied()) {
			from = { type: 'current' };
		}
		// Cursor lands in the first empty endpoint field (From filled with
		// current location → To). Context menu overrides via opts.focus since
		// its endpoint arrives async, after the panel is already open.
		focusRequest = opts?.focus ?? (!from ? 'from' : !to ? 'to' : null);
		syncUrl();
	},

	/** One-shot read of the requested endpoint focus (set by openPanel). */
	consumeFocusRequest(): 'from' | 'to' | null {
		const r = focusRequest;
		focusRequest = null;
		return r;
	},

	/** Close the panel WITHOUT discarding the route (routing-persistence.md
	 * § Restore on reopen): endpoints, mode, time, results, cascade state
	 * and `lastQueryKey` all survive, so reopening restores the exact view
	 * with no re-query. Only the selection (map overlay) and any shared
	 * context are dropped, plus the routing URL params — the address
	 * reflects what is visible. */
	closePanel() {
		panelOpen = false;
		sharedShare = null;
		sharedOnly = false;
		sharedExpired = false;
		pendingShareFingerprint = null;
		selectedItinerary = null;
		selectedFingerprint = null;
		pendingFingerprint = null;
		selectionInvalid = false;
		pushedEntry = false;
		expandedFingerprint = null;
		mapModeFlag = false;
		abortInFlight();
		// A close mid-query leaves the flags dangling — the aborted run
		// skips its finally-clear because it no longer owns pendingAbort.
		// lastQueryKey is only ever set by a completed run, so an aborted
		// query re-runs via the panel's query effect on reopen.
		loading = false;
		loadingMore = null;
		const url = currentUrl();
		writeUrl(url, {
			from: null, to: null, vias: [], mode: 'leave', time: null, route: null
		});
		if (url.href !== window.location.href) {
			replaceState(url, { ...page.state, routeSelection: undefined });
		}
	},

	/** Reset to the no-route-set state (routing-persistence.md § Clear-route
	 * button) — endpoints, time, results and selection all drop; the panel
	 * stays open. Never touches the recents list. */
	clearRoute() {
		abortInFlight();
		from = null;
		to = null;
		vias = [];
		mode = 'leave';
		time = null;
		resolvedNowTime = null;
		results = [];
		directRoutes = [];
		directSelected = 0;
		loading = false;
		loadingMore = null;
		error = null;
		hasQueried = false;
		lastQueryKey = null;
		resetCascadeState();
		invalidateSelection();
		const url = currentUrl();
		writeUrl(url, {
			from: null, to: null, vias: [], mode: 'leave', time: null, route: null
		});
		if (url.href !== window.location.href) {
			replaceState(url, { ...page.state, routeSelection: undefined });
		}
	},

	/** Load a complete query in one shot (recents selection —
	 * routing-persistence.md § Recent routes list). One state write + one
	 * URL sync; the panel's query effect then runs the query. */
	loadRoute(next: {
		from: Endpoint; to: Endpoint; vias?: Via[];
		mode: TimeMode; time: string | null;
	}) {
		abortInFlight();
		from = next.from;
		to = next.to;
		vias = next.vias ? next.vias.slice(0, MAX_VIAS) : [];
		// A recent recorded on a direct tab can carry point vias — the
		// transit tab can't express them (same rule as setTravelMode), so
		// they drop rather than ride as silently ignored rows.
		if (travelMode === 'transit') {
			vias = vias.filter((v) => !v.station || v.station.type === 'station');
		}
		mode = next.mode;
		time = next.time;
		timeVersion++;
		results = [];
		directRoutes = [];
		directSelected = 0;
		hasQueried = false;
		error = null;
		lastQueryKey = null;
		resetCascadeState();
		invalidateSelection();
		syncUrl();
	},

	setFrom(ep: Endpoint | null) {
		abortInFlight();
		from = ep;
		results = [];
		directRoutes = [];
		directSelected = 0;
		hasQueried = false;
		error = null;
		lastQueryKey = null;
		invalidateSelection();
		syncUrl();
	},

	/** Insert an empty via row at `index` (via-stops.md § Panel UI: the
	 * `+` on a row means "insert a stop after this row"). An empty row is
	 * invisible to the query, so the shown results survive until it is
	 * filled. */
	insertViaAt(index: number) {
		if (vias.length >= MAX_VIAS) return;
		const i = Math.max(0, Math.min(index, vias.length));
		vias = [...vias.slice(0, i), { station: null, wait: 0 }, ...vias.slice(i)];
		syncUrl();
	},

	/** The To row's `+`: the current destination becomes the last via and
	 * a fresh empty destination opens below it. Only an endpoint the tab
	 * accepts as a via can make that move — stations everywhere, points
	 * on the direct tabs too (ViaEndpoint). */
	promoteToToVia(): boolean {
		if (!to || vias.length >= MAX_VIAS) return false;
		const ok = to.type === 'station'
			|| (travelMode !== 'transit' && to.type === 'point');
		if (!ok) return false;
		abortInFlight();
		vias = [...vias, { station: to as Exclude<Endpoint, { type: 'current' }>, wait: 0 }];
		to = null;
		results = [];
		directRoutes = [];
		directSelected = 0;
		hasQueried = false;
		error = null;
		lastQueryKey = null;
		invalidateSelection();
		syncUrl();
		return true;
	},

	/** Fill (or blank) one via row. Transit accepts stations only; the
	 * direct tabs also accept points (ViaEndpoint). Anything else empties
	 * the row rather than silently changing its meaning. */
	setVia(index: number, ep: Endpoint | null) {
		const row = vias[index];
		if (!row) return;
		const before = viaSignature();
		const ok = ep && (ep.type === 'station'
			|| (travelMode !== 'transit' && ep.type === 'point'));
		const station = ok ? (ep as Exclude<Endpoint, { type: 'current' }>) : null;
		vias = vias.map((v, i) => (i === index ? { ...v, station } : v));
		commitViaEdit(before);
	},

	setViaWait(index: number, minutes: number) {
		const row = vias[index];
		if (!row) return;
		const before = viaSignature();
		const wait = Math.min(MAX_VIA_WAIT_MIN, Math.max(0, Math.round(minutes)));
		vias = vias.map((v, i) => (i === index ? { ...v, wait } : v));
		commitViaEdit(before);
	},

	/** A via row's clear control removes the row outright — unlike From /
	 * To, whose clear only empties the field (via-stops.md § Panel UI). */
	removeVia(index: number) {
		if (!vias[index]) return;
		const before = viaSignature();
		vias = vias.filter((_, i) => i !== index);
		commitViaEdit(before);
	},

	setTo(ep: Endpoint | null) {
		abortInFlight();
		to = ep;
		results = [];
		directRoutes = [];
		directSelected = 0;
		hasQueried = false;
		error = null;
		lastQueryKey = null;
		invalidateSelection();
		syncUrl();
	},

	/** Switch the panel's travel mode tab (pedestrian-bicycle-routing.md
	 * § Mode tabs). Endpoints are shared across the tabs; results are
	 * mode-specific, so the shown list clears and the panel's query
	 * effect re-runs for the new mode. `persist: false` is the deep-link
	 * restore — the link's mode drives this visit without overwriting
	 * the stored preference. */
	setTravelMode(m: TravelMode, opts?: { persist?: boolean }) {
		if (travelMode === m) return;
		abortInFlight();
		travelMode = m;
		// Point vias only exist on the direct tabs (ViaEndpoint) — the
		// transit engine takes stop ids, so those rows drop on entry.
		// Waits stay put in the other direction: the direct tabs simply
		// ignore them (no control, not queried, not serialised), so a tab
		// round-trip never loses a transit errand plan.
		if (m === 'transit') {
			vias = vias.filter((v) => !v.station || v.station.type === 'station');
		}
		if (opts?.persist !== false) {
			try {
				localStorage.setItem(TRAVEL_MODE_KEY, m);
			} catch {
				// Storage unavailable — the choice still holds this session.
			}
		}
		results = [];
		directRoutes = [];
		directSelected = 0;
		hasQueried = false;
		error = null;
		lastQueryKey = null;
		invalidateSelection();
		syncUrl();
	},

	/** Select one of the direct route alternatives — from its card or by
	 * tapping its (muted) line on the map. No history entry and no URL
	 * param: the query itself is fully in the URL and re-running it
	 * restores the primary selection. */
	selectDirectRoute(index: number) {
		if (index < 0 || index >= directRoutes.length) return;
		directSelected = index;
	},

	/** Expand the narrow-screen direct-mode bottom sheet back to the full
	 * panel (edit the query); collapse returns to the docked sheet. */
	expandDirectSheet() {
		directSheetExpanded = true;
	},

	collapseDirectSheet() {
		directSheetExpanded = false;
	},

	setMode(m: TimeMode) {
		abortInFlight();
		mode = m;
		results = [];
		directRoutes = [];
		directSelected = 0;
		hasQueried = false;
		error = null;
		lastQueryKey = null;
		invalidateSelection();
		syncUrl();
	},

	setTime(t: string | null) {
		abortInFlight();
		time = t;
		// A fresh "now" (refresh button / explicit reset) must re-stamp on
		// the next query rather than reuse the previous run's timestamp.
		resolvedNowTime = null;
		timeVersion++;
		results = [];
		directRoutes = [];
		directSelected = 0;
		hasQueried = false;
		error = null;
		lastQueryKey = null;
		invalidateSelection();
		syncUrl();
	},

	swap() {
		abortInFlight();
		const tmp = from;
		from = to;
		to = tmp;
		// The whole chain reverses, waits travelling with their vias
		// (via-stops.md § Panel UI).
		vias = [...vias].reverse();
		results = [];
		directRoutes = [];
		directSelected = 0;
		hasQueried = false;
		error = null;
		lastQueryKey = null;
		invalidateSelection();
		syncUrl();
	},

	/** Select one of the current `results` for map rendering (route-display.md
	 * § Lifecycle). Pushes a browser history entry so back closes the route
	 * view; state carries the fingerprint so the back/forward $effect in
	 * Map.svelte can reconcile against it. */
	selectItinerary(it: Itinerary) {
		const fp = itineraryFingerprint(it);
		const wasSelected = selectedFingerprint !== null;
		selectedItinerary = it;
		selectedFingerprint = fp;
		pendingFingerprint = null;
		selectionInvalid = false;
		const url = currentUrl();
		writeUrl(url, { from, to, mode, time, route: fp });
		if (!wasSelected) {
			pushState(url, { ...page.state, routeSelection: fp });
			pushedEntry = true;
		} else {
			replaceState(url, { ...page.state, routeSelection: fp });
		}
	},

	/** UI-driven close (× on the selected result card). When the current
	 * history entry was pushed for this selection, pop it via
	 * history.back() so back never lands on a stale route-view entry —
	 * Map.svelte's back/forward $effect then does the teardown. Otherwise
	 * (auto-select / URL restore) clear in place. */
	dismissSelectedItinerary() {
		if (!selectedItinerary && !selectedFingerprint) return;
		if (pushedEntry && page.state?.routeSelection) {
			pushedEntry = false;
			history.back();
			return;
		}
		this.clearSelectedItineraryFromHistory();
	},

	/** Drop the current selection without touching browser history — used
	 * by Map.svelte after a back-driven pop already consumed the pushed
	 * entry. */
	clearSelectedItineraryFromHistory() {
		selectedItinerary = null;
		selectedFingerprint = null;
		pendingFingerprint = null;
		selectionInvalid = false;
		pushedEntry = false;
		mapModeFlag = false;
		// Dismissing the shared card's selection exits the single-connection
		// filter — the full list is then the only sensible thing to show.
		sharedOnly = false;
		const url = currentUrl();
		writeUrl(url, { from, to, mode, time, route: null });
		if (url.href !== window.location.href) {
			// Strip routeSelection explicitly — reusing page.state verbatim
			// would re-stamp the stale fingerprint, and Map.svelte's
			// back/forward effect would read it as a forward-restore and
			// silently re-select the just-dismissed itinerary.
			replaceState(url, { ...page.state, routeSelection: undefined });
		}
	},

	/** Toggle a card's details expansion (accordion: opening one closes any
	 * other). Primary-click behavior per routing-map-details-split.md. */
	toggleExpanded(it: Itinerary) {
		const fp = itineraryFingerprint(it);
		expandedFingerprint = expandedFingerprint === fp ? null : fp;
	},

	/** Mobile fullscreen map mode. No-op without a selection: the map icon
	 * always selects first. Never armed by auto-select or URL restore. */
	enterMapMode() {
		if (selectedItinerary) mapModeFlag = true;
		else if (travelMode !== 'transit' && directRoutes.length > 0) mapModeFlag = true;
	},

	exitMapMode() {
		mapModeFlag = false;
	},

	/** Open the panel on a /s/<id> share landing (connection-sharing.md
	 * § Shared view). `null` = unknown/deleted id — panel opens with only
	 * the gone-error. Otherwise the stored query context is direct-written
	 * (leave-at, anchored on the shared departure) and the share fingerprint
	 * armed; the panel's query effect then runs the verification query. */
	hydrateShare(share: ShareData | null) {
		panelOpen = true;
		if (!share) {
			sharedExpired = true;
			return;
		}
		from = share.from;
		to = share.to;
		// Shares created before vias existed simply have none.
		vias = (share.vias ?? []).slice(0, MAX_VIAS);
		mode = 'leave';
		time = share.itinerary.startTime;
		sharedShare = share;
		sharedOnly = true;
		sharedExpired = false;
		pendingShareFingerprint = share.fingerprint;
		pushedEntry = false;
	},

	/** Leave single-connection display (earlier/later buttons) — the list
	 * then shows every fetched result like a normal query. */
	exitSharedOnly() {
		sharedOnly = false;
	},

	/** Direct-write initial state from a URL restore. Doesn't re-serialise.
	 * The restored time is always concrete (writes stamp "now" — see
	 * `resolvedNowTime`), so a reload re-queries the original timestamp,
	 * never a fresh "now". URL options apply session-only: the link's
	 * settings drive this tab's queries without touching localStorage. */
	hydrate(next: {
		from: Endpoint | null; to: Endpoint | null; vias?: Via[];
		mode: TimeMode; travel?: TravelMode; time: string | null;
		route: string | null;
		options?: RoutingOptionValues;
	}) {
		from = next.from;
		to = next.to;
		vias = next.vias ? next.vias.slice(0, MAX_VIAS) : [];
		mode = next.mode;
		// The deep link's travel mode overrides the persisted choice for
		// this visit only (pedestrian-bicycle-routing.md § Mode tabs) —
		// direct write, never into localStorage.
		if (next.travel) travelMode = next.travel;
		time = next.time;
		if (next.options) routingOptions.applySession(next.options);
		pendingFingerprint = next.route;
		selectedFingerprint = next.route;
		pushedEntry = false;
		panelOpen = true;
	},

	/** Endpoint-input refresh button on a "Current location" endpoint:
	 * drop the cached geolocation fix and re-run the query with a fresh
	 * position. The dedup key ignores the resolved coord, so the guard
	 * must be cleared explicitly. */
	refreshCurrentLocation() {
		invalidateCurrent();
		lastQueryKey = null;
		if (from && to) void routingState.runQuery();
	},

	/** A query-affecting routing option changed (walking speed, safety
	 * mode, minimize walking — the latter drives the fork's walk-point
	 * table since routing-options.md § Minimize walking): clear the
	 * shown results like any other input edit; the panel's query effect
	 * then re-runs the cascade with the new params. Options ride in the
	 * URL (non-default values only), so sync it. */
	optionsChanged() {
		abortInFlight();
		results = [];
		directRoutes = [];
		directSelected = 0;
		hasQueried = false;
		error = null;
		lastQueryKey = null;
		invalidateSelection();
		syncUrl();
	},

	async runQuery() {
		if (!from || !to) return;
		// Direct cycling / walking query — its own, much simpler pipeline
		// (no cascade, no time). Dedup key covers everything the Valhalla
		// request can see.
		if (travelMode !== 'transit') {
			const directKey = JSON.stringify({
				travel: travelMode, from, to, vias: viaSignature(),
				walkSpeed: travelMode === 'walk' ? routingOptions.walkSpeed : null
			});
			if (directKey === lastQueryKey && !error) return;
			await runDirectQuery(directKey);
			return;
		}
		const key = JSON.stringify({
			from, to, vias: viaSignature(), mode, time,
			walkSpeed: routingOptions.walkSpeed,
			safety: routingOptions.safety,
			// Minimize walking is a query param since it drives the fork's
			// walk-point table (routing-options.md § Minimize walking).
			minWalk: routingOptions.minimizeWalking
		});
		if (key === lastQueryKey && !error) return;
		// A null time means "now" — pin it to a concrete timestamp for this
		// run and put it on the URL immediately, so the address always
		// carries the time the results are computed for (even if the query
		// errors or comes back empty). State `time` stays null: the panel
		// keeps showing "now" and a later re-run re-stamps. The server gets
		// the pinned value too, so a later earlier/later replay anchors on
		// the same instant.
		if (!time) {
			resolvedNowTime = new Date().toISOString();
			syncUrl();
		}
		const queryTime: string = time ?? resolvedNowTime!;
		error = null;
		loading = true;
		hasQueried = true;
		abortInFlight();
		const ac = new AbortController();
		pendingAbort = ac;
		resetCascadeState();
		try {
			if (from.type === 'current' || to.type === 'current') {
				try { resolvedCurrentCoord = await resolveCurrent(); }
				catch (e) {
					if (ac.signal.aborted) return;
					error = geolocationErrorMessage(e);
					results = [];
					return;
				}
			}

			// One request: the server runs the whole cascade — narrow query,
			// wide retry on its triggers, time-advance hops, pruning — and
			// returns the final list (server-side-transit-planning.md). A
			// pending share fingerprint rides along so the server verifies it
			// against its raw (unpruned) candidate set — dominance pruning
			// must never turn a still-running connection into a false expiry.
			const share = pendingShareFingerprint;
			const res = await plan(planArgs(queryTime, share), ac.signal);
			if (ac.signal.aborted) return;
			walkBaselineSec = res.walkBaselineSec;
			results = res.itineraries;

			// Reconcile the pending share fingerprint (connection-sharing.md
			// § Shared view). On a confirmed no-match, report to the server,
			// which re-verifies before actually deleting the share files.
			if (share) {
				pendingShareFingerprint = null;
				const match = res.shareMatch ?? null;
				if (match) {
					selectedItinerary = match;
					selectedFingerprint = itineraryFingerprint(match);
					selectionInvalid = false;
					// The shared connection opens with its leg details visible —
					// the recipient came to look at exactly this connection.
					expandedFingerprint = selectedFingerprint;
					// Stamp page.state (URL untouched — the /s/<id> address is
					// the share link and must stay clean): without the
					// routeSelection marker, Map.svelte's back/forward effect
					// reads the selection as a stale leftover and clears it.
					replaceState(currentUrl(), {
						...page.state, routeSelection: selectedFingerprint
					});
				} else {
					sharedOnly = false;
					sharedExpired = true;
					if (sharedShare) reportShareExpired(sharedShare.id);
				}
			}

			// Reconcile a pending fingerprint from a cold-load restore
			// (route-display.md § Lifecycle). Match one of the returned
			// itineraries by fingerprint; if none does, flag the URL
			// selection as invalid so the panel can show an error message.
			// The URL param is retained on invalid so the user can share /
			// retry the same address without it silently disappearing.
			if (pendingFingerprint) {
				const wanted = pendingFingerprint;
				pendingFingerprint = null;
				const match = results.find((r) => itineraryFingerprint(r) === wanted);
				if (match) {
					selectedItinerary = match;
					selectedFingerprint = wanted;
					selectionInvalid = false;
				} else {
					selectedItinerary = null;
					selectedFingerprint = null;
					selectionInvalid = true;
					const url = currentUrl();
					writeUrl(url, {
						from, to, mode, time, route: null
					});
					if (url.href !== window.location.href) {
						replaceState(url, page.state);
					}
				}
			}
			// Auto-select the most relevant result on a fresh query, so the
			// user sees a route on the map immediately without having to
			// click. For leave-at that's the first (earliest arrival); for
			// arrive-by the list sorts by departure ascending, so the most
			// relevant (latest departure) sits at the end. Skipped when the
			// cold-load restore is pending (matched above) or invalid
			// (concept: show the error, don't silently swap in a different
			// route).
			// Also skipped right after a share expiry — the error must not be
			// upstaged by silently putting a different connection on the map.
			if (!selectedFingerprint && !selectionInvalid && !sharedExpired && results.length > 0) {
				// Always the chronological edge, minimize-walking included:
				// the pick has to be predictable from the mode alone, and a
				// comfort-best (crown) pick reads as an arbitrary jump when
				// the list reloads after an option change.
				const it = mode === 'arrive' ? results[results.length - 1] : results[0];
				const fp = itineraryFingerprint(it);
				selectedItinerary = it;
				selectedFingerprint = fp;
				const url = currentUrl();
				writeUrl(url, { from, to, mode, time, route: fp });
				if (url.href !== window.location.href) {
					replaceState(url, { ...page.state, routeSelection: fp });
				}
			}
			// A route was shown → record it (routing-persistence.md § Recent
			// routes list / § Connect). Covers fresh queries, URL restores and
			// shared landings alike; empty result sets are not worth
			// remembering. Async fire-and-forget: current-location endpoints
			// are materialized (reverse geocode) before the entry and its
			// Connect tiles are stored.
			if (results.length > 0 && from && to) {
				void recordRecentRoute(from, to, queryVias(), mode, time);
			}
			lastQueryKey = key;
		} catch (e) {
			if ((e as Error).name === 'AbortError') return;
			error = userFacingError(e);
			results = [];
		} finally {
			// Only the run that still owns `pendingAbort` may clear `loading`.
			// A superseded run (aborted by a newer runQuery) reaching here
			// must not flip the flag while its successor is still in flight
			// — the panel would flash "No connections found".
			if (pendingAbort === ac) {
				pendingAbort = null;
				loading = false;
			}
		}
	},

	async loadMoreEarlier() {
		await loadMoreInDirection('earlier');
	},

	async loadMoreLater() {
		await loadMoreInDirection('later');
	}
};

export type RoutingState = typeof routingState;
