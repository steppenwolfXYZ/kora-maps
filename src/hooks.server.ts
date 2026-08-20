import type { Handle } from '@sveltejs/kit';
import { timingSafeEqual } from 'node:crypto';
import { env } from '$env/dynamic/private';

function safeEqual(a: string, b: string): boolean {
	const ba = Buffer.from(a);
	const bb = Buffer.from(b);
	return ba.length === bb.length && timingSafeEqual(ba, bb);
}

// Basic auth for the /stats page. Credentials come from STATS_USER /
// STATS_PASS in .env (prod: the ENV_VARS repo secret); with either one
// unset the page pretends not to exist.
export const handle: Handle = async ({ event, resolve }) => {
	const { pathname } = event.url;
	if (pathname === '/stats' || pathname.startsWith('/stats/')) {
		const user = env.STATS_USER;
		const pass = env.STATS_PASS;
		if (!user || !pass) return new Response('Not found', { status: 404 });

		const header = event.request.headers.get('authorization') ?? '';
		let ok = false;
		if (header.startsWith('Basic ')) {
			const decoded = Buffer.from(header.slice(6), 'base64').toString('utf8');
			const sep = decoded.indexOf(':');
			ok =
				sep !== -1 &&
				safeEqual(decoded.slice(0, sep), user) &&
				safeEqual(decoded.slice(sep + 1), pass);
		}
		if (!ok) {
			return new Response('Authentication required', {
				status: 401,
				headers: { 'www-authenticate': 'Basic realm="Kora Maps stats", charset="UTF-8"' }
			});
		}
	}
	return resolve(event);
};
