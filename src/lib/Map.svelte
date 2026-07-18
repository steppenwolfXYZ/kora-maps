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
		'transit-stop-fill-transit_stops_rail-far',
		'transit-stop-fill-transit_stops_tram-far',
		'transit-stop-fill-transit_stops_regional-far',
		'transit-stop-fill-transit_stops_bus-far'
	];

	const TRANSIT_STOP_LABEL_LAYERS = [
		'transit-stop-label-transit_stops_rail-far-normal',
		'transit-stop-label-transit_stops_rail-far-other',
		'transit-stop-label-transit_stops_tram-far-normal',
		'transit-stop-label-transit_stops_tram-far-other',
		'transit-stop-label-transit_stops_regional-far-normal',
		'transit-stop-label-transit_stops_regional-far-other',
		'transit-stop-label-transit_stops_bus-far-normal',
		'transit-stop-label-transit_stops_bus-far-other',
		'transit-stop-label-pill-normal',
		'transit-stop-label-pill-other'
	];

	const TRANSIT_STOP_PILL_LAYERS = [
		'transit-stop-pill-fill',
		'transit-stop-pill-casing',
		'transit-stop-pill-connector',
		'transit-stop-pill-connector-casing',
		'transit-stop-pill-endpoint',
	];

	// Every stop-symbology layer, toggled as one unit by the view switch
	// (see .claude/concepts/view-modes.md). Debug layers stay independent.
	const STOP_SYMBOLOGY_LAYERS = [
		...TRANSIT_STOP_DOT_LAYERS,
		...TRANSIT_STOP_PILL_LAYERS,
		...TRANSIT_STOP_LABEL_LAYERS,
		'transit-stop-indicator',
		'close-zoom-station-backdrop',
		'close-zoom-pill-arrow-casing',
		'close-zoom-pill-arrow-fill',
		'close-zoom-pill-arrow-ref',
	];

	const PLACE_LABEL_LAYERS = ['label-place', 'label-state', 'label-country'];

	type ViewMode = 'standard' | 'transit-focus';
	// Dev override: transit-focus while stop rendering is under active work.
	// The concept (view-modes.md) specifies 'standard' as the shipped default.
	const DEFAULT_VIEW = 'transit-focus' as ViewMode;
	let viewMode = $state<ViewMode>(DEFAULT_VIEW);
	let mapRef: maplibregl.Map | null = null;

	function applyViewMode(map: maplibregl.Map, mode: ViewMode) {
		for (const id of STOP_SYMBOLOGY_LAYERS) {
			if (!map.getLayer(id)) continue;
			map.setLayoutProperty(id, 'visibility', mode === 'transit-focus' ? 'visible' : 'none');
		}
		for (const id of PLACE_LABEL_LAYERS) {
			if (!map.getLayer(id)) continue;
			map.setLayoutProperty(id, 'visibility', mode === 'standard' ? 'visible' : 'none');
		}
	}

	function setView(mode: ViewMode) {
		viewMode = mode;
		if (mapRef) applyViewMode(mapRef, mode);
	}

	const DEBUG_STOP_LAYER = 'debug-stop-dot';

	$effect(() => {
		// Bake the default view into the style before map creation so the
		// first frame already matches — no flash on load. DEFAULT_VIEW (a
		// plain const, not the reactive viewMode) so this effect never
		// re-runs (and recreates the map) on a view toggle.
		for (const layer of style.layers) {
			const l = layer as maplibregl.LayerSpecification & { layout?: object };
			if (STOP_SYMBOLOGY_LAYERS.includes(layer.id)) {
				l.layout = {
					...l.layout,
					visibility: DEFAULT_VIEW === 'transit-focus' ? 'visible' : 'none'
				};
			} else if (PLACE_LABEL_LAYERS.includes(layer.id)) {
				l.layout = {
					...l.layout,
					visibility: DEFAULT_VIEW === 'standard' ? 'visible' : 'none'
				};
			}
		}

		const map = new maplibregl.Map({
			container,
			style,
			// Honor center & zoom from the style file; fall back to safe defaults
			center: (style.center as [number, number]) ?? [0, 0],
			zoom: style.zoom ?? 2,
			hash: true,
			attributionControl: false
		});

		(window as any).map = map;
		mapRef = map;
		// Navigation controls (zoom +/-, compass)
		map.addControl(new maplibregl.NavigationControl(), 'top-right');

		// Compact attribution in the corner
		map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');

		// Scale bar (metric) — shows real-world distance for the current zoom
		map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');

		// Keep zoom indicator in sync
		const updateZoom = () => {
			zoom = parseFloat(map.getZoom().toFixed(2));
		};
		map.on('load', updateZoom);
		map.on('zoom', updateZoom);

		// Debug tooltip: click a transit line to see its properties
		let popup: maplibregl.Popup | null = null;

		map.on('load', () => {
			// Sync the view in case the user toggled before the style
			// finished loading (the baked default only covers 'standard').
			applyViewMode(map, viewMode);

			// Pointer cursor when hovering transit lines and stops
			const hoverLayers = [
				...TRANSIT_LINE_LAYERS,
				...TRANSIT_STOP_DOT_LAYERS,
				...TRANSIT_STOP_PILL_LAYERS,
				DEBUG_STOP_LAYER
			];
			for (const layer of hoverLayers) {
				map.on('mouseenter', layer, () => { map.getCanvas().style.cursor = 'pointer'; });
				map.on('mouseleave', layer, () => { map.getCanvas().style.cursor = ''; });
			}
		});

		map.on('click', (e) => {
			if (popup) { popup.remove(); popup = null; }
			const fmt = (v: unknown) => v == null ? '–' : String(v);

			// Debug stop dot takes highest priority — these are the data probe.
			const debugStopFeatures = map.getLayer(DEBUG_STOP_LAYER)
				? map.queryRenderedFeatures(e.point, { layers: [DEBUG_STOP_LAYER] })
				: [];
			if (debugStopFeatures.length) {
				const p = debugStopFeatures[0].properties as Record<string, unknown>;
				const lengthVal = typeof p.platform_length === 'number'
					? `${p.platform_length} m`
					: p.platform_length ? `${p.platform_length} m` : '– (default)';
				let linesHtml = '';
				if (p.lines_json) {
					try {
						const lines: { ref: string; color: string; mode: string;
							origin: string; destination: string; osm_ids?: string[] }[] =
							JSON.parse(String(p.lines_json));
						const currentOsmId = p.current_osm_id != null ? String(p.current_osm_id) : '';
						if (lines.length) {
							const badges = lines.map(l => {
								const label = l.ref || l.mode || '?';
								const c = (l.color || '#888888').replace('#', '');
								const r = parseInt(c.slice(0, 2), 16);
								const g = parseInt(c.slice(2, 4), 16);
								const b = parseInt(c.slice(4, 6), 16);
								const lum = r * 0.299 + g * 0.587 + b * 0.114;
								const fg = lum > 140 ? '#000' : '#fff';
								const route = `${l.origin || '?'} → ${l.destination || '?'}`;
								const titleAttr = ` title="${route.replace(/"/g, '&quot;')}"`;
								const isCurrent = currentOsmId !== '' && Array.isArray(l.osm_ids)
									&& l.osm_ids.includes(currentOsmId);
								const ring = isCurrent
									? 'box-shadow:0 0 0 2px #000, 0 0 0 4px #fff;'
									: '';
								return `<span${titleAttr} style="display:inline-block;background:#${c};color:${fg};border-radius:3px;padding:1px 5px;margin:3px 4px 3px 0;font-size:10px;font-weight:600;letter-spacing:0.03em;cursor:default;${ring}">${label}</span>`;
							}).join('');
							linesHtml = `<div style="margin-top:6px">${badges}</div>`;
						}
					} catch { /* ignore malformed */ }
				}
				const html = `<div style="font-family:'Saira',sans-serif;font-size:12px;line-height:1.5">
					<b>${fmt(p.stop_name) || '(no name)'}</b> &ensp;[${fmt(p.mode)}]<br>
					id: ${fmt(p.stop_id)}<br>
					platform length: ${lengthVal}
					${linesHtml}
				</div>`;
				popup = new maplibregl.Popup({ maxWidth: '320px' })
					.setLngLat(e.lngLat)
					.setHTML(html)
					.addTo(map);
				return;
			}

			// Station click takes priority over line click
			const stopFeatures = map.queryRenderedFeatures(e.point, {
				layers: [...TRANSIT_STOP_PILL_LAYERS, ...TRANSIT_STOP_DOT_LAYERS]
			});
			if (stopFeatures.length) {
				const p = stopFeatures[0].properties as Record<string, unknown>;

				// Per-zoom lookup: far-zoom absorbers carry lines_json_zN
				// and dep_hr_zN reflecting the lines / departures folded in
				// at that zoom (see stops-far-zoom-dot-redesign.md and
				// popups.md). Pills (z ≥ 14) use the base fields.
				const zoomFloor = Math.max(7, Math.min(12, Math.floor(map.getZoom())));
				const linesRaw = (p as Record<string, unknown>)[`lines_json_z${zoomFloor}`]
					?? p.lines_json;
				const depHrAtZoom = (p as Record<string, unknown>)[`dep_hr_z${zoomFloor}`];
				const depHr = typeof depHrAtZoom === 'number'
					? depHrAtZoom
					: (typeof p.dep_hr === 'number' ? p.dep_hr as number : null);

				let depLine = '';
				if (typeof depHr === 'number' && depHr > 0) {
					const disp = depHr < 10 ? depHr.toFixed(1) : String(Math.round(depHr));
					depLine = `<div style="margin-top:2px">Departures: <b>${disp}</b>/h</div>`;
				}

				let linesHtml = '';
				if (linesRaw) {
					try {
						const lines: {
							ref: string; color: string; mode: string;
							name?: string; tooltip?: string;
						}[] = JSON.parse(String(linesRaw));
						if (lines.length) {
							// Flat alternating children: badge, terminus, badge, terminus…
							// Collapsed mode hides termini and lets badges flow with
							// flex-wrap; expanded mode switches to a 2-col grid whose
							// first column is `max-content` — every badge stretches to
							// the widest label width so terminus text aligns.
							const cells = lines.map(l => {
								const label = l.ref || l.mode || '?';
								const lum = parseInt(l.color.slice(1, 3), 16) * 0.299
									+ parseInt(l.color.slice(3, 5), 16) * 0.587
									+ parseInt(l.color.slice(5, 7), 16) * 0.114;
								const fg = lum > 140 ? '#000' : '#fff';
								const tip = l.tooltip || l.name || '';
								const titleAttr = tip
									? ` title="${tip.replace(/"/g, '&quot;')}"`
									: '';
								const badge = `<span class="popup-badge"${titleAttr} style="background:${l.color};color:${fg}">${label}</span>`;
								const terminus = `<span class="popup-line-terminus">${tip.replace(/</g, '&lt;')}</span>`;
								return badge + terminus;
							}).join('');

							linesHtml = `<details class="popup-lines">
								<summary class="popup-lines-summary">
									<span class="popup-chevron">▸</span>
									<span class="popup-lines-list">${cells}</span>
								</summary>
							</details>`;
						}
					} catch { /* ignore malformed */ }
				}

				const html = `<style>
					.popup-lines { margin-top: 6px; }
					.popup-lines-summary { list-style: none; cursor: pointer; display: flex; align-items: flex-start; gap: 4px; }
					.popup-lines-summary::-webkit-details-marker { display: none; }
					.popup-chevron { display: inline-block; color: #888; font-size: 9px; padding-top: 4px; transition: transform 0.15s ease; flex: 0 0 auto; }
					.popup-lines[open] .popup-chevron { transform: rotate(90deg); }
					.popup-badge { display: inline-block; border-radius: 3px; padding: 2px 6px; font-size: 11px; font-weight: 800; letter-spacing: 0.02em; cursor: default; text-align: center; }
					.popup-lines-list { display: flex; flex-wrap: wrap; gap: 4px 3px; }
					.popup-line-terminus { display: none; }
					.popup-lines[open] .popup-lines-list { display: grid; grid-template-columns: max-content 1fr; column-gap: 8px; row-gap: 3px; align-items: center; }
					.popup-lines[open] .popup-badge { display: block; }
					.popup-lines[open] .popup-line-terminus { display: inline; color: #444; font-size: 12px; }
				</style><div style="font-family:'Saira',sans-serif;font-size:13px;line-height:1.4;color:#222">
					<div style="font-weight:700;font-size:15px">${fmt(p.stop_name) || '(no name)'}</div>
					${linesHtml}
					${depLine}
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
			const html = `<div style="font-family:'Saira',sans-serif;font-size:12px;line-height:1.5">
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

		return () => {
			mapRef = null;
			map.remove();
		};
	});
</script>

<div class="map-wrap">
	<div bind:this={container} class="map"></div>

	<div class="view-toggle" role="group" aria-label="Map view">
		<button
			class:active={viewMode === 'standard'}
			onclick={() => setView('standard')}
		>Standard</button>
		<button
			class:active={viewMode === 'transit-focus'}
			onclick={() => setView('transit-focus')}
		>Transit</button>
	</div>

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

	.view-toggle {
		position: absolute;
		top: 1rem;
		left: 1rem;
		display: flex;
		background: #ffffff;
		border-radius: 999px;
		box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3);
		overflow: hidden;
		user-select: none;
	}

	.view-toggle button {
		border: none;
		background: transparent;
		font-family: 'Noto Sans', 'Helvetica Neue', Arial, sans-serif;
		font-size: 0.8rem;
		padding: 0.35rem 0.8rem;
		cursor: pointer;
		color: #333;
	}

	.view-toggle button.active {
		background: #333;
		color: #fff;
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
