<script lang="ts">
	import type maplibregl from 'maplibre-gl';

	let { map }: { map: maplibregl.Map | null } = $props();

	type Entry = { n: string; u: string; c: [number, number]; m?: string; t?: string };
	type Indexed = Entry & { fold: string; words: string[] };

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

	// Mirrors MODE_RANK in scripts/transit/_state.py.
	const MODE_RANK: Record<string, number> = {
		train:        0,
		metro:        1,
		tram:         2,
		bus:          3,
		mountain:     4,
		ferry:        5,
		regional_bus: 6,
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
		small_bus:       11,
	};
	const STOP_TIER_RANK_MAX = 11;

	// Ranking weights (stop-search.md § Ranking). Starting values.
	const W_MATCH    = 5;
	const W_MODE     = 1;
	const W_TIER     = 1;
	const W_DISTANCE = 1;

	// Distance decay characteristic length in km (100 * exp(-d / DIST_DECAY_KM)).
	const DIST_DECAY_KM = 30;
	const EARTH_KM = 6371;

	// Match tiers (stop-search.md § Ranking): 8-tier cascade, evaluated
	// top-down, first condition that holds wins. Multi-word queries are
	// token-based and order-insensitive; every token must match at least
	// as substring for the stop to be a hit at all.
	function matchTierScore(name: string, words: string[], tokens: string[]): number {
		// Per-token match strength: 3 = full word, 2 = word prefix,
		// 1 = substring, 0 = no match (kills the whole hit).
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
			if (strength === 3) anyFull = true; else allFull = false;
			if (strength >= 2) anyPrefix = true; else allPrefix = false;
		}

		// 1. Exact: all tokens full words AND every name word matched.
		if (allFull && fullMatched.size === words.length) return 100;
		// 2. Name-prefix start: all tokens word-prefixes AND the name's
		//    first word fully matched by some token.
		if (allPrefix && fullMatched.has(0)) return 80;
		// 3. Contains name prefix: some token is a prefix of the name's
		//    first word (rest already known ≥ substring).
		if (words.length > 0 && tokens.some(t => words[0].startsWith(t))) return 70;
		// 4. All words full match.
		if (allFull) return 50;
		// 5. Some words full match.
		if (anyFull) return 40;
		// 6. All word-prefix.
		if (allPrefix) return 30;
		// 7. Some word-prefix.
		if (anyPrefix) return 20;
		// 8. Substring only.
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
		const x = dLon * cosLat * Math.PI / 180;
		const y = dLat * Math.PI / 180;
		const distKm = EARTH_KM * Math.sqrt(x * x + y * y);
		return 100 * Math.exp(-distKm / DIST_DECAY_KM);
	}

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
				index = data.map(e => {
					const f = fold(e.n);
					// Words split on any non-alphanumeric run: commas, parens,
					// slashes, dots, hyphens are separators, not word content.
					return { ...e, fold: f, words: f.split(/[^\p{L}\p{N}]+/u).filter(Boolean) };
				});
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
		const tokens = q.split(/\s+/).filter(Boolean);
		if (!tokens.length) return [];
		const c = map?.getCenter();
		const cLon = c?.lng ?? 0;
		const cLat = c?.lat ?? 0;
		const cosLat = c ? Math.cos((cLat * Math.PI) / 180) : 1;
		const scored: { e: Indexed; score: number }[] = [];
		for (const e of index) {
			const match = matchTierScore(e.fold, e.words, tokens);
			if (match === 0) continue;
			const mode = modeScore(e.m);
			const tier = tierScore(e.t);
			const dist = c ? distanceScore(e.c[0] - cLon, e.c[1] - cLat, cosLat) : 0;
			const score = W_MATCH * match + W_MODE * mode + W_TIER * tier + W_DISTANCE * dist;
			scored.push({ e, score });
		}
		scored.sort((a, b) => b.score - a.score);
		return scored.slice(0, MAX_RESULTS).map(s => s.e);
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
			speed: 4.8,
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
