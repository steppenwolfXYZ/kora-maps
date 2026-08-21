import { mkdir, readFile, rename, unlink, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { env } from '$env/dynamic/private';
import type { ShareData, SharePayload } from '$lib/routing/share';
import { endpointToParam } from '$lib/routing/url';

// File-backed share store (connection-sharing.md): one <id>.json +
// <id>.png per share. Deliberately trivial so a later database migration is
// one-record-per-file. The directory must live OUTSIDE the app build dir in
// production (deploys overwrite app/): set SHARES_DIR in .env, e.g.
// /var/www/koramaps.app/shares. The dev default lands in the gitignored
// data/ tree.

const SHARES_DIR = env.SHARES_DIR || 'data/shares';

const ID_RE = /^[A-Za-z0-9]{8}$/;
const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';

/** Deterministic share id: hash of the connection identity (share
 * fingerprint + canonical endpoint tokens). Sharing the exact same
 * connection twice — by anyone — maps to the same id, so creation is
 * idempotent and repeat clicks reuse the stored share instead of piling up
 * duplicates. Still non-enumerable (it's a hash, not a counter); the
 * per-byte mod-62 bias is irrelevant for identity purposes. */
export function deriveShareId(p: SharePayload): string {
	const key = [p.fingerprint, endpointToParam(p.from), endpointToParam(p.to)].join('|');
	const digest = createHash('sha256').update(key).digest();
	let id = '';
	for (let i = 0; i < 8; i++) id += ALPHABET[digest[i] % 62];
	return id;
}

/** Reject anything that isn't a well-formed id BEFORE it touches a path —
 * ids come straight from URLs. */
export function isValidShareId(id: string): boolean {
	return ID_RE.test(id);
}

function jsonPath(id: string): string {
	return path.join(SHARES_DIR, `${id}.json`);
}
function pngPath(id: string): string {
	return path.join(SHARES_DIR, `${id}.png`);
}

export async function readShare(id: string): Promise<ShareData | null> {
	if (!isValidShareId(id)) return null;
	try {
		return JSON.parse(await readFile(jsonPath(id), 'utf8')) as ShareData;
	} catch {
		return null;
	}
}

export async function readShareImage(id: string): Promise<Buffer | null> {
	if (!isValidShareId(id)) return null;
	try {
		return await readFile(pngPath(id));
	} catch {
		return null;
	}
}

export async function writeShare(data: ShareData, png: Uint8Array): Promise<void> {
	await mkdir(SHARES_DIR, { recursive: true });
	// tmp + rename so a crash mid-write never leaves a half-parseable share.
	const jp = jsonPath(data.id);
	const pp = pngPath(data.id);
	await writeFile(`${pp}.tmp`, png);
	await rename(`${pp}.tmp`, pp);
	await writeFile(`${jp}.tmp`, JSON.stringify(data));
	await rename(`${jp}.tmp`, jp);
}

export async function deleteShare(id: string): Promise<void> {
	if (!isValidShareId(id)) return;
	await unlink(jsonPath(id)).catch(() => {});
	await unlink(pngPath(id)).catch(() => {});
}

// ---------------------------------------------------------------------------
// Payload validation — the POST body is untrusted. Structural checks only;
// anything off → null (caller responds 400).

const MAX_LEGS = 30;
const MAX_STR = 300;

function okStr(v: unknown, required = false): boolean {
	if (v === undefined || v === null) return !required;
	return typeof v === 'string' && v.length <= MAX_STR;
}

function okIso(v: unknown): boolean {
	return typeof v === 'string' && v.length <= 40 && Number.isFinite(Date.parse(v));
}

function okEndpoint(ep: unknown): boolean {
	if (typeof ep !== 'object' || ep === null) return false;
	const e = ep as Record<string, unknown>;
	if (e.type === 'station') {
		return okStr(e.uic, true) && okStr(e.name, true) && okCoord(e.coord)
			&& okStr(e.mode) && okStr(e.pid);
	}
	if (e.type === 'point') {
		return okCoord(e.coord) && okStr(e.displayName)
			&& (e.kind === undefined || e.kind === 'address' || e.kind === 'poi');
	}
	return false; // `current` must have been resolved client-side
}

function okCoord(c: unknown): boolean {
	return Array.isArray(c) && c.length === 2
		&& typeof c[0] === 'number' && Number.isFinite(c[0])
		&& typeof c[1] === 'number' && Number.isFinite(c[1]);
}

function okLeg(l: unknown): boolean {
	if (typeof l !== 'object' || l === null) return false;
	const leg = l as Record<string, unknown>;
	if (!okStr(leg.mode, true) || !okIso(leg.startTime) || !okIso(leg.endTime)) return false;
	for (const k of ['routeShortName', 'routeColor', 'routeId', 'tripHeadsign',
		'agencyId', 'agencyName', 'tripId', 'headsign'] as const) {
		if (!okStr(leg[k])) return false;
	}
	for (const side of ['from', 'to'] as const) {
		const p = leg[side];
		if (p === undefined) continue;
		if (typeof p !== 'object' || p === null) return false;
		const place = p as Record<string, unknown>;
		if (!okStr(place.name) || !okStr(place.stopId) || !okStr(place.parentId)
			|| !okStr(place.track)) return false;
	}
	return true;
}

export function validateSharePayload(body: unknown): SharePayload | null {
	if (typeof body !== 'object' || body === null) return null;
	const b = body as Record<string, unknown>;
	if (b.v !== 1) return null;
	if (typeof b.fingerprint !== 'string' || !/^[0-9a-f]{8}$/.test(b.fingerprint)) return null;
	if (!okEndpoint(b.from) || !okEndpoint(b.to)) return null;
	const it = b.itinerary as Record<string, unknown> | undefined;
	if (typeof it !== 'object' || it === null) return null;
	if (!okIso(it.startTime) || !okIso(it.endTime)) return null;
	if (typeof it.duration !== 'number' || !Number.isFinite(it.duration)) return null;
	if (!Array.isArray(it.legs) || it.legs.length === 0 || it.legs.length > MAX_LEGS) return null;
	if (!it.legs.every(okLeg)) return null;
	const colors = b.legColors;
	if (!Array.isArray(colors) || colors.length !== it.legs.length) return null;
	if (!colors.every((c) => typeof c === 'string' && /^#[0-9a-fA-F]{3,8}$/.test(c))) return null;
	return b as unknown as SharePayload;
}
