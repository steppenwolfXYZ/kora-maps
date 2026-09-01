import type { Itinerary, Leg, LegMode } from './types';
import { legDuration } from './ranking';

// Shared itinerary formatting + icon helpers, extracted from
// ResultCard.svelte so the route map-mode header (RouteMapHeader.svelte)
// renders the same summary without duplicating the logic.

export function isTransitMode(mode: LegMode): boolean {
	return mode !== 'WALK' && mode !== 'BIKE' && mode !== 'CAR';
}

export function fmtTime(iso: string): string {
	const d = new Date(iso);
	return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export function fmtDuration(secs: number): string {
	const m = Math.round(secs / 60);
	if (m < 60) return `${m} min`;
	const h = Math.floor(m / 60);
	const rem = m % 60;
	return rem ? `${h} h ${rem} min` : `${h} h`;
}

export function fmtDistance(metres: number): string {
	if (metres < 1000) return `${Math.round(metres)} m`;
	const km = metres / 1000;
	return km < 10 ? `${km.toFixed(1)} km` : `${Math.round(km)} km`;
}

// Ascent / descent of a walk, e.g. "\u2191 53m \u2193 2m". Both halves always
// render, so a flat walk reads as an explicit "\u2191 0m \u2193 0m" rather than
// looking like missing data.
export function fmtElevation(up: number, down: number): string {
	return `\u2191 ${Math.round(up)}m \u2193 ${Math.round(down)}m`;
}

// Map MOTIS mode strings to Material Symbols icon names — parallel to
// StopSearch's MODE_ICON.
export const MODE_ICON: Record<string, string> = {
	WALK:          'directions_walk',
	BIKE:          'directions_bike',
	CAR:           'directions_car',
	TRANSIT:       'directions_transit',
	TRAM:          'tram',
	SUBWAY:        'subway',
	METRO:         'subway',
	RAIL:          'train',
	HIGHSPEED_RAIL:'train',
	LONG_DISTANCE: 'train',
	NIGHT_RAIL:    'train',
	REGIONAL_RAIL: 'train',
	REGIONAL_FAST_RAIL: 'train',
	BUS:           'directions_bus',
	COACH:         'directions_bus',
	FERRY:         'directions_boat',
	CABLE_CAR:     'gondola_lift',
	GONDOLA:       'gondola_lift',
	FUNICULAR:     'gondola_lift',
	AIRPLANE:      'flight'
};

export function iconFor(mode: LegMode): string {
	return MODE_ICON[mode] ?? 'directions_transit';
}

export function badgeTextColor(hex: string): string {
	const h = hex.replace(/^#/, '');
	const r = parseInt(h.slice(0, 2), 16);
	const g = parseInt(h.slice(2, 4), 16);
	const b = parseInt(h.slice(4, 6), 16);
	const lum = r * 0.299 + g * 0.587 + b * 0.114;
	return lum > 140 ? '#000' : '#fff';
}

// Legs to render in the strip: short inter-transit transfer walks (≤ 6 min
// between two transit rides) are dropped entirely; first/last-mile walks
// and longer inter-transit walks are kept, always with their minutes.
export function displayLegs(
	it: Itinerary
): { leg: Leg; dur: number; isWalk: boolean; index: number }[] {
	const transitIdx = it.legs
		.map((l, i) => (isTransitMode(l.mode) ? i : -1))
		.filter((i) => i >= 0);
	const firstT = transitIdx[0] ?? -1;
	const lastT = transitIdx[transitIdx.length - 1] ?? -1;
	// `index` is the leg's position in `it.legs` — kept because callers
	// (the via markers of via-stops.md) need to line up strip entries with
	// per-leg data the strip itself drops.
	const out: { leg: Leg; dur: number; isWalk: boolean; index: number }[] = [];
	it.legs.forEach((leg, i) => {
		const dur = legDuration(leg);
		const isWalk = leg.mode === 'WALK';
		if (isWalk && i > firstT && i < lastT && dur <= 6 * 60) return;
		out.push({ leg, dur, isWalk, index: i });
	});
	return out;
}
