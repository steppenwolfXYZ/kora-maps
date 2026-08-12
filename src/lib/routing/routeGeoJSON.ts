import type { FeatureCollection, Feature, LineString, Point } from 'geojson';
import type { Itinerary, Leg, LegPlace } from './types';
import { decodePolyline } from './polyline';
import { legBadgeColor } from './legColor';
import type { StationEntry } from './stationIndex';

// Min-zoom per stop_tier — mirrors the visibility bands used by the map's
// own dots and labels (scripts/style/transit_stations.py: LABEL_SIZE_Z*).
// Passthrough dots and labels appear only from this zoom up.
const TIER_MIN_ZOOM: Record<string, number> = {
	major_train: 7, main_train: 7, important_train: 7,
	major_mountain: 7, ferry_stop: 7,
	train_station: 10, small_train: 10,
	mountain_stop: 12, major_hub: 12, big_station: 12, normal_stop: 12,
	small_bus: 13
};
const DEFAULT_MIN_ZOOM = 12;

// Mode rank for disc dedup — mirrors MODE_RANK in scripts/transit/_state.py.
// Lower number = higher priority when two nearby stations compete.
const MODE_RANK: Record<string, number> = {
	train: 0, metro: 1, tram: 2, bus: 3, mountain: 4, ferry: 5, regional_bus: 6
};

function legBucket(leg: Leg): string {
	const rt = leg.routeType;
	if (rt !== undefined) {
		if ([100, 101, 102, 103, 105, 106, 107, 109].includes(rt)) return 'train';
		if ([116, 1300, 1303, 1400].includes(rt)) return 'mountain';
		if (rt === 401) return 'metro';
		if (rt === 700 || rt === 702 || rt === 800) return 'bus';
		if (rt === 900) return 'tram';
		if (rt === 1000) return 'ferry';
	}
	switch (leg.mode) {
		case 'TRAM': return 'tram';
		case 'SUBWAY': case 'METRO': return 'metro';
		case 'RAIL': case 'HIGHSPEED_RAIL': case 'LONG_DISTANCE':
		case 'NIGHT_RAIL': case 'REGIONAL_RAIL': case 'REGIONAL_FAST_RAIL':
			return 'train';
		case 'BUS': case 'COACH': return 'bus';
		case 'FERRY': return 'ferry';
		case 'CABLE_CAR': case 'GONDOLA': case 'FUNICULAR': return 'mountain';
	}
	return 'bus';
}

function legRank(leg: Leg): number {
	return MODE_RANK[legBucket(leg)] ?? 99;
}

function haversineMeters(a: [number, number], b: [number, number]): number {
	const R = 6371000;
	const lat1 = a[1] * Math.PI / 180;
	const lat2 = b[1] * Math.PI / 180;
	const dLat = lat2 - lat1;
	const dLon = (b[0] - a[0]) * Math.PI / 180;
	const s = Math.sin(dLat / 2) ** 2
		+ Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
	return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
}

// GeoJSON assembly for a selected itinerary. Builds one feature per layer
// role — the layer code (routeLayers.ts) then filters each layer by
// `role`. See route-display.md for the visual rules.
//
// Roles produced here:
//   walk          — LineString for a walking leg
//   transit       — LineString for a transit leg (fill; `color` prop)
//   connector     — short LineString between the two discs of a transfer
//   disc          — Point at a leg endpoint (transfer / first-board /
//                   last-alight when a walk is attached)
//   passthrough   — Point at an intermediate stop on a transit leg
//   endpoint      — Point at journey start / end (used for the start /
//                   goal icons; DOM markers pick these up)

export interface RouteGeoJSONResult {
	features: FeatureCollection;
	/** [lon, lat] pairs of the start and goal icons — DOM markers. */
	startCoord: [number, number] | null;
	goalCoord: [number, number] | null;
	/** Overall bbox of every rendered element, for auto-frame. */
	bbox: [number, number, number, number] | null;
	/** Parent stop UICs (with any `ch_Parent`/`ch_` prefix stripped) that
	 * appear anywhere on the route — used to filter the map's own stop
	 * symbology so route members stay visible while non-members hide. */
	memberUics: string[];
}

/** Strip MOTIS's dataset prefix ("ch_Parent"…, "ch_"…) to get the bare
 * UIC that the map's stop features carry as `parent_station`. */
function bareUicFrom(id: string | undefined): string | null {
	if (!id) return null;
	const m = id.match(/(\d+)/);
	return m ? m[1] : null;
}

function legCoords(leg: Leg): [number, number][] {
	const g = leg.legGeometry;
	if (!g || typeof g.points !== 'string' || !g.points) return [];
	return decodePolyline(g.points, g.precision ?? 5);
}

function placeCoord(p: LegPlace | undefined): [number, number] | null {
	if (!p || typeof p.lat !== 'number' || typeof p.lon !== 'number') return null;
	return [p.lon, p.lat];
}

/** Snap a MOTIS stop coord onto the leg polyline: prefer the polyline
 * endpoint when it exists (that's where the vehicle stops), fall back to
 * the reported stop coord. Concept § Disc position: "snap to the leg
 * polyline, preferably at the polyline endpoint". */
function snapEndpoint(
	coords: [number, number][],
	which: 'start' | 'end',
	fallback: [number, number] | null
): [number, number] | null {
	if (coords.length) return which === 'start' ? coords[0] : coords[coords.length - 1];
	return fallback;
}

function isTransit(mode: string): boolean {
	return mode !== 'WALK' && mode !== 'BIKE' && mode !== 'CAR';
}

function updateBBox(
	bb: [number, number, number, number] | null,
	coord: [number, number]
): [number, number, number, number] {
	if (!bb) return [coord[0], coord[1], coord[0], coord[1]];
	return [
		Math.min(bb[0], coord[0]), Math.min(bb[1], coord[1]),
		Math.max(bb[2], coord[0]), Math.max(bb[3], coord[1])
	];
}

function minZoomFor(tier: string | undefined): number {
	return tier && tier in TIER_MIN_ZOOM ? TIER_MIN_ZOOM[tier] : DEFAULT_MIN_ZOOM;
}

export function buildRouteGeoJSON(
	itinerary: Itinerary,
	routeColorIndex: Map<string, string> | null,
	stationIndex: Map<string, StationEntry> | null
): RouteGeoJSONResult {
	const features: Feature[] = [];
	let bbox: [number, number, number, number] | null = null;
	const memberUicSet = new Set<string>();
	let featureId = 0;

	// Decode every leg's polyline up-front — the disc / connector logic
	// downstream also needs the endpoint coords.
	const decoded: [number, number][][] = itinerary.legs.map(legCoords);

	// Walking + transit polylines, and pass-through dots for transit legs.
	itinerary.legs.forEach((leg, i) => {
		const coords = decoded[i];
		if (coords.length >= 2) {
			for (const c of coords) bbox = updateBBox(bbox, c);
			const isWalk = leg.mode === 'WALK';
			const color = isWalk ? '#1a1a1a' : legBadgeColor(routeColorIndex, leg);
			features.push({
				type: 'Feature',
				id: featureId++,
				geometry: { type: 'LineString', coordinates: coords } as LineString,
				properties: {
					role: isWalk ? 'walk' : 'transit',
					color,
					leg_index: i
				}
			});
		}
		if (isTransit(leg.mode)) {
			for (const st of leg.intermediateStops ?? []) {
				const c = placeCoord(st);
				if (!c) continue;
				bbox = updateBBox(bbox, c);
				const uic = bareUicFrom(st.parentId ?? st.stopId ?? undefined);
				if (uic) memberUicSet.add(uic);
				const tier = uic ? stationIndex?.get(uic)?.t : undefined;
				// Fallback tier so unknown-tier stops still get a size + Regular
				// font from the map's tier-based label expression, rather than
				// falling into the '0-size / no font' default branch.
				const normalizedTier = tier ?? 'normal_stop';
				features.push({
					type: 'Feature',
					id: featureId++,
					geometry: { type: 'Point', coordinates: c } as Point,
					properties: {
						role: 'passthrough',
						leg_index: i,
						stop_name: st.name ?? '',
						stop_tier: normalizedTier,
						stop_min_zoom: minZoomFor(tier)
					}
				});
			}
			// Also add the leg's own endpoints as member UICs.
			for (const p of [leg.from, leg.to]) {
				const uic = bareUicFrom(p?.parentId ?? p?.stopId ?? undefined);
				if (uic) memberUicSet.add(uic);
			}
		}
	});

	// Discs + connectors at leg boundaries. Rule (route-display.md §
	// Stops on the route):
	//   transit ↔ transit    → two discs + connector
	//   walk    → transit    → single disc (first boarding after walk)
	//   transit → walk       → single disc (last alighting before walk)
	//   walk    → walk       → nothing (shouldn't happen in practice)
	// Journey start: if the first leg is transit → start icon (no disc).
	// Journey start: if the first leg is walk → start icon at its start.
	// Same for the journey end mirrored to `goal`.
	for (let i = 0; i < itinerary.legs.length - 1; i++) {
		const a = itinerary.legs[i];
		const b = itinerary.legs[i + 1];
		const aCoords = decoded[i];
		const bCoords = decoded[i + 1];
		const aEnd = snapEndpoint(aCoords, 'end', placeCoord(a.to));
		const bStart = snapEndpoint(bCoords, 'start', placeCoord(b.from));
		// Discs at any leg boundary carry the transferring station's name for
		// labeling. Both a.to and b.from name the same station on a transfer
		// (the transfer is between platforms at one station); take whichever
		// side has it. `disc_min_zoom` starts at 0 (always visible) and gets
		// bumped by the dedup pass below when a higher-ranked station sits
		// too close.
		const discName = a.to?.name || b.from?.name || '';
		const discUic = bareUicFrom(a.to?.parentId ?? a.to?.stopId ?? b.from?.parentId ?? b.from?.stopId ?? undefined) ?? '';
		if (isTransit(a.mode) && isTransit(b.mode)) {
			if (aEnd) {
				features.push({
					type: 'Feature',
					id: featureId++,
					geometry: { type: 'Point', coordinates: aEnd } as Point,
					properties: {
						role: 'disc', kind: 'arrive',
						stop_name: a.to?.name ?? discName,
						parent_uic: bareUicFrom(a.to?.parentId ?? a.to?.stopId ?? undefined) ?? discUic,
						mode_rank: legRank(a),
						disc_min_zoom: 0
					}
				});
			}
			if (bStart) {
				features.push({
					type: 'Feature',
					id: featureId++,
					geometry: { type: 'Point', coordinates: bStart } as Point,
					properties: {
						role: 'disc', kind: 'depart',
						stop_name: '',
						parent_uic: bareUicFrom(b.from?.parentId ?? b.from?.stopId ?? undefined) ?? discUic,
						mode_rank: legRank(b),
						disc_min_zoom: 0
					}
				});
			}
			if (aEnd && bStart) {
				features.push({
					type: 'Feature',
					id: featureId++,
					geometry: {
						type: 'LineString',
						coordinates: [aEnd, bStart]
					} as LineString,
					properties: {
						role: 'connector',
						parent_uic: discUic,
						disc_min_zoom: 0
					}
				});
			}
		} else if (!isTransit(a.mode) && isTransit(b.mode)) {
			if (bStart) {
				features.push({
					type: 'Feature',
					id: featureId++,
					geometry: { type: 'Point', coordinates: bStart } as Point,
					properties: {
						role: 'disc', kind: 'board',
						stop_name: b.from?.name ?? '',
						parent_uic: bareUicFrom(b.from?.parentId ?? b.from?.stopId ?? undefined) ?? '',
						mode_rank: legRank(b),
						disc_min_zoom: 0
					}
				});
			}
		} else if (isTransit(a.mode) && !isTransit(b.mode)) {
			if (aEnd) {
				features.push({
					type: 'Feature',
					id: featureId++,
					geometry: { type: 'Point', coordinates: aEnd } as Point,
					properties: {
						role: 'disc', kind: 'alight',
						stop_name: a.to?.name ?? '',
						parent_uic: bareUicFrom(a.to?.parentId ?? a.to?.stopId ?? undefined) ?? '',
						mode_rank: legRank(a),
						disc_min_zoom: 0
					}
				});
			}
		}
	}

	// Dedup discs by parent UIC + mode rank. Same-UIC discs (arrive +
	// depart at one transfer station) always share a min_zoom — they're a
	// pair, hiding one without the other would look broken. Across UICs,
	// a lower-ranked station hides whenever a higher-ranked one sits
	// within visual overlap distance at the current zoom.
	const discFeatures = features.filter((f) =>
		(f.properties?.role === 'disc' || f.properties?.role === 'connector')
		&& f.properties?.parent_uic
	);
	// Group by UIC.
	interface DiscGroup {
		uic: string;
		rank: number;
		coord: [number, number];
		features: Feature[];
	}
	const groups = new Map<string, DiscGroup>();
	for (const f of discFeatures) {
		const uic = f.properties!.parent_uic as string;
		const rank = (f.properties!.mode_rank as number | undefined) ?? 99;
		const geom = f.geometry;
		let c: [number, number] | null = null;
		if (geom.type === 'Point') c = geom.coordinates as [number, number];
		else if (geom.type === 'LineString' && geom.coordinates.length)
			c = geom.coordinates[0] as [number, number];
		if (!c) continue;
		const g = groups.get(uic);
		if (g) {
			g.rank = Math.min(g.rank, rank);
			g.features.push(f);
		} else {
			groups.set(uic, { uic, rank, coord: c, features: [f] });
		}
	}
	// Sort groups by rank (best first), then process in order.
	const sortedGroups = [...groups.values()].sort((a, b) => a.rank - b.rank);
	// Effective disc radius (px) used for overlap detection. Chosen below
	// the actual disc radius (~9-10 px at pill zoom) so a slight visual
	// collision between two nearby stations is allowed before dedup kicks
	// in — the user prefers this over dedup firing too eagerly.
	const DEDUP_RADIUS_PX = 6;
	const MERCATOR_SCALE = 156543.03392;
	const kept: DiscGroup[] = [];
	for (const g of sortedGroups) {
		let minZoom = 0;
		for (const higher of kept) {
			const d = haversineMeters(g.coord, higher.coord);
			if (d <= 0) { minZoom = Math.max(minZoom, 22); continue; }
			const latRad = (g.coord[1] + higher.coord[1]) * Math.PI / 360;
			// Min zoom at which the two are far enough apart in pixels:
			//   2 * R_px < d / mpp(z, lat)
			//   z > log2(2 * R_px * MERCATOR_SCALE * cos(lat) / d)
			const z = Math.log2(
				2 * DEDUP_RADIUS_PX * MERCATOR_SCALE * Math.cos(latRad) / d
			);
			minZoom = Math.max(minZoom, Math.ceil(z));
		}
		for (const f of g.features) {
			f.properties!.disc_min_zoom = Math.max(0, minZoom);
		}
		kept.push(g);
	}

	// Journey endpoints — the start / goal icons the concept describes.
	// Rendered as DOM markers separately from the source; the icon logic
	// picks them off the returned coords.
	const firstLeg = itinerary.legs[0];
	const lastLeg = itinerary.legs[itinerary.legs.length - 1];
	const firstCoords = decoded[0];
	const lastCoords = decoded[decoded.length - 1];
	const startCoord = snapEndpoint(firstCoords, 'start', placeCoord(firstLeg?.from));
	const goalCoord = snapEndpoint(lastCoords, 'end', placeCoord(lastLeg?.to));
	if (startCoord) bbox = updateBBox(bbox, startCoord);
	if (goalCoord) bbox = updateBBox(bbox, goalCoord);

	// If the journey starts with a transit leg (no preceding walk), the
	// concept swaps that first boarding disc for the start icon. Nothing
	// extra to add here — the disc rule above only writes discs at
	// walk↔transit boundaries and at transit↔transit transfers. Same
	// mirror at the end.
	//
	// When the first leg IS a walk, we render both: the start icon at the
	// walk's origin AND the disc at the walk→transit boundary. Symmetric
	// at the end.

	return {
		features: { type: 'FeatureCollection', features },
		startCoord,
		goalCoord,
		bbox,
		memberUics: Array.from(memberUicSet)
	};
}
