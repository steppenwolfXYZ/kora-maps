import type { Handle } from '@sveltejs/kit';
import { timingSafeEqual } from 'node:crypto';
import { env } from '$env/dynamic/private';
import { readShare } from '$lib/server/share/store';
import { shareDescription, shareTitle } from '$lib/server/share/render';

function safeEqual(a: string, b: string): boolean {
	const ba = Buffer.from(a);
	const bb = Buffer.from(b);
	return ba.length === bb.length && timingSafeEqual(ba, bb);
}

function escAttr(s: string): string {
	return s
		.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
		.replaceAll('"', '&quot;');
}

/** Rewrite one static meta tag's content attribute in the app.html template. */
function setMeta(html: string, attr: 'name' | 'property', key: string, value: string): string {
	const re = new RegExp(`(<meta ${attr}="${key}" content=")[^"]*(")`);
	return html.replace(re, `$1${escAttr(value)}$2`);
}

// Basic auth for the /stats page. Credentials come from STATS_USER /
// STATS_PASS in .env (prod: the ENV_VARS repo secret); with either one
// unset the page pretends not to exist.
export const handle: Handle = async ({ event, resolve }) => {
	const { pathname } = event.url;

	// Share landing pages (connection-sharing.md § Preview image): app.html
	// carries the site-wide static OG tags, and page-level <svelte:head>
	// overrides are deliberately not used anywhere — crawlers would see two
	// competing tag sets and most prefer the first (the generic one). The
	// /s/<id> route is the one sanctioned exception, implemented here by
	// rewriting the static tags in the rendered template. The share doc is
	// stashed in locals so the page's server load doesn't read disk again.
	const shareMatch = pathname.match(/^\/s\/([A-Za-z0-9]+)$/);
	if (shareMatch) {
		const share = await readShare(shareMatch[1]);
		event.locals.share = share;
		if (share) {
			const title = `${shareTitle(share)} – Kora Maps`;
			const desc = shareDescription(share);
			const pageUrl = `${event.url.origin}/s/${share.id}`;
			const imageUrl = `${pageUrl}/image.png`;
			return resolve(event, {
				transformPageChunk: ({ html }) => {
					let out = html.replace(
						'<title>Kora Maps</title>',
						`<title>${escAttr(title)}</title>`
					);
					out = setMeta(out, 'name', 'description', desc);
					out = setMeta(out, 'property', 'og:title', shareTitle(share));
					out = setMeta(out, 'property', 'og:description', desc);
					out = setMeta(out, 'property', 'og:url', pageUrl);
					out = setMeta(out, 'property', 'og:image', imageUrl);
					out = setMeta(out, 'name', 'twitter:card', 'summary_large_image');
					return out;
				}
			});
		}
	}
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
