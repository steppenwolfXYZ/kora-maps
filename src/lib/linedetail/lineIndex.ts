// line_index.json access + ?line= deep-link handling for the
// line-detail view (line-detail-view.md § Deep link). The URL carries
// the selection as `?line=<key1>[,<key2>...]`. Multiple keys occur when
// a popup badge merges same-(ref, mode) lines across agencies. The keys
// resolve against `/map-assets/line_index.json` (baked by step 07:
// pipeline_setup.py § "write OUT_LINE_INDEX"). replaceState is used so
// per-interaction updates don't pollute browser history.

import { replaceState } from '$app/navigation';

// Entered by clicking a line badge in the station or line popup. "The
// line" is all variants of its (ref, agency_id, mode) group: `keys` are
// the canonical line keys baked into badge entries / line features,
// `bbox` the group's union bbox for the camera fit.
export interface LineDetailSelection {
	keys: string[];
	bbox: [number, number, number, number];
	ref: string;
	mode: string;
	color: string;
	route: string;
}

export interface LineServiceVariant {
	route: string;
	/** 7-char Mo..Su mask, '1' = served */
	days: string;
	/** Average first/last departure of both ends, seconds from midnight
	 * (may exceed 24 h) */
	dep?: [number, number];
	/** Runs per active day (busiest direction) — departures on a day
	 * the line actually runs */
	rpd: number;
	/** Irregular departure pattern (e.g. peak-only service) */
	irr?: boolean;
	/** ISO operating period — present only when the line is seasonal */
	from?: string;
	to?: string;
}

export interface LineServiceInfo {
	days: string;
	dep?: [number, number];
	rpd: number;
	irr?: boolean;
	from?: string;
	to?: string;
	/** One row per distinct terminus pair, busiest first */
	variants: LineServiceVariant[];
}

export interface LineIndexEntry {
	ref: string;
	mode: string;
	color: string;
	bbox: [number, number, number, number];
	route: string;
	service?: LineServiceInfo;
}

export const URL_LINE_PARAM = 'line';
const LINE_INDEX_URL = '/map-assets/line_index.json';

// line_index.json is fetched once per session and shared between the
// deep-link resolver and the service summary in the title bar.
let lineIndexPromise: Promise<Record<string, LineIndexEntry> | null> | null = null;
export function loadLineIndex(): Promise<Record<string, LineIndexEntry> | null> {
	if (!lineIndexPromise) {
		lineIndexPromise = fetch(LINE_INDEX_URL)
			.then((res) => (res.ok ? res.json() : null))
			.catch(() => null);
	}
	return lineIndexPromise;
}

export function readLineDeepLinkFromUrl(): string[] | null {
	if (typeof window === 'undefined') return null;
	const params = new URLSearchParams(window.location.search);
	const raw = params.get(URL_LINE_PARAM);
	if (!raw) return null;
	const keys = raw.split(',').map((s) => s.trim()).filter(Boolean);
	return keys.length ? keys : null;
}

/** Remove the ?line param in place — used when the view closes without
 * a history record to pop (deep-link entry) or when deep-link
 * resolution fails. The empty state also drops any `lineDetail`
 * marker from page.state. */
export function clearLineDeepLinkFromUrl() {
	if (typeof window === 'undefined') return;
	const url = new URL(window.location.href);
	url.searchParams.delete(URL_LINE_PARAM);
	// SvelteKit's replaceState, not window.history.replaceState — the
	// raw call wipes the router's history state and trips its dev
	// warning.
	replaceState(url, {});
}

export async function resolveLineDeepLink(keys: string[]): Promise<LineDetailSelection | null> {
	try {
		const index = await loadLineIndex();
		if (!index) return null;
		const resolved: LineIndexEntry[] = [];
		const resolvedKeys: string[] = [];
		for (const k of keys) {
			const e = index[k];
			if (e && Array.isArray(e.bbox) && e.bbox.length === 4
			    && e.bbox.every((v) => Number.isFinite(v))) {
				resolved.push(e);
				resolvedKeys.push(k);
			}
		}
		if (!resolved.length) return null;
		let bb: [number, number, number, number] =
			[resolved[0].bbox[0], resolved[0].bbox[1],
			 resolved[0].bbox[2], resolved[0].bbox[3]];
		for (let i = 1; i < resolved.length; i++) {
			const b = resolved[i].bbox;
			bb = [Math.min(bb[0], b[0]), Math.min(bb[1], b[1]),
			      Math.max(bb[2], b[2]), Math.max(bb[3], b[3])];
		}
		const first = resolved[0];
		return {
			keys: resolvedKeys,
			bbox: bb,
			ref: first.ref,
			mode: first.mode,
			color: first.color,
			route: first.route,
		};
	} catch {
		return null;
	}
}

/** Merge the service blocks of a multi-key selection (same ref+mode
 * across agencies): busiest entry carries the headline cadence, span
 * and season, days union, variant rows concatenate. */
export function mergeServiceInfo(infos: LineServiceInfo[]): LineServiceInfo | null {
	if (!infos.length) return null;
	if (infos.length === 1) return infos[0];
	const busiest = infos.reduce((a, b) => (b.rpd > a.rpd ? b : a));
	const days = Array.from({ length: 7 }, (_, i) =>
		infos.some((s) => s.days[i] === '1') ? '1' : '0').join('');
	return {
		...busiest,
		days,
		variants: infos.flatMap((s) => s.variants)
			.sort((a, b) => b.rpd - a.rpd)
	};
}
