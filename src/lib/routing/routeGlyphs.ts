// The route-endpoint glyphs shared by the station popup's Route from / to
// buttons (map/popups/html.ts) and the map context menu
// (MapContextMenu.svelte): a dot with a line running edge to edge, so
// the from and to glyphs placed side by side read as one route line
// `o──|──o`, and the via glyph `──o──` as a stop along it. The SVG
// spans the whole host box (viewBox = box, 1 unit = 1px) and the line
// reaches the box edge with no padding — that is what lets neighbouring
// glyphs fuse. Where the glyphs stack vertically instead (context menu,
// the panel's endpoint rows) the dot sits flush at the outer edge
// (`inset` = `r`) so every row spans the same width and the lines align
// as one column: `o────` / `──o──` / `────o`. The map's route pins
// (routeLayers.ts) carry the same glyphs, clipped to the pin head.

export type RouteGlyphSide = 'from' | 'to' | 'via';

export interface RouteGlyphBox {
	w: number;
	h: number;
	/** Dot radius. */
	r: number;
	/** Dot centre's distance from the outer edge (from / to only). */
	inset: number;
	/** Line thickness — match any divider it has to fuse with. */
	line: number;
}

export function routeGlyphSvg(side: RouteGlyphSide, b: RouteGlyphBox): string {
	const cx = side === 'from' ? b.inset : side === 'to' ? b.w - b.inset : b.w / 2;
	const lineX = side === 'from' ? cx : 0;
	const lineW = side === 'from' ? b.w - cx : side === 'to' ? cx : b.w;
	return `<svg viewBox="0 0 ${b.w} ${b.h}" aria-hidden="true">`
		+ `<rect x="${lineX}" y="${(b.h - b.line) / 2}" width="${lineW}" height="${b.line}"/>`
		+ `<circle cx="${cx}" cy="${b.h / 2}" r="${b.r}"/></svg>`;
}
