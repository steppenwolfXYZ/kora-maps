import type { PageServerLoad } from './$types';

// The share document is loaded once in hooks.server.ts (which also rewrites
// the static OG tags) and handed through locals — a second disk read here
// would be wasted.

export const load: PageServerLoad = ({ locals }) => {
	return { share: locals.share ?? null };
};
