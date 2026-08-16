// Shared narrow/wide breakpoint for the routing UI
// (routing-map-details-split.md § Constraints). One constant drives the
// map-icon behavior (peek vs fullscreen map mode) and the camera framing;
// the panel CSS media query must be kept in sync manually.

export const NARROW_BREAKPOINT = 700;

export function isNarrow(): boolean {
	return typeof window !== 'undefined' && window.innerWidth < NARROW_BREAKPOINT;
}
