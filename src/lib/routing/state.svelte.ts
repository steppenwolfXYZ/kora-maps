import { pushState, replaceState } from '$app/navigation';
import { page } from '$app/state';
import { plan } from './client';
import { itineraryFingerprint } from './fingerprint';
import { geolocationErrorMessage, resolveCurrent } from './geolocation';
import type { Endpoint, Itinerary, TimeMode } from './types';
import { writeRoutingQuery } from './url';

// Reactive routing state (Svelte 5 runes). One instance shared across the
// app — Map.svelte and RoutingPanel read from it, entry-point handlers
// mutate it. See transit-routing.md § Routing panel / § Entry points.

let panelOpen = $state(false);
let from = $state<Endpoint | null>(null);
let to = $state<Endpoint | null>(null);
let mode = $state<TimeMode>('leave');
let time = $state<string | null>(null);

let results = $state<Itinerary[]>([]);
let loading = $state(false);
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

let pendingAbort: AbortController | null = null;

// Cascade tuning — see performance discussion.
const NARROW_PRE_POST_SEC = 7200;   // 2 h — narrow default per query
const WIDE_PRE_POST_SEC   = 28800;  // 8 h — server hard cap, used on escalation
const LONG_WAIT_THRESHOLD_SEC = 3600; // 1 h wait triggers pre/post escalation
const TARGET_RESULT_COUNT = 5;
// Stage 3 time-advance cascade — MOTIS's nextPageCursor stalls on remote
// destinations (returns 0 with the same cursor value), so instead of
// paging via cursor we advance `time` past the last returned itinerary
// and re-query fresh.
const HOP_MS = 2 * 3600 * 1000;         // 2 h step when a hop returns empty
const HOP_SEARCH_WINDOW_SEC = 7200;     // matches HOP_MS so windows don't gap
const MAX_SPAN_MS = 5 * 24 * 3600 * 1000; // stop after 5 days of advance
const MAX_EMPTY_STREAK = 3;             // stop after N consecutive empty hops

/** True when any transit leg in the itinerary is preceded by a wait
 * longer than `LONG_WAIT_THRESHOLD_SEC`. Signals that expanding the
 * walking budget might reach a nearer stop with better-timed service. */
function hasLongWait(it: Itinerary): boolean {
	const legs = it.legs;
	for (let i = 0; i < legs.length; i++) {
		const leg = legs[i];
		if (leg.mode === 'WALK') continue;
		const prevEnd = i > 0
			? Date.parse(legs[i - 1].endTime)
			: Date.parse(it.startTime);
		const wait = (Date.parse(leg.startTime) - prevEnd) / 1000;
		if (wait > LONG_WAIT_THRESHOLD_SEC) return true;
	}
	return false;
}

function currentUrl(): URL {
	return new URL(window.location.href);
}

function syncUrl() {
	const url = currentUrl();
	writeRoutingQuery(url, {
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
	if (!selectedItinerary && !selectedFingerprint && !pendingFingerprint) return;
	selectedItinerary = null;
	selectedFingerprint = null;
	pendingFingerprint = null;
	selectionInvalid = false;
}

export const routingState = {
	get open() { return panelOpen; },
	get from() { return from; },
	get to() { return to; },
	get mode() { return mode; },
	get time() { return time; },
	get results() { return results; },
	get loading() { return loading; },
	get error() { return error; },
	get hasQueried() { return hasQueried; },
	get selectedItinerary() { return selectedItinerary; },
	get selectedFingerprint() { return selectedFingerprint; },
	get selectionInvalid() { return selectionInvalid; },

	openPanel() {
		if (panelOpen) return;
		panelOpen = true;
		// Fresh open with no state: prefill From with current location (concept
		// § Endpoint inputs). If URL restoration filled `from` first, skip.
		if (!from && !to) from = { type: 'current' };
		syncUrl();
	},

	closePanel() {
		panelOpen = false;
		results = [];
		error = null;
		hasQueried = false;
		selectedItinerary = null;
		selectedFingerprint = null;
		pendingFingerprint = null;
		selectionInvalid = false;
		if (pendingAbort) { pendingAbort.abort(); pendingAbort = null; }
		const url = currentUrl();
		writeRoutingQuery(url, {
			from: null, to: null, mode: 'leave', time: null, route: null
		});
		if (url.href !== window.location.href) {
			replaceState(url, page.state);
		}
		from = null;
		to = null;
		mode = 'leave';
		time = null;
	},

	setFrom(ep: Endpoint | null) {
		from = ep;
		results = [];
		hasQueried = false;
		error = null;
		invalidateSelection();
		syncUrl();
	},

	setTo(ep: Endpoint | null) {
		to = ep;
		results = [];
		hasQueried = false;
		error = null;
		invalidateSelection();
		syncUrl();
	},

	setMode(m: TimeMode) {
		mode = m;
		results = [];
		hasQueried = false;
		error = null;
		invalidateSelection();
		syncUrl();
	},

	setTime(t: string | null) {
		time = t;
		results = [];
		hasQueried = false;
		error = null;
		invalidateSelection();
		syncUrl();
	},

	swap() {
		const tmp = from;
		from = to;
		to = tmp;
		results = [];
		hasQueried = false;
		error = null;
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
		writeRoutingQuery(url, { from, to, mode, time, route: fp });
		if (!wasSelected) {
			pushState(url, { ...page.state, routeSelection: fp });
		} else {
			replaceState(url, { ...page.state, routeSelection: fp });
		}
	},

	/** UI-driven close (× on the selected result card). When the pushed
	 * history entry that carries the selection is current (page.state
	 * has `routeSelection`), pop it via history.back() so back never
	 * lands on a stale route-view entry — Map.svelte's back/forward
	 * $effect then does the teardown. Otherwise clear in place. */
	dismissSelectedItinerary() {
		if (!selectedItinerary && !selectedFingerprint) return;
		if (page.state?.routeSelection) {
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
		const url = currentUrl();
		writeRoutingQuery(url, { from, to, mode, time, route: null });
		if (url.href !== window.location.href) {
			replaceState(url, page.state);
		}
	},

	/** Direct-write initial state from a URL restore. Doesn't re-serialise. */
	hydrate(next: {
		from: Endpoint | null; to: Endpoint | null;
		mode: TimeMode; time: string | null;
		route: string | null;
	}) {
		from = next.from;
		to = next.to;
		mode = next.mode;
		time = next.time;
		pendingFingerprint = next.route;
		selectedFingerprint = next.route;
		panelOpen = true;
	},

	async runQuery() {
		if (!from || !to) return;
		error = null;
		loading = true;
		hasQueried = true;
		if (pendingAbort) pendingAbort.abort();
		const ac = new AbortController();
		pendingAbort = ac;
		try {
			let currentCoord: [number, number] | null = null;
			if (from.type === 'current' || to.type === 'current') {
				try { currentCoord = await resolveCurrent(); }
				catch (e) {
					error = geolocationErrorMessage(e);
					loading = false;
					results = [];
					return;
				}
			}

			// Sort chronologically — earliest arrival first for leave-at,
			// latest departure first for arrive-by. Applies after every
			// cascade stage so partial results are ordered while more roll
			// in.
			const sortFn = mode === 'arrive'
				? (a: Itinerary, b: Itinerary) => Date.parse(b.startTime) - Date.parse(a.startTime)
				: (a: Itinerary, b: Itinerary) => Date.parse(a.endTime) - Date.parse(b.endTime);

			let pre = NARROW_PRE_POST_SEC;
			let post = NARROW_PRE_POST_SEC;
			let combined: Itinerary[] = [];

			const publish = () => {
				results = [...combined].sort(sortFn).slice(0, TARGET_RESULT_COUNT);
			};

			const doQuery = async (timeArg: string | null, searchWindow?: number) => {
				return await plan({
					from: from!, to: to!, mode, time: timeArg, currentCoord,
					maxPreTransitTime: pre,
					maxPostTransitTime: post,
					searchWindow
				}, ac.signal);
			};

			// Stage 1 — narrow initial query (fast for typical cases).
			let res = await doQuery(time);
			if (ac.signal.aborted) return;
			combined = [...(res.itineraries ?? []), ...(res.direct ?? [])];

			// Stage 2 — escalate walking budget on trigger:
			//   (a) narrow query returned nothing, or
			//   (b) any returned itinerary has a >1 h wait at start or
			//       between transit legs.
			// Escalation replaces `combined` (different candidate set with
			// a wider walking radius, not comparable via merge).
			const needsEscalation = combined.length === 0
				|| combined.some(hasLongWait);
			if (needsEscalation) {
				pre = WIDE_PRE_POST_SEC;
				post = WIDE_PRE_POST_SEC;
				res = await doQuery(time);
				if (ac.signal.aborted) return;
				combined = [...(res.itineraries ?? []), ...(res.direct ?? [])];
			}
			publish();

			// Stage 3 — time-advance cascade. MOTIS's nextPageCursor stalls
			// on remote destinations (returns 0 with an unchanged cursor
			// value even when later timetable entries exist), so we walk
			// forward by re-querying with `time` bumped past the last known
			// result. Dedupe by fingerprint; stop at TARGET_RESULT_COUNT,
			// MAX_SPAN_MS, or MAX_EMPTY_STREAK consecutive empty hops.
			const initialEpoch = time ? Date.parse(time) : Date.now();
			const advanceDir = mode === 'arrive' ? -1 : 1;
			const seen = new Set(combined.map(itineraryFingerprint));
			const startsOf = (its: Itinerary[]) => its.map(i => Date.parse(i.startTime));

			let queryEpoch: number;
			if (combined.length) {
				const starts = startsOf(combined);
				queryEpoch = (mode === 'arrive' ? Math.min(...starts) : Math.max(...starts))
					+ advanceDir * 60_000;
			} else {
				queryEpoch = initialEpoch + advanceDir * HOP_MS;
			}
			let emptyStreak = 0;

			while (results.length < TARGET_RESULT_COUNT && !ac.signal.aborted) {
				if (Math.abs(queryEpoch - initialEpoch) > MAX_SPAN_MS) break;
				if (emptyStreak >= MAX_EMPTY_STREAK) break;

				const hopTime = new Date(queryEpoch).toISOString();
				res = await doQuery(hopTime, HOP_SEARCH_WINDOW_SEC);
				if (ac.signal.aborted) return;

				const items = [...(res.itineraries ?? []), ...(res.direct ?? [])];
				const fresh = items.filter((it) => {
					const fp = itineraryFingerprint(it);
					if (seen.has(fp)) return false;
					seen.add(fp);
					return true;
				});

				if (fresh.length === 0) {
					emptyStreak++;
					queryEpoch += advanceDir * HOP_MS;
				} else {
					emptyStreak = 0;
					combined = [...combined, ...fresh];
					publish();
					const starts = startsOf(fresh);
					queryEpoch = (mode === 'arrive' ? Math.min(...starts) : Math.max(...starts))
						+ advanceDir * 60_000;
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
					writeRoutingQuery(url, {
						from, to, mode, time, route: null
					});
					if (url.href !== window.location.href) {
						replaceState(url, page.state);
					}
				}
			}
			// Auto-select the first result on a fresh query, so the user
			// sees a route on the map immediately without having to click.
			// Skipped when the cold-load restore is pending (matched above)
			// or invalid (concept: show the error, don't silently swap in
			// a different route).
			if (!selectedFingerprint && !selectionInvalid && results.length > 0) {
				const it = results[0];
				const fp = itineraryFingerprint(it);
				selectedItinerary = it;
				selectedFingerprint = fp;
				const url = currentUrl();
				writeRoutingQuery(url, { from, to, mode, time, route: fp });
				if (url.href !== window.location.href) {
					replaceState(url, { ...page.state, routeSelection: fp });
				}
			}
		} catch (e) {
			if ((e as Error).name === 'AbortError') return;
			error = e instanceof Error ? e.message : String(e);
			results = [];
		} finally {
			if (pendingAbort === ac) pendingAbort = null;
			loading = false;
		}
	}
};

export type RoutingState = typeof routingState;
