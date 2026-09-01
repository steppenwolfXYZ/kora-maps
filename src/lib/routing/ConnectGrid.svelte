<script lang="ts">
	// Connect tab (routing-persistence.md § Connect): drag-to-connect
	// board of the user's most-used places — stations, addresses and POIs.
	// Press a cell, drag — a line follows the pointer — and release on
	// another cell to make the route (start cell = From, release cell =
	// To). The bottom row holds the current-location cell and the two
	// empty half-cells: a connection drawn through "Start" leaves From
	// empty, through "Stop" leaves To empty (the place on the other end
	// of the line fills the opposite side).
	import { untrack } from 'svelte';
	import {
		connectStations, coldStartSuggestions, placeEndpoint, type ConnectPlace
	} from './connect.svelte';
	import { geolocationDenied, hasGeolocation } from './geolocation.svelte';
	import { loadStationIndex, type StationEntry } from './stationIndex';
	import { modeMidColor } from './legColor';
	import type { Endpoint } from './types';

	let { getMapCenter, onConnect }: {
		getMapCenter: () => [number, number] | null;
		/** Drag result: exactly one side may be null (empty start / stop). */
		onConnect: (from: Endpoint | null, to: Endpoint | null) => void;
	} = $props();

	const GRID_CAPACITY = 10;
	const EMPTY_FROM = 'empty-from';
	const EMPTY_TO = 'empty-to';
	const CURRENT = 'current';

	// Kept in sync with StopSearch's MODE_ICON / POI_ICON / ADDRESS_ICON.
	const POI_ICON = 'place';
	const ADDRESS_ICON = 'home_work';
	const MODE_ICON: Record<string, string> = {
		train:        'train',
		metro:        'subway',
		tram:         'tram',
		bus:          'directions_bus',
		regional_bus: 'directions_bus',
		ferry:        'directions_boat',
		mountain:     'gondola_lift',
	};

	// Suggestions are computed once per mount from the map center at open
	// time — a moving map must not reshuffle the board under the user.
	// The index itself is kept for tile-color lookups (usage-sourced tiles
	// come from localStorage, which stores no colors).
	let suggestions = $state<StationEntry[]>([]);
	let stationIdx = $state<Map<string, StationEntry> | null>(null);
	$effect(() => {
		const anchor = getMapCenter();
		const used = untrack(() => connectStations.list);
		const free = GRID_CAPACITY - Math.min(used.length, GRID_CAPACITY);
		void loadStationIndex().then((idx) => {
			if (!idx) return;
			stationIdx = idx;
			if (!anchor || free <= 0) return;
			suggestions = coldStartSuggestions(
				anchor, idx, new Set(used.map((e) => e.u)), free);
		});
	});

	type Grad = { a: string; b: string };
	type Tile = { u: string; n: string; icon: string | null; grad: Grad | null; ep: Endpoint };

	function stationEp(s: StationEntry): Endpoint {
		return { type: 'station', uic: s.u, name: s.n, coord: s.c, mode: s.m, pid: s.p };
	}

	/** Tile gradient ends: baked average → dominant color from the index
	 * when present; else a tint→tone of the mode mid-color; else null
	 * (CSS anthracite fallback — which is what address / POI tiles wear,
	 * having no line colors of their own). */
	function tileGrad(u: string, m?: string): Grad | null {
		const e = stationIdx?.get(u);
		if (e?.ca && e?.cd) return { a: e.ca, b: e.cd };
		const mid = modeMidColor(m);
		return mid ? { a: `color-mix(in srgb, ${mid} 72%, #fff)`, b: mid } : null;
	}

	function placeIcon(e: ConnectPlace): string | null {
		if (e.ty === 'point') return e.k === 'address' ? ADDRESS_ICON : POI_ICON;
		return (e.m && MODE_ICON[e.m]) ?? null;
	}

	let tiles = $derived.by<Tile[]>(() => {
		const real: Tile[] = connectStations.list
			.slice(0, GRID_CAPACITY)
			.map((s: ConnectPlace) => ({
				u: s.u, n: s.n, icon: placeIcon(s),
				grad: s.ty === 'point' ? null : tileGrad(s.u, s.m),
				ep: placeEndpoint(s)
			}));
		const seen = new Set(real.map((t) => t.u));
		const fill: Tile[] = suggestions
			.filter((s) => !seen.has(s.u))
			.slice(0, GRID_CAPACITY - real.length)
			.map((s) => ({
				u: s.u, n: s.n, icon: (s.m && MODE_ICON[s.m]) ?? null,
				grad: tileGrad(s.u, s.m), ep: stationEp(s)
			}));
		return [...real, ...fill];
	});

	let showCurrent = $derived(hasGeolocation() && !geolocationDenied());

	// ── Drag state ──────────────────────────────────────────────────────
	let boardEl: HTMLDivElement | null = $state(null);
	let dragFrom = $state<string | null>(null);
	let lineStart = $state<{ x: number; y: number } | null>(null);
	let linePos = $state<{ x: number; y: number } | null>(null);
	let dragTarget = $state<string | null>(null);

	function endpointOf(id: string): Endpoint | null {
		if (id === CURRENT) return { type: 'current' };
		const t = tiles.find((tile) => tile.u === id);
		return t ? t.ep : null;
	}

	function validPair(a: string, b: string): boolean {
		if (a === b) return false;
		// A line between the two empty cells means nothing.
		const empties = [a, b].filter((id) => id === EMPTY_FROM || id === EMPTY_TO);
		return empties.length < 2;
	}

	function relPoint(clientX: number, clientY: number): { x: number; y: number } {
		const r = boardEl!.getBoundingClientRect();
		return { x: clientX - r.left, y: clientY - r.top };
	}

	function startDrag(e: PointerEvent, id: string) {
		if (e.button !== 0 && e.pointerType === 'mouse') return;
		e.preventDefault();
		const cell = (e.currentTarget as HTMLElement).getBoundingClientRect();
		dragFrom = id;
		lineStart = relPoint(cell.left + cell.width / 2, cell.top + cell.height / 2);
		linePos = relPoint(e.clientX, e.clientY);
		dragTarget = null;
	}

	function moveDrag(e: PointerEvent) {
		if (!dragFrom || !boardEl) return;
		linePos = relPoint(e.clientX, e.clientY);
		const hit = document.elementFromPoint(e.clientX, e.clientY)
			?.closest('[data-conn]') as HTMLElement | null;
		const id = hit?.dataset.conn ?? null;
		dragTarget = id && validPair(dragFrom, id) ? id : null;
	}

	function endDrag() {
		if (dragFrom && dragTarget) commit(dragFrom, dragTarget);
		dragFrom = null;
		lineStart = null;
		linePos = null;
		dragTarget = null;
	}

	/** Turn the drawn connection into a route. The empty cells force their
	 * side to stay empty regardless of drag direction; otherwise the drag
	 * direction decides: start cell = From, release cell = To. */
	function commit(a: string, b: string) {
		if (a === EMPTY_FROM || b === EMPTY_FROM) {
			const other = a === EMPTY_FROM ? b : a;
			onConnect(null, endpointOf(other));
		} else if (a === EMPTY_TO || b === EMPTY_TO) {
			const other = a === EMPTY_TO ? b : a;
			onConnect(endpointOf(other), null);
		} else {
			onConnect(endpointOf(a), endpointOf(b));
		}
	}
</script>

<svelte:window
	onpointermove={dragFrom ? moveDrag : undefined}
	onpointerup={dragFrom ? endDrag : undefined}
	onpointercancel={dragFrom ? endDrag : undefined}
/>

<!-- The board is a pointer-drag surface; the cells carry no tap/keyboard
     semantics (drag-only interaction, see concept). The endpoint inputs
     above remain the accessible path to the same result. -->
<div class="connect-board" class:dragging={dragFrom !== null} bind:this={boardEl}>
	<div class="cells">
		{#each tiles as t (t.u)}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div
				class="cell filled"
				class:drag-source={dragFrom === t.u}
				class:drag-target={dragTarget === t.u}
				style:--tile-a={t.grad?.a}
				style:--tile-b={t.grad?.b}
				data-conn={t.u}
				onpointerdown={(e) => startDrag(e, t.u)}
			>
				{#if t.icon}
					<span class="material-symbols-outlined cell-icon" aria-hidden="true">{t.icon}</span>
				{/if}
				<span class="cell-label">{t.n}</span>
			</div>
		{/each}
		{#if tiles.length % 2 === 1}
			<div class="cell filler" aria-hidden="true"></div>
		{/if}
	</div>
	<div class="bottom-row">
		{#if showCurrent}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div
				class="cell filled cell-current"
				class:drag-source={dragFrom === CURRENT}
				class:drag-target={dragTarget === CURRENT}
				data-conn={CURRENT}
				onpointerdown={(e) => startDrag(e, CURRENT)}
			>
				<span class="material-symbols-outlined cell-icon" aria-hidden="true">my_location</span>
				<span class="cell-label">Current location</span>
			</div>
		{/if}
		<div class="halves">
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div
				class="cell half filled cell-start"
				class:drag-source={dragFrom === EMPTY_FROM}
				class:drag-target={dragTarget === EMPTY_FROM}
				data-conn={EMPTY_FROM}
				onpointerdown={(e) => startDrag(e, EMPTY_FROM)}
			>
				<span class="material-symbols-outlined cell-icon" aria-hidden="true">trip_origin</span>
				<span class="cell-label">Start</span>
			</div>
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div
				class="cell half filled cell-stop"
				class:drag-source={dragFrom === EMPTY_TO}
				class:drag-target={dragTarget === EMPTY_TO}
				data-conn={EMPTY_TO}
				onpointerdown={(e) => startDrag(e, EMPTY_TO)}
			>
				<span class="material-symbols-outlined cell-icon" aria-hidden="true">place</span>
				<span class="cell-label">Stop</span>
			</div>
		</div>
	</div>
	{#if lineStart && linePos}
		<svg class="drag-line" aria-hidden="true">
			<line x1={lineStart.x} y1={lineStart.y} x2={linePos.x} y2={linePos.y} />
			<circle cx={lineStart.x} cy={lineStart.y} r="3.5" />
		</svg>
	{/if}
</div>

<style>
	/* Lined board: 1px gaps over a line-colored background draw the grid
	   lines; the outer border + radius close the frame. */
	.connect-board {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 1px;
		background: var(--gray-200);
		border: 1px solid var(--gray-200);
		border-radius: 0.55rem;
		overflow: hidden;
		touch-action: none;
		/* Refuse flex shrinking in the scrolling .rp-suggest — with
		   overflow:hidden the board would shrink-and-clip its cells
		   instead of overflowing (which is what makes the parent
		   scroll). */
		flex-shrink: 0;
	}
	.connect-board.dragging {
		user-select: none;
		-webkit-user-select: none;
		cursor: grabbing;
	}
	.cells {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 1px;
	}
	/* Same 2-column grid as .cells so the centre line aligns exactly;
	   the second column nests its own 2-column grid for the halves. */
	.bottom-row {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 1px;
	}
	.halves {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 1px;
		min-width: 0;
	}
	/* Geolocation unavailable → no current-location cell; the halves
	   span the full row. */
	.halves:only-child {
		grid-column: 1 / -1;
	}
	.cell {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		min-width: 0;
		min-height: 4rem;
		background: var(--white);
		font-size: 0.82rem;
		line-height: 1.25;
		color: var(--gray-850);
		padding: 0.5rem 0.55rem;
		cursor: grab;
	}
	.cell.filler {
		cursor: default;
	}
	/* Filled tiles wear a full-tile 135° gradient (brand-gradient
	   direction) with white text/icon. Station tiles: average line color →
	   dominant line color, baked into the search index (`ca` / `cd`),
	   passed inline as --tile-a/--tile-b; with an older index the ends
	   fall back to a tint→tone of the mode mid-color (computed in
	   tileGrad). Address / POI tiles have no line colors and keep the
	   anthracite fallback. The utility cells and the no-color fallback
	   derive the same tint→tone shape from a single --tile-c. Hover and drag states
	   deepen the fill rather than swapping to gray. */
	.cell.filled {
		--tile-c: var(--anthracite);
		--tile-a: color-mix(in srgb, var(--tile-c) 72%, #fff);
		--tile-b: var(--tile-c);
		background: linear-gradient(135deg, var(--tile-a) 0%, var(--tile-b) 100%);
		color: var(--white);
	}
	/* Bottom row: flat fills (no gradient — that stays a station thing),
	   three clearly distinct tones: blue-tinted gray for current
	   location, light gray for Start, dark gray for Stop. */
	.cell.filled.cell-current { background: #5d6f82; }
	.cell.filled.cell-start   { background: #7b7b7b; }
	.cell.filled.cell-stop    { background: #5c5c5c; }
	.cell.filled:hover {
		filter: brightness(0.94);
	}
	.cell.filled.drag-source,
	.cell.filled.drag-target {
		filter: brightness(0.85);
	}
	.cell:not(.filler):not(.filled):hover {
		background: var(--gray-50);
	}
	.cell:not(.filled).drag-source,
	.cell:not(.filled).drag-target {
		background: var(--gray-100);
	}
	.cell-icon {
		flex: 0 0 auto;
		font-size: 1rem;
		color: var(--gray-500);
	}
	.cell.filled .cell-icon {
		color: var(--white);
	}
	/* Two lines before truncation — long Swiss stop names ("Grindelwald
	   Terminal", "Melchtal Stöckalp (Talstation)") wrap instead of
	   ellipsizing after a few characters. */
	.cell-label {
		min-width: 0;
		display: -webkit-box;
		-webkit-box-orient: vertical;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		overflow: hidden;
	}
	.drag-line {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		pointer-events: none;
	}
	.drag-line line {
		stroke: var(--brand);
		stroke-width: 2.5;
		stroke-linecap: round;
	}
	.drag-line circle {
		fill: var(--brand);
	}
</style>
