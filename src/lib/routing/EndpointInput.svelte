<script lang="ts">
	import type { Endpoint } from './types';
	import { indexStations, searchStations, type IndexedStation } from './stationSearch';
	import { loadStationIndex } from './stationIndex';
	import { hasGeolocation } from './geolocation';

	// One side of the routing panel's From / To pair. Shows the current
	// endpoint label; focusing turns the row into a search input whose
	// dropdown lists "Current location" (when available) as the first
	// suggestion, followed by station matches once the user types.

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
	const geoAvailable = hasGeolocation();

	$effect(() => {
		let cancelled = false;
		loadStationIndex().then((m) => {
			if (cancelled || !m) return;
			index = indexStations(m.values());
		});
		return () => { cancelled = true; };
	});

	const results = $derived(searchStations(index, query));

	function labelFor(ep: Endpoint | null): string {
		if (!ep) return '';
		if (ep.type === 'current') return 'Current location';
		if (ep.type === 'point') return 'Point on map';
		return ep.name || ep.uic;
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
		onChange(ep);
	}

	function pickStation(e: IndexedStation) {
		// Prefer the walkable-platform-snapped coord for routing (avoids
		// MOTIS's OSR starting the walker on a `sidewalk=separate` road);
		// fall back to the GTFS-derived coord when no snap was baked.
		// See transit-routing.md § Endpoint inputs.
		commit({ type: 'station', uic: e.u, name: e.n, coord: e.cw ?? e.c });
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
		setTimeout(() => { editing = false; query = ''; }, 120);
	}

	// "Current location" is only offered when the user hasn't started
	// typing a station name — once there's a query, only station matches
	// belong in the dropdown.
	const showCurrent = $derived(geoAvailable && endpoint?.type !== 'current' && !query.trim());

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			editing = false;
			query = '';
			inputEl?.blur();
			return;
		}
		const rowCount = (showCurrent ? 1 : 0) + results.length;
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			highlighted = Math.min(highlighted + 1, rowCount - 1);
			return;
		}
		if (e.key === 'ArrowUp') {
			e.preventDefault();
			highlighted = Math.max(highlighted - 1, 0);
			return;
		}
		if (e.key === 'Enter') {
			e.preventDefault();
			if (showCurrent && highlighted === 0) { pickCurrent(); return; }
			const offset = showCurrent ? 1 : 0;
			const pick = results[highlighted - offset] ?? results[0];
			if (pick) pickStation(pick);
			return;
		}
	}
</script>

<div class="ep-row">
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
			<ul class="ep-menu" role="listbox">
				{#if showCurrent}
					<li
						class="ep-row-item ep-row-current"
						class:highlighted={highlighted === 0}
						role="option"
						aria-selected={highlighted === 0}
						onmousedown={(e) => { e.preventDefault(); pickCurrent(); }}
						onmouseenter={() => (highlighted = 0)}
					>
						<span class="ep-icon material-symbols-outlined">my_location</span>
						<span class="ep-text">Current location</span>
					</li>
				{/if}
				{#each results as r, i (r.u)}
					{@const idx = i + (showCurrent ? 1 : 0)}
					<li
						class="ep-row-item"
						class:highlighted={highlighted === idx}
						role="option"
						aria-selected={highlighted === idx}
						onmousedown={(e) => { e.preventDefault(); pickStation(r); }}
						onmouseenter={() => (highlighted = idx)}
					>
						<span class="ep-icon material-symbols-outlined" aria-hidden="true">place</span>
						<span class="ep-text">{r.n}</span>
					</li>
				{/each}
				{#if results.length === 0 && query.trim()}
					<li class="ep-empty">No matches</li>
				{/if}
			</ul>
		{/if}
	{:else}
		<button class="ep-value" onclick={startEdit} aria-label="Change {label.toLowerCase()}">
			<span class="ep-icon material-symbols-outlined" aria-hidden="true">
				{endpoint.type === 'current' ? 'my_location' : endpoint.type === 'point' ? 'location_on' : 'place'}
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
		position: absolute;
		top: calc(100% + 0.3rem);
		left: 0;
		right: 0;
		margin: 0;
		padding: 0.25rem 0;
		list-style: none;
		background: #ffffff;
		border-radius: 0.55rem;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
		max-height: 40vh;
		overflow-y: auto;
		z-index: 20;
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
</style>
