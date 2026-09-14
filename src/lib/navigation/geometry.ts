// Pure route geometry for bicycle navigation (bicycle-navigation.md):
// cumulative distances along the shape, projecting a position fix onto
// the route, and the small metric helpers the tracker needs. Everything
// works in a local equirectangular metre space (lon scaled by cos lat) —
// ample precision at route scale and cheap enough to scan a whole route
// per fix.

export type LonLat = [number, number];

const M_PER_DEG = 111320;

export interface RouteGeometry {
	coords: LonLat[];
	/** Cumulative metres from the route start at each coord. */
	cum: number[];
	totalM: number;
	/** cos(mean latitude) — the lon scale of the local metre space. */
	kLat: number;
}

export function buildGeometry(coords: LonLat[]): RouteGeometry {
	let latSum = 0;
	for (const c of coords) latSum += c[1];
	const kLat = Math.cos(((latSum / Math.max(1, coords.length)) * Math.PI) / 180);
	const cum: number[] = [0];
	for (let i = 1; i < coords.length; i++) {
		const dx = (coords[i][0] - coords[i - 1][0]) * M_PER_DEG * kLat;
		const dy = (coords[i][1] - coords[i - 1][1]) * M_PER_DEG;
		cum.push(cum[i - 1] + Math.hypot(dx, dy));
	}
	return { coords, cum, totalM: cum[cum.length - 1] ?? 0, kLat };
}

/** Metres between two positions. */
export function distanceM(a: LonLat, b: LonLat): number {
	const kLat = Math.cos((((a[1] + b[1]) / 2) * Math.PI) / 180);
	return Math.hypot((b[0] - a[0]) * M_PER_DEG * kLat, (b[1] - a[1]) * M_PER_DEG);
}

/** Compass bearing from a to b in degrees, 0 = north, clockwise. */
export function bearingDeg(a: LonLat, b: LonLat): number {
	const kLat = Math.cos((((a[1] + b[1]) / 2) * Math.PI) / 180);
	const dx = (b[0] - a[0]) * kLat;
	const dy = b[1] - a[1];
	return ((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360;
}

/** Shortest-arc blend of two headings (degrees); `t` = weight of `next`. */
export function blendHeading(prev: number, next: number, t: number): number {
	let d = ((next - prev + 540) % 360) - 180;
	return (prev + d * t + 360) % 360;
}

export interface Projection {
	/** Metres along the route at the projected point. */
	cumM: number;
	/** Metres between the fix and the projected point. */
	distM: number;
	/** Index of the segment (coords[i] → coords[i+1]) the point lies on. */
	segment: number;
	point: LonLat;
}

/** Candidates within this band of the closest segment compete on route
 * continuity instead of raw distance — an out-and-back route or a loop
 * has two segments almost equally close to the rider, and only the one
 * near the previous progress is the right one. */
const CANDIDATE_BAND_M = 15;
/** Candidates closer than this along the route are the same locality
 * (adjacent segments around one point), never rival passes. */
const LOCALITY_M = 100;

/** Nearest point on the route to `p`. With a previous progress value
 * the near-tie candidates resolve toward it, so progress never jumps to
 * a later pass of the same street. */
export function projectOntoRoute(
	g: RouteGeometry,
	p: LonLat,
	prevCumM: number | null
): Projection {
	const { coords, cum, kLat } = g;
	const px = p[0] * M_PER_DEG * kLat;
	const py = p[1] * M_PER_DEG;
	let best: Projection | null = null;
	const candidates: Projection[] = [];
	for (let i = 0; i < coords.length - 1; i++) {
		const ax = coords[i][0] * M_PER_DEG * kLat, ay = coords[i][1] * M_PER_DEG;
		const bx = coords[i + 1][0] * M_PER_DEG * kLat, by = coords[i + 1][1] * M_PER_DEG;
		const vx = bx - ax, vy = by - ay;
		const len2 = vx * vx + vy * vy;
		let t = len2 > 0 ? ((px - ax) * vx + (py - ay) * vy) / len2 : 0;
		t = t < 0 ? 0 : t > 1 ? 1 : t;
		const qx = ax + vx * t, qy = ay + vy * t;
		const d = Math.hypot(px - qx, py - qy);
		if (best && d > best.distM + CANDIDATE_BAND_M) continue;
		const proj: Projection = {
			cumM: cum[i] + (cum[i + 1] - cum[i]) * t,
			distM: d,
			segment: i,
			point: [qx / (M_PER_DEG * kLat), qy / M_PER_DEG]
		};
		candidates.push(proj);
		if (!best || d < best.distM) best = proj;
	}
	if (!best) return { cumM: 0, distM: Infinity, segment: 0, point: p };
	if (prevCumM === null) return best;
	// Near-ties, one representative per locality (the closest candidate
	// of each), then the locality nearest to where the rider already was.
	const ties = candidates
		.filter((c) => c.distM <= best!.distM + CANDIDATE_BAND_M)
		.sort((a, b) => a.distM - b.distM);
	const localities: Projection[] = [];
	for (const c of ties) {
		if (!localities.some((l) => Math.abs(l.cumM - c.cumM) < LOCALITY_M)) localities.push(c);
	}
	let pick = best;
	let pickScore = Infinity;
	for (const c of localities) {
		const score = Math.abs(c.cumM - prevCumM);
		if (score < pickScore) { pickScore = score; pick = c; }
	}
	return pick;
}
