// Client-side contour lines (see hillshade-and-contours.md). Built by
// maplibre-contour from the same Mapterhorn terrain DEM the hillshade
// layer consumes (source config in scripts/config.yaml → terrain.source).

import maplibregl from 'maplibre-gl';
import mlcontour from 'maplibre-contour';

const CONTOUR_SOURCE_ID = 'contour-source';
const CONTOUR_LAYERS = ['contour-line-minor', 'contour-line-major', 'contour-label-major'];

// Contour DEM manager. Constructed lazily: DemSource spawns a Web
// Worker, which doesn't exist during SSR — creation is triggered from
// client-only code (addContourLayers).
let demSource: InstanceType<typeof mlcontour.DemSource> | null = null;
function getDemSource() {
	if (!demSource) {
		demSource = new mlcontour.DemSource({
			url: 'https://tiles.mapterhorn.com/{z}/{x}/{y}.webp',
			encoding: 'terrarium',
			maxzoom: 12,
			worker: true,
			cacheSize: 100,
			timeoutMs: 10_000
		});
		demSource.setupMaplibre(maplibregl);
	}
	return demSource;
}

export function addContourLayers(
	map: maplibregl.Map,
	style: maplibregl.StyleSpecification,
	enabled: boolean
) {
	const meta = (style.metadata as any)?.['carfree:terrain'];
	const c = meta?.contours ?? {};
	const vis = enabled ? 'visible' : 'none';

	// Register the vector tile source that maplibre-contour serves via
	// its own protocol handler. Thresholds pair [minor_m, major_m] per
	// zoom band; below z9 no contours are generated.
	map.addSource(CONTOUR_SOURCE_ID, {
		type: 'vector',
		tiles: [
			getDemSource().contourProtocolUrl({
				multiplier: 1,
				thresholds: {
					9: [200, 1000],
					11: [100, 500],
					13: [50, 250],
					15: [10, 50]
				},
				elevationKey: 'ele',
				levelKey: 'level',
				contourLayer: 'contours'
			})
		],
		maxzoom: 15
	});

	// Insert contours just below the transit block (station backdrop +
	// lines + stops): above the basemap and roads, below transit.
	const beforeId = map.getLayer('close-zoom-station-backdrop')
		? 'close-zoom-station-backdrop'
		: undefined;

	map.addLayer({
		id: 'contour-line-minor',
		type: 'line',
		source: CONTOUR_SOURCE_ID,
		'source-layer': 'contours',
		minzoom: 9,
		filter: ['==', ['get', 'level'], 0],
		layout: { visibility: vis, 'line-join': 'round' },
		paint: {
			'line-color': c.color_minor ?? '#8a6a3c',
			'line-width': c.width_minor ?? 0.4,
			'line-opacity': c.opacity ?? 0.7
		}
	}, beforeId);

	map.addLayer({
		id: 'contour-line-major',
		type: 'line',
		source: CONTOUR_SOURCE_ID,
		'source-layer': 'contours',
		minzoom: 9,
		filter: ['==', ['get', 'level'], 1],
		layout: { visibility: vis, 'line-join': 'round' },
		paint: {
			'line-color': c.color_major ?? '#6a4a24',
			'line-width': c.width_major ?? 1.0,
			'line-opacity': c.opacity ?? 0.7
		}
	}, beforeId);

	map.addLayer({
		id: 'contour-label-major',
		type: 'symbol',
		source: CONTOUR_SOURCE_ID,
		'source-layer': 'contours',
		minzoom: 12,
		filter: ['==', ['get', 'level'], 1],
		layout: {
			visibility: vis,
			'symbol-placement': 'line',
			'symbol-spacing': 400,
			'text-field': ['concat', ['to-string', ['get', 'ele']], ' m'],
			'text-font': ['Saira Regular'],
			'text-size': c.label_size ?? 10,
			'text-max-angle': 25
		},
		paint: {
			'text-color': c.label_color ?? '#4a3624',
			'text-halo-color': c.label_halo ?? '#f4ecdccc',
			'text-halo-width': 1.2
		}
	}, beforeId);
}

export function setContoursVisible(map: maplibregl.Map, enabled: boolean) {
	const vis = enabled ? 'visible' : 'none';
	for (const id of CONTOUR_LAYERS) {
		if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', vis);
	}
}
