import type { FeatureCollection, Feature, LineString, Point } from 'geojson';
import type { Itinerary, Leg, LegPlace } from './types';
import { decodePolyline } from './polyline';
import { legBadgeColor } from './legColor';

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

export function buildRouteGeoJSON(
	itinerary: Itinerary,
	routeColorIndex: Map<string, string> | null
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
				features.push({
					type: 'Feature',
					id: featureId++,
					geometry: { type: 'Point', coordinates: c } as Point,
					properties: {
						role: 'passthrough',
						leg_index: i
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
		if (isTransit(a.mode) && isTransit(b.mode)) {
			if (aEnd) {
				features.push({
					type: 'Feature',
					id: featureId++,
					geometry: { type: 'Point', coordinates: aEnd } as Point,
					properties: { role: 'disc', kind: 'arrive' }
				});
			}
			if (bStart) {
				features.push({
					type: 'Feature',
					id: featureId++,
					geometry: { type: 'Point', coordinates: bStart } as Point,
					properties: { role: 'disc', kind: 'depart' }
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
					properties: { role: 'connector' }
				});
			}
		} else if (!isTransit(a.mode) && isTransit(b.mode)) {
			if (bStart) {
				features.push({
					type: 'Feature',
					id: featureId++,
					geometry: { type: 'Point', coordinates: bStart } as Point,
					properties: { role: 'disc', kind: 'board' }
				});
			}
		} else if (isTransit(a.mode) && !isTransit(b.mode)) {
			if (aEnd) {
				features.push({
					type: 'Feature',
					id: featureId++,
					geometry: { type: 'Point', coordinates: aEnd } as Point,
					properties: { role: 'disc', kind: 'alight' }
				});
			}
		}
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
