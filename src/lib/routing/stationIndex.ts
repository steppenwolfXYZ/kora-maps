// Shared loader for `stop_search_index.json` (baked by step 07). One fetch
// per session, reused by RoutingPanel search and by URL restore. Mirrors
// the pattern in Map.svelte's `loadLineIndex`.

export interface StationEntry {
	/** Display name */
	n: string;
	/** Merged UIC */
	u: string;
	/** [lon, lat] — GTFS-derived station coord. Stable; used for search
	 *  distance-scoring and map fly-to on selection. */
	c: [number, number];
	/** [lon, lat] — walkable coord (nearest OSM platform centroid), only
	 *  present when the pipeline snap found one. Routing endpoints should
	 *  send `cw ?? c` to MOTIS so the pedestrian router doesn't start on
	 *  a road with `sidewalk=separate`. See transit-routing.md
	 *  § Endpoint inputs. */
	cw?: [number, number];
	/** Highest-ranked mode */
	m?: string;
	/** Stop tier */
	t?: string;
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
