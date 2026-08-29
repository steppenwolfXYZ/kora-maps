<script lang="ts">
	// Connect tab (routing-persistence.md § Connect): drag-to-connect
	// station board. Press a cell, drag — a line follows the pointer —
	// and release on another cell to make the route (start cell = From,
	// release cell = To). The bottom row holds the current-location cell
	// and the two empty half-cells: a connection drawn through "Start"
	// leaves From empty, through "Stop" leaves To empty (the station on
	// the other end of the line fills the opposite side).
	import { untrack } from 'svelte';
	import {
		connectStations, coldStartSuggestions, type ConnectStation
	} from './connect.svelte';
	import { geolocationDenied, hasGeolocation } from './geolocation.svelte';
	import { loadStationIndex, type StationEntry } from './stationIndex';
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

	// Kept in sync with StopSearch's MODE_ICON.
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
	let suggestions = $state<StationEntry[]>([]);
	$effect(() => {
		const anchor = getMapCenter();
		if (!anchor) return;
		const used = untrack(() => connectStations.list);
		const free = GRID_CAPACITY - Math.min(used.length, GRID_CAPACITY);
		if (free <= 0) return;
		void loadStationIndex().then((idx) => {
			if (!idx) return;
			suggestions = coldStartSuggestions(
				anchor, idx, new Set(used.map((e) => e.u)), free);
		});
	});

	type Tile = { u: string; n: string; m?: string; ep: Endpoint };

	function stationEp(s: { u: string; n: string; c: [number, number]; m?: string; p?: string }): Endpoint {
		return { type: 'station', uic: s.u, name: s.n, coord: s.c, mode: s.m, pid: s.p };
	}

	let tiles = $derived.by<Tile[]>(() => {
		const real: Tile[] = connectStations.list
			.slice(0, GRID_CAPACITY)
			.map((s: ConnectStation) => ({ u: s.u, n: s.n, m: s.m, ep: stationEp(s) }));
		const seen = new Set(real.map((t) => t.u));
		const fill: Tile[] = suggestions
			.filter((s) => !seen.has(s.u))
			.slice(0, GRID_CAPACITY - real.length)
			.map((s) => ({ u: s.u, n: s.n, m: s.m, ep: stationEp(s) }));
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
				class="cell"
				class:drag-source={dragFrom === t.u}
				class:drag-target={dragTarget === t.u}
				data-conn={t.u}
				onpointerdown={(e) => startDrag(e, t.u)}
			>
				{#if t.m && MODE_ICON[t.m]}
					<span class="material-symbols-outlined cell-icon" aria-hidden="true">{MODE_ICON[t.m]}</span>
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
				class="cell"
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
				class="cell half"
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
				class="cell half"
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
		min-height: 3.4rem;
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
	.cell:not(.filler):hover {
		background: var(--gray-50);
	}
	.cell.drag-source,
	.cell.drag-target {
		background: var(--gray-100);
	}
	.cell-icon {
		flex: 0 0 auto;
		font-size: 1rem;
		color: var(--gray-500);
	}
	.cell-label {
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
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
