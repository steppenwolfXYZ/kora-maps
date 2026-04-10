<script lang="ts">
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';

	/** Resolved MapLibre style object loaded from /style.json */
	let { style }: { style: maplibregl.StyleSpecification } = $props();

	let container: HTMLDivElement;
	let zoom = $state(0);

	$effect(() => {
		const map = new maplibregl.Map({
			container,
			style,
			// Honor center & zoom from the style file; fall back to safe defaults
			center: (style.center as [number, number]) ?? [0, 0],
			zoom: style.zoom ?? 2,
			attributionControl: false
		});

		(window as any).map = map;
		// Navigation controls (zoom +/-, compass)
		map.addControl(new maplibregl.NavigationControl(), 'top-right');

		// Compact attribution in the corner
		map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');

		// Keep zoom indicator in sync
		const updateZoom = () => {
			zoom = parseFloat(map.getZoom().toFixed(2));
		};
		map.on('load', updateZoom);
		map.on('zoom', updateZoom);

		return () => map.remove();
	});
</script>

<div class="map-wrap">
	<div bind:this={container} class="map"></div>

	<div class="zoom-badge" aria-label="Current zoom level">
		z&thinsp;{zoom}
	</div>
</div>

<style>
	.map-wrap {
		position: relative;
		width: 100vw;
		height: 100vh;
	}

	.map {
		width: 100%;
		height: 100%;
	}

	.zoom-badge {
		position: absolute;
		bottom: 2rem;
		left: 50%;
		transform: translateX(-50%);
		background: rgba(0, 0, 0, 0.55);
		color: #fff;
		font-family: 'ui-monospace', 'SFMono-Regular', 'Menlo', monospace;
		font-size: 0.75rem;
		letter-spacing: 0.05em;
		padding: 0.25rem 0.6rem;
		border-radius: 999px;
		pointer-events: none;
		backdrop-filter: blur(4px);
		-webkit-backdrop-filter: blur(4px);
		user-select: none;
	}
</style>
