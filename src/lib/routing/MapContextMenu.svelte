<script lang="ts">
	import { routingState } from './state.svelte';
	import type { Endpoint } from './types';
	import { reverseAddress } from '$lib/geocoding/client';
	import { routeGlyphSvg, type RouteGlyphBox } from './routeGlyphs';

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

	// Item glyphs (routeGlyphs.ts, dot flush at the outer edge): from
	// `o────`, via `──o──`, to `────o` in brand red on the plain menu
	// background (a red chip behind each was tried and found too heavy).
	const GLYPH_BOX: RouteGlyphBox = { w: 30, h: 20, r: 6, inset: 6, line: 2 };
	const FROM_SVG = routeGlyphSvg('from', GLYPH_BOX);
	const VIA_SVG = routeGlyphSvg('via', GLYPH_BOX);
	const TO_SVG = routeGlyphSvg('to', GLYPH_BOX);

	// A map point can only be a via on the direct tabs — transit vias are
	// stations (types.ts ViaEndpoint), so the entry is hidden there rather
	// than offered and silently dropped by setVia.
	const canAddVia = $derived(routingState.travelMode !== 'transit' && routingState.canAddVia);

	async function pickAsPoint(side: 'from' | 'to' | 'via') {
		if (!anchor) return;
		const coord: [number, number] = [anchor.lng, anchor.lat];
		const seq = ++pickSeq;
		// Focus override: the picked endpoint arrives async (reverse geocode),
		// so at open time both fields are empty — point the cursor at the
		// side the pick won't fill. A via fills neither, so the panel's own
		// defaults apply (current location may prefill From).
		if (!routingState.open) {
			if (side === 'via') routingState.openPanel();
			else routingState.openPanel({ prefillCurrent: false, focus: side === 'from' ? 'to' : 'from' });
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
		else if (side === 'to') routingState.setTo(ep);
		else {
			// Appended as the last via — the row is created and filled in one
			// go so no empty row flashes in the panel during the geocode.
			const index = routingState.vias.length;
			routingState.insertViaAt(index);
			routingState.setVia(index, ep);
		}
	}
</script>

{#if anchor}
	<div
		class="mcm"
		style="left:{anchor.x}px; top:{anchor.y}px"
		role="menu"
	>
		<button role="menuitem" onclick={() => pickAsPoint('from')}>
			<span class="mcm-glyph">{@html FROM_SVG}</span>
			<span>Route <b>from</b> here</span>
		</button>
		{#if canAddVia}
			<button role="menuitem" onclick={() => pickAsPoint('via')}>
				<span class="mcm-glyph">{@html VIA_SVG}</span>
				<span>Route <b>via</b> here</span>
			</button>
		{/if}
		<button role="menuitem" onclick={() => pickAsPoint('to')}>
			<span class="mcm-glyph">{@html TO_SVG}</span>
			<span>Route <b>to</b> here</span>
		</button>
	</div>
{/if}

<style>
	.mcm {
		position: absolute;
		z-index: 30;
		background: var(--white);
		border-radius: 0.5rem;
		/* Top-left corner stays square: it sits exactly on the click
		   point and so points at it. */
		border-top-left-radius: 0;
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
	.mcm button b { font-weight: 600; }

	.mcm-glyph {
		display: block;
		width: 30px;
		height: 20px;
		flex: 0 0 auto;
		color: var(--brand);
	}
	.mcm-glyph :global(svg) {
		display: block;
		width: 100%;
		height: 100%;
		fill: currentColor;
	}
</style>
