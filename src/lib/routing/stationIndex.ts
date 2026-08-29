// Shared loader for `stop_search_index.json` (baked by step 07). One fetch
// per session, reused by RoutingPanel search and by URL restore. Mirrors
// the pattern in Map.svelte's `loadLineIndex`.

export interface StationEntry {
	/** Display name */
	n: string;
	/** Merged UIC — the stable client-facing station key */
	u: string;
	/** MOTIS parent stop id ("Parentch:1:sloid:7000"); the routing
	 *  place id is "ch_" + p (client.ts formatPlace). Absent in
	 *  pre-SLOID index files — formatPlace falls back to the legacy
	 *  "Parent<uic>" shape. */
	p?: string;
	/** [lon, lat] — GTFS-derived station coord. Stable; used for search
	 *  distance-scoring and map fly-to on selection. Routing does NOT
	 *  send this coord — station endpoints go to MOTIS as stop IDs
	 *  (`ch_Parent<uic>`, see client.ts formatPlace), so the obsolete
	 *  `cw` walkable-coord workaround was removed. Older index files may
	 *  still carry a `cw` key; it is ignored. */
	c: [number, number];
	/** Highest-ranked mode */
	m?: string;
	/** Stop tier */
	t?: string;
	/** Dominant line color — the station's drawn dot color. Absent in
	 *  older index files. */
	cd?: string;
	/** Average color — mean-RGB over every distinct line color serving
	 *  the station, all modes. Absent in older index files. */
	ca?: string;
}

const INDEX_URL = '/map-assets/stop_search_index.json';

let promise: Promise<Map<string, StationEntry> | null> | null = null;

export function loadStationIndex(): Promise<Map<string, StationEntry> | null> {
	if (!promise) {
		promise = fetch(INDEX_URL)
			.then((r) => (r.ok ? r.json() as Promise<StationEntry[]> : Promise.reject(new Error(`HTTP ${r.status}`))))
			.then((entries) => {
				const map = new Map<string, StationEntry>();
				for (const e of entries) map.set(e.u, e);
				return map;
			})
			.catch(() => null);
	}
	return promise;
}
