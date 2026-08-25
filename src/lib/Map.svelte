<script lang="ts">
	// Map root: renders the MapLibre container plus the overlay chrome,
	// and ties the pieces together — map plumbing in map/createMap.ts,
	// cross-feature coordination in map/orchestration.svelte.ts, overlay
	// UI in map/MapChrome.svelte, shared UI state in map/uiState.svelte.ts.
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import { Protocol } from 'pmtiles';
	import { routingState } from './routing/state.svelte';
	import MapChrome from './map/MapChrome.svelte';
	import { createKoraMap } from './map/createMap';
	import {
		setupMapOrchestration, wireMapFeatures, resetMapFeatures, suppressHashJump
	} from './map/orchestration.svelte';

	// Register the pmtiles:// protocol handler once at module level
	const pmtilesProtocol = new Protocol();
	maplibregl.addProtocol('pmtiles', pmtilesProtocol.tile.bind(pmtilesProtocol));

	/** Resolved MapLibre style object loaded from /style.json */
	let { style }: { style: maplibregl.StyleSpecification } = $props();

	let container: HTMLDivElement;

	setupMapOrchestration();

	$effect(() => {
		const { map, destroy } = createKoraMap(container, style, suppressHashJump);
		wireMapFeatures(map);
		return () => {
			resetMapFeatures();
			destroy();
		};
	});
</script>

<div class="map-wrap" class:routing-active={routingState.open} class:routing-map-mode={routingState.mapMode}>
	<div bind:this={container} class="map"></div>
	<MapChrome />
</div>

<style>
	/* MapLibre control styling lives in app.css (§ MapLibre controls) —
	   it targets vendor DOM and depends only on the .map-wrap state
	   classes set above. */
	.map-wrap {
		position: relative;
		width: 100vw;
		height: 100vh;
		height: 100dvh;
	}

	.map {
		width: 100%;
		height: 100%;
	}
</style>
