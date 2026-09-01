<script lang="ts">
	import type { Endpoint } from './types';
	import ViaWaitSelect from './ViaWaitSelect.svelte';
	import { indexStations, searchStations, type IndexedStation } from './stationSearch';
	import { loadStationIndex } from './stationIndex';
	import { geolocationDenied, hasGeolocation } from './geolocation.svelte';
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
		/** True when the opposite endpoint is already "Current location" —
		 * suppresses the suggestion on this side (a current→current route is
		 * pointless). */
		otherIsCurrent?: boolean;
		/** Shown as a refresh button while the endpoint is "Current
		 * location" — re-resolves the position (and re-queries). */
		onRefreshCurrent?: () => void;
		/** Via row (via-stops.md): only stations are offered — the routing
		 * engine takes stop ids for vias, never coordinates — the clear
		 * control removes the whole row, and the wait control renders. */
		via?: boolean;
		/** Requested minimum stay in minutes; via rows only. */
		wait?: number;
		onWait?: (minutes: number) => void;
		/** "Insert a stop after this row" — rendered as a `+` at the row's
		 * right end whenever the row carries a value and another via still
		 * fits. */
		onAddAfter?: () => void;
	}

	let {
		label, endpoint, placeholder, onChange,
		otherIsCurrent = false, onRefreshCurrent,
		via = false, wait = 0, onWait, onAddAfter
	}: Props = $props();

	let index = $state<IndexedStation[]>([]);
	let query = $state('');
	let editing = $state(false);
	// Dropdown visibility, separate from `editing`: a programmatic focus
	// (panel open-time cursor placement) keeps the input focused but the
	// menu closed so it doesn't cover the other endpoint row — it opens on
	// click into the field or on typing.
	let menuOpen = $state(false);
	let suppressMenuOnFocus = false;
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
		if (!editing || !menuOpen) return;
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
		// Via rows are station-only, so the geocoder is never asked.
		if (via || q.length < 2) {
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
		menuOpen = true;
		query = '';
		highlighted = 0;
		queueMicrotask(() => inputEl?.focus());
	}

	/** Exit search mode without committing — the row shows its endpoint
	 * value again. Used by the Connect board, whose drag fills endpoints
	 * from outside while this input may still sit in its (empty) search
	 * form and would otherwise hide the fresh value. */
	export function stopEdit() {
		editing = false;
		menuOpen = false;
		query = '';
		geoResults = [];
		inputEl?.blur();
	}

	/** Programmatic focus for the panel's open-time cursor placement —
	 * focuses the input without opening the dropdown. */
	export function focusSearch() {
		suppressMenuOnFocus = true;
		editing = true;
		menuOpen = false;
		query = '';
		highlighted = 0;
		queueMicrotask(() => inputEl?.focus());
	}

	function commit(ep: Endpoint | null) {
		editing = false;
		menuOpen = false;
		query = '';
		geoResults = [];
		onChange(ep);
	}

	function pickStation(e: IndexedStation) {
		// Prefer the walkable-platform-snapped coord for routing (avoids
		// MOTIS's OSR starting the walker on a `sidewalk=separate` road);
		// fall back to the GTFS-derived coord when no snap was baked.
		// See transit-routing.md § Endpoint inputs.
		commit({ type: 'station', uic: e.u, name: e.n, coord: e.c, mode: e.m, pid: e.p });
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
		if (via) {
			// A via row's clear removes the row itself (via-stops.md
			// § Panel UI) — there is nothing left to type into.
			editing = false;
			menuOpen = false;
			query = '';
			geoResults = [];
			onChange(null);
			return;
		}
		commit(null);
		startEdit();
	}

	function onBlur() {
		// Delay so click on a dropdown row lands before we tear down.
		setTimeout(() => { editing = false; menuOpen = false; query = ''; geoResults = []; }, 120);
	}

	function onFocus() {
		editing = true;
		// A programmatic focus keeps the menu closed (see focusSearch).
		if (suppressMenuOnFocus) suppressMenuOnFocus = false;
		else menuOpen = true;
	}

	// "Current location" is only offered when the user hasn't started
	// typing — once there's a query, only search matches belong in the
	// dropdown. Hidden once the permission has been denied, and when the
	// other side already uses it. Still offered while this side itself has
	// it set, so re-picking it is possible after clicking into the field.
	const showCurrent = $derived(
		!via && geoAvailable && !geolocationDenied() && !otherIsCurrent && !query.trim()
	);

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
			menuOpen = false;
			query = '';
			geoResults = [];
			inputEl?.blur();
			return;
		}
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			if (!menuOpen) {
				menuOpen = true;
				return;
			}
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
			onfocus={onFocus}
			onmousedown={() => { if (editing) menuOpen = true; }}
			oninput={() => { menuOpen = true; }}
			onblur={onBlur}
			onkeydown={onKey}
		/>
		{#if editing && menuOpen}
			<ul class="ep-menu" role="listbox" style={menuStyle}>
				{#if !query.trim()}
					<li class="ep-hint">Start typing to search stations or locations</li>
				{/if}
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
		{#if via}
			<!-- An unfilled via row still needs a way out: its × drops the
			     row, the same as on a filled one. -->
			<button
				class="ep-clear icon-btn"
				onmousedown={(e) => { e.preventDefault(); onChange(null); }}
				aria-label="Remove this stop"
			>×</button>
		{/if}
	{:else}
		<button class="ep-value" onclick={startEdit} aria-label="Change {label.toLowerCase()}">
			<span class="ep-icon material-symbols-outlined" aria-hidden="true">
				{endpointIcon(endpoint)}
			</span>
			<span class="ep-text">{labelFor(endpoint)}</span>
		</button>
		{#if endpoint.type === 'current' && onRefreshCurrent}
			<button
				class="ep-refresh icon-btn"
				onclick={onRefreshCurrent}
				aria-label="Update current location"
			>
				<span class="material-symbols-outlined">refresh</span>
			</button>
		{/if}
		{#if via && onWait}
			<ViaWaitSelect
				{wait}
				onChange={onWait}
				stationName={labelFor(endpoint)}
			/>
		{/if}
		<button
			class="ep-clear icon-btn"
			onclick={clear}
			aria-label={via ? `Remove via ${labelFor(endpoint)}` : `Clear ${label.toLowerCase()}`}
		>×</button>
		{#if onAddAfter}
			<button
				class="ep-add icon-btn"
				onclick={onAddAfter}
				aria-label="Add a stop after {labelFor(endpoint)}"
				title="Add a stop after this one"
			>+</button>
		{/if}
	{/if}
</div>

<style>
	.ep-row {
		position: relative;
		display: flex;
		align-items: center;
		gap: 0.4rem;
		/* Permanent transparent border so the gradient focus ring can
		   appear without a layout shift (padding compensates). */
		border: 2px solid transparent;
		padding: calc(0.35rem - 2px) calc(0.5rem - 2px);
		background: var(--gray-50);
		border-radius: 0.55rem;
		font-family: var(--font-ui);
	}
	.ep-row:focus-within {
		background: linear-gradient(var(--gray-50), var(--gray-50)) padding-box, var(--gradient-brand-input) border-box;
	}

	.ep-label {
		flex: 0 0 auto;
		font-size: 0.7rem;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		/* Uppercase micro-title — anthracite per ux-guidelines.md. */
		color: var(--anthracite);
		width: var(--control-size);
	}

	.ep-input {
		flex: 1 1 auto;
		border: none;
		background: transparent;
		font-family: inherit;
		font-size: 0.9rem;
		color: var(--gray-850);
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
		color: var(--gray-850);
		cursor: pointer;
		padding: 0.15rem 0;
		min-width: 0;
	}

	/* Base look + hover from .icon-btn (app.css); sizing only here. */
	.ep-clear {
		flex: 0 0 auto;
		font-size: 1.1rem;
		line-height: 1;
		padding: 0.15rem 0.3rem;
	}
	.ep-refresh {
		flex: 0 0 auto;
		padding: 0.15rem 0.25rem;
	}
	/* Same backgroundless icon button as the clear ×; the glyph is a plain
	   "+" so no new Material Symbol has to enter the subset. */
	.ep-add {
		flex: 0 0 auto;
		font-size: 1.05rem;
		line-height: 1;
		padding: 0.15rem 0.3rem;
	}
	.ep-refresh :global(.material-symbols-outlined) {
		font-size: 1rem;
		line-height: 1;
		display: block;
	}

	.ep-menu {
		/* Fixed positioning escapes the routing panel's `overflow: hidden`.
		   Coordinates come from an inline `style` attribute computed in the
		   component from the row's bounding rect (updated on resize/scroll). */
		position: fixed;
		margin: 0;
		padding: 0.25rem 0;
		list-style: none;
		background: var(--white);
		border-radius: 0.55rem;
		box-shadow: var(--shadow-popover);
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
		color: var(--gray-850);
		cursor: pointer;
	}
	.ep-row-item.highlighted { background: var(--anthracite); color: var(--white); }
	.ep-row-item.highlighted .ep-icon { color: var(--white); }

	.ep-icon {
		width: 1.1rem;
		height: 1.1rem;
		font-size: 1.1rem;
		line-height: 1;
		color: var(--gray-500);
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
		color: var(--gray-400);
		font-style: italic;
	}

	.ep-hint {
		padding: 0.35rem 0.7rem;
		font-size: 0.8rem;
		color: var(--gray-400);
		list-style: none;
	}

	.ep-divider {
		height: 1px;
		margin: 0.2rem 0.7rem;
		background: #e2e2e2;
		list-style: none;
	}
</style>
