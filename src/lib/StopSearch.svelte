<script lang="ts">
	import type maplibregl from 'maplibre-gl';
	import { loadStationIndex } from '$lib/routing/stationIndex';
	import { indexStations, searchStations, type IndexedStation } from '$lib/routing/stationSearch';
	import { searchPlaces, type GeocodeResult } from '$lib/geocoding/client';
	import { AutocompleteScheduler } from '$lib/geocoding/scheduler';
	import { openStationPopup, openPlacePopup } from '$lib/map/popups/handlers';

	let { map }: { map: maplibregl.Map | null } = $props();

	const MAX_STATIONS = 8;
	const FLYTO_ZOOM = 16;
	// Geocoded hits: a POI / house number wants a close look; a named
	// place (village, suburb) is an area, so stay wider.
	const FLYTO_ZOOM_PLACE = 13;

	const MODE_ICON: Record<string, string> = {
		train:        'train',
		metro:        'subway',
		tram:         'tram',
		bus:          'directions_bus',
		regional_bus: 'directions_bus',
		ferry:        'directions_boat',
		mountain:     'gondola_lift',
	};
	const STATION_FALLBACK_ICON = 'directions_transit_filled';
	const POI_ICON = 'place';
	const ADDRESS_ICON = 'home_work';

	let index = $state<IndexedStation[]>([]);
	let indexError = $state<string | null>(null);
	let query = $state('');
	let open = $state(false);
	let highlighted = $state(0);
	let geoResults = $state<GeocodeResult[]>([]);
	let inputEl: HTMLInputElement | null = $state(null);

	// Same rate-limit + single-slot pending queue as the routing endpoint
	// inputs (geocoding-search.md § Rate limiting and request coalescing).
	const scheduler = new AutocompleteScheduler<GeocodeResult[]>({
		minIntervalMs: 100,
		fetcher: (q, signal) => searchPlaces(q, signal),
		onResult: (results, q) => {
			if (q === query.trim()) geoResults = results;
		}
	});

	$effect(() => {
		let cancelled = false;
		loadStationIndex().then((m) => {
			if (cancelled) return;
			if (!m) { indexError = 'index unavailable'; return; }
			index = indexStations(m.values());
		});
		return () => { cancelled = true; };
	});

	$effect(() => () => scheduler.dispose());

	// Below 2 chars, clear stale results and skip the network (matches the
	// proxy's own gate).
	$effect(() => {
		const q = query.trim();
		if (q.length < 2) {
			geoResults = [];
			return;
		}
		scheduler.request(q);
	});

	const mapCenter = $derived.by<[number, number] | null>(() => {
		void query;
		const c = map?.getCenter();
		return c ? [c.lng, c.lat] : null;
	});

	const stationResults = $derived(
		searchStations(index, query, MAX_STATIONS, mapCenter)
	);

	type Row =
		| { kind: 'station'; station: IndexedStation }
		| { kind: 'geo'; result: GeocodeResult };

	const rows = $derived<Row[]>([
		...stationResults.map((s) => ({ kind: 'station' as const, station: s })),
		...geoResults.map((r) => ({ kind: 'geo' as const, result: r }))
	]);

	$effect(() => {
		void rows;
		highlighted = 0;
	});

	function selectStation(e: IndexedStation) {
		if (!map) return;
		map.flyTo({ center: e.c, zoom: FLYTO_ZOOM, speed: 4.8, essential: true });
		// Popup opens once the camera has settled — the station popup's
		// content is read off the rendered stop feature (stop-search.md
		// § Selection).
		openStationPopup(map, { name: e.n, uic: e.u, coord: e.c });
		close();
	}

	function selectGeo(r: GeocodeResult) {
		if (!map) return;
		map.flyTo({
			center: r.coord,
			zoom: r.kind === 'place' ? FLYTO_ZOOM_PLACE : FLYTO_ZOOM,
			speed: 4.8,
			essential: true
		});
		openPlacePopup(map, r);
		close();
	}

	function selectRow(row: Row) {
		if (row.kind === 'station') selectStation(row.station);
		else selectGeo(row.result);
	}

	function close() {
		open = false;
		inputEl?.blur();
	}

	function onKey(e: KeyboardEvent) {
		if (!open && (e.key === 'ArrowDown' || e.key === 'Enter')) {
			open = true;
		}
		if (e.key === 'Enter') {
			e.preventDefault();
			const pick = rows[highlighted] ?? rows[0];
			if (pick) selectRow(pick);
			return;
		}
		if (e.key === 'Escape') {
			close();
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
	}
</script>

<div class="stop-search">
	<input
		bind:this={inputEl}
		type="search"
		autocomplete="off"
		placeholder="Search stops, places, addresses"
		bind:value={query}
		onfocus={(e) => {
			open = true;
			(e.currentTarget as HTMLInputElement).select();
		}}
		onblur={() => {
			setTimeout(() => { open = false; }, 120);
		}}
		onkeydown={onKey}
	/>
	{#if open && query.trim().length > 0}
		<ul class="results" role="listbox">
			{#if indexError && rows.length === 0}
				<li class="empty">Index unavailable</li>
			{:else if rows.length === 0}
				<li class="empty">No matches</li>
			{:else}
				{#each rows as row, i (row.kind === 'station' ? `s:${row.station.u}` : `g:${i}`)}
					{#if row.kind === 'geo' && i === stationResults.length && stationResults.length > 0}
						<li class="divider" aria-hidden="true"></li>
					{/if}
					<li
						class="result"
						class:highlighted={i === highlighted}
						role="option"
						aria-selected={i === highlighted}
						onmousedown={(e) => {
							e.preventDefault();
							selectRow(row);
						}}
						onmouseenter={() => (highlighted = i)}
					>
						<span class="mode-icon material-symbols-outlined" aria-hidden="true">
							{#if row.kind === 'station'}
								{(row.station.m && MODE_ICON[row.station.m]) || STATION_FALLBACK_ICON}
							{:else}
								{row.result.kind === 'address' ? ADDRESS_ICON : POI_ICON}
							{/if}
						</span>
						<span class="stop-name">
							{row.kind === 'station' ? row.station.n : row.result.displayName}
						</span>
					</li>
				{/each}
			{/if}
		</ul>
	{/if}
</div>

<style>
	.stop-search {
		position: relative;
		width: 18rem;
		font-family: var(--font-ui);
	}
	@media (max-width: 600px) {
		.stop-search {
			/* Shrink to fill whatever the parent .top-controls row leaves
			   after the view toggle, instead of overflowing the viewport. */
			width: auto;
			flex: 1 1 auto;
			min-width: 0;
		}
	}
	input {
		width: 100%;
		box-sizing: border-box;
		/* Same height as the menu toggle button so the top-controls row
		   aligns (MapMenu .menu-toggle is 2.1rem). */
		height: var(--control-size);
		padding: 0 calc(0.8rem - 2px);
		/* Permanent transparent border so the gradient focus ring can
		   appear without a layout shift (padding compensates). */
		border: 2px solid transparent;
		border-radius: var(--radius-pill);
		background: var(--white);
		box-shadow: var(--shadow-control);
		font-family: inherit;
		font-size: 0.85rem;
		line-height: 1.2;
		color: var(--gray-850);
		outline: none;
	}
	input:focus {
		background: linear-gradient(var(--white), var(--white)) padding-box, var(--gradient-brand-input) border-box;
	}
	.results {
		position: absolute;
		top: calc(100% + 0.3rem);
		left: 0;
		right: 0;
		margin: 0;
		padding: 0.25rem 0;
		list-style: none;
		background: var(--white);
		border-radius: 0.5rem;
		box-shadow: var(--shadow-popover);
		max-height: 60vh;
		overflow-y: auto;
		z-index: 10;
	}
	.result {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.35rem 0.7rem;
		font-size: 0.85rem;
		color: var(--gray-850);
		cursor: pointer;
	}
	.result.highlighted {
		background: var(--anthracite);
		color: var(--white);
	}
	.mode-icon {
		display: inline-block;
		width: 1.1rem;
		height: 1.1rem;
		font-size: 1.1rem;
		line-height: 1;
		color: var(--gray-500);
		flex: 0 0 auto;
	}
	.result.highlighted .mode-icon {
		color: var(--white);
	}
	.stop-name {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.empty {
		padding: 0.35rem 0.7rem;
		font-size: 0.85rem;
		color: var(--gray-400);
		font-style: italic;
	}
	.divider {
		height: 1px;
		margin: 0.2rem 0.7rem;
		background: var(--gray-200);
		list-style: none;
	}
</style>
