import { pushState, replaceState } from '$app/navigation';
import { page } from '$app/state';
import { plan } from './client';
import { itineraryFingerprint } from './fingerprint';
import { geolocationErrorMessage, resolveCurrent } from './geolocation';
import { pruneDominated } from './ranking';
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

let pendingAbort: AbortController | null = null;

// Whether the current history entry was pushed by `selectItinerary` for
// the active selection. Only then does `dismissSelectedItinerary` consume
// it via history.back() — an auto-selected or URL-restored selection
// lives on an entry it never pushed, so × must clear in place instead
// (back() on a single-entry history is a silent no-op and the selection
// would survive). Replace-stamping doesn't change the flag: a replaced
// entry is still the pushed one.
let pushedEntry = false;

// Cascade tuning — see performance discussion.
const NARROW_PRE_POST_SEC = 7200;   // 2 h — narrow default per query
const WIDE_PRE_POST_SEC   = 28800;  // 8 h — server hard cap, used on escalation
const LONG_WAIT_THRESHOLD_SEC = 3600; // 1 h wait triggers pre/post escalation
const TARGET_RESULT_COUNT = 5;
// Sparse-service escalation — if the narrow cascade reveals a ≥4 h stretch
// of daytime (06–21 local) with no service (either between two consecutive
// results, or between the last result and how far the hop cascade has
// searched), redo everything with the wide walking budget.
const SPARSE_GAP_THRESHOLD_SEC = 4 * 3600;
const DAY_START_HOUR = 6;
const DAY_END_HOUR = 21;
// Stage 3 time-advance cascade — MOTIS's nextPageCursor stalls on remote
// destinations (returns 0 with the same cursor value), so instead of
// paging via cursor we advance `time` past the last returned itinerary
// and re-query fresh.
const HOP_MS = 2 * 3600 * 1000;         // 2 h step when a hop returns empty
const HOP_SEARCH_WINDOW_SEC = 7200;     // matches HOP_MS so windows don't gap
const MAX_SPAN_MS = 5 * 24 * 3600 * 1000; // stop after 5 days of advance
const MAX_EMPTY_STREAK = 3;             // stop after N consecutive empty hops

// Cascade state — shared between runQuery and loadMoreEarlier / loadMoreLater.
// Not reactive; every mutation of `combined` flows through publishResults()
// which is the sole writer of the reactive `results` array. `resultTarget`
// is the current `.slice()` cap and is bumped by TARGET_RESULT_COUNT on
// every loadMore click.
let combined: Itinerary[] = [];
let seenFingerprints = new Set<string>();
let resolvedCurrentCoord: [number, number] | null = null;
let resultTarget = TARGET_RESULT_COUNT;

function abortInFlight() {
	if (!pendingAbort) return;
	pendingAbort.abort();
	pendingAbort = null;
}

function resetCascadeState() {
	combined = [];
	seenFingerprints = new Set();
	resolvedCurrentCoord = null;
	resultTarget = TARGET_RESULT_COUNT;
}

function currentSortFn() {
	// Sort ascending in both modes so the "Earlier connections" (top) /
	// "Later connections" (bottom) buttons align with the direction they
	// load — arrive-by used to sort descending, which put earlier-loaded
	// results at the bottom and the earlier button at the top. Auto-
	// select compensates by picking the relevant end (last for arrive-by).
	return mode === 'arrive'
		? (a: Itinerary, b: Itinerary) => Date.parse(a.startTime) - Date.parse(b.startTime)
		: (a: Itinerary, b: Itinerary) => Date.parse(a.endTime) - Date.parse(b.endTime);
}

function publishResults() {
	results = pruneDominated(combined, mode).sort(currentSortFn()).slice(0, resultTarget);
}

/** Hop `time` in `dir` (+1 forward, −1 backward) starting at `startEpoch`
 * and merge fresh itineraries into `combined` until `results.length`
 * reaches `resultTarget`, MAX_EMPTY_STREAK consecutive empty hops fire,
 * or MAX_SPAN_MS from `startEpoch` is exceeded. Publishes intermediate
 * results after every fresh batch. Caller owns `pendingAbort`.
 *
 * `shouldEscalate` (when provided) is called with the current search
 * frontier — the point up to which we've searched, either the last fresh
 * result's anchor or the empty-hop query time — after every iteration.
 * When it returns true the cascade returns `'escalate'` so the caller can
 * redo the pipeline with a wider walking budget. Otherwise `'done'`. */
async function runHopCascade(
	dir: 1 | -1,
	startEpoch: number,
	pre: number,
	post: number,
	ac: AbortController,
	shouldEscalate?: (frontierMs: number) => boolean
): Promise<'done' | 'escalate'> {
	let queryEpoch = startEpoch;
	let emptyStreak = 0;
	while (results.length < resultTarget && !ac.signal.aborted) {
		if (Math.abs(queryEpoch - startEpoch) > MAX_SPAN_MS) break;
		if (emptyStreak >= MAX_EMPTY_STREAK) break;
		const hopTime = new Date(queryEpoch).toISOString();
		const res = await plan({
			from: from!, to: to!, mode, time: hopTime,
			currentCoord: resolvedCurrentCoord,
			maxPreTransitTime: pre,
			maxPostTransitTime: post,
			searchWindow: HOP_SEARCH_WINDOW_SEC
		}, ac.signal);
		if (ac.signal.aborted) return 'done';
		const items = [...(res.itineraries ?? []), ...(res.direct ?? [])];
		const fresh = items.filter((it) => {
			const fp = itineraryFingerprint(it);
			if (seenFingerprints.has(fp)) return false;
			seenFingerprints.add(fp);
			return true;
		});
		if (fresh.length === 0) {
			emptyStreak++;
			queryEpoch += dir * HOP_MS;
		} else {
			emptyStreak = 0;
			combined = [...combined, ...fresh];
			publishResults();
			const starts = fresh.map((i) => Date.parse(i.startTime));
			queryEpoch = (dir === 1 ? Math.max(...starts) : Math.min(...starts))
				+ dir * 60_000;
		}
		if (shouldEscalate?.(queryEpoch)) return 'escalate';
	}
	return 'done';
}

/** Length in seconds of the longest continuous slice of [startMs, endMs]
 * that fits entirely inside a single day's 06–21 local-time window. Used
 * to test whether a service gap contains ≥ SPARSE_GAP_THRESHOLD_SEC of
 * "daytime hours when service should be available". */
function maxDaytimeSliceSec(startMs: number, endMs: number): number {
	if (endMs <= startMs) return 0;
	const first = new Date(startMs);
	first.setHours(0, 0, 0, 0);
	let max = 0;
	for (let d = first.getTime(); d < endMs; d += 24 * 3600 * 1000) {
		const day = new Date(d);
		const dtStart = new Date(day.getFullYear(), day.getMonth(), day.getDate(), DAY_START_HOUR).getTime();
		const dtEnd = new Date(day.getFullYear(), day.getMonth(), day.getDate(), DAY_END_HOUR).getTime();
		const sliceStart = Math.max(startMs, dtStart);
		const sliceEnd = Math.min(endMs, dtEnd);
		if (sliceEnd > sliceStart) {
			const secs = (sliceEnd - sliceStart) / 1000;
			if (secs > max) max = secs;
		}
	}
	return max;
}

/** True when the timeline (query time + itinerary anchor times + current
 * cascade frontier) contains a consecutive gap whose daytime slice on any
 * single day reaches SPARSE_GAP_THRESHOLD_SEC. Signals that the narrow
 * walking radius reaches only sparse service and the wide radius should
 * be tried — the trigger fires from both real inter-result gaps and from
 * empty hops (the frontier advances past the last known result). */
function hasSparseServiceGap(
	its: Itinerary[],
	queryTimeMs: number,
	frontierMs: number,
	m: TimeMode
): boolean {
	const key = m === 'arrive' ? 'endTime' : 'startTime';
	const anchors = its.map((i) => Date.parse(i[key]));
	const timeline = [...new Set([queryTimeMs, frontierMs, ...anchors])]
		.sort((a, b) => a - b);
	for (let i = 0; i < timeline.length - 1; i++) {
		if (timeline[i + 1] - timeline[i] < SPARSE_GAP_THRESHOLD_SEC * 1000) continue;
		if (maxDaytimeSliceSec(timeline[i], timeline[i + 1]) >= SPARSE_GAP_THRESHOLD_SEC) {
			return true;
		}
	}
	return false;
}

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
	pushedEntry = false;
	expandedFingerprint = null;
	mapModeFlag = false;
}

/** Extend the result set in one chronological direction. Bumps
 * `resultTarget` by TARGET_RESULT_COUNT and hops until that many more
 * results survive pruning, empty-streak or MAX_SPAN fires. Called only
 * when an initial query has completed with at least one result — the
 * bumped target is naturally reset by resetCascadeState() when a fresh
 * runQuery starts. */
async function loadMoreInDirection(direction: 'earlier' | 'later') {
	if (loading || loadingMore) return;
	if (!from || !to || results.length === 0) return;
	abortInFlight();
	const ac = new AbortController();
	pendingAbort = ac;
	loadingMore = direction;
	resultTarget += TARGET_RESULT_COUNT;
	const dir: 1 | -1 = direction === 'later' ? 1 : -1;
	const starts = combined.map((i) => Date.parse(i.startTime));
	const extreme = dir === 1 ? Math.max(...starts) : Math.min(...starts);
	// leave-at query time is a departure with a forward window, so backward
	// hops need to shift a full window back to sit before the current range.
	// arrive-by query time is an arrival with a backward window, so a 60 s
	// nudge already moves into fresh territory.
	const startEpoch = dir === 1
		? extreme + 60_000
		: (mode === 'leave' ? extreme - HOP_MS : extreme - 60_000);
	try {
		await runHopCascade(dir, startEpoch, WIDE_PRE_POST_SEC, WIDE_PRE_POST_SEC, ac);
	} catch (e) {
		if ((e as Error).name !== 'AbortError') {
			error = e instanceof Error ? e.message : String(e);
		}
	} finally {
		if (pendingAbort === ac) pendingAbort = null;
		loadingMore = null;
	}
}

export const routingState = {
	get open() { return panelOpen; },
	get from() { return from; },
	get to() { return to; },
	get mode() { return mode; },
	get time() { return time; },
	get results() { return results; },
	get loading() { return loading; },
	get loadingMore() { return loadingMore; },
	get error() { return error; },
	get hasQueried() { return hasQueried; },
	get selectedItinerary() { return selectedItinerary; },
	get selectedFingerprint() { return selectedFingerprint; },
	get selectionInvalid() { return selectionInvalid; },
	get expandedFingerprint() { return expandedFingerprint; },
	// Effective only while a selection exists — the flag alone never
	// surfaces map mode on its own.
	get mapMode() { return mapModeFlag && selectedItinerary !== null; },

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
		pushedEntry = false;
		expandedFingerprint = null;
		mapModeFlag = false;
		abortInFlight();
		resetCascadeState();
		const url = currentUrl();
		writeRoutingQuery(url, {
			from: null, to: null, mode: 'leave', time: null, route: null
		});
		if (url.href !== window.location.href) {
			replaceState(url, { ...page.state, routeSelection: undefined });
		}
		from = null;
		to = null;
		mode = 'leave';
		time = null;
	},

	setFrom(ep: Endpoint | null) {
		abortInFlight();
		from = ep;
		results = [];
		hasQueried = false;
		error = null;
		invalidateSelection();
		syncUrl();
	},

	setTo(ep: Endpoint | null) {
		abortInFlight();
		to = ep;
		results = [];
		hasQueried = false;
		error = null;
		invalidateSelection();
		syncUrl();
	},

	setMode(m: TimeMode) {
		abortInFlight();
		mode = m;
		results = [];
		hasQueried = false;
		error = null;
		invalidateSelection();
		syncUrl();
	},

	setTime(t: string | null) {
		abortInFlight();
		time = t;
		results = [];
		hasQueried = false;
		error = null;
		invalidateSelection();
		syncUrl();
	},

	swap() {
		abortInFlight();
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
		const url = currentUrl();
		writeRoutingQuery(url, { from, to, mode, time, route: null });
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
	},

	exitMapMode() {
		mapModeFlag = false;
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
		pushedEntry = false;
		panelOpen = true;
	},

	async runQuery() {
		if (!from || !to) return;
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
					error = geolocationErrorMessage(e);
					loading = false;
					results = [];
					return;
				}
			}

			let pre = NARROW_PRE_POST_SEC;
			let post = NARROW_PRE_POST_SEC;

			const doQuery = async (timeArg: string | null, searchWindow?: number) => {
				return await plan({
					from: from!, to: to!, mode, time: timeArg,
					currentCoord: resolvedCurrentCoord,
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
			//   (c) — stage 3 discovers a ≥4 h daytime service gap; handled
			//        via the runHopCascade escalation return below.
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
			// Seed the dedupe set now that `combined` has stabilised for stages
			// 1 + 2 — stage 3 (and any later loadMore) then filters against it.
			seenFingerprints = new Set(combined.map(itineraryFingerprint));
			publishResults();

			// Stage 3 — time-advance cascade. MOTIS's nextPageCursor stalls
			// on remote destinations (returns 0 with an unchanged cursor
			// value even when later timetable entries exist), so we walk
			// forward by re-querying with `time` bumped past the last known
			// result. Dedupe by fingerprint; stop at TARGET_RESULT_COUNT,
			// MAX_SPAN_MS, or MAX_EMPTY_STREAK consecutive empty hops.
			const initialEpoch = time ? Date.parse(time) : Date.now();
			const advanceDir: 1 | -1 = mode === 'arrive' ? -1 : 1;
			const startEpochFrom = (its: Itinerary[]): number => {
				if (!its.length) return initialEpoch + advanceDir * HOP_MS;
				const starts = its.map((i) => Date.parse(i.startTime));
				return (mode === 'arrive' ? Math.min(...starts) : Math.max(...starts))
					+ advanceDir * 60_000;
			};
			// Only arm the sparse-gap escalation check while the narrow
			// budget is still in effect. If (a)/(b) already escalated to
			// wide above there is no wider budget to retry with.
			const shouldEscalate = pre === NARROW_PRE_POST_SEC
				? (frontier: number) => hasSparseServiceGap(combined, initialEpoch, frontier, mode)
				: undefined;
			const outcome = await runHopCascade(
				advanceDir, startEpochFrom(combined), pre, post, ac, shouldEscalate
			);
			if (ac.signal.aborted) return;

			// Stage 2c — sparse-service gap discovered mid-cascade. Redo the
			// full narrow flow (stage 1 + stage 3) with the wide walking
			// budget; the wider candidate set is not merge-comparable with
			// the narrow one.
			if (outcome === 'escalate') {
				pre = WIDE_PRE_POST_SEC;
				post = WIDE_PRE_POST_SEC;
				combined = [];
				seenFingerprints = new Set();
				const wideRes = await doQuery(time);
				if (ac.signal.aborted) return;
				combined = [...(wideRes.itineraries ?? []), ...(wideRes.direct ?? [])];
				seenFingerprints = new Set(combined.map(itineraryFingerprint));
				publishResults();
				await runHopCascade(advanceDir, startEpochFrom(combined), pre, post, ac);
				if (ac.signal.aborted) return;
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
			// Auto-select the most relevant result on a fresh query, so the
			// user sees a route on the map immediately without having to
			// click. For leave-at that's the first (earliest arrival); for
			// arrive-by the list sorts by departure ascending, so the most
			// relevant (latest departure) sits at the end. Skipped when the
			// cold-load restore is pending (matched above) or invalid
			// (concept: show the error, don't silently swap in a different
			// route).
			if (!selectedFingerprint && !selectionInvalid && results.length > 0) {
				const it = mode === 'arrive' ? results[results.length - 1] : results[0];
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
	},

	async loadMoreEarlier() {
		await loadMoreInDirection('earlier');
	},

	async loadMoreLater() {
		await loadMoreInDirection('later');
	}
};

export type RoutingState = typeof routingState;
