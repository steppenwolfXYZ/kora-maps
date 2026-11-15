// Ambient SvelteKit app types.
import type { ShareData } from '$lib/routing/share';

declare global {
	namespace App {
		interface Locals {
			/** Share document for /s/<id> requests, loaded by hooks.server.ts
			 * (which also rewrites the OG meta tags from it). Null when the
			 * id is unknown or already deleted. */
			share?: ShareData | null;
		}
		interface PageState {
			/** Present while the line-detail view (Map.svelte) sits on this
			 * history entry; carries the selection so browser back closes
			 * the view and forward restores it. Mirrors
			 * LineDetailSelection. */
			lineDetail?: {
				keys: string[];
				bbox: [number, number, number, number];
				ref: string;
				mode: string;
				color: string;
				route: string;
			};
			/** The selected itinerary's fingerprint (route-display.md
			 * § Lifecycle), stamped on the current entry — never pushed —
			 * so back / forward onto the entry restores the selection. */
			routeSelection?: string;
			/** Present while bicycle navigation is active (bicycle-
			 * navigation.md): entering pushes an entry carrying it, so
			 * browser back leaves navigation and nothing else. */
			navigation?: boolean;
			/** Routing history layers (routing/state.svelte.ts § History
			 * layers): panel open → route entered → mobile map mode, one
			 * history entry each, so back peels exactly one layer. The
			 * value is the `routingDepth` of the entry that pushed the
			 * layer; 0 = stamped onto an existing entry (cold-load / share
			 * restore), which cannot be popped and is cleared in place. */
			routingPanel?: number;
			routingQuery?: number;
			routeMapMode?: number;
			/** The map menu's own entry (same depth semantics) — back
			 * closes the menu. */
			mapMenu?: number;
			/** Number of entries pushed through lib/historyStack.ts up to
			 * and including this entry — lets a UI close pop a layer
			 * together with everything above it. */
			routingDepth?: number;
		}
	}
}

export {};
