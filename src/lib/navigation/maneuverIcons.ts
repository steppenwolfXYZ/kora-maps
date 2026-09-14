// Turn icons of the navigation banner (bicycle-navigation.md § Maneuver
// banner): inline stroke SVGs drawn in the current text color, so the
// same glyph works white-on-red in the banner disc and gray in the
// "then" preview. Left-hand kinds mirror the right-hand path — one
// drawing per turn family. Inline rather than icon-font glyphs because
// the Material subset would need a dozen new entries for one feature.

import { ROUNDABOUT_EXIT_BEARING, type ManeuverKind, type RoundaboutExit } from './guidance';

// All drawn on the 24-unit grid with a 2.4 stroke and round caps; every
// arrow shaft enters from the bottom (the rider's own position) and the
// head sits where the movement ends, so the family reads consistently
// at 2.3rem in the banner disc and at 1.3rem in the "then" preview.
const RIGHT_PATHS: Record<string, string> = {
	// Shaft up the middle, head at the top.
	straight: 'M12 20V5M7.5 9.5 12 5l4.5 4.5',
	// Up, then a long 45° leg to the upper right so the head clears the
	// shaft.
	'slight-right': 'M9 20v-6.5l8-8M11.5 5.5H17V11',
	// Up, a quarter-circle bend, then straight right.
	right: 'M7 20v-8a5 5 0 0 1 5-5h6.5M14.5 3l4 4-4 4',
	// Up, then back down to the right — more than a right angle. The
	// diagonal runs long enough for the head to clear the shaft.
	'sharp-right': 'M8 20V6.5l9.5 9.5M17.5 10.5V16H12',
	// Up the left, over the top, back down the right.
	'uturn-right': 'M8 20V10a4 4 0 0 1 8 0v6M12 12l4 4 4-4',
	// Hull, cabin, funnel, water.
	ferry: 'M4 15h16l-2.5 4h-11zM8 15v-4h8v4M11 11V8h2v3M3 21.5c1.5-1.5 3-1.5 4.5 0s3 1.5 4.5 0 3-1.5 4.5 0 3 1.5 4.5 0',
	// Car-shuttle train (Autoverlad): a flat wagon on its rail, a bike on
	// the deck — the road sign's car, swapped for what the rider brings.
	shuttle: 'M2 20.5H22M6.5 17.3a1.2 1.2 0 1 0 2.4 0a1.2 1.2 0 1 0-2.4 0M15.1 17.3a1.2 1.2 0 1 0 2.4 0a1.2 1.2 0 1 0-2.4 0M3 14H21'
		+ 'M5 10a3 3 0 1 0 6 0a3 3 0 1 0-6 0M13 10a3 3 0 1 0 6 0a3 3 0 1 0-6 0'
		+ 'M8 10 10.5 5h3.5l2 5M14 5l-2.5 5L10.5 5',
	// Four steps rising to the right.
	stairs: 'M3 20h4.5v-4.5H12V11h4.5V6.5H21',
	// Pole with a swallow-tailed flag.
	destination: 'M6 21V3M6 4h11.5l-3 4.5 3 4.5H6',
	// Cabin with the up / down call arrows.
	elevator: 'M6.5 3h11v18h-11zM9.5 10l2.5-2.5 2.5 2.5M9.5 14l2.5 2.5 2.5-2.5'
};

/** Glyphs with fine detail draw thinner than the 2.4 arrows. */
const STROKE: Partial<Record<string, number>> = { shuttle: 1.8 };

const MIRRORED: Partial<Record<ManeuverKind, string>> = {
	'slight-left': 'slight-right',
	left: 'right',
	'sharp-left': 'sharp-right',
	'uturn-left': 'uturn-right'
};

// ── Roundabouts ──────────────────────────────────────────────────────────
// Drawn the way Material's roundabout_left / _right are: the path the
// rider actually travels — in from below, around the ring, out through
// the exit — as the thick stroke with the arrowhead at the exit, and
// the rest of the ring as a thin trace so the shape still says
// "roundabout". Right-hand traffic circulates counterclockwise seen from
// above, so the arc length grows with the exit angle: a right exit is a
// quarter turn, straight on a half, left three quarters. Generated from
// the exit bearing so all eight variants share one construction.
const RA_CX = 12, RA_CY = 13, RA_R = 4.5, RA_OUT = 9.5, RA_HEAD = 3.2;

function raPoint(phiDeg: number, r: number): [number, number] {
	const a = (phiDeg * Math.PI) / 180;
	return [RA_CX + r * Math.cos(a), RA_CY + r * Math.sin(a)];
}
const n = (v: number) => String(Math.round(v * 100) / 100);
const pt = ([x, y]: [number, number]) => `${n(x)} ${n(y)}`;

/** Counterclockwise-on-screen arc from φ1 down to φ2 (screen angles,
 * clockwise from +x), split so no piece exceeds 180°. */
function raArc(phi1: number, phi2: number): string {
	let span = (((phi1 - phi2) % 360) + 360) % 360;
	if (span === 0) span = 360;
	let out = '';
	let from = phi1;
	while (span > 0) {
		const step = Math.min(span, 180);
		from -= step;
		out += `A${RA_R} ${RA_R} 0 0 0 ${pt(raPoint(from, RA_R))}`;
		span -= step;
	}
	return out;
}

function roundaboutSvg(exit: RoundaboutExit): string {
	const bearing = ROUNDABOUT_EXIT_BEARING[exit];
	// The U-turn leaves where it came in: entry and exit run as two
	// parallel vertical spokes either side of the ring's bottom, the way
	// the rider actually sees it — in on the right, out on the left.
	const uturn = exit === 'uturn';
	const UTURN_DX = 2.2;
	const uturnPhi = (Math.atan2(Math.sqrt(RA_R * RA_R - UTURN_DX * UTURN_DX), UTURN_DX) * 180) / Math.PI;
	const entryPhi = uturn ? uturnPhi : 90;
	const exitPhi = uturn ? 180 - uturnPhi : (((bearing - 90) % 360) + 360) % 360;
	const entryRing = raPoint(entryPhi, RA_R);
	const exitRing = raPoint(exitPhi, RA_R);
	const entryOuter: [number, number] = uturn ? [entryRing[0], RA_CY + RA_OUT] : raPoint(entryPhi, RA_OUT);
	const tip: [number, number] = uturn ? [exitRing[0], RA_CY + RA_OUT] : raPoint(exitPhi, RA_OUT);
	// Arrowhead: two arms at 45° back from the tip along the spoke.
	const a = uturn ? Math.PI / 2 : (exitPhi * Math.PI) / 180;
	const d = [Math.cos(a), Math.sin(a)];
	const p = [-Math.sin(a), Math.cos(a)];
	const k = RA_HEAD / Math.SQRT2;
	const arm1: [number, number] = [tip[0] + (-d[0] + p[0]) * k, tip[1] + (-d[1] + p[1]) * k];
	const arm2: [number, number] = [tip[0] + (-d[0] - p[0]) * k, tip[1] + (-d[1] - p[1]) * k];
	const travelled = `M${pt(entryOuter)}L${pt(entryRing)}${raArc(entryPhi, exitPhi)}L${pt(tip)}`
		+ `M${pt(arm1)}L${pt(tip)}L${pt(arm2)}`;
	const rest = `M${pt(exitRing)}${raArc(exitPhi, entryPhi)}`;
	return `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" `
		+ `stroke-linecap="round" stroke-linejoin="round">`
		+ `<path d="${rest}" stroke-width="1.1" opacity="0.55"/>`
		+ `<path d="${travelled}" stroke-width="2.4"/></svg>`;
}

export function maneuverIconSvg(kind: ManeuverKind): string {
	if (kind === 'roundabout') return roundaboutSvg('straight');
	if (kind.startsWith('roundabout-')) return roundaboutSvg(kind.slice(11) as RoundaboutExit);
	const base = MIRRORED[kind];
	const d = RIGHT_PATHS[base ?? kind] ?? RIGHT_PATHS.straight;
	const transform = base ? ' transform="matrix(-1 0 0 1 24 0)"' : '';
	return `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" `
		+ `stroke-width="${STROKE[kind] ?? 2.4}" stroke-linecap="round" stroke-linejoin="round">`
		+ `<path d="${d}"${transform}/></svg>`;
}
