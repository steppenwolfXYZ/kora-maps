// The rider's position marker (bicycle-navigation.md § Follow-me map).
// Without a heading: a brand-red dot with a white ring — "here". With
// one: a large brand-red navigation arrow with a white outline, rotated
// in map space so it points along the direction of travel however the
// camera turns — in attached mode that is straight up.

import maplibregl from 'maplibre-gl';

const SIZE = 52;

export class RiderMarker {
	private marker: maplibregl.Marker;
	private arrow: SVGElement;
	private dot: SVGElement;

	constructor(map: maplibregl.Map, coord: [number, number]) {
		const el = document.createElement('div');
		el.className = 'nav-rider';
		el.style.cssText = [
			`width: ${SIZE}px`, `height: ${SIZE}px`, 'pointer-events: none',
			'filter: drop-shadow(0 1px 3px rgba(0,0,0,0.45))'
		].join(';');
		el.innerHTML = `
			<svg viewBox="0 0 52 52" width="${SIZE}" height="${SIZE}" xmlns="http://www.w3.org/2000/svg">
				<g class="nav-rider-dot">
					<circle cx="26" cy="26" r="11" style="fill: var(--brand); stroke: var(--white)" stroke-width="3"/>
				</g>
				<path class="nav-rider-arrow" d="M26 4 L42 44 L26 35 L10 44 Z"
					style="fill: var(--brand); stroke: var(--white)" stroke-width="3" stroke-linejoin="round"/>
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
