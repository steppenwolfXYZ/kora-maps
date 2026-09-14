// The rider's position marker (bicycle-navigation.md § Follow-me map):
// a brand-red disc with a white heading chevron, rotated in map space
// so it points along the direction of travel however the camera turns.
// Without a heading the chevron gives way to a plain dot — the marker
// then just says "here", not "this way".

import maplibregl from 'maplibre-gl';

const SIZE = 40;

export class RiderMarker {
	private marker: maplibregl.Marker;
	private arrow: SVGElement;
	private dot: SVGElement;

	constructor(map: maplibregl.Map, coord: [number, number]) {
		const el = document.createElement('div');
		el.className = 'nav-rider';
		el.style.cssText = [
			`width: ${SIZE}px`, `height: ${SIZE}px`, 'pointer-events: none',
			'filter: drop-shadow(0 1px 3px rgba(0,0,0,0.4))'
		].join(';');
		el.innerHTML = `
			<svg viewBox="0 0 40 40" width="${SIZE}" height="${SIZE}" xmlns="http://www.w3.org/2000/svg">
				<circle cx="20" cy="20" r="13" style="fill: var(--brand); stroke: var(--white)" stroke-width="3"/>
				<path class="nav-rider-arrow" d="M20 9.5 L27 22 L20 18.5 L13 22 Z" style="fill: var(--white)"/>
				<circle class="nav-rider-dot" cx="20" cy="20" r="4" style="fill: var(--white)"/>
			</svg>`;
		this.arrow = el.querySelector('.nav-rider-arrow')!;
		this.dot = el.querySelector('.nav-rider-dot')!;
		this.marker = new maplibregl.Marker({
			element: el,
			rotationAlignment: 'map',
			pitchAlignment: 'map'
		}).setLngLat(coord).addTo(map);
		this.update(coord, null);
	}

	update(coord: [number, number], heading: number | null) {
		this.marker.setLngLat(coord);
		const has = heading !== null;
		this.arrow.style.display = has ? '' : 'none';
		this.dot.style.display = has ? 'none' : '';
		if (has) this.marker.setRotation(heading);
	}

	remove() {
		this.marker.remove();
	}
}
