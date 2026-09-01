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
	/** [lon, lat] of each via stop on this route, in journey order — DOM
	 * pin markers, same treatment as start / goal (via-stops.md). Empty
	 * when the query has no vias. */
	viaCoords: [number, number][];
}

// Reverse lookup parent-stop-id ("Parentch:1:sloid:7000") → merged UIC,
// built once per station-index Map (sloid-stop-identity.md: since the
// SLOID migration the UIC is no longer derivable from the ids themselves;
// the index's `p` field is the bridge).
const parentIdIndexCache = new WeakMap<Map<string, StationEntry>, Map<string, string>>();
function uicByParentId(stationIndex: Map<string, StationEntry> | null): Map<string, string> | null {
	if (!stationIndex) return null;
	let rev = parentIdIndexCache.get(stationIndex);
	if (!rev) {
		rev = new Map();
		for (const e of stationIndex.values()) if (e.p) rev.set(e.p, e.u);
		parentIdIndexCache.set(stationIndex, rev);
	}
	return rev;
}

/** Resolve a MOTIS place (parentId preferred, stopId as fallback) to the
 * station key the map's stop features carry as `parent_station` — the
 * merged UIC. Handles the dataset prefix ("ch_"), the legacy numeric
 * scheme ("Parent8507000" / "8507000:0:1", still used for foreign stops)
 * and the SLOID scheme ("Parentch:1:sloid:7000" / "ch:1:sloid:7000:0:19",
 * resolved through the station index). Unknown SLOID stations fall back to
 * their station-SLOID so same-station discs still group together. */
function stationKeyFrom(
	p: LegPlace | undefined,
	byParentId: Map<string, string> | null
): string | null {
	const raw = p?.parentId ?? p?.stopId;
	if (!raw) return null;
	const id = raw.replace(/^ch_/, '');
	const legacy = id.match(/^(?:Parent)?(\d+)(?::|$)/);
	if (legacy) return legacy[1];
	const sloid = id.match(/^(?:Parent)?(ch:1:sloid:\d+)/);
	if (sloid) return byParentId?.get(`Parent${sloid[1]}`) ?? sloid[1];
	return id;
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

/** Bbox of one leg — decoded polyline when present, plus the from/to
 * place coords as fallback. Used by Map.svelte to focus a clicked leg
 * from the expanded result card. */
export function legBounds(leg: Leg): [number, number, number, number] | null {
	let bb: [number, number, number, number] | null = null;
	for (const c of legCoords(leg)) bb = updateBBox(bb, c);
	for (const p of [leg.from, leg.to]) {
		const c = placeCoord(p);
		if (c) bb = updateBBox(bb, c);
	}
	return bb;
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
	stationIndex: Map<string, StationEntry> | null,
	/** Merged UICs of the query's via stops, in journey order
	 * (via-stops.md). Each one that appears on the route contributes a
	 * coordinate to `viaCoords`, which routeLayers plants a pin on — the
	 * same treatment start and goal get. */
	viaUics?: Set<string> | null
): RouteGeoJSONResult {
	const features: Feature[] = [];
	let bbox: [number, number, number, number] | null = null;
	const memberUicSet = new Set<string>();
	const viaPassCoord = new Map<string, [number, number]>();
	const byParentId = uicByParentId(stationIndex);
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
				const uic = stationKeyFrom(st, byParentId);
				if (uic) memberUicSet.add(uic);
				// A via ridden past on board has no leg boundary, so its pin
				// hangs off the intermediate stop instead of off a disc.
				if (uic && viaUics?.has(uic) && !viaPassCoord.has(uic)) {
					viaPassCoord.set(uic, c);
				}
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
				const uic = stationKeyFrom(p, byParentId);
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
	// Every transit boarding / alighting gets a disc — including the first
	// boarding and the last alighting even when no walk leg is adjacent
	// (handled below the boundary loop). The start/goal icon is a separate
	// DOM marker that either overlays the disc (journey starts/ends at the
	// station) or sits at the walk's outer end.
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
		const discUic = stationKeyFrom(a.to, byParentId) ?? stationKeyFrom(b.from, byParentId) ?? '';
		if (isTransit(a.mode) && isTransit(b.mode)) {
			if (aEnd) {
				features.push({
					type: 'Feature',
					id: featureId++,
					geometry: { type: 'Point', coordinates: aEnd } as Point,
					properties: {
						role: 'disc', kind: 'arrive',
						stop_name: a.to?.name ?? discName,
						parent_uic: stationKeyFrom(a.to, byParentId) ?? discUic,
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
						parent_uic: stationKeyFrom(b.from, byParentId) ?? discUic,
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
						parent_uic: stationKeyFrom(b.from, byParentId) ?? '',
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
						parent_uic: stationKeyFrom(a.to, byParentId) ?? '',
						mode_rank: legRank(a),
						disc_min_zoom: 0
					}
				});
			}
		}
	}

	// First-boarding and last-alighting discs when the terminal leg is
	// transit — the boundary loop above only fires between adjacent legs,
	// so a journey that starts/ends with transit (no adjacent walk) needs
	// its terminal disc emitted here. Dedup below folds a same-UIC pair
	// with an adjacent-walk disc into one, so this is safe even in cases
	// where a walk is present.
	const firstLeg = itinerary.legs[0];
	const lastLeg = itinerary.legs[itinerary.legs.length - 1];
	if (firstLeg && isTransit(firstLeg.mode)) {
		const boardCoord = snapEndpoint(decoded[0], 'start', placeCoord(firstLeg.from));
		if (boardCoord) {
			features.push({
				type: 'Feature',
				id: featureId++,
				geometry: { type: 'Point', coordinates: boardCoord } as Point,
				properties: {
					role: 'disc', kind: 'board',
					stop_name: firstLeg.from?.name ?? '',
					parent_uic: stationKeyFrom(firstLeg.from, byParentId) ?? '',
					mode_rank: legRank(firstLeg),
					disc_min_zoom: 0
				}
			});
		}
	}
	if (lastLeg && isTransit(lastLeg.mode)) {
		const alightCoord = snapEndpoint(decoded[decoded.length - 1], 'end', placeCoord(lastLeg.to));
		if (alightCoord) {
			features.push({
				type: 'Feature',
				id: featureId++,
				geometry: { type: 'Point', coordinates: alightCoord } as Point,
				properties: {
					role: 'disc', kind: 'alight',
					stop_name: lastLeg.to?.name ?? '',
					parent_uic: stationKeyFrom(lastLeg.to, byParentId) ?? '',
					mode_rank: legRank(lastLeg),
					disc_min_zoom: 0
				}
			});
		}
	}

	// Pin coordinate for every via. Taken in one sweep rather than at each
	// of the four disc-emitting sites — the rule is purely "which station
	// is this", independent of why the disc exists. A disc wins over an
	// intermediate stop: at a via the traveller alights at, the disc sits
	// on the actual platform the legs use.
	const viaDiscCoord = new Map<string, [number, number]>();
	if (viaUics?.size) {
		for (const f of features) {
			if (f.properties?.role !== 'disc') continue;
			const uic = f.properties.parent_uic as string | undefined;
			if (!uic || !viaUics.has(uic) || viaDiscCoord.has(uic)) continue;
			viaDiscCoord.set(uic, (f.geometry as Point).coordinates as [number, number]);
		}
	}
	const viaCoords: [number, number][] = [];
	for (const uic of viaUics ?? []) {
		const c = viaDiscCoord.get(uic) ?? viaPassCoord.get(uic);
		if (c) viaCoords.push(c);
	}

	// Dedup discs by proximity. Every disc is its own candidate — station
	// identity plays no role (a transfer's two platform discs at one station
	// collapse like any other close pair). Discs are ranked by mode
	// (train first), ties keep itinerary order; a lower-ranked disc hides
	// whenever a kept disc sits within visual overlap distance at the
	// current zoom. Connectors follow their endpoint discs (hidden while
	// either end is).
	interface DiscCand {
		rank: number;
		coord: [number, number];
		feature: Feature;
	}
	const cands: DiscCand[] = [];
	for (const f of features) {
		if (f.properties?.role !== 'disc' || f.geometry.type !== 'Point') continue;
		cands.push({
			rank: (f.properties.mode_rank as number | undefined) ?? 99,
			coord: f.geometry.coordinates as [number, number],
			feature: f
		});
	}
	// Stable sort: best rank first, itinerary order within a rank.
	const sortedCands = cands.map((c, i) => [c, i] as const)
		.sort((a, b) => a[0].rank - b[0].rank || a[1] - b[1])
		.map(([c]) => c);
	// Effective disc radius (px) used for overlap detection. Chosen below
	// the actual disc radius (~9-10 px at pill zoom) so a slight visual
	// collision between two nearby discs is allowed before dedup kicks
	// in — the user prefers this over dedup firing too eagerly.
	const DEDUP_RADIUS_PX = 6;
	// Metres per pixel at zoom 0 on the equator under MapLibre's 512-px
	// tile convention (40075016.686 / 512). The 256-px value (156543) is
	// one zoom level off and made dedup fire a level too early.
	const MPP_Z0 = 78271.517;
	const kept: DiscCand[] = [];
	for (const c of sortedCands) {
		let minZoom = 0;
		for (const higher of kept) {
			const d = haversineMeters(c.coord, higher.coord);
			if (d <= 0) { minZoom = Math.max(minZoom, 22); continue; }
			const latRad = (c.coord[1] + higher.coord[1]) * Math.PI / 360;
			// Min zoom at which the two are far enough apart in pixels:
			//   2 * R_px < d / mpp(z, lat),  mpp(z, lat) = MPP_Z0 * cos(lat) / 2^z
			//   z > log2(2 * R_px * MPP_Z0 * cos(lat) / d)
			// The layer expression tests `disc_min_zoom <= z` at integer
			// zoom steps; round to the nearest step (ceil made dedup fire
			// up to a full level too early, floor would let discs reappear
			// at half the threshold distance).
			const z = Math.log2(2 * DEDUP_RADIUS_PX * MPP_Z0 * Math.cos(latRad) / d);
			minZoom = Math.max(minZoom, Math.round(z));
		}
		c.feature.properties!.disc_min_zoom = Math.max(0, minZoom);
		kept.push(c);
	}
	// Connectors: visible only while both endpoint discs are. Endpoint
	// coords are the very disc coords (aEnd / bStart), so an exact match
	// finds the owning discs.
	const discMinZoomAt = (pt: [number, number]): number => {
		let m = 0;
		for (const c of cands) {
			if (c.coord[0] === pt[0] && c.coord[1] === pt[1])
				m = Math.max(m, c.feature.properties!.disc_min_zoom as number);
		}
		return m;
	};
	for (const f of features) {
		if (f.properties?.role !== 'connector' || f.geometry.type !== 'LineString') continue;
		const [p0, p1] = f.geometry.coordinates as [number, number][];
		f.properties.disc_min_zoom = Math.max(discMinZoomAt(p0), discMinZoomAt(p1));
	}

	// Journey endpoints — the start / goal icons the concept describes.
	// Rendered as DOM markers separately from the source; the icon logic
	// picks them off the returned coords. The icons overlay the terminal
	// discs when the journey starts/ends at the station, and sit at the
	// walk's outer end when a walk precedes/follows the transit legs.
	const firstCoords = decoded[0];
	const lastCoords = decoded[decoded.length - 1];
	const startCoord = snapEndpoint(firstCoords, 'start', placeCoord(firstLeg?.from));
	const goalCoord = snapEndpoint(lastCoords, 'end', placeCoord(lastLeg?.to));
	if (startCoord) bbox = updateBBox(bbox, startCoord);
	if (goalCoord) bbox = updateBBox(bbox, goalCoord);

	return {
		features: { type: 'FeatureCollection', features },
		startCoord,
		goalCoord,
		bbox,
		memberUics: Array.from(memberUicSet),
		viaCoords
	};
}
