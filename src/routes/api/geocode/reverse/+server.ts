import { error, json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { PHOTON_BASE, PHOTON_USER_AGENT, PHOTON_EXCLUDE_TAGS } from '$lib/geocoding/config';

// Reverse-geocoding proxy → Photon (geocoding-search.md § Reverse geocoding).
// Stateless; no cache; no `lang` param (local-language names).

export const GET: RequestHandler = async ({ url, fetch }) => {
	const lonRaw = url.searchParams.get('lon');
	const latRaw = url.searchParams.get('lat');
	const lon = Number(lonRaw);
	const lat = Number(latRaw);
	if (!Number.isFinite(lon) || !Number.isFinite(lat)) {
		throw error(400, 'lon/lat required');
	}

	const upstream = new URL('/reverse', PHOTON_BASE);
	upstream.searchParams.set('lon', String(lon));
	upstream.searchParams.set('lat', String(lat));
	// Ask for a small list so the client can pick the first feature that is
	// actually AT the query point vs. having to prefix "Nähe" — see
	// geocoding-search.md § Reverse geocoding.
	upstream.searchParams.set('limit', '5');
	for (const tag of PHOTON_EXCLUDE_TAGS) upstream.searchParams.append('osm_tag', tag);

	const res = await fetch(upstream.toString(), {
		headers: { 'user-agent': PHOTON_USER_AGENT }
	});
	if (!res.ok) throw error(res.status, `photon reverse failed: ${res.status}`);
	return new Response(await res.text(), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
};
