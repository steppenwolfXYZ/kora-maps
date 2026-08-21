import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import type { ShareData } from '$lib/routing/share';
import { deriveShareId, readShare, validateSharePayload, writeShare } from '$lib/server/share/store';
import { renderShareImage } from '$lib/server/share/render';

// POST /api/share — create a share (connection-sharing.md). Renders the
// og:image PNG up front so serving it later is a plain file read (and
// deletion makes it 404 naturally).

const MAX_BODY_BYTES = 200_000;

// Minimal per-IP throttle — this is a public endpoint that writes files.
// In-memory only (resets on restart), which is enough to blunt accidental
// loops and dumb abuse.
const WINDOW_MS = 60_000;
const MAX_PER_WINDOW = 10;
const recent = new Map<string, number[]>();

function throttled(ip: string): boolean {
	const now = Date.now();
	const stamps = (recent.get(ip) ?? []).filter((t) => now - t < WINDOW_MS);
	if (stamps.length >= MAX_PER_WINDOW) {
		recent.set(ip, stamps);
		return true;
	}
	stamps.push(now);
	recent.set(ip, stamps);
	if (recent.size > 10_000) recent.clear(); // unbounded-growth backstop
	return false;
}

export const POST: RequestHandler = async ({ request, getClientAddress }) => {
	if (throttled(getClientAddress())) {
		return new Response('Too many requests', { status: 429 });
	}
	const raw = await request.text();
	if (raw.length > MAX_BODY_BYTES) {
		return new Response('Payload too large', { status: 413 });
	}
	let body: unknown;
	try {
		body = JSON.parse(raw);
	} catch {
		return new Response('Invalid JSON', { status: 400 });
	}
	const payload = validateSharePayload(body);
	if (!payload) return new Response('Invalid share payload', { status: 400 });

	// Deterministic id → idempotent creation: the exact same connection
	// (same share fingerprint + endpoints) reuses the stored share instead
	// of creating a duplicate on every click.
	const id = deriveShareId(payload);
	if (await readShare(id)) return json({ id });

	const share: ShareData = {
		...payload,
		id,
		createdAt: new Date().toISOString()
	};
	try {
		const png = await renderShareImage(share);
		await writeShare(share, png);
	} catch (e) {
		console.error('[share] create failed:', e);
		return new Response('Share creation failed', { status: 500 });
	}
	return json({ id: share.id });
};
