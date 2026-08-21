import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { read } from '$app/server';
import { initWasm, Resvg } from '@resvg/resvg-wasm';
import type { Itinerary, Leg } from '$lib/routing/types';
import type { ShareData } from '$lib/routing/share';
import { badgeTextColor, fmtDuration, isTransitMode } from '$lib/routing/itineraryFormat';
import { transferCount, walkSeconds } from '$lib/routing/ranking';
import saira400 from './fonts/saira-latin-400.ttf';
import saira700 from './fonts/saira-latin-700.ttf';
import saira800 from './fonts/saira-latin-800.ttf';
import sairaExt400 from './fonts/saira-latin-ext-400.ttf';
import sairaExt700 from './fonts/saira-latin-ext-700.ttf';
import sairaExt800 from './fonts/saira-latin-ext-800.ttf';
import logoSvg from '../../../../static/logo.svg?raw';

// og:image renderer (connection-sharing.md § Preview image): the connection
// header card as SVG, rasterized to a 1200×630 PNG with resvg-wasm. The wasm
// build is deliberate — the deploy artifact is assembled on an x64 runner
// but serves on an arm64 VPS, so a native binding would ship the wrong
// architecture. Fonts are static Saira instances (fontdb can't read the
// committed variable woff2s); regeneration: scripts note in the fonts dir
// is not needed — re-run fontTools instancer on static/fonts/saira-vf-*.woff2
// at weights 400/700/800 if the app font ever changes.

const W = 1200;
const H = 630;

// All time/date rendering is pinned to the timetable's zone — the server
// runs in UTC and the viewer's locale is unknown at render time.
const TZ = 'Europe/Zurich';
const timeFmt = new Intl.DateTimeFormat('en-GB', {
	timeZone: TZ, hour: '2-digit', minute: '2-digit', hour12: false
});
const dateFmt = new Intl.DateTimeFormat('en-GB', {
	timeZone: TZ, weekday: 'short', day: 'numeric', month: 'short'
});

function t(iso: string): string {
	return timeFmt.format(new Date(iso));
}

function esc(s: string): string {
	return s
		.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
		.replaceAll('"', '&quot;').replaceAll("'", '&apos;');
}

// Rough Saira advance width ≈ 0.52 em per char — good enough to shrink a
// line that would overflow its box (no shaping available at build time).
const AVG_EM = 0.52;
function fitSize(text: string, maxWidth: number, size: number, min = 22): number {
	const est = text.length * AVG_EM * size;
	if (est <= maxWidth) return size;
	return Math.max(min, Math.floor(maxWidth / (text.length * AVG_EM)));
}
function estWidth(text: string, size: number): number {
	return text.length * AVG_EM * size;
}

/** First/last transit station + times — mirrors ResultCard's summary line. */
function transitEndpoints(it: Itinerary): { fromName: string; fromTime: string; toName: string; toTime: string } | null {
	const transit = it.legs.filter((l) => isTransitMode(l.mode));
	if (!transit.length) return null;
	const first = transit[0];
	const last = transit[transit.length - 1];
	return {
		fromName: first.from?.name ?? '',
		fromTime: first.startTime,
		toName: last.to?.name ?? '',
		toTime: last.endTime
	};
}

/** og:title — "Bern 09:34 – Vounetse 11:28" (falls back to bare times for
 * walk-only shares). */
export function shareTitle(share: ShareData): string {
	const it = share.itinerary;
	const ep = transitEndpoints(it);
	if (!ep) return `Walk ${t(it.startTime)} – ${t(it.endTime)}`;
	return `${ep.fromName} ${t(ep.fromTime)} – ${ep.toName} ${t(ep.toTime)}`;
}

/** og:description — "Thu 21 Aug · 1 h 54 min · 3 transfers · 8 min walking". */
export function shareDescription(share: ShareData): string {
	const it = share.itinerary;
	const transfers = transferCount(it);
	return `${dateFmt.format(new Date(it.startTime))} · ${fmtDuration(it.duration)} · ` +
		`${transfers} transfer${transfers === 1 ? '' : 's'} · ` +
		`${fmtDuration(walkSeconds(it))} walking`;
}

interface BadgeSpec {
	text: string;
	bg: string;
	fg: string;
}

function badgeSpecs(share: ShareData): BadgeSpec[] {
	const out: BadgeSpec[] = [];
	share.itinerary.legs.forEach((leg: Leg, i: number) => {
		if (!isTransitMode(leg.mode)) return;
		const bg = share.legColors[i] ?? '#888888';
		out.push({ text: leg.routeShortName || '•', bg, fg: badgeTextColor(bg) });
	});
	return out;
}

/** Badge chain SVG, truncated to `maxWidth` with a trailing "+N" pill when
 * the connection has more legs than fit. Walk legs are omitted — the meta
 * line carries the walking summary. */
function badgeRow(specs: BadgeSpec[], x: number, y: number, maxWidth: number): string {
	const SIZE = 40;      // badge font size
	const HGT = 66;       // badge height
	const PADX = 24;      // horizontal padding inside a badge
	const GAP = 14;       // gap around the chevron
	const CHEV = 20;      // chevron glyph width
	const widthOf = (s: BadgeSpec) => Math.max(HGT, estWidth(s.text, SIZE) + 2 * PADX);

	let shown = specs.length;
	const totalWith = (n: number, plus: number) => {
		let w = 0;
		for (let i = 0; i < n; i++) w += widthOf(specs[i]) + (i > 0 ? GAP * 2 + CHEV : 0);
		if (plus > 0) w += GAP * 2 + CHEV + widthOf({ text: `+${plus}`, bg: '', fg: '' });
		return w;
	};
	while (shown > 1 && totalWith(shown, specs.length - shown) > maxWidth) shown--;
	const plus = specs.length - shown;

	let cx = x;
	const parts: string[] = [];
	const emit = (s: BadgeSpec, muted = false) => {
		const w = widthOf(s);
		const bg = muted ? '#e4e4e4' : s.bg;
		const fg = muted ? '#555555' : s.fg;
		parts.push(`<rect x="${cx.toFixed(1)}" y="${y}" width="${w.toFixed(1)}" height="${HGT}" rx="12" fill="${bg}"/>`);
		parts.push(`<text x="${(cx + w / 2).toFixed(1)}" y="${y + HGT / 2 + SIZE * 0.36}" font-size="${SIZE}" font-weight="800" fill="${fg}" text-anchor="middle">${esc(s.text)}</text>`);
		cx += w;
	};
	specs.slice(0, shown).forEach((s, i) => {
		if (i > 0) {
			cx += GAP;
			const cyc = y + HGT / 2;
			parts.push(`<polyline points="${cx + 3},${cyc - 14} ${cx + CHEV - 3},${cyc} ${cx + 3},${cyc + 14}" fill="none" stroke="#bbbbbb" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>`);
			cx += CHEV + GAP;
		}
		emit(s);
	});
	if (plus > 0) {
		cx += GAP;
		const cyc = y + HGT / 2;
		parts.push(`<polyline points="${cx + 3},${cyc - 14} ${cx + CHEV - 3},${cyc} ${cx + 3},${cyc + 14}" fill="none" stroke="#bbbbbb" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>`);
		cx += CHEV + GAP;
		emit({ text: `+${plus}`, bg: '', fg: '' }, true);
	}
	return parts.join('\n');
}

function buildSvg(share: ShareData): string {
	const it = share.itinerary;

	// Card box.
	const CX = 60, CY = 60, CW = 1080, CH = 510, R = 36;
	// Logo column on the right (logo.svg viewBox is 4489×6851, portrait).
	const LOGO_H = 300;
	const LOGO_W = LOGO_H * (4489 / 6851);
	const LOGO_X = CX + CW - 70 - LOGO_W;
	const LOGO_Y = CY + (CH - LOGO_H) / 2;
	// Text region.
	const TX = CX + 72;
	const TR = LOGO_X - 44; // right edge available to text
	const TW = TR - TX;

	const logoData = `data:image/svg+xml;base64,${Buffer.from(logoSvg).toString('base64')}`;

	const timeRange = `${t(it.startTime)} – ${t(it.endTime)}`;
	const durText = fmtDuration(it.duration);
	const durSize = 38;
	const timeSize = fitSize(timeRange, TW - estWidth(durText, durSize) - 40, 58);

	const ep = transitEndpoints(it);
	let routeLine = '';
	if (ep) {
		const full = `${ep.fromName} ${t(ep.fromTime)} – ${ep.toName} ${t(ep.toTime)}`;
		const size = fitSize(full, TW, 40);
		routeLine = `<text x="${TX}" y="290" font-size="${size}" fill="#444444">` +
			`<tspan font-weight="700" fill="#1a1a1a">${esc(ep.fromName)}</tspan> ${t(ep.fromTime)}` +
			` – <tspan font-weight="700" fill="#1a1a1a">${esc(ep.toName)}</tspan> ${t(ep.toTime)}</text>`;
	}

	const specs = badgeSpecs(share);
	const badges = specs.length
		? badgeRow(specs, TX, 330, TW)
		: `<text x="${TX}" y="375" font-size="40" font-weight="700" fill="#444444">Walk</text>`;

	const transfers = transferCount(it);
	const meta = `${dateFmt.format(new Date(it.startTime))} · ` +
		`${transfers} transfer${transfers === 1 ? '' : 's'} · ` +
		`${fmtDuration(walkSeconds(it))} walking`;

	return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" font-family="Saira">
	<defs>
		<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
			<stop offset="0" stop-color="#a0c8a0"/>
			<stop offset="1" stop-color="#c4a078"/>
		</linearGradient>
		<filter id="shadow" x="-10%" y="-10%" width="120%" height="120%">
			<feGaussianBlur stdDeviation="10"/>
		</filter>
	</defs>
	<rect width="${W}" height="${H}" fill="url(#bg)"/>
	<rect x="${CX + 4}" y="${CY + 12}" width="${CW}" height="${CH}" rx="${R}" fill="#000000" opacity="0.25" filter="url(#shadow)"/>
	<rect x="${CX}" y="${CY}" width="${CW}" height="${CH}" rx="${R}" fill="#ffffff"/>
	<text x="${TX}" y="200" font-size="${timeSize}" font-weight="700" fill="#1a1a1a">${esc(timeRange)}</text>
	<text x="${TR}" y="200" font-size="${durSize}" fill="#555555" text-anchor="end">${esc(durText)}</text>
	${routeLine}
	${badges}
	<text x="${TX}" y="490" font-size="34" fill="#777777">${esc(meta)}</text>
	<image x="${LOGO_X.toFixed(1)}" y="${LOGO_Y.toFixed(1)}" width="${LOGO_W.toFixed(1)}" height="${LOGO_H}" href="${logoData}"/>
</svg>`;
}

// ---------------------------------------------------------------------------
// Rasterization. initWasm may only run once per process; the font assets are
// loaded through SvelteKit's `read` (works in dev and inside the adapter-node
// build, where the .ttf imports resolve to emitted assets). The wasm blob is
// read straight out of node_modules — it ships with the deploy artifact.

let ready: Promise<Uint8Array[]> | null = null;

function init(): Promise<Uint8Array[]> {
	if (!ready) {
		ready = (async () => {
			const require = createRequire(import.meta.url);
			const wasmPath = require.resolve('@resvg/resvg-wasm/index_bg.wasm');
			try {
				await initWasm(await readFile(wasmPath));
			} catch (e) {
				// "Already initialized" — a dev-server module reload re-ran a
				// fresh copy of this module in a process whose wasm is already
				// up. Anything else is a real failure.
				if (!String(e).includes('Already initialized')) throw e;
			}
			return Promise.all(
				[saira400, saira700, saira800, sairaExt400, sairaExt700, sairaExt800]
					.map(async (asset) => new Uint8Array(await read(asset).arrayBuffer()))
			);
		})();
		ready.catch(() => { ready = null; }); // allow retry after a failed init
	}
	return ready;
}

export async function renderShareImage(share: ShareData): Promise<Uint8Array> {
	const fontBuffers = await init();
	const resvg = new Resvg(buildSvg(share), {
		font: { fontBuffers, loadSystemFonts: false, defaultFontFamily: 'Saira' },
		fitTo: { mode: 'width', value: W }
	});
	return resvg.render().asPng();
}
