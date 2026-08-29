import { browser } from '$app/environment';
import type { StationEntry } from './stationIndex';
import type { Endpoint } from './types';

// Connect tab data (routing-persistence.md § Connect): the user's most-used
// stations, localStorage-backed like recents, plus the cold-start suggestion
// logic that fills the grid while real usage is still thin.

const STORAGE_KEY = 'kora.connect.stations';
const MAX_ENTRIES = 30;
// Usage decay half-life-ish constant: an unused station's score halves in
// ~60 days (decay applied lazily whenever the station is used again).
const DECAY_DAYS = 90;

export interface ConnectStation {
	u: string;
	n: string;
	c: [number, number];
	m?: string;
	p?: string;
	/** Recency-decayed usage score. */
	score: number;
	/** Epoch ms of last use. */
	lastAt: number;
}

let entries = $state<ConnectStation[]>(browser ? readStorage() : []);

function readStorage(): ConnectStation[] {
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

function isValidEntry(e: unknown): e is ConnectStation {
	if (typeof e !== 'object' || e === null) return false;
	const r = e as Record<string, unknown>;
	return typeof r.u === 'string' && typeof r.n === 'string'
		&& Array.isArray(r.c) && r.c.length === 2
		&& typeof r.score === 'number' && typeof r.lastAt === 'number';
}

function writeStorage() {
	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
	} catch {
		// Storage unavailable — the in-memory list still works this session.
	}
}

export const connectStations = {
	/** Usage-ranked list, best first. */
	get list(): ConnectStation[] { return entries; },

	/** Bump a station's usage (called for each station endpoint of a shown
	 * route). Older usage decays so the ranking tracks current habits. */
	record(ep: Endpoint) {
		if (ep.type !== 'station') return;
		const now = Date.now();
		const prev = entries.find((e) => e.u === ep.uic);
		const decayed = prev
			? prev.score * Math.exp(-((now - prev.lastAt) / 86_400_000) / DECAY_DAYS)
			: 0;
		const next: ConnectStation = {
			u: ep.uic, n: ep.name, c: ep.coord, m: ep.mode, p: ep.pid,
			score: decayed + 1, lastAt: now
		};
		entries = [next, ...entries.filter((e) => e.u !== ep.uic)]
			.sort((a, b) => b.score - a.score)
			.slice(0, MAX_ENTRIES);
		writeStorage();
	}
};

// Mirrors LABEL_TIER_RANK in scripts/transit/stops/pipeline_render.py (and
// STOP_TIER_RANK in StopSearch.svelte). Lower = more important.
const STOP_TIER_RANK: Record<string, number> = {
	major_train:      0,
	main_train:       1,
	important_train:  2,
	train_station:    3,
	small_train:      4,
	major_mountain:   5,
	ferry_stop:       6,
	mountain_stop:    7,
	major_hub:        8,
	big_station:      9,
	normal_stop:     10,
	small_bus:       11,
};
const TIER_RANK_MAX = 11;
const TOP_TIER_COUNT = 4;

function tierRank(t: string | undefined): number {
	return STOP_TIER_RANK[t ?? ''] ?? TIER_RANK_MAX;
}

/** Equirectangular squared distance — ordering-only, no need for metres. */
function dist2(a: [number, number], b: [number, number]): number {
	const cosLat = Math.cos((a[1] * Math.PI) / 180);
	const dx = (a[0] - b[0]) * cosLat;
	const dy = a[1] - b[1];
	return dx * dx + dy * dy;
}

/** Cold-start suggestions (routing-persistence.md § Connect): the closest
 * station, the closest station of each tier above the closest one's tier,
 * and the TOP_TIER_COUNT closest highest-tier stations. Deduped (also
 * against `exclude`), sorted by distance, capped to `limit`. */
export function coldStartSuggestions(
	anchor: [number, number],
	index: Map<string, StationEntry>,
	exclude: Set<string>,
	limit: number
): StationEntry[] {
	if (limit <= 0) return [];
	const all = [...index.values()]
		.map((e) => ({ e, d: dist2(anchor, e.c) }))
		.sort((a, b) => a.d - b.d);
	if (all.length === 0) return [];

	const picked = new Map<string, { e: StationEntry; d: number }>();
	const closest = all[0];
	picked.set(closest.e.u, closest);

	// Closest station of each tier more important than the closest one's.
	const closestRank = tierRank(closest.e.t);
	for (let rank = 0; rank < closestRank; rank++) {
		const hit = all.find(({ e }) => tierRank(e.t) === rank);
		if (hit) picked.set(hit.e.u, hit);
	}

	// The N closest top-tier stations (big hubs are useful from anywhere).
	let topFound = 0;
	for (const hit of all) {
		if (topFound >= TOP_TIER_COUNT) break;
		if (tierRank(hit.e.t) !== 0) continue;
		picked.set(hit.e.u, hit);
		topFound++;
	}

	return [...picked.values()]
		.filter(({ e }) => !exclude.has(e.u))
		.sort((a, b) => a.d - b.d)
		.slice(0, limit)
		.map(({ e }) => e);
}
