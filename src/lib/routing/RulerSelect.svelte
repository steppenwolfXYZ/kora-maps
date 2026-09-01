<script lang="ts">
	// Ruler-style discrete selector (routing-options.md § UI): a draggable
	// handle snapping to evenly spaced stops on a track; while dragging,
	// the description line below previews the hovered stop and the change
	// commits on release. Clicking the track jumps straight to the nearest
	// stop; arrow keys move one stop.

	interface Stop {
		id: string;
		label: string;
		desc: string;
		/** Material Symbols glyph shown next to the description while this
		 * stop is selected/previewed. */
		icon?: string;
	}

	let { stops, value, onChange, label, icon }: {
		stops: Stop[];
		value: string;
		onChange: (id: string) => void;
		label: string;
		/** Material Symbols glyph shown before the title. */
		icon?: string;
	} = $props();

	let trackEl: HTMLDivElement | null = $state(null);
	let dragging = $state(false);
	let dragIndex = $state(0);

	let valueIndex = $derived(Math.max(0, stops.findIndex((s) => s.id === value)));
	let shownIndex = $derived(dragging ? dragIndex : valueIndex);
	let shown = $derived(stops[shownIndex]);

	function pct(i: number): number {
		return stops.length > 1 ? (i / (stops.length - 1)) * 100 : 0;
	}

	// Handle fill = the track gradient sampled at the handle's position.
	// The ramp is piecewise (calm -> neutral -> intense, see the CSS
	// custom props), so the mix runs against the matching half.
	function handleColor(i: number): string {
		const t = pct(i);
		return t <= 50
			? `color-mix(in srgb, var(--ruler-cm) ${t * 2}%, var(--ruler-c0))`
			: `color-mix(in srgb, var(--ruler-c1) ${(t - 50) * 2}%, var(--ruler-cm))`;
	}

	function indexFromX(clientX: number): number {
		if (!trackEl) return valueIndex;
		const r = trackEl.getBoundingClientRect();
		const frac = (clientX - r.left) / r.width;
		return Math.min(stops.length - 1, Math.max(0, Math.round(frac * (stops.length - 1))));
	}

	function commit(i: number) {
		if (stops[i] && stops[i].id !== value) onChange(stops[i].id);
	}

	function onPointerDown(e: PointerEvent) {
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
		dragging = true;
		dragIndex = indexFromX(e.clientX);
	}

	function onPointerMove(e: PointerEvent) {
		if (dragging) dragIndex = indexFromX(e.clientX);
	}

	function onPointerUp(e: PointerEvent) {
		if (!dragging) return;
		dragging = false;
		commit(indexFromX(e.clientX));
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') {
			e.preventDefault();
			commit(Math.max(0, valueIndex - 1));
		} else if (e.key === 'ArrowRight' || e.key === 'ArrowUp') {
			e.preventDefault();
			commit(Math.min(stops.length - 1, valueIndex + 1));
		} else if (e.key === 'Home') {
			e.preventDefault();
			commit(0);
		} else if (e.key === 'End') {
			e.preventDefault();
			commit(stops.length - 1);
		}
	}
</script>

<div class="ruler">
	<span class="ruler-label">{label}</span>
	<div class="ruler-row">
	{#if icon}<span class="material-symbols-outlined ruler-icon" aria-hidden="true">{icon}</span>{/if}
	<div
		class="ruler-track-wrap"
		bind:this={trackEl}
		role="slider"
		tabindex="0"
		aria-label={label}
		aria-valuemin={0}
		aria-valuemax={stops.length - 1}
		aria-valuenow={shownIndex}
		aria-valuetext={shown?.label}
		onpointerdown={onPointerDown}
		onpointermove={onPointerMove}
		onpointerup={onPointerUp}
		onpointercancel={() => (dragging = false)}
		onkeydown={onKey}
	>
		<div class="ruler-track"></div>
		{#each stops as s, i (s.id)}
			<div class="ruler-tick" style:left="{pct(i)}%"></div>
		{/each}
		<div
			class="ruler-handle"
			class:dragging
			style:left="{pct(shownIndex)}%"
			style:background={handleColor(shownIndex)}
		>
			{#if shown?.icon}<span class="material-symbols-outlined ruler-handle-icon" aria-hidden="true">{shown.icon}</span>{/if}
		</div>
	</div>
	</div>
	<div class="ruler-desc">
		<strong>{shown?.label}</strong> {shown?.desc}
	</div>
</div>

<style>
	.ruler {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		/* Calm→neutral→intense track ramp; the handle's fill samples the
		   same ramp at its position (color-mix inline). The neutral middle
		   is a medium gray — dark enough to carry the handle's white
		   glyph. */
		--ruler-c0: #a9c6e8;
		--ruler-cm: #888;
		--ruler-c1: #e03131;
	}
	/* Uppercase micro-title — anthracite per ux-guidelines.md. */
	.ruler-label {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		font-size: 0.62rem;
		font-weight: 600;
		letter-spacing: 0.07em;
		text-transform: uppercase;
		color: var(--anthracite);
	}
	/* Icon + track share a row: the bare anthracite glyph sits left of
	   the ruler bar, vertically centered on it. */
	.ruler-row {
		display: flex;
		align-items: center;
		gap: 0.45rem;
	}
	.ruler-row :global(.ruler-icon) {
		flex: 0 0 auto;
		font-size: 1.25rem;
		line-height: 1;
		color: var(--anthracite);
	}
	.ruler-row .ruler-track-wrap {
		flex: 1 1 auto;
	}
	.ruler-track-wrap {
		position: relative;
		height: 1.8rem;
		/* Keep the endpoint ticks/handle inside the row: the positioning
		   context is inset so 0%/100% sit half a handle from the edges. */
		margin: 0 0.55rem;
		cursor: pointer;
		touch-action: none;
		border-radius: 0.4rem;
	}
	.ruler-track-wrap:focus-visible {
		outline: 2px solid var(--kora-green);
		outline-offset: 2px;
	}
	/* Calm → intense: soft blue on the left, hot red on the right —
	   mirrors both rulers' semantics (slow → running, cautious →
	   daring). Deliberately horizontal, not the diagonal brand angle:
	   the color encodes the x-axis itself. */
	.ruler-track {
		position: absolute;
		left: 0;
		right: 0;
		top: 50%;
		height: 3px;
		margin-top: -1.5px;
		border-radius: 2px;
		background: linear-gradient(90deg, var(--ruler-c0), var(--ruler-cm) 50%, var(--ruler-c1));
	}
	.ruler-tick {
		position: absolute;
		top: 50%;
		width: 2px;
		height: 0.55rem;
		transform: translate(-50%, -50%);
		border-radius: 1px;
		background: var(--anthracite);
	}
	/* The handle carries the current stop's glyph in white; its fill is
	   set inline — the track gradient sampled at the handle's position —
	   so disc and bar always agree on the color under the handle. */
	.ruler-handle {
		position: absolute;
		top: 50%;
		width: 1.65rem;
		height: 1.65rem;
		display: flex;
		align-items: center;
		justify-content: center;
		transform: translate(-50%, -50%);
		border-radius: 50%;
		box-shadow: 0 1px 3px rgba(0, 0, 0, 0.35);
		border: 2px solid var(--white);
		transition: left 0.12s ease-out, background 0.12s ease-out;
		pointer-events: none;
	}
	.ruler-handle :global(.ruler-handle-icon) {
		font-size: 1rem;
		line-height: 1;
		color: var(--white);
	}
	.ruler-handle.dragging {
		transition: none;
		transform: translate(-50%, -50%) scale(1.15);
	}
	.ruler-desc {
		display: flex;
		align-items: center;
		gap: 0.3rem;
		font-size: 0.72rem;
		line-height: 1.35;
		color: var(--gray-500);
		min-height: 1.9em;
	}
	.ruler-desc strong {
		color: var(--gray-800);
		font-weight: 600;
	}
</style>
