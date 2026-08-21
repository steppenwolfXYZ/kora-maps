import type { RequestHandler } from './$types';
import { readShareImage } from '$lib/server/share/store';

// GET /s/<id>/image.png — the pre-rendered og:image. Just a file read;
// after expiry deletion this 404s, which is exactly the point. Short cache
// so deletion propagates to link-preview caches reasonably quickly.

export const GET: RequestHandler = async ({ params }) => {
	const png = await readShareImage(params.id);
	if (!png) return new Response('Not found', { status: 404 });
	return new Response(new Uint8Array(png), {
		headers: {
			'content-type': 'image/png',
			'cache-control': 'public, max-age=300'
		}
	});
};
