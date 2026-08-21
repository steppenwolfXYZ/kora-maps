import type { RequestHandler } from './$types';
import { deleteShare, readShare } from '$lib/server/share/store';
import { verifyShare } from '$lib/server/share/verify';

// DELETE /api/share/<id> — expiry cleanup (connection-sharing.md § Shared
// view). The client fires this after its own re-query found no match, but
// the share is only deleted once the SERVER's re-query confirms the
// connection is gone — otherwise anyone holding the link could delete a
// still-valid share, and a client-side transient would destroy it.

export const DELETE: RequestHandler = async ({ params }) => {
	const share = await readShare(params.id);
	if (!share) return new Response(null, { status: 404 });

	const result = await verifyShare(share);
	if (result === 'present') return new Response('Share still valid', { status: 409 });
	if (result === 'error') return new Response('Verification unavailable', { status: 502 });

	await deleteShare(share.id);
	return new Response(null, { status: 204 });
};
