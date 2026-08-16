<script lang="ts">
	import type { Endpoint } from './types';
	import { indexStations, searchStations, type IndexedStation } from './stationSearch';
	import { loadStationIndex } from './stationIndex';
	import { hasGeolocation } from './geolocation';
	import { searchPlaces, type GeocodeResult } from '$lib/geocoding/client';
	import { AutocompleteScheduler } from '$lib/geocoding/scheduler';

	// Icons per row kind. Stations use a per-mode transit icon so they never
	// look the same as a POI (both used `place` before, which made the merged
	// dropdown hard to scan). Kept in sync with StopSearch's MODE_ICON so the
	// two search UIs read the same.
	const STATION_MODE_ICON: Record<string, string> = {
		train:        'train',
		metro:        'subway',
		tram:         'tram',
		bus:          'directions_bus',
		regional_bus: 'directions_bus',
		ferry:        'directions_boat',
		mountain:     'gondola_lift'
	};
	const STATION_FALLBACK_ICON = 'directions_transit_filled';
	const POI_ICON = 'place';
	const ADDRESS_ICON = 'home_work';

	function stationIcon(s: IndexedStation): string {
		return (s.m && STATION_MODE_ICON[s.m]) || STATION_FALLBACK_ICON;
	}

	// One side of the routing panel's From / To pair. Shows the current
	// endpoint label; focusing turns the row into a search input whose
	// dropdown lists "Current location" (when available) as the first
	// suggestion, then transit-station matches from the local index, then
	// Photon geocoding matches (addresses + POIs) — see geocoding-search.md.

	interface Props {
		label: string;
		endpoint: Endpoint | null;
		placeholder: string;
		onChange: (ep: Endpoint | null) => void;
	}

	let { label, endpoint, placeholder, onChange }: Props = $props();

	let index = $state<IndexedStation[]>([]);
	let query = $state('');
	let editing = $state(false);
	let highlighted = $state(0);
	let inputEl: HTMLInputElement | null = $state(null);
	let rowEl: HTMLDivElement | null = $state(null);
	let menuStyle = $state('');
	let geoResults = $state<GeocodeResult[]>([]);
	const geoAvailable = hasGeolocation();

	// Scheduler owns the rate-limit + single-slot pending queue per
	// geocoding-search.md § Rate limiting and request coalescing. Reused
	// across every keystroke on this input; disposed on component teardown.
	const scheduler = new AutocompleteScheduler<GeocodeResult[]>({
		minIntervalMs: 100,
		fetcher: (q, signal) => searchPlaces(q, signal),
		onResult: (results, q) => {
			// Only apply if the current input still matches (guards against a
			// racy stale delivery slipping through).
			if (q === query.trim()) geoResults = results;
		}
	});

	$effect(() => {
		let cancelled = false;
		loadStationIndex().then((m) => {
			if (cancelled || !m) return;
			index = indexStations(m.values());
		});
		return () => { cancelled = true; };
	});

	$effect(() => () => scheduler.dispose());

	// Menu positioning: the panel has `overflow: hidden` for its results-
	// scroll container, which would clip an absolutely-positioned dropdown.
	// We anchor the menu with `position: fixed`, computing its rect from the
	// row's bounding box, and update on window resize/scroll so it stays
	// pinned when the page scrolls.
	function updateMenuPos() {
		if (!rowEl) return;
		const r = rowEl.getBoundingClientRect();
		menuStyle = `left:${r.left}px; top:${r.bottom + 4}px; width:${r.width}px;`;
	}

	$effect(() => {
		if (!editing) return;
		updateMenuPos();
		const handler = () => updateMenuPos();
		window.addEventListener('resize', handler);
		// Capture phase so we get scroll events from any scrolling ancestor,
		// not just the window.
		window.addEventListener('scroll', handler, true);
		return () => {
			window.removeEventListener('resize', handler);
			window.removeEventListener('scroll', handler, true);
		};
	});

	const stationResults = $derived(searchStations(index, query));

	// Fire the geocoding request when query changes. Below 2 chars, clear
	// stale results and skip the network (matches the proxy's own gate).
	$effect(() => {
		const q = query.trim();
		if (q.length < 2) {
			geoResults = [];
			return;
		}
		scheduler.request(q);
	});

	function formatCoord(c: [number, number]): string {
		return `${c[1].toFixed(4)}, ${c[0].toFixed(4)}`;
	}

	function labelFor(ep: Endpoint | null): string {
		if (!ep) return '';
		if (ep.type === 'current') return 'Current location';
		if (ep.type === 'point') return ep.displayName ?? formatCoord(ep.coord);
		return ep.name || ep.uic;
	}

	function endpointIcon(ep: Endpoint): string {
		if (ep.type === 'current') return 'my_location';
		if (ep.type === 'point') return ep.kind === 'poi' ? POI_ICON : ADDRESS_ICON;
		return (ep.mode && STATION_MODE_ICON[ep.mode]) || STATION_FALLBACK_ICON;
	}

	function startEdit() {
		editing = true;
		query = '';
		highlighted = 0;
		queueMicrotask(() => inputEl?.focus());
	}

	function commit(ep: Endpoint | null) {
		editing = false;
		query = '';
		geoResults = [];
		onChange(ep);
	}

	function pickStation(e: IndexedStation) {
		// Prefer the walkable-platform-snapped coord for routing (avoids
		// MOTIS's OSR starting the walker on a `sidewalk=separate` road);
		// fall back to the GTFS-derived coord when no snap was baked.
		// See transit-routing.md § Endpoint inputs.
		commit({ type: 'station', uic: e.u, name: e.n, coord: e.cw ?? e.c, mode: e.m });
	}

	function pickGeo(r: GeocodeResult) {
		// GeocodeResult.kind is one of 'address' | 'poi' | 'place'; the
		// endpoint icon only distinguishes address vs POI, so 'place'
		// (villages, hamlets — landmark-like) rides with 'poi'.
		const kind: 'address' | 'poi' = r.kind === 'address' ? 'address' : 'poi';
		commit({ type: 'point', coord: r.coord, displayName: r.displayName, kind });
	}

	function pickCurrent() {
		commit({ type: 'current' });
	}

	function clear() {
		commit(null);
		startEdit();
	}

	function onBlur() {
		// Delay so click on a dropdown row lands before we tear down.
		setTimeout(() => { editing = false; query = ''; geoResults = []; }, 120);
	}

	// "Current location" is only offered when the user hasn't started
	// typing — once there's a query, only search matches belong in the
	// dropdown.
	const showCurrent = $derived(geoAvailable && endpoint?.type !== 'current' && !query.trim());

	type Row =
		| { kind: 'current' }
		| { kind: 'station'; station: IndexedStation }
		| { kind: 'geo'; result: GeocodeResult };

	const rows = $derived<Row[]>([
		...(showCurrent ? [{ kind: 'current' } as Row] : []),
		...stationResults.map((s) => ({ kind: 'station' as const, station: s })),
		...geoResults.map((r) => ({ kind: 'geo' as const, result: r }))
	]);

	// Index at which the geo section starts (used for a divider above it).
	const geoStartIdx = $derived(
		(showCurrent ? 1 : 0) + stationResults.length
	);

	function pickRow(row: Row) {
		if (row.kind === 'current') pickCurrent();
		else if (row.kind === 'station') pickStation(row.station);
		else pickGeo(row.result);
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			editing = false;
			query = '';
			geoResults = [];
			inputEl?.blur();
			return;
		}
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			highlighted = Math.min(highlighted + 1, rows.length - 1);
			return;
		}
		if (e.key === 'ArrowUp') {
			e.preventDefault();
			highlighted = Math.max(highlighted - 1, 0);
			return;
		}
		if (e.key === 'Enter') {
			e.preventDefault();
			const pick = rows[highlighted] ?? rows[0];
			if (pick) pickRow(pick);
			return;
		}
	}

	// Whenever the row set changes underneath us, clamp the highlight so we
	// don't end up pointing past the end of the list (e.g. results narrowed
	// after a keystroke).
	$effect(() => {
		if (highlighted >= rows.length) highlighted = Math.max(0, rows.length - 1);
	});
</script>

<div class="ep-row" bind:this={rowEl}>
	<span class="ep-label">{label}</span>
	{#if editing || !endpoint}
		<input
			bind:this={inputEl}
			class="ep-input"
			type="search"
			autocomplete="off"
			bind:value={query}
			{placeholder}
			onfocus={() => { editing = true; }}
			onblur={onBlur}
			onkeydown={onKey}
		/>
		{#if editing}
			<ul class="ep-menu" role="listbox" style={menuStyle}>
				{#each rows as row, i (row.kind === 'current' ? 'c' : row.kind === 'station' ? `s:${row.station.u}` : `g:${i}`)}
					{#if row.kind === 'geo' && i === geoStartIdx && geoStartIdx > 0}
						<li class="ep-divider" aria-hidden="true"></li>
					{/if}
					{#if row.kind === 'current'}
						<li
							class="ep-row-item ep-row-current"
							class:highlighted={highlighted === i}
							role="option"
							aria-selected={highlighted === i}
							onmousedown={(e) => { e.preventDefault(); pickCurrent(); }}
							onmouseenter={() => (highlighted = i)}
						>
							<span class="ep-icon material-symbols-outlined">my_location</span>
							<span class="ep-text">Current location</span>
						</li>
					{:else if row.kind === 'station'}
						<li
							class="ep-row-item"
							class:highlighted={highlighted === i}
							role="option"
							aria-selected={highlighted === i}
							onmousedown={(e) => { e.preventDefault(); pickStation(row.station); }}
							onmouseenter={() => (highlighted = i)}
						>
							<span class="ep-icon material-symbols-outlined" aria-hidden="true">{stationIcon(row.station)}</span>
							<span class="ep-text">{row.station.n}</span>
						</li>
					{:else}
						<li
							class="ep-row-item"
							class:highlighted={highlighted === i}
							role="option"
							aria-selected={highlighted === i}
							onmousedown={(e) => { e.preventDefault(); pickGeo(row.result); }}
							onmouseenter={() => (highlighted = i)}
						>
							<span class="ep-icon material-symbols-outlined" aria-hidden="true">
								{row.result.kind === 'address' ? ADDRESS_ICON : POI_ICON}
							</span>
							<span class="ep-text">{row.result.displayName}</span>
						</li>
					{/if}
				{/each}
				{#if rows.length === 0 && query.trim()}
					<li class="ep-empty">No matches</li>
				{/if}
			</ul>
		{/if}
	{:else}
		<button class="ep-value" onclick={startEdit} aria-label="Change {label.toLowerCase()}">
			<span class="ep-icon material-symbols-outlined" aria-hidden="true">
				{endpointIcon(endpoint)}
			</span>
			<span class="ep-text">{labelFor(endpoint)}</span>
		</button>
		<button class="ep-clear" onclick={clear} aria-label="Clear {label.toLowerCase()}">×</button>
	{/if}
</div>

<style>
	.ep-row {
		position: relative;
		display: flex;
		align-items: center;
		gap: 0.4rem;
		padding: 0.35rem 0.5rem;
		background: #f5f5f5;
		border-radius: 0.55rem;
		font-family: 'Saira', 'Helvetica Neue', Arial, sans-serif;
	}

	.ep-label {
		flex: 0 0 auto;
		font-size: 0.7rem;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: #999;
		width: 2.1rem;
	}

	.ep-input {
		flex: 1 1 auto;
		border: none;
		background: transparent;
		font-family: inherit;
		font-size: 0.9rem;
		color: #222;
		outline: none;
		padding: 0.15rem 0;
		min-width: 0;
	}

	.ep-value {
		flex: 1 1 auto;
		display: flex;
		align-items: center;
		gap: 0.35rem;
		border: none;
		background: transparent;
		text-align: left;
		font-family: inherit;
		font-size: 0.9rem;
		color: #222;
		cursor: pointer;
		padding: 0.15rem 0;
		min-width: 0;
	}

	.ep-clear {
		flex: 0 0 auto;
		border: none;
		background: transparent;
		color: #888;
		font-size: 1.1rem;
		line-height: 1;
		padding: 0.15rem 0.3rem;
		border-radius: 999px;
		cursor: pointer;
	}
	.ep-clear:hover { background: #eee; color: #000; }

	.ep-menu {
		/* Fixed positioning escapes the routing panel's `overflow: hidden`.
		   Coordinates come from an inline `style` attribute computed in the
		   component from the row's bounding rect (updated on resize/scroll). */
		position: fixed;
		margin: 0;
		padding: 0.25rem 0;
		list-style: none;
		background: #ffffff;
		border-radius: 0.55rem;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
		max-height: 40vh;
		overflow-y: auto;
		z-index: 30;
	}

	.ep-row-item {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.35rem 0.7rem;
		font-size: 0.9rem;
		color: #222;
		cursor: pointer;
	}
	.ep-row-item.highlighted { background: #333; color: #fff; }
	.ep-row-item.highlighted .ep-icon { color: #fff; }

	.ep-icon {
		width: 1.1rem;
		height: 1.1rem;
		font-size: 1.1rem;
		line-height: 1;
		color: #666;
		flex: 0 0 auto;
	}

	.ep-text {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.ep-empty {
		padding: 0.35rem 0.7rem;
		font-size: 0.85rem;
		color: #888;
		font-style: italic;
	}

	.ep-divider {
		height: 1px;
		margin: 0.2rem 0.7rem;
		background: #e2e2e2;
		list-style: none;
	}
</style>
