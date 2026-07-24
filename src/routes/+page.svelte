<script lang="ts">
	import { onMount } from 'svelte';
	import Map from '$lib/Map.svelte';
	import type { StyleSpecification } from 'maplibre-gl';

	// style.json is a pipeline artifact served from /map-assets/ — it never
	// exists inside the server build, so it must not be fetched in a load
	// function (SSR would 404). Fetching client-side keeps the page fully
	// server-renderable; app.html carries a matching <link rel="preload">
	// so the download starts with the document, not after hydration.
	//
	// $state.raw, NOT $state: Map.svelte's init effect reads style.layers
	// and mutates layer.layout in place (visibility pre-bake). A deeply
	// reactive proxy would make that effect self-triggering — it re-runs
	// forever, recreating the map and flooding history.replaceState via
	// MapLibre's hash option.
	let style: StyleSpecification | null = $state.raw(null);
	let error: string | null = $state(null);

	onMount(async () => {
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
