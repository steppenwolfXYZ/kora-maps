import type { StationEntry } from './stationIndex';

// Compact station search shared by the routing endpoint inputs. Same input
// index as StopSearch; the ranking here is deliberately simpler than
// StopSearch's 8-tier cascade — good enough for the panel, easy to swap
// later if needed.

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

export function searchStations(index: IndexedStation[], query: string, limit = 8): IndexedStation[] {
	const q = fold(query.trim());
	if (!q) return [];
	const tokens = q.split(/\s+/).filter(Boolean);
	if (!tokens.length) return [];
	const scored: { e: IndexedStation; s: number }[] = [];
	for (const e of index) {
		let ok = true;
		let score = 0;
		for (const t of tokens) {
			let ts = 0;
			for (const w of e.words) {
				if (w === t) { ts = 3; break; }
				if (w.startsWith(t)) { ts = Math.max(ts, 2); }
			}
			if (ts === 0 && e.fold.includes(t)) ts = 1;
			if (ts === 0) { ok = false; break; }
			score += ts;
		}
		if (!ok) continue;
		if (e.words[0]?.startsWith(tokens[0])) score += 5;
		scored.push({ e, s: score });
	}
	scored.sort((a, b) => b.s - a.s);
	return scored.slice(0, limit).map((x) => x.e);
}
