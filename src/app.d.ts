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
			/** Present while a routing itinerary is selected for map
			 * rendering (route-display.md § Lifecycle). Carries the
			 * fingerprint so browser back closes the route overlay and
			 * forward restores it. */
			routeSelection?: string;
		}
	}
}

export {};
