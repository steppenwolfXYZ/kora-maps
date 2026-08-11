<script lang="ts">
	import { routingState } from './state.svelte';
	import type { Endpoint } from './types';

	interface Props {
		/** Screen-space anchor (x, y) or null when hidden. */
		anchor: { x: number; y: number; lng: number; lat: number } | null;
		onClose: () => void;
	}

	let { anchor, onClose }: Props = $props();

	function pickAsPoint(side: 'from' | 'to') {
		if (!anchor) return;
		const ep: Endpoint = { type: 'point', coord: [anchor.lng, anchor.lat] };
		if (side === 'from') routingState.setFrom(ep);
		else routingState.setTo(ep);
		if (!routingState.open) routingState.openPanel();
		onClose();
	}
</script>

{#if anchor}
	<div
		class="mcm"
		style="left:{anchor.x}px; top:{anchor.y}px"
		role="menu"
	>
		<button role="menuitem" onclick={() => pickAsPoint('from')}>
			<span class="mcm-icon material-symbols-outlined">trip_origin</span>
			Route from here
		</button>
		<button role="menuitem" onclick={() => pickAsPoint('to')}>
			<span class="mcm-icon material-symbols-outlined">place</span>
			Route to here
		</button>
	</div>
{/if}

<style>
	.mcm {
		position: absolute;
		z-index: 30;
		background: #ffffff;
		border-radius: 0.5rem;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
		padding: 0.25rem 0;
		font-family: 'Saira', sans-serif;
		min-width: 11rem;
	}
	.mcm button {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		width: 100%;
		background: transparent;
		border: none;
		text-align: left;
		font-family: inherit;
		font-size: 0.9rem;
		color: #222;
		padding: 0.4rem 0.75rem;
		cursor: pointer;
	}
	.mcm button:hover { background: #f2f2f2; }

	.mcm-icon {
		font-size: 1.1rem;
		line-height: 1;
		color: #666;
	}
</style>
