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
		'transit-ferry', 'transit-metro', 'transit-tram', 'transit-train'
	];

	const TRANSIT_STOP_DOT_LAYERS = [
		'transit-stop-fill-transit_stops_rail',
		'transit-stop-fill-transit_stops_tram',
		'transit-stop-fill-transit_stops_regional',
		'transit-stop-fill-transit_stops_bus',
		'transit-stop-fill-ferry'
	];

	const TRANSIT_STOP_PILL_LAYERS = [
		'transit-stop-pill-fill',
		'transit-stop-pill-casing'
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
			// Pointer cursor when hovering transit lines and stops
			const hoverLayers = [
				...TRANSIT_LINE_LAYERS,
				...TRANSIT_STOP_DOT_LAYERS,
				...TRANSIT_STOP_PILL_LAYERS
			];
			for (const layer of hoverLayers) {
				map.on('mouseenter', layer, () => { map.getCanvas().style.cursor = 'pointer'; });
				map.on('mouseleave', layer, () => { map.getCanvas().style.cursor = ''; });
			}
		});

		map.on('click', (e) => {
			if (popup) { popup.remove(); popup = null; }
			const fmt = (v: unknown) => v == null ? '–' : String(v);

			// Station click takes priority over line click
			const stopFeatures = map.queryRenderedFeatures(e.point, {
				layers: [...TRANSIT_STOP_PILL_LAYERS, ...TRANSIT_STOP_DOT_LAYERS]
			});
			if (stopFeatures.length) {
				const p = stopFeatures[0].properties as Record<string, unknown>;
				const kind = p.feature_type === 'pill' ? 'pill'
				           : p.feature_type === 'connector' ? 'connector'
				           : 'stop';
				const countLine = p.stop_count != null ? `&ensp;count: ${fmt(p.stop_count)}` : '';
				const html = `<div style="font-family:monospace;font-size:11px;line-height:1.5">
					<b>${fmt(p.stop_name) || '(no name)'}</b> &ensp;[${fmt(p.mode)} ${kind}]${countLine}<br>
					id: ${fmt(p.stop_id)}<br>
					parent: ${fmt(p.parent_station)}
				</div>`;
				popup = new maplibregl.Popup({ maxWidth: '320px' })
					.setLngLat(e.lngLat)
					.setHTML(html)
					.addTo(map);
				return;
			}

			const lineFeatures = map.queryRenderedFeatures(e.point, { layers: TRANSIT_LINE_LAYERS });
			if (!lineFeatures.length) return;

			const p = lineFeatures[0].properties as Record<string, unknown>;
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
