import { buildStats } from '$lib/server/stats';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async () => {
	return { stats: buildStats() };
};
