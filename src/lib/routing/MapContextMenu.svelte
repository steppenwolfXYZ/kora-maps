<script lang="ts">
	import { routingState } from './state.svelte';
	import type { Endpoint } from './types';
	import { reverseAddress } from '$lib/geocoding/client';

	interface Props {
		/** Screen-space anchor (x, y) or null when hidden. */
		anchor: { x: number; y: number; lng: number; lat: number } | null;
		onClose: () => void;
	}

	let { anchor, onClose }: Props = $props();

	// Upper bound on the reverse-geocode wait before the endpoint is set
	// nameless. Keeps a slow / down geocoder from blocking routing.
	const REVERSE_GEOCODE_TIMEOUT_MS = 2000;
	// Monotonic pick counter — a later pick supersedes an earlier one whose
	// geocode is still pending, so two quick right-clicks can't land out of
	// order.
	let pickSeq = 0;

	async function pickAsPoint(side: 'from' | 'to') {
		if (!anchor) return;
		const coord: [number, number] = [anchor.lng, anchor.lat];
		const seq = ++pickSeq;
		if (!routingState.open) routingState.openPanel();
		onClose();
		// Resolve the address first, then set the endpoint once — setting it
		// nameless and attaching the name later would rewrite the endpoint
		// and trigger a second routing query. Concept: never a POI name —
		// the client's reverseAddress enforces that. See geocoding-search.md
		// § Reverse geocoding.
		const ac = new AbortController();
		const timer = setTimeout(() => ac.abort(), REVERSE_GEOCODE_TIMEOUT_MS);
		let name: string | null = null;
		try { name = await reverseAddress(coord[0], coord[1], ac.signal); }
		finally { clearTimeout(timer); }
		if (seq !== pickSeq) return;
		const ep: Endpoint = name
			? { type: 'point', coord, displayName: name, kind: 'address' }
			: { type: 'point', coord };
		if (side === 'from') routingState.setFrom(ep);
		else routingState.setTo(ep);
	}
</script>

{#if anchor}
	<div
		class="mcm"
		style="left:{anchor.x}px; top:{anchor.y}px"
		role="menu"
	>
		<button role="menuitem" onclick={() => pickAsPoint('from')}>
			<span class="mcm-icon material-symbols-outlined">play_arrow</span>
			Route from here
		</button>
		<button role="menuitem" onclick={() => pickAsPoint('to')}>
			<span class="mcm-icon material-symbols-outlined">sports_score</span>
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
