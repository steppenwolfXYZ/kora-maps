// Nginx access-log parser for the /stats page. Reads the per-site
// combined-format log plus its rotated siblings (.1, .2.gz, …) and
// aggregates per-day hits, routing plan requests, unique client IPs,
// and the most-requested route pairs. Place tokens stay unresolved
// here ("u:<uic>" / "c:<lat>,<lon>") — the page resolves them to
// station names client-side via stop_search_index.json, which never
// exists inside the server build (deployment.md § SSR constraints).
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { basename, dirname, join } from 'node:path';
import { gunzipSync } from 'node:zlib';
import { env } from '$env/dynamic/private';

const DEFAULT_LOG = '/var/log/nginx/koramaps/access.log';

// Rough bot/tooling filter on user agent. Matches are counted
// separately per day and excluded from hits / plans / unique IPs.
const BOT_UA =
	/bot|crawl|spider|slurp|preview|scan|monitor|probe|python|curl|wget|headless|go-http|okhttp|zgrab|censys/i;

// combined format: ip - user [time] "request" status bytes "referer" "ua"
const LINE_RE = /^(\S+) \S+ \S+ \[([^\]]+)\] "([^"]*)" (\d{3}) \S+ "[^"]*" "([^"]*)"/;

const MONTHS: Record<string, string> = {
	Jan: '01', Feb: '02', Mar: '03', Apr: '04', May: '05', Jun: '06',
	Jul: '07', Aug: '08', Sep: '09', Oct: '10', Nov: '11', Dec: '12'
};

export interface DayStats {
	/** ISO day, e.g. "2026-08-20" (server-local time from the log) */
	day: string;
	hits: number;
	planRequests: number;
	uniqueIps: number;
	botHits: number;
}

export interface RoutePair {
	/** "u:<uic>" | "c:<lat>,<lon>" (3 decimals) | "?:<raw>" */
	from: string;
	to: string;
	count: number;
}

export interface Stats {
	available: boolean;
	logPath: string;
	days: DayStats[];
	topRoutes: RoutePair[];
	totalHits: number;
	totalPlans: number;
	totalUniqueIps: number;
}

/** "20/Aug/2026:20:17:01 +0200" → "2026-08-20" */
function logTimeToDay(t: string): string | null {
	const m = t.match(/^(\d{2})\/([A-Za-z]{3})\/(\d{4}):/);
	if (!m || !MONTHS[m[2]]) return null;
	return `${m[3]}-${MONTHS[m[2]]}-${m[1]}`;
}

/** fromPlace/toPlace value → grouping token */
function placeToken(raw: string): string {
	const station = raw.match(/^ch_Parent(\d+)$/);
	if (station) return `u:${station[1]}`;
	const coord = raw.match(/^(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$/);
	if (coord) return `c:${(+coord[1]).toFixed(3)},${(+coord[2]).toFixed(3)}`;
	return `?:${raw.slice(0, 60)}`;
}

function readLogFiles(logPath: string): string[] {
	const dir = dirname(logPath);
	const base = basename(logPath);
	if (!existsSync(dir)) return [];
	const files = readdirSync(dir)
		.filter((f) => f === base || f.startsWith(base + '.'))
		.map((f) => join(dir, f));
	const texts: string[] = [];
	for (const f of files) {
		try {
			const buf = readFileSync(f);
			texts.push(f.endsWith('.gz') ? gunzipSync(buf).toString('utf8') : buf.toString('utf8'));
		} catch {
			// unreadable / rotated away mid-read — skip
		}
	}
	return texts;
}

export function buildStats(): Stats {
	const logPath = env.STATS_ACCESS_LOG || DEFAULT_LOG;
	const texts = readLogFiles(logPath);

	const days = new Map<string, { hits: number; plans: number; ips: Set<string>; bots: number }>();
	const routeCounts = new Map<string, number>();
	const allIps = new Set<string>();
	let totalHits = 0;
	let totalPlans = 0;

	for (const text of texts) {
		for (const line of text.split('\n')) {
			const m = line.match(LINE_RE);
			if (!m) continue;
			const [, ip, time, request, , ua] = m;
			const day = logTimeToDay(time);
			if (!day) continue;
			let d = days.get(day);
			if (!d) days.set(day, (d = { hits: 0, plans: 0, ips: new Set(), bots: 0 }));

			if (BOT_UA.test(ua)) {
				d.bots++;
				continue;
			}
			d.hits++;
			totalHits++;
			d.ips.add(ip);
			allIps.add(ip);

			// request = "GET /path?query HTTP/1.1"
			const target = request.split(' ')[1] ?? '';
			if (!target.startsWith('/routing/api/v1/plan')) continue;
			d.plans++;
			totalPlans++;
			const qs = target.indexOf('?');
			if (qs === -1) continue;
			try {
				const params = new URLSearchParams(target.slice(qs + 1));
				const from = params.get('fromPlace');
				const to = params.get('toPlace');
				if (!from || !to) continue;
				const key = `${placeToken(from)}|${placeToken(to)}`;
				routeCounts.set(key, (routeCounts.get(key) ?? 0) + 1);
			} catch {
				// malformed query string — skip
			}
		}
	}

	const topRoutes: RoutePair[] = [...routeCounts.entries()]
		.sort((a, b) => b[1] - a[1])
		.slice(0, 30)
		.map(([key, count]) => {
			const [from, to] = key.split('|');
			return { from, to, count };
		});

	return {
		available: texts.length > 0,
		logPath,
		days: [...days.entries()]
			.sort((a, b) => (a[0] < b[0] ? 1 : -1))
			.map(([day, d]) => ({
				day,
				hits: d.hits,
				planRequests: d.plans,
				uniqueIps: d.ips.size,
				botHits: d.bots
			})),
		topRoutes,
		totalHits,
		totalPlans,
		totalUniqueIps: allIps.size
	};
}
