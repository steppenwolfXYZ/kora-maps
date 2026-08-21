<script lang="ts">
	import { onMount } from 'svelte';
	import Map from '$lib/Map.svelte';
	import { routingState } from '$lib/routing/state.svelte';
	import { urlHasRoutingQuery } from '$lib/routing/url';
	import type { StyleSpecification } from 'maplibre-gl';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	// Shared-connection landing (connection-sharing.md § Shared view): the
	// normal map shell (same style.json loading rules as the root route —
	// see src/routes/+page.svelte for why this must stay client-side), plus
	// share hydration into the routing store. Map.svelte itself needs no
	// share awareness.
	let style: StyleSpecification | null = $state.raw(null);
	let error: string | null = $state(null);

	onMount(async () => {
		// When the viewer has since edited the query, syncUrl has written
		// ?from/?to onto this URL — on reload those params are the newer
		// intent and Map.svelte's cold-load restore handles them; hydrating
		// the share as well would fight it.
		if (!urlHasRoutingQuery(new URL(window.location.href))) {
			routingState.hydrateShare(data.share);
		}
		try {
			const res = await fetch('/map-assets/style.json');
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			style = await res.json();
		} catch (e) {
			error =
				`Failed to load style.json (${e instanceof Error ? e.message : e}). ` +
				'Make sure the map assets are available under /map-assets/.';
		}
	});
</script>

<svelte:head>
	<link rel="preload" href="/map-assets/style.json" as="fetch" crossorigin="anonymous" />
</svelte:head>

{#if style}
	<Map {style} />
{:else if error}
	<p class="style-error">{error}</p>
{/if}

<style>
	.style-error {
		position: fixed;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 24px;
		font-family: 'Saira', sans-serif;
		text-align: center;
	}
</style>
