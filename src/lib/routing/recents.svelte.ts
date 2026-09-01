import { browser } from '$app/environment';
import { MAX_VIAS, MAX_VIA_WAIT_MIN, type Endpoint, type FilledVia, type TimeMode } from './types';
import { endpointToParam } from './url';

// Recent routes (routing-persistence.md § Recent routes list). localStorage-
// backed, capped, deduped by from/to pair — showing an already-listed pair
// moves it to the top and refreshes its time/mode. Storage failures (private
// mode, blocked storage) degrade to an empty, non-persisting list.

const STORAGE_KEY = 'kora.routing.recents';
const MAX_ENTRIES = 30;

export interface RecentRoute {
	from: Endpoint;
	to: Endpoint;
	/** Via stops of the recorded route (via-stops.md). Absent on entries
	 * stored before vias existed. */
	vias?: FilledVia[];
	mode: TimeMode;
	/** ISO-8601 timestamp of the query, `null` = "now". */
	time: string | null;
	/** Epoch ms of when the route was last shown. */
	at: number;
}

let entries = $state<RecentRoute[]>(browser ? readStorage() : []);

function readStorage(): RecentRoute[] {
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (!raw) return [];
		const parsed = JSON.parse(raw);
		if (!Array.isArray(parsed)) return [];
		return parsed.filter(isValidEntry).slice(0, MAX_ENTRIES);
	} catch {
		return [];
	}
}

function isValidEntry(e: unknown): e is RecentRoute {
	if (typeof e !== 'object' || e === null) return false;
	const r = e as Record<string, unknown>;
	return isValidEndpoint(r.from) && isValidEndpoint(r.to)
		&& (r.vias === undefined || isValidViaList(r.vias))
		&& (r.mode === 'leave' || r.mode === 'arrive')
		&& (r.time === null || typeof r.time === 'string')
		&& typeof r.at === 'number';
}

function isValidViaList(v: unknown): v is FilledVia[] {
	if (!Array.isArray(v) || v.length > MAX_VIAS) return false;
	return v.every((e) => {
		if (typeof e !== 'object' || e === null) return false;
		const row = e as Record<string, unknown>;
		const st = row.station as Record<string, unknown> | null;
		if (!st || st.type !== 'station' || typeof st.uic !== 'string') return false;
		return typeof row.wait === 'number'
			&& row.wait >= 0 && row.wait <= MAX_VIA_WAIT_MIN;
	});
}

function isValidEndpoint(e: unknown): e is Endpoint {
	if (typeof e !== 'object' || e === null) return false;
	const ep = e as Record<string, unknown>;
	// 'current' endpoints are not accepted: new entries materialize them
	// into point endpoints at record time (state.svelte.ts), and legacy
	// stored entries can't reproduce their result — this filter hides
	// them on read.
	if (ep.type === 'station') return typeof ep.uic === 'string' && typeof ep.name === 'string';
	if (ep.type === 'point') return Array.isArray(ep.coord) && ep.coord.length === 2;
	return false;
}

function writeStorage() {
	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
	} catch {
		// Storage unavailable — the in-memory list still works this session.
	}
}

/** Identity of a recent entry. The via chain and its waits are part of it
 * (via-stops.md § Persistence and sharing) — two otherwise identical
 * routes through different stops are different routes. */
function pairKey(from: Endpoint, to: Endpoint, vias?: FilledVia[]): string {
	const mid = (vias ?? []).map((v) => `${v.station.uic}:${v.wait}`).join(',');
	return `${endpointToParam(from)}>${mid}>${endpointToParam(to)}`;
}

export const recentRoutes = {
	get list(): RecentRoute[] { return entries; },

	/** Record a shown route (called when a query returns results). Moves an
	 * existing from/to pair to the top and refreshes its time/mode. */
	record(
		from: Endpoint, to: Endpoint, vias: FilledVia[],
		mode: TimeMode, time: string | null
	) {
		const key = pairKey(from, to, vias);
		const rest = entries.filter((e) => pairKey(e.from, e.to, e.vias) !== key);
		const entry: RecentRoute = { from, to, mode, time, at: Date.now() };
		if (vias.length > 0) entry.vias = vias;
		entries = [entry, ...rest].slice(0, MAX_ENTRIES);
		writeStorage();
	}
};

/** Short display label for an endpoint — mirrors EndpointInput's labelFor. */
export function endpointLabel(ep: Endpoint): string {
	if (ep.type === 'current') return 'Current location';
	if (ep.type === 'point') {
		return ep.displayName ?? `${ep.coord[1].toFixed(4)}, ${ep.coord[0].toFixed(4)}`;
	}
	return ep.name || ep.uic;
}
