// Ambient SvelteKit app types.
declare global {
	namespace App {
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
		}
	}
}

export {};
