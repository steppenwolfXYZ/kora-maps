// The time-difference bubble on a live alternative (bicycle-navigation.md
// § Live alternatives): a small chrome-styled pill ("+3 min", "−1 min")
// planted on the alternative just past where it parts from the
// navigated route. A DOM marker, not a map label — the style has no
// sprite for label backgrounds, and the app's own chrome is the look.
// Styled in app.css § Bicycle navigation (vendor-DOM marker).

import maplibregl from 'maplibre-gl';

export function deltaLabel(deltaSec: number): string {
	const min = Math.round(deltaSec / 60);
	if (min === 0) return 'same time';
	return `${min > 0 ? '+' : '−'}${Math.abs(min)} min`;
}

export function makeAlternativeBubble(
	map: maplibregl.Map,
	coord: [number, number],
	deltaSec: number
): maplibregl.Marker {
	// The marker element is MapLibre's (absolutely positioned); the pill
	// is a child so its own styling never fights that positioning.
	const el = document.createElement('div');
	const pill = document.createElement('div');
	pill.className = 'nav-alt-bubble';
	pill.classList.toggle('slower', deltaSec > 30);
	pill.textContent = deltaLabel(deltaSec);
	el.appendChild(pill);
	return new maplibregl.Marker({ element: el, anchor: 'bottom', offset: [0, -6] })
		.setLngLat(coord)
		.addTo(map);
}
