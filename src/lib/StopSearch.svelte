<script lang="ts">
	import type maplibregl from 'maplibre-gl';

	let { map }: { map: maplibregl.Map | null } = $props();

	type Entry = { n: string; u: string; c: [number, number]; m?: string };
	type Indexed = Entry & { fold: string };

	const MAX_RESULTS = 10;
	const FLYTO_ZOOM = 16;

	const MODE_ICON: Record<string, string> = {
		train:        'train',
		metro:        'subway',
		tram:         'tram',
		bus:          'directions_bus',
		regional_bus: 'directions_bus',
		ferry:        'directions_boat',
		mountain:     'gondola_lift',
	};

	let index = $state<Indexed[]>([]);
	let indexError = $state<string | null>(null);
	let query = $state('');
	let open = $state(false);
	let highlighted = $state(0);
	let inputEl: HTMLInputElement | null = $state(null);
	let listEl: HTMLUListElement | null = $state(null);

	function fold(s: string): string {
		return s.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();
	}

	$effect(() => {
		let cancelled = false;
		fetch('/map-assets/stop_search_index.json')
			.then(r => {
				if (!r.ok) throw new Error(`HTTP ${r.status}`);
				return r.json() as Promise<Entry[]>;
			})
			.then(data => {
				if (cancelled) return;
				index = data.map(e => ({ ...e, fold: fold(e.n) }));
			})
			.catch(err => {
				if (cancelled) return;
				indexError = String(err);
			});
		return () => { cancelled = true; };
	});

	const results = $derived.by<Indexed[]>(() => {
		const q = fold(query.trim());
		if (!q) return [];
		const hits = index.filter(e => e.fold.includes(q));
		if (!map) return hits.slice(0, MAX_RESULTS);
		const c = map.getCenter();
		const cLon = c.lng;
		const cLat = c.lat;
		const latScale = Math.cos((cLat * Math.PI) / 180);
		hits.sort((a, b) => {
			const dxA = (a.c[0] - cLon) * latScale;
			const dyA = a.c[1] - cLat;
			const dxB = (b.c[0] - cLon) * latScale;
			const dyB = b.c[1] - cLat;
			return dxA * dxA + dyA * dyA - (dxB * dxB + dyB * dyB);
		});
		return hits.slice(0, MAX_RESULTS);
	});

	$effect(() => {
		void results;
		highlighted = 0;
	});

	function select(entry: Indexed) {
		if (!map) return;
		map.flyTo({
			center: entry.c,
			zoom: FLYTO_ZOOM,
			essential: true,
		});
		open = false;
		inputEl?.blur();
	}

	function onKey(e: KeyboardEvent) {
		if (!open && (e.key === 'ArrowDown' || e.key === 'Enter')) {
			open = true;
		}
		if (e.key === 'Enter') {
			e.preventDefault();
			const pick = results[highlighted] ?? results[0];
			if (pick) select(pick);
			return;
		}
		if (e.key === 'Escape') {
			open = false;
			inputEl?.blur();
			return;
		}
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			highlighted = Math.min(highlighted + 1, results.length - 1);
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
		placeholder="Search stops"
		bind:value={query}
		onfocus={() => (open = true)}
		onblur={() => {
			setTimeout(() => { open = false; }, 120);
		}}
		onkeydown={onKey}
	/>
	{#if open && query.trim().length > 0}
		<ul bind:this={listEl} class="results" role="listbox">
			{#if indexError}
				<li class="empty">Index unavailable</li>
			{:else if results.length === 0}
				<li class="empty">No matches</li>
			{:else}
				{#each results as r, i (r.u)}
					<li
						class="result"
						class:highlighted={i === highlighted}
						role="option"
						aria-selected={i === highlighted}
						onmousedown={(e) => {
							e.preventDefault();
							select(r);
						}}
						onmouseenter={() => (highlighted = i)}
					>
						{#if r.m && MODE_ICON[r.m]}
							<span class="mode-icon material-symbols-outlined" aria-hidden="true">{MODE_ICON[r.m]}</span>
						{:else}
							<span class="mode-icon" aria-hidden="true"></span>
						{/if}
						<span class="stop-name">{r.n}</span>
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
		font-family: 'Saira', 'Helvetica Neue', Arial, sans-serif;
	}
	input {
		width: 100%;
		box-sizing: border-box;
		padding: 0.4rem 0.8rem;
		border: none;
		border-radius: 999px;
		background: #ffffff;
		box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3);
		font-family: inherit;
		font-size: 0.85rem;
		line-height: 1.2;
		color: #222;
		outline: none;
	}
	input:focus {
		box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3), 0 0 0 2px #333;
	}
	.results {
		position: absolute;
		top: calc(100% + 0.3rem);
		left: 0;
		right: 0;
		margin: 0;
		padding: 0.25rem 0;
		list-style: none;
		background: #ffffff;
		border-radius: 0.5rem;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
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
		color: #222;
		cursor: pointer;
	}
	.result.highlighted {
		background: #333;
		color: #fff;
	}
	.mode-icon {
		display: inline-block;
		width: 1.1rem;
		height: 1.1rem;
		font-size: 1.1rem;
		line-height: 1;
		color: #666;
		flex: 0 0 auto;
	}
	.result.highlighted .mode-icon {
		color: #fff;
	}
	.stop-name {
		flex: 1 1 auto;
		min-width: 0;
	}
	.empty {
		padding: 0.35rem 0.7rem;
		font-size: 0.85rem;
		color: #888;
		font-style: italic;
	}
</style>
