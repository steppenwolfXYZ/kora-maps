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
		// Focus override: the picked endpoint arrives async (reverse geocode),
		// so at open time both fields are empty — point the cursor at the
		// side the pick won't fill.
		if (!routingState.open) {
			routingState.openPanel({ prefillCurrent: false, focus: side === 'from' ? 'to' : 'from' });
		}
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
			<!-- Same play / stop glyphs as the map's start and goal pins
			     (routeLayers.ts) and the popup route buttons. -->
			<svg class="mcm-icon" viewBox="0 0 12 12" aria-hidden="true">
				<path d="M3 1.4 L10.2 6 L3 10.6 Z" />
			</svg>
			Route from here
		</button>
		<button role="menuitem" onclick={() => pickAsPoint('to')}>
			<svg class="mcm-icon" viewBox="0 0 12 12" aria-hidden="true">
				<rect x="2.4" y="2.4" width="7.2" height="7.2" />
			</svg>
			Route to here
		</button>
	</div>
{/if}

<style>
	.mcm {
		position: absolute;
		z-index: 30;
		background: var(--white);
		border-radius: 0.5rem;
		box-shadow: var(--shadow-popover);
		padding: 0.25rem 0;
		font-family: var(--font-ui);
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
		color: var(--gray-850);
		padding: 0.4rem 0.75rem;
		cursor: pointer;
	}
	.mcm button:hover { background: var(--gray-75); }

	.mcm-icon {
		width: 0.8rem;
		height: 0.8rem;
		flex: 0 0 auto;
		fill: var(--brand);
	}
</style>
