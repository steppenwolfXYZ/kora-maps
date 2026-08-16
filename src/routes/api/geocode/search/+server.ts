import { error, json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import {
	PHOTON_BASE,
	PHOTON_USER_AGENT,
	SEARCH_BBOX,
	SEARCH_LIMIT,
	PHOTON_EXCLUDE_TAGS
} from '$lib/geocoding/config';

// Forward search proxy → Photon (geocoding-search.md § Provider and proxy).
// Stateless; no cache; forwards the query with a fixed bbox filter and no
// `lang` param (Photon returns OSM's base `name` tag, i.e. local-language).

export const GET: RequestHandler = async ({ url, fetch }) => {
	const q = url.searchParams.get('q')?.trim() ?? '';
	if (q.length < 2) return json({ features: [] });

	const upstream = new URL('/api/', PHOTON_BASE);
	upstream.searchParams.set('q', q);
	upstream.searchParams.set('limit', String(SEARCH_LIMIT));
	upstream.searchParams.set('bbox', SEARCH_BBOX);
	for (const tag of PHOTON_EXCLUDE_TAGS) upstream.searchParams.append('osm_tag', tag);

	const res = await fetch(upstream.toString(), {
		headers: { 'user-agent': PHOTON_USER_AGENT }
	});
	if (!res.ok) throw error(res.status, `photon search failed: ${res.status}`);
	return new Response(await res.text(), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
};
