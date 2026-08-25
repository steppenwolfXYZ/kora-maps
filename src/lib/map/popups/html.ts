// Popup HTML builders (popups.md). Builders take typed payload objects
// already extracted from map features (see handlers.ts) — never raw
// MapLibre feature objects. This is the seam for a later migration to
// Svelte-rendered popups: only the render layer here would be swapped.

const fmt = (v: unknown) => v == null ? '–' : String(v);

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
	const routeBtnHtml = d.coord ? `<div class="popup-route-btns">
		<button class="popup-route-btn" data-route-side="from" data-route-endpoint="${encodeURIComponent(JSON.stringify({
			uic: d.uic || undefined, name: d.stopName, coord: d.coord
		}))}">
			<span class="material-symbols-outlined" aria-hidden="true">play_arrow</span>Route from here
		</button>
		<button class="popup-route-btn" data-route-side="to" data-route-endpoint="${encodeURIComponent(JSON.stringify({
			uic: d.uic || undefined, name: d.stopName, coord: d.coord
		}))}">
			<span class="material-symbols-outlined" aria-hidden="true">sports_score</span>Route to here
		</button>
	</div>` : '';

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
		.popup-route-btns { display: flex; gap: 4px; margin-top: 8px; }
		.popup-route-btn { flex: 1 1 0; display: inline-flex; align-items: center; justify-content: center; gap: 4px; padding: 4px 6px; border: 1px solid var(--gray-200); border-radius: 4px; background: var(--gray-50); color: var(--gray-850); font-family: inherit; font-size: 11px; cursor: pointer; }
		.popup-route-btn:hover { background: var(--gray-100); }
		.popup-route-btn .material-symbols-outlined { font-size: 14px; color: var(--gray-500); }
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
	const routeBtnHtml = d.coord ? `<div class="popup-route-btns">
		<button class="popup-route-btn" data-route-side="from" data-route-endpoint="${encodeURIComponent(JSON.stringify({
			uic: d.uic || undefined, name: d.stopName, coord: d.coord
		}))}">
			<span class="material-symbols-outlined" aria-hidden="true">trip_origin</span>Route from here
		</button>
		<button class="popup-route-btn" data-route-side="to" data-route-endpoint="${encodeURIComponent(JSON.stringify({
			uic: d.uic || undefined, name: d.stopName, coord: d.coord
		}))}">
			<span class="material-symbols-outlined" aria-hidden="true">place</span>Route to here
		</button>
	</div>` : '';
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
		.popup-route-btns { display: flex; gap: 4px; margin-top: 8px; }
		.popup-route-btn { flex: 1 1 0; display: inline-flex; align-items: center; justify-content: center; gap: 4px; padding: 4px 6px; border: 1px solid var(--gray-200); border-radius: 4px; background: var(--gray-50); color: var(--gray-850); font-family: inherit; font-size: 11px; cursor: pointer; }
		.popup-route-btn:hover { background: var(--gray-100); }
		.popup-route-btn .material-symbols-outlined { font-size: 14px; color: var(--gray-500); }
	</style><div style="font-family:var(--font-ui);font-size:13px;line-height:1.4;color:var(--gray-850)">
		<div class="popup-pa-title">${fmt(d.stopName) || '(no name)'}</div>
		<div class="popup-pa-row">
			<span class="popup-pa-badge"${dataAttr} style="background:${d.color};color:${fg};${cursor}">${label}</span>
			<span class="popup-pa-route">${routeSafe}</span>
		</div>
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
