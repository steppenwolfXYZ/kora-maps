<script lang="ts">
	import maplibregl from 'maplibre-gl';
	import 'maplibre-gl/dist/maplibre-gl.css';
	import { Protocol } from 'pmtiles';

	// Register the pmtiles:// protocol handler once at module level
	const pmtilesProtocol = new Protocol();
	maplibregl.addProtocol('pmtiles', pmtilesProtocol.tile.bind(pmtilesProtocol));

	/** Resolved MapLibre style object loaded from /style.json */
	let { style }: { style: maplibregl.StyleSpecification } = $props();

	let container: HTMLDivElement;
	let zoom = $state(0);

	const TRANSIT_LINE_LAYERS = [
		'transit-mountain', 'transit-regional_bus', 'transit-bus',
		'transit-ferry', 'transit-metro', 'transit-tram', 'transit-train', 'transit-intercity'
	];

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

		// Debug tooltip: click a transit line to see its properties
		let popup: maplibregl.Popup | null = null;

		map.on('load', () => {
			// Pointer cursor when hovering transit lines
			for (const layer of TRANSIT_LINE_LAYERS) {
				map.on('mouseenter', layer, () => { map.getCanvas().style.cursor = 'pointer'; });
				map.on('mouseleave', layer, () => { map.getCanvas().style.cursor = ''; });
			}
		});

		map.on('click', (e) => {
			const features = map.queryRenderedFeatures(e.point, { layers: TRANSIT_LINE_LAYERS });
			if (popup) { popup.remove(); popup = null; }
			if (!features.length) return;

			const p = features[0].properties as Record<string, unknown>;
			const fmt = (v: unknown) => v == null ? '–' : String(v);
			const html = `<div style="font-family:monospace;font-size:11px;line-height:1.5">
				<b>${fmt(p.mode)}</b> &nbsp;ref: ${fmt(p.ref)}<br>
				${p.name ? String(p.name).substring(0, 60) : ''}<br>
				freq: ${typeof p.freq_score === 'number' ? p.freq_score.toFixed(2) : fmt(p.freq_score)}&ensp;
				spd: ${fmt(p.speed_kmh)} km/h&ensp;
				w: ${fmt(p.width_base)}<br>
				osm: ${fmt(p.osm_id)}
			</div>`;

			popup = new maplibregl.Popup({ maxWidth: '320px' })
				.setLngLat(e.lngLat)
				.setHTML(html)
				.addTo(map);
		});

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
