import type { StationEntry } from './stationIndex';

// Station search shared by the routing endpoint inputs and by the map-wide
// search bar (StopSearch), so both UIs order the same stations the same way
// (stop-search.md § Ranking): weighted sum of a match tier, a mode-rank
// score, a stop-tier score and — when the caller passes a map center — a
// distance-decay score. The routing panel omits the center and simply
// drops that term.

export interface IndexedStation extends StationEntry {
	fold: string;
	words: string[];
}

export function fold(s: string): string {
	return s.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();
}

export function indexStations(entries: Iterable<StationEntry>): IndexedStation[] {
	const out: IndexedStation[] = [];
	for (const e of entries) {
		const f = fold(e.n);
		out.push({ ...e, fold: f, words: f.split(/[^\p{L}\p{N}]+/u).filter(Boolean) });
	}
	return out;
}

// Mirrors MODE_RANK in scripts/transit/_state.py.
const MODE_RANK: Record<string, number> = {
	train:        0,
	metro:        1,
	tram:         2,
	bus:          3,
	mountain:     4,
	ferry:        5,
	regional_bus: 6
};
const MODE_RANK_MAX = 6;

// Mirrors LABEL_TIER_RANK in scripts/transit/stops/pipeline_render.py.
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
	small_bus:       11
};
const STOP_TIER_RANK_MAX = 11;

const W_MATCH = 5;
const W_MODE = 1;
const W_TIER = 1;
const W_DISTANCE = 1;

// Distance decay characteristic length in km (100 * exp(-d / DIST_DECAY_KM)).
const DIST_DECAY_KM = 30;
const EARTH_KM = 6371;

// 8-tier match cascade — first condition that holds wins. Multi-word
// queries are token-based and order-insensitive; every token must match at
// least as a substring for the stop to be a hit at all.
function matchTierScore(name: string, words: string[], tokens: string[]): number {
	let allFull = true;
	let anyFull = false;
	let allPrefix = true;
	let anyPrefix = false;
	const fullMatched = new Set<number>();
	for (const t of tokens) {
		let strength = 0;
		for (let wi = 0; wi < words.length; wi++) {
			const w = words[wi];
			if (w === t) {
				strength = 3;
				fullMatched.add(wi);
				break;
			}
			if (strength < 2 && w.startsWith(t)) strength = 2;
		}
		if (strength < 2 && name.includes(t)) strength = 1;
		if (strength === 0) return 0;
		if (strength === 3) anyFull = true;
		else allFull = false;
		if (strength >= 2) anyPrefix = true;
		else allPrefix = false;
	}
	if (allFull && fullMatched.size === words.length) return 100;
	if (allPrefix && fullMatched.has(0)) return 80;
	if (words.length > 0 && tokens.some((t) => words[0].startsWith(t))) return 70;
	if (allFull) return 50;
	if (anyFull) return 40;
	if (allPrefix) return 30;
	if (anyPrefix) return 20;
	return 10;
}

function modeScore(mode: string | undefined): number {
	const r = MODE_RANK[mode ?? ''];
	if (r === undefined) return 0;
	return ((MODE_RANK_MAX - r) / MODE_RANK_MAX) * 100;
}

function tierScore(tier: string | undefined): number {
	const r = STOP_TIER_RANK[tier ?? ''];
	if (r === undefined) return 0;
	return ((STOP_TIER_RANK_MAX - r) / STOP_TIER_RANK_MAX) * 100;
}

function distanceScore(dLon: number, dLat: number, cosLat: number): number {
	const x = ((dLon * cosLat) * Math.PI) / 180;
	const y = (dLat * Math.PI) / 180;
	const distKm = EARTH_KM * Math.sqrt(x * x + y * y);
	return 100 * Math.exp(-distKm / DIST_DECAY_KM);
}

/** `center` is `[lon, lat]` of the current map view; when given, results
 * near the view are promoted (stop-search.md § Ranking / Distance). */
export function searchStations(
	index: IndexedStation[],
	query: string,
	limit = 8,
	center?: [number, number] | null
): IndexedStation[] {
	const q = fold(query.trim());
	if (!q) return [];
	const tokens = q.split(/\s+/).filter(Boolean);
	if (!tokens.length) return [];
	const cosLat = center ? Math.cos((center[1] * Math.PI) / 180) : 1;
	const scored: { e: IndexedStation; s: number }[] = [];
	for (const e of index) {
		const match = matchTierScore(e.fold, e.words, tokens);
		if (match === 0) continue;
		const dist = center
			? distanceScore(e.c[0] - center[0], e.c[1] - center[1], cosLat)
			: 0;
		const s = W_MATCH * match + W_MODE * modeScore(e.m)
			+ W_TIER * tierScore(e.t) + W_DISTANCE * dist;
		scored.push({ e, s });
	}
	scored.sort((a, b) => b.s - a.s);
	return scored.slice(0, limit).map((x) => x.e);
}
