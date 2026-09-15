// The rider's position marker (bicycle-navigation.md § Follow-me map).
// Without a heading: a brand-red dot with a white ring — "here". With
// one: a large brand-red navigation arrow with a white outline, rotated
// in map space so it points along the direction of travel however the
// camera turns — in attached mode that is straight up. Pitch-aligned to
// the map, so it lies on the road; the fixed on-screen arrow it hands
// over to applies the same perspective squash, so the two are one
// shape.

import maplibregl from 'maplibre-gl';

const SIZE = 64;

export class RiderMarker {
	private marker: maplibregl.Marker;
	private box: HTMLElement;
	private arrow: SVGElement;
	private dot: SVGElement;

	constructor(map: maplibregl.Map, coord: [number, number]) {
		const el = document.createElement('div');
		el.className = 'nav-rider';
		el.style.cssText = [
			`width: ${SIZE}px`, `height: ${SIZE}px`, 'pointer-events: none',
			'filter: drop-shadow(0 1px 3px rgba(0,0,0,0.45))'
		].join(';');
		// The inner box scales (CSS transition) between the map-marker size
		// and the fixed on-screen arrow's size, so the handover between the
		// two is one continuous motion; the outer element stays MapLibre's.
		el.innerHTML = `
			<div class="nav-rider-box" style="width:${SIZE}px;height:${SIZE}px;transform-origin:50% 50%">
			<svg viewBox="0 0 64 64" width="${SIZE}" height="${SIZE}" xmlns="http://www.w3.org/2000/svg">
				<g class="nav-rider-dot">
					<circle cx="32" cy="32" r="20" style="fill: var(--brand); stroke: var(--white)" stroke-width="3.5"/>
					<circle cx="32" cy="32" r="6.5" style="fill: var(--white)"/>
				</g>
				<path class="nav-rider-arrow" d="M32 6 L51 54 L32 43 L13 54 Z"
					style="fill: var(--brand); stroke: var(--white)" stroke-width="3" stroke-linejoin="round"/>
			</svg>
			</div>`;
		this.box = el.querySelector('.nav-rider-box')!;
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
		if (this.glide !== null) cancelAnimationFrame(this.glide);
		this.glide = null;
		this.marker.setLngLat(coord);
		const has = heading !== null;
		this.arrow.style.display = has ? '' : 'none';
		this.dot.style.display = has ? 'none' : '';
		if (has) this.marker.setRotation(heading);
	}

	/** Off while following — the fixed on-screen arrow stands in. */
	setVisible(visible: boolean) {
		this.marker.getElement().style.visibility = visible ? '' : 'hidden';
	}

	/** Grow toward the fixed arrow's size while the camera brings the
	 * rider into place (`scale` = fixed size / marker size), shrink back
	 * when following is suspended. */
	setScale(scale: number, ms: number) {
		this.box.style.transition = `transform ${ms}ms ease`;
		this.box.style.transform = `scale(${scale})`;
	}

	private glide: number | null = null;

	/** Slide from `from` to the current position over `ms` — the
	 * detach handover: the marker starts where the fixed arrow was and
	 * settles on the rider's true position. */
	glideFrom(from: [number, number], to: [number, number], ms: number) {
		if (this.glide !== null) cancelAnimationFrame(this.glide);
		const t0 = performance.now();
		const step = (t: number) => {
			const k = Math.min(1, (t - t0) / ms);
			const e = 1 - (1 - k) * (1 - k);
			this.marker.setLngLat([from[0] + (to[0] - from[0]) * e, from[1] + (to[1] - from[1]) * e]);
			this.glide = k < 1 ? requestAnimationFrame(step) : null;
		};
		this.glide = requestAnimationFrame(step);
	}

	remove() {
		this.marker.remove();
	}
}
