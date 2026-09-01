// Popup HTML builders (popups.md). Builders take typed payload objects
// already extracted from map features (see handlers.ts) — never raw
// MapLibre feature objects. This is the seam for a later migration to
// Svelte-rendered popups: only the render layer here would be swapped.

const fmt = (v: unknown) => v == null ? '–' : String(v);
const esc = (v: string) => v.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// ── Route from / to buttons ─────────────────────────────────────────────
// Shared by the station, pill-arrow and place popups. One gray group
// pill holds a route icon plus a brand-red segmented pill whose two
// halves — split by a white hairline — carry the same play triangle /
// stop square the map's start and goal pins use (drawn as inline SVG:
// the icon font's subset has no `stop` glyph, and reusing the pin
// shapes keeps the pair identical everywhere). Labels live in the
// tooltip only. Endpoint payload is decoded by handlers.ts §
// wirePopupRouteClicks.

export interface RouteButtonEndpoint {
	/** Merged UIC — omitted for non-station places. */
	uic?: string;
	name: string;
	coord: [number, number];
}

const ROUTE_BTN_CSS = `
		.popup-route-group { display: inline-flex; align-items: center; gap: 9px; margin-top: 10px; padding: 6px 6px 6px 12px; background: var(--gray-100); border-radius: var(--radius-pill); }
		.popup-route-lead { font-size: 22px; line-height: 1; color: var(--anthracite); flex: 0 0 auto; }
		.popup-route-pill { display: inline-flex; background: var(--brand); border-radius: var(--radius-pill); overflow: hidden; }
		.popup-route-btn { display: inline-flex; align-items: center; justify-content: center; width: 34px; height: 28px; padding: 0; border: none; background: transparent; color: var(--white); cursor: pointer; transition: background 0.12s ease; }
		.popup-route-btn + .popup-route-btn { border-left: 1.5px solid var(--white); }
		.popup-route-btn:hover { background: var(--brand-hover); }
		.popup-route-btn svg { width: 13px; height: 13px; display: block; fill: currentColor; }`;

// Same shapes as makeStartIconElement / makeGoalIconElement in
// routing/routeLayers.ts, normalised to a 12×12 box.
const PLAY_SVG = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 1.4 L10.2 6 L3 10.6 Z"/></svg>';
const STOP_SVG = '<svg viewBox="0 0 12 12" aria-hidden="true"><rect x="2.4" y="2.4" width="7.2" height="7.2"/></svg>';

function routeButtonsHtml(ep: RouteButtonEndpoint | null): string {
	if (!ep) return '';
	const payload = encodeURIComponent(JSON.stringify({
		uic: ep.uic || undefined, name: ep.name, coord: ep.coord
	}));
	const btn = (side: 'from' | 'to', glyph: string, tip: string) =>
		`<button class="popup-route-btn" type="button" title="${tip}" aria-label="${tip}"`
		+ ` data-route-side="${side}" data-route-endpoint="${payload}">${glyph}</button>`;
	return `<div class="popup-route-group">`
		+ `<span class="popup-route-lead material-symbols-outlined" aria-hidden="true">directions</span>`
		+ `<span class="popup-route-pill">`
		+ btn('from', PLAY_SVG, 'Route from here')
		+ btn('to', STOP_SVG, 'Route to here')
		+ `</span></div>`;
}

// ── Debug stop popup ────────────────────────────────────────────────────

export interface DebugStopPopupData {
	stopName: unknown;
	mode: unknown;
	stopId: unknown;
	platformLength: unknown;
	linesJson: unknown;
	currentOsmId: string;
}

export function buildDebugStopPopupHtml(d: DebugStopPopupData): string {
	const lengthVal = typeof d.platformLength === 'number'
		? `${d.platformLength} m`
		: d.platformLength ? `${d.platformLength} m` : '– (default)';
	let linesHtml = '';
	if (d.linesJson) {
		try {
			const lines: { ref: string; color: string; mode: string;
				origin: string; destination: string; osm_ids?: string[] }[] =
				JSON.parse(String(d.linesJson));
			if (lines.length) {
				const badges = lines.map(l => {
					const label = l.ref || l.mode || '?';
					const c = (l.color || '#888888').replace('#', '');
					const r = parseInt(c.slice(0, 2), 16);
					const g = parseInt(c.slice(2, 4), 16);
					const b = parseInt(c.slice(4, 6), 16);
					const lum = r * 0.299 + g * 0.587 + b * 0.114;
					const fg = lum > 140 ? '#000' : '#fff';
					const route = `${l.origin || '?'} → ${l.destination || '?'}`;
					const titleAttr = ` title="${route.replace(/"/g, '&quot;')}"`;
					const isCurrent = d.currentOsmId !== '' && Array.isArray(l.osm_ids)
						&& l.osm_ids.includes(d.currentOsmId);
					const ring = isCurrent
						? 'box-shadow:0 0 0 2px #000, 0 0 0 4px #fff;'
						: '';
					return `<span${titleAttr} style="display:inline-block;background:#${c};color:${fg};border-radius:3px;padding:1px 5px;margin:3px 4px 3px 0;font-size:10px;font-weight:600;letter-spacing:0.03em;cursor:default;${ring}">${label}</span>`;
				}).join('');
				linesHtml = `<div style="margin-top:6px">${badges}</div>`;
			}
		} catch { /* ignore malformed */ }
	}
	return `<div style="font-family:var(--font-ui);font-size:12px;line-height:1.5">
		<b>${fmt(d.stopName) || '(no name)'}</b> &ensp;[${fmt(d.mode)}]<br>
		id: ${fmt(d.stopId)}<br>
		platform length: ${lengthVal}
		${linesHtml}
	</div>`;
}

// ── Station popup ───────────────────────────────────────────────────────

export interface StationPopupData {
	stopName: string;
	/** '' when unknown */
	uic: string;
	/** Feature's own geometry coord (not the click position) */
	coord: [number, number] | null;
	/** Zoom-resolved departures per hour (see handlers.ts) */
	depHr: number | null;
	/** Zoom-resolved raw lines JSON string from the feature */
	linesRaw: unknown;
}

export function buildStationPopupHtml(d: StationPopupData): string {
	// Station endpoint payload for the popup route buttons — see
	// transit-routing.md § Entry points / Station popup buttons.
	const routeBtnHtml = routeButtonsHtml(
		d.coord ? { uic: d.uic, name: d.stopName, coord: d.coord } : null);

	let depLine = '';
	if (typeof d.depHr === 'number' && d.depHr > 0) {
		const disp = d.depHr < 10 ? d.depHr.toFixed(1) : String(Math.round(d.depHr));
		depLine = `<div style="margin-top:2px">Departures: <b>${disp}</b>/h</div>`;
	}

	let linesHtml = '';
	if (d.linesRaw) {
		try {
			const lines: {
				ref: string; color: string; mode: string;
				name?: string; tooltip?: string;
				keys?: string[]; bbox?: number[]; route?: string;
			}[] = JSON.parse(String(d.linesRaw));
			if (lines.length) {
				// Flat alternating children: badge, terminus, badge, terminus…
				// Collapsed mode hides termini and lets badges flow with
				// flex-wrap; expanded mode switches to a 2-col grid whose
				// first column is `max-content` — every badge stretches to
				// the widest label width so terminus text aligns.
				const cells = lines.map(l => {
					const label = l.ref || l.mode || '?';
					const lum = parseInt(l.color.slice(1, 3), 16) * 0.299
						+ parseInt(l.color.slice(3, 5), 16) * 0.587
						+ parseInt(l.color.slice(5, 7), 16) * 0.114;
					const fg = lum > 140 ? '#000' : '#fff';
					const tip = l.tooltip || l.name || '';
					const titleAttr = tip
						? ` title="${tip.replace(/"/g, '&quot;')}"`
						: '';
					// Line-detail payload: present only when the tiles carry
					// the baked keys + bbox (line-detail-view.md).
					const canDetail = Array.isArray(l.keys) && l.keys.length > 0
						&& Array.isArray(l.bbox) && l.bbox.length === 4;
					const dataAttr = canDetail
						? ` data-line-detail="${encodeURIComponent(JSON.stringify({
							keys: l.keys, bbox: l.bbox, ref: l.ref || '',
							mode: l.mode || '', color: l.color,
							route: l.route || l.tooltip || ''
						}))}"`
						: '';
					const cursor = canDetail ? 'cursor:pointer' : 'cursor:default';
					const badge = `<span class="popup-badge"${titleAttr}${dataAttr} style="background:${l.color};color:${fg};${cursor}">${label}</span>`;
					const terminus = `<span class="popup-line-terminus">${tip.replace(/</g, '&lt;')}</span>`;
					return badge + terminus;
				}).join('');

				linesHtml = `<details class="popup-lines">
					<summary class="popup-lines-summary">
						<span class="popup-chevron">▸</span>
						<span class="popup-lines-list">${cells}</span>
					</summary>
				</details>`;
			}
		} catch { /* ignore malformed */ }
	}

	return `<style>
		.popup-lines { margin-top: 6px; }
		.popup-lines-summary { list-style: none; cursor: pointer; display: flex; align-items: flex-start; gap: 4px; }
		.popup-lines-summary::-webkit-details-marker { display: none; }
		.popup-chevron { display: inline-block; color: var(--gray-400); font-size: 9px; padding-top: 4px; transition: transform 0.15s ease; flex: 0 0 auto; }
		.popup-lines[open] .popup-chevron { transform: rotate(90deg); }
		.popup-badge { display: inline-block; border-radius: 3px; padding: 2px 6px; font-size: 11px; font-weight: 800; letter-spacing: 0.02em; cursor: default; text-align: center; }
		.popup-lines-list { display: flex; flex-wrap: wrap; gap: 4px 3px; }
		.popup-line-terminus { display: none; }
		.popup-lines[open] .popup-lines-list { display: grid; grid-template-columns: max-content 1fr; column-gap: 8px; row-gap: 3px; align-items: center; max-height: 200px; overflow-y: auto; overflow-x: hidden; }
		.popup-lines[open] .popup-badge { display: block; }
		.popup-lines[open] .popup-line-terminus { display: inline; color: var(--gray-700); font-size: 12px; }
${ROUTE_BTN_CSS}
	</style><div style="font-family:var(--font-ui);font-size:13px;line-height:1.4;color:var(--gray-850)">
		<div style="font-weight:700;font-size:15px">${fmt(d.stopName) || '(no name)'}</div>
		${linesHtml}
		${depLine}
		${routeBtnHtml}
	</div>`;
}

// ── Pill-arrow popup (z17+) ─────────────────────────────────────────────
// Single-line summary for the specific (station, line) the pill-arrow
// represents. Rendered as one row in the same visual grid as the line
// popup (badge + A ↔ B). See popups.md § Pill-arrow popup.

export interface PillArrowPopupData {
	ref: string;
	mode: string;
	color: string;
	stopName: string;
	/** '' when unknown */
	uic: string;
	/** Representative point of the polygon feature */
	coord: [number, number] | null;
	firstTerminus: string;
	lastTerminus: string;
	/** Raw baked `line_key` property ('' when absent) */
	lineKey: string;
	/** Raw baked `line_bbox` property (comma-separated string) */
	lineBbox: string;
}

export function buildPillArrowPopupHtml(d: PillArrowPopupData): string {
	const routeBtnHtml = routeButtonsHtml(
		d.coord ? { uic: d.uic, name: d.stopName, coord: d.coord } : null);
	let route = '';
	if (d.firstTerminus && d.lastTerminus) {
		route = d.firstTerminus === d.lastTerminus
			? d.firstTerminus : `${d.firstTerminus} ↔ ${d.lastTerminus}`;
	}
	else if (d.firstTerminus) route = d.firstTerminus;
	else if (d.lastTerminus)  route = d.lastTerminus;
	const routeSafe = route.replace(/</g, '&lt;');
	const label = d.ref || d.mode || '?';
	const lum = parseInt(d.color.slice(1, 3), 16) * 0.299
		+ parseInt(d.color.slice(3, 5), 16) * 0.587
		+ parseInt(d.color.slice(5, 7), 16) * 0.114;
	const fg = lum > 140 ? '#000' : '#fff';
	// Line-detail-view payload: mirror of the station / line popup
	// badges. Enabled only when the tiles carry line_key + line_bbox on
	// the pill-arrow feature.
	const bboxParts = d.lineBbox.split(',').map(Number);
	const canDetail = !!d.lineKey && bboxParts.length === 4
		&& bboxParts.every((n) => Number.isFinite(n));
	const dataAttr = canDetail
		? ` data-line-detail="${encodeURIComponent(JSON.stringify({
			keys: [d.lineKey],
			bbox: bboxParts,
			ref: d.ref, mode: d.mode, color: d.color,
			route
		}))}"`
		: '';
	const cursor = canDetail ? 'cursor:pointer' : 'cursor:default';
	return `<style>
		.popup-pa-title { font-weight:700; font-size:15px; margin-bottom:6px; }
		.popup-pa-row { display: grid; grid-template-columns: max-content 1fr; column-gap: 8px; align-items: center; }
		.popup-pa-badge { display: block; border-radius: 3px; padding: 2px 6px; font-size: 11px; font-weight: 800; letter-spacing: 0.02em; text-align: center; }
		.popup-pa-route { color: var(--gray-700); font-size: 12px; }
${ROUTE_BTN_CSS}
	</style><div style="font-family:var(--font-ui);font-size:13px;line-height:1.4;color:var(--gray-850)">
		<div class="popup-pa-title">${fmt(d.stopName) || '(no name)'}</div>
		<div class="popup-pa-row">
			<span class="popup-pa-badge"${dataAttr} style="background:${d.color};color:${fg};${cursor}">${label}</span>
			<span class="popup-pa-route">${routeSafe}</span>
		</div>
		${routeBtnHtml}
	</div>`;
}

// ── Place popup (POI / address) ─────────────────────────────────────────
// Opened when the main search bar moves the map to a geocoded result
// (stop-search.md § Selection). Deliberately minimal: what the place is
// called, where it is, and the two route buttons.

export interface PlacePopupData {
	/** POI name, or the address itself for address results. */
	title: string;
	/** Second line — the POI's street address; null while the reverse
	 * lookup is still pending or when there is nothing to add. */
	address: string | null;
	/** Drives the leading icon only. */
	kind: 'poi' | 'address' | 'place';
	coord: [number, number] | null;
}

export function buildPlacePopupHtml(d: PlacePopupData): string {
	const icon = d.kind === 'address' ? 'home_work' : 'place';
	const routeBtnHtml = routeButtonsHtml(
		d.coord ? { name: d.title, coord: d.coord } : null);
	const addressHtml = d.address
		? `<div class="popup-place-address">${esc(d.address)}</div>`
		: '';
	return `<style>
		.popup-place { font-family: var(--font-ui); font-size: 13px; line-height: 1.4; color: var(--gray-850); }
		.popup-place-title { display: flex; align-items: flex-start; gap: 5px; font-weight: 700; font-size: 15px; }
		.popup-place-title .material-symbols-outlined { font-size: 17px; line-height: 1.25; color: var(--gray-500); flex: 0 0 auto; }
		.popup-place-address { margin-top: 2px; color: var(--gray-700); }${ROUTE_BTN_CSS}
	</style><div class="popup-place">
		<div class="popup-place-title">
			<span class="material-symbols-outlined" aria-hidden="true">${icon}</span>
			<span>${esc(d.title) || '(no name)'}</span>
		</div>
		${addressHtml}
		${routeBtnHtml}
	</div>`;
}

// ── Line popup ──────────────────────────────────────────────────────────

export interface LinePopupGroup {
	ref: string;
	mode: string;
	color: string;
	name: string;
	/** Distinct terminus names in first-seen order */
	termini: string[];
	lineKeys: string[];
	bbox: [number, number, number, number] | null;
}

export function buildLinePopupHtml(lines: LinePopupGroup[]): string {
	// Cells: alternating badge + terminus; grid layout in the wrapper
	// gives every badge the same width (widest ref) and left-flushes
	// the terminus text — matches the expanded station popup.
	const cells = lines.map(l => {
		const label = l.ref || l.mode || '?';
		const lum = parseInt(l.color.slice(1, 3), 16) * 0.299
			+ parseInt(l.color.slice(3, 5), 16) * 0.587
			+ parseInt(l.color.slice(5, 7), 16) * 0.114;
		const fg = lum > 140 ? '#000' : '#fff';
		const route = l.termini.length === 2
			? `${l.termini[0]} ↔ ${l.termini[1]}`
			: l.termini.join(' · ');
		const routeSafe = route.replace(/</g, '&lt;');
		const canDetail = l.lineKeys.length > 0 && l.bbox !== null;
		const dataAttr = canDetail
			? ` data-line-detail="${encodeURIComponent(JSON.stringify({
				keys: l.lineKeys, bbox: l.bbox, ref: l.ref,
				mode: l.mode, color: l.color, route
			}))}"`
			: '';
		const cursor = canDetail ? 'cursor:pointer' : 'cursor:default';
		const badge = `<span class="popup-badge"${dataAttr} style="background:${l.color};color:${fg};${cursor}">${label}</span>`;
		const terminus = `<span class="popup-line-terminus">${routeSafe}</span>`;
		return badge + terminus;
	}).join('');

	return `<style>
		.popup-line-list { font-family:var(--font-ui); color:var(--gray-850); }
		.popup-line-list .popup-badge { display: block; border-radius: 3px; padding: 2px 6px; font-size: 11px; font-weight: 800; letter-spacing: 0.02em; text-align: center; }
		.popup-line-list .popup-cells { display: grid; grid-template-columns: max-content 1fr; column-gap: 8px; row-gap: 3px; align-items: center; max-height: 200px; overflow-y: auto; overflow-x: hidden; }
		.popup-line-list .popup-line-terminus { color: var(--gray-700); font-size: 12px; }
	</style><div class="popup-line-list">
		<div class="popup-cells">${cells}</div>
	</div>`;
}
