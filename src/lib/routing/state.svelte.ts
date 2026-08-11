import { replaceState } from '$app/navigation';
import { page } from '$app/state';
import { plan } from './client';
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

let pendingAbort: AbortController | null = null;

function currentUrl(): URL {
	return new URL(window.location.href);
}

function syncUrl() {
	const url = currentUrl();
	writeRoutingQuery(url, { from, to, mode, time });
	if (url.href === window.location.href) return;
	// Preserve SvelteKit page state (line-detail marker etc.) — wiping it
	// would drop other views' history markers on every routing edit.
	replaceState(url, page.state);
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
		if (pendingAbort) { pendingAbort.abort(); pendingAbort = null; }
		const url = currentUrl();
		writeRoutingQuery(url, { from: null, to: null, mode: 'leave', time: null });
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
		syncUrl();
	},

	setTo(ep: Endpoint | null) {
		to = ep;
		results = [];
		hasQueried = false;
		error = null;
		syncUrl();
	},

	setMode(m: TimeMode) {
		mode = m;
		results = [];
		hasQueried = false;
		error = null;
		syncUrl();
	},

	setTime(t: string | null) {
		time = t;
		results = [];
		hasQueried = false;
		error = null;
		syncUrl();
	},

	swap() {
		const tmp = from;
		from = to;
		to = tmp;
		results = [];
		hasQueried = false;
		error = null;
		syncUrl();
	},

	/** Direct-write initial state from a URL restore. Doesn't re-serialise. */
	hydrate(next: {
		from: Endpoint | null; to: Endpoint | null;
		mode: TimeMode; time: string | null;
	}) {
		from = next.from;
		to = next.to;
		mode = next.mode;
		time = next.time;
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
			const res = await plan({ from, to, mode, time, currentCoord }, ac.signal);
			if (ac.signal.aborted) return;
			// Merge transit itineraries with MOTIS's `direct` walk-only
			// options — a walk that beats the fastest transit connection
			// (or fills a gap between departures) should surface in the
			// same list.
			//
			// Sort chronologically so the option that gets you there
			// soonest is first: by arrival time for `leave`, by departure
			// time (latest first) for `arrive`. This also correctly
			// promotes walking-heavy itineraries when they leave now and
			// arrive before any bus.
			const combined = [...(res.itineraries ?? []), ...(res.direct ?? [])];
			if (mode === 'arrive') {
				combined.sort((a, b) => Date.parse(b.startTime) - Date.parse(a.startTime));
			} else {
				combined.sort((a, b) => Date.parse(a.endTime) - Date.parse(b.endTime));
			}
			results = combined.slice(0, 5);
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
