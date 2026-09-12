import {
	boardingCount, pruneDominated, walkSeconds, type RankOptions,
	REVERSE_DISPLACE_MAX_GAP_MS, T_SLACK_MS
} from '$lib/routing/ranking';
import { itineraryFingerprint } from '$lib/routing/fingerprint';
import { shareFingerprint } from '$lib/routing/share';
import { planOptionParams, type PlanOptionParams, type RoutingOptionValues } from '$lib/routing/optionParams';
import type { Itinerary, PlanResponse, TimeMode } from '$lib/routing/types';
import { allItineraries, planMotis, type MotisVia } from './motis';

// The transit search cascade (server-side-transit-planning.md). One
// /api/plan request runs the whole thing here against MOTIS over
// loopback and returns the final, pruned, sorted list — the browser sees
// exactly one request per user action. The stages, triggers and pruning
// are the ones the client used to run (transit-routing.md § Ranking,
// routing-options.md); moving them here changed no rule.
//
//   Stage 1  narrow initial query (30 min walking budget, 15 min window)
//   Stage 2  wide retry (8 h budget + full transfer table) on trigger
//   Stage 3  time-advance hop cascade until the result target is met
//   Stage 2c sparse-service gap found mid-cascade → redo wide
//
// "Earlier" / "later" are the same engine continuing past the shown
// list's edge. The endpoint is stateless: the request carries the full
// extension history and the engine replays it — MOTIS is deterministic,
// so the replay reproduces the list the client already shows and then
// extends it. A small in-memory cache of finished states short-cuts the
// replay for the common click-through case.
//
// Coverage (search-coverage-window.md): the state records the time spans
// it has actually searched — departures for leave-at hops, arrivals for
// arrive-by hops — taken from the interval the fork reports having
// searched, never inferred from what came back. A response is a Pareto
// set, not a time slice: under minimize walking it routinely holds a
// walk-lighter journey hours out, and seeding the next hop from that
// anchor skipped everything in between. Only journeys anchored inside the
// searched span enter the candidate set; the shown list is therefore a
// pure function of the query and its coverage, whichever path built it.
//
// Settled display (search-coverage-window.md § Settled display): a
// journey is shown only once coverage reaches past the span in which a
// dominator of it could lie, so what is on screen can never be retired
// by a later load — no pinning, the list stays a function of coverage
// and only ever grows at the extended end.

export type Extension = 'earlier' | 'later';

export interface PlanQuery {
	/** MOTIS place strings (place.ts). Never a `current` endpoint — the
	 * client resolves its geolocation to a coordinate before sending. */
	fromPlace: string;
	toPlace: string;
	mode: TimeMode;
	/** Concrete ISO timestamp — the client pins "now" before sending so
	 * the replay of an extension sees the same anchor. */
	time: string;
	vias: MotisVia[];
	options: RoutingOptionValues;
	/** Share verification (connection-sharing.md § Shared view): the share
	 * fingerprint to look for. Switches the initial stage to the wide
	 * budget from the start — a shared connection with a long first/last-
	 * mile walk would be invisible to the narrow query and read as
	 * expired. */
	share: string | null;
}

export interface PlanOutcome {
	itineraries: Itinerary[];
	/** comfort-walk-baseline.md: the query's unavoidable walking (seconds),
	 * summed from the fork's per-endpoint minima. */
	walkBaselineSec: number;
	/** The walking budget the cascade settled on. */
	budget: 'narrow' | 'wide';
	/** Share mode only: the raw (unpruned) itinerary matching the wanted
	 * share fingerprint, or null when the connection is gone. Absent when
	 * no share was asked for. */
	shareMatch?: Itinerary | null;
	/** The wall-clock ceiling cut the search short — what came back is
	 * what was found so far. */
	truncated: boolean;
}

// Cascade tuning — see performance discussion.
// Narrow default is 30 min walking: every extra kilometre of walking
// radius costs real Valhalla matrix time per query in the MOTIS fork
// (the pre/post offsets are a live one-to-many call for coordinate
// endpoints). 30 min covers the normal case; the escalation lifts to the
// 8 h server cap when the narrow search comes up short.
const NARROW_PRE_POST_SEC = 1800;   // 30 min — narrow default per query
const WIDE_PRE_POST_SEC   = 28800;  // 8 h — server hard cap, used on escalation
const LONG_WAIT_THRESHOLD_SEC = 3600; // 1 h wait triggers pre/post escalation
const TARGET_RESULT_COUNT = 5;
const INITIAL_SEARCH_WINDOW_SEC = 900;
// Sparse-service escalation — if the narrow cascade reveals a ≥4 h stretch
// of daytime (06–21 local) with no service (either between two consecutive
// results, or between the last result and how far the hop cascade has
// searched), redo everything with the wide walking budget.
const SPARSE_GAP_THRESHOLD_SEC = 4 * 3600;
const DAY_START_HOUR = 6;
const DAY_END_HOUR = 21;
// Local time of the service area. The daytime window used to be judged
// in the browser's zone; the server's own zone is whatever the VPS runs
// on, so the zone is pinned explicitly.
const LOCAL_TZ = 'Europe/Zurich';
// Stage 3 time-advance cascade — MOTIS's page cursors stall on remote
// destinations (returns 0 with the same cursor value), so instead of
// paging via cursor we advance `time` to the coverage frontier and
// re-query fresh. Each hop asks for a 2 h window; MOTIS extends it on its
// own until it has enough journeys, so a service gap (the night) is
// crossed inside one call and reported back as covered.
const HOP_SEARCH_WINDOW_SEC = 7200;
const MAX_SPAN_MS = 5 * 24 * 3600 * 1000; // stop after 5 days of advance
// There is no merge cap any more: a hop merges everything inside the
// span it searched and coverage advances by that whole span. The cap
// existed to stop a batch from replacing the visible list; with the
// settled display the visible list always sits at the head (tail) of
// the sorted survivors, so a batch can only append.

// Wall-clock guard: a pathological search must not hold a server worker
// indefinitely. Past the ceiling the hop loop stops and returns what it
// has; a single MOTIS call gets at most the call timeout.
const REQUEST_DEADLINE_MS = 25_000;
const MOTIS_CALL_TIMEOUT_MS = 20_000;
// Below this much remaining time no further hop is started.
const MIN_HOP_BUDGET_MS = 1_500;

/** A half-open time span [from, to) in epoch ms. */
interface Span {
	from: number;
	to: number;
}

/** Everything a finished stage leaves behind — the input to the next
 * extension. Treated as immutable once cached: an extension clones
 * before it merges. */
interface CascadeState {
	combined: Itinerary[];
	seen: Set<string>;
	/** Coverage (search-coverage-window.md): every journey departing
	 * inside `departures` is known, every journey arriving inside
	 * `arrivals` is known. Leave-at hops grow the first, arrive-by hops
	 * the second. Null until a query has run on that axis. Replaced, never
	 * mutated, so a cached state stays intact. */
	departures: Span | null;
	arrivals: Span | null;
	/** Walking budget (pre/post seconds) the list was built with. */
	budget: number;
	/** Display quota per side: `forwardTarget` caps the journeys on the
	 * departure-covered side (after a leave-at time, or the "later" side
	 * of an arrive-by query), `backwardTarget` those on the arrival-
	 * covered side. The query's own side starts at TARGET_RESULT_COUNT,
	 * the other at zero; every "later" adds to the forward quota, every
	 * "earlier" to the backward one. */
	forwardTarget: number;
	backwardTarget: number;
	walkBaselineSec: number;
	truncated: boolean;
}

/** Thrown when the deadline or the call timeout hits before the first
 * usable result exists — the endpoint turns it into a 504. */
export class PlanDeadlineError extends Error {
	constructor() {
		super('plan deadline exceeded');
		this.name = 'PlanDeadlineError';
	}
}

function isAbortLike(e: unknown): boolean {
	const name = (e as Error)?.name;
	return name === 'AbortError' || name === 'TimeoutError';
}

/** The span a response actually searched — the fork's koraSearchedFrom /
 * koraSearchedTo: the requested window plus nigiri's own contiguous
 * extension, on the axis the query mode bounds. The sole source of
 * coverage; a server without the fields is an error, never a guess. */
function searchedSpan(res: PlanResponse): Span {
	const from = res.koraSearchedFrom ? Date.parse(res.koraSearchedFrom) : NaN;
	const to = res.koraSearchedTo ? Date.parse(res.koraSearchedTo) : NaN;
	if (Number.isNaN(from) || Number.isNaN(to) || to < from) {
		throw new Error('MOTIS response carries no usable koraSearchedFrom/koraSearchedTo');
	}
	return { from, to };
}

/** An itinerary's anchor on the axis a query mode bounds: departure for
 * leave-at, arrival for arrive-by. */
function anchorMs(it: Itinerary, mode: TimeMode): number {
	return Date.parse(mode === 'arrive' ? it.endTime : it.startTime);
}

/** Membership rule: the itineraries of a response anchored inside the
 * span it searched. Anything else — typically a walk-lighter Pareto
 * point hours out — is discarded, not pooled: the hop that covers its
 * time returns it again. */
function insideSpan(res: PlanResponse, mode: TimeMode, span: Span): Itinerary[] {
	return allItineraries(res).filter((it) => {
		const a = anchorMs(it, mode);
		return a >= span.from && a < span.to;
	});
}

function unionSpan(a: Span | null, b: Span): Span {
	return a ? { from: Math.min(a.from, b.from), to: Math.max(a.to, b.to) } : b;
}

// ---------------------------------------------------------------------------
// Local-time helpers (Europe/Zurich) for the daytime window.

const localFmt = new Intl.DateTimeFormat('en-US', {
	timeZone: LOCAL_TZ, hourCycle: 'h23',
	year: 'numeric', month: '2-digit', day: '2-digit',
	hour: '2-digit', minute: '2-digit', second: '2-digit'
});

function localParts(ms: number): { y: number; m: number; d: number; h: number; min: number; s: number } {
	const p: Record<string, number> = {};
	for (const part of localFmt.formatToParts(ms)) {
		if (part.type !== 'literal') p[part.type] = Number(part.value);
	}
	return { y: p.year, m: p.month, d: p.day, h: p.hour, min: p.minute, s: p.second };
}

/** Zone offset (local − UTC) in ms at the given instant. */
function localOffsetMs(ms: number): number {
	const p = localParts(ms);
	const asUtc = Date.UTC(p.y, p.m - 1, p.d, p.h, p.min, p.s);
	return asUtc - Math.floor(ms / 1000) * 1000;
}

/** Instant of the local wall-clock time y-m-d h:00 in LOCAL_TZ. */
function localWallTime(y: number, m: number, d: number, h: number): number {
	const guess = Date.UTC(y, m - 1, d, h);
	const off = localOffsetMs(guess - localOffsetMs(guess));
	return guess - off;
}

/** Length in seconds of the longest continuous slice of [startMs, endMs]
 * that fits entirely inside a single day's 06–21 local-time window. Used
 * to test whether a service gap contains ≥ SPARSE_GAP_THRESHOLD_SEC of
 * "daytime hours when service should be available". */
function maxDaytimeSliceSec(startMs: number, endMs: number): number {
	if (endMs <= startMs) return 0;
	const first = localParts(startMs);
	let max = 0;
	for (let k = 0; ; k++) {
		const dayUtc = new Date(Date.UTC(first.y, first.m - 1, first.d + k));
		const y = dayUtc.getUTCFullYear();
		const m = dayUtc.getUTCMonth() + 1;
		const d = dayUtc.getUTCDate();
		if (localWallTime(y, m, d, 0) >= endMs) break;
		const dtStart = localWallTime(y, m, d, DAY_START_HOUR);
		const dtEnd = localWallTime(y, m, d, DAY_END_HOUR);
		const sliceStart = Math.max(startMs, dtStart);
		const sliceEnd = Math.min(endMs, dtEnd);
		if (sliceEnd > sliceStart) {
			const secs = (sliceEnd - sliceStart) / 1000;
			if (secs > max) max = secs;
		}
	}
	return max;
}

/** True when the timeline (query time + itinerary anchor times + current
 * cascade frontier) contains a consecutive gap whose daytime slice on any
 * single day reaches SPARSE_GAP_THRESHOLD_SEC. Signals that the narrow
 * walking radius reaches only sparse service and the wide radius should
 * be tried — the trigger fires from both real inter-result gaps and from
 * empty hops (the frontier advances past the last known result). */
function hasSparseServiceGap(
	its: Itinerary[], queryTimeMs: number, frontierMs: number, m: TimeMode
): boolean {
	const key = m === 'arrive' ? 'endTime' : 'startTime';
	const anchors = its.map((i) => Date.parse(i[key]));
	const timeline = [...new Set([queryTimeMs, frontierMs, ...anchors])]
		.sort((a, b) => a - b);
	for (let i = 0; i < timeline.length - 1; i++) {
		if (timeline[i + 1] - timeline[i] < SPARSE_GAP_THRESHOLD_SEC * 1000) continue;
		if (maxDaytimeSliceSec(timeline[i], timeline[i + 1]) >= SPARSE_GAP_THRESHOLD_SEC) {
			return true;
		}
	}
	return false;
}

// ---------------------------------------------------------------------------
// The engine.

class Cascade {
	private readonly q: PlanQuery;
	private readonly deadline: number;
	private readonly signal: AbortSignal | undefined;
	private readonly optionParams: PlanOptionParams;
	private readonly plannedDwellSec: number;
	private readonly viaWaitByStop: Map<string, number> | null;

	constructor(q: PlanQuery, deadline: number, signal?: AbortSignal) {
		this.q = q;
		this.deadline = deadline;
		this.signal = signal;
		this.optionParams = planOptionParams(q.options);
		// via-stops.md § Planned dwell: the sum of the REQUESTED via waits,
		// and parent-stop id → wait so ranking can tell deliberate stop-time
		// from dead time. `null` when no via asks for a wait.
		this.plannedDwellSec = q.vias.reduce((s, v) => s + v.waitMin, 0) * 60;
		const withWait = q.vias.filter((v) => v.waitMin > 0);
		this.viaWaitByStop = withWait.length === 0
			? null
			: new Map(withWait.map((v) => [v.placeId, v.waitMin * 60]));
	}

	private rankOptions(state: CascadeState): RankOptions {
		return {
			minimizeWalking: this.q.options.minimizeWalking,
			plannedDwellSec: this.plannedDwellSec,
			viaWaitByStop: this.viaWaitByStop,
			walkBaselineSec: state.walkBaselineSec
		};
	}

	private sortFn() {
		// Sort ascending in both modes so the "Earlier connections" (top) /
		// "Later connections" (bottom) buttons align with the direction they
		// load. Auto-select on the client compensates by picking the
		// relevant end (last for arrive-by).
		return this.q.mode === 'arrive'
			? (a: Itinerary, b: Itinerary) => Date.parse(a.startTime) - Date.parse(b.startTime)
			: (a: Itinerary, b: Itinerary) => Date.parse(a.endTime) - Date.parse(b.endTime);
	}

	/** Which side of the query time a journey lies on: +1 when it is
	 * known through departure coverage (departs at or after a leave-at
	 * time, or arrives after an arrive-by time), −1 when known through
	 * arrival coverage. The query's own side is +1 for leave-at, −1 for
	 * arrive-by; the other side only fills through an opposite extension. */
	private side(it: Itinerary): 1 | -1 {
		const t = Date.parse(this.q.time);
		return this.q.mode === 'arrive'
			? (Date.parse(it.endTime) <= t ? -1 : 1)
			: (Date.parse(it.startTime) >= t ? 1 : -1);
	}

	/** How far past a journey coverage must reach before no unseen
	 * dominator can exist: every Pareto or comfort dominator departs
	 * before the journey's arrival (arrives after its departure, for the
	 * arrival axis), give or take the ranking's time slack. Minimize
	 * walking adds its reverse-displacement reach — a walk-lighter
	 * journey up to 3 h later on the primary axis may still displace. */
	private settleReachMs(): number {
		return T_SLACK_MS + (this.q.options.minimizeWalking ? REVERSE_DISPLACE_MAX_GAP_MS : 0);
	}

	/** Settled: coverage on one axis spans the journey plus the reach, so
	 * every dominator of it is already a candidate. A settled journey can
	 * never be retired by a later load. */
	private settled(it: Itinerary, state: CascadeState): boolean {
		const reach = this.settleReachMs();
		const start = Date.parse(it.startTime);
		const end = Date.parse(it.endTime);
		const d = state.departures;
		const a = state.arrivals;
		return (d !== null && d.from <= start && d.to >= end + reach)
			|| (a !== null && a.to > end && a.from <= start - reach);
	}

	/** The list as the panel shows it: minimize-walking suppression,
	 * dominance pruning under the side rule, settled filter, chronological
	 * sort, per-side display quota. A pure function of the candidate set,
	 * the coverage and the quotas — never of the path that built them. */
	publish(state: CascadeState): Itinerary[] {
		// Minimize walking: direct walk itineraries beyond 30 min are never
		// shown (routing-options.md § Minimize walking — suppression rules).
		const candidates = this.q.options.minimizeWalking
			? state.combined.filter((it) => boardingCount(it) > 0 || walkSeconds(it) <= 1800)
			: state.combined;
		// Side rule: a journey on the far side of the query time (before a
		// leave-at time, after an arrive-by time) never prunes one on the
		// near side — leaving earlier than asked is not an alternative to
		// the connection asked for. Near-side journeys are pruned among
		// themselves; far-side ones against everything.
		const near: 1 | -1 = this.q.mode === 'arrive' ? -1 : 1;
		const opts = this.rankOptions(state);
		const nearPruned = pruneDominated(candidates.filter((it) => this.side(it) === near), this.q.mode, opts);
		const farPruned = pruneDominated(candidates, this.q.mode, opts).filter((it) => this.side(it) !== near);
		const forward = near === 1 ? nearPruned : farPruned;
		const backward = near === 1 ? farPruned : nearPruned;
		// Each side shows its settled survivors nearest the query time:
		// the head of the forward side, the tail of the backward side.
		// Settledness is monotone along the sort, so growth on one side
		// only ever appends at that side's far end.
		const settledSorted = (its: Itinerary[]) =>
			its.filter((it) => this.settled(it, state)).sort(this.sortFn());
		const fwd = settledSorted(forward).slice(0, state.forwardTarget);
		const bwdAll = settledSorted(backward);
		const bwd = state.backwardTarget > 0 ? bwdAll.slice(-state.backwardTarget) : [];
		return [...bwd, ...fwd];
	}

	/** Published journeys on `dir`'s side, against that side's quota. */
	private sideCount(state: CascadeState, dir: 1 | -1): number {
		return this.publish(state).filter((it) => this.side(it) === dir).length;
	}

	private sideTarget(state: CascadeState, dir: 1 | -1): number {
		return dir === 1 ? state.forwardTarget : state.backwardTarget;
	}

	private remainingMs(): number {
		return this.deadline - Date.now();
	}

	private callSignal(): AbortSignal {
		const budget = Math.max(1, Math.min(MOTIS_CALL_TIMEOUT_MS, this.remainingMs()));
		const timeout = AbortSignal.timeout(budget);
		return this.signal ? AbortSignal.any([this.signal, timeout]) : timeout;
	}

	private async query(
		mode: TimeMode, time: string, searchWindow: number, budget: number
	): Promise<PlanResponse> {
		if (this.remainingMs() <= 0) throw new PlanDeadlineError();
		return planMotis({
			fromPlace: this.q.fromPlace,
			toPlace: this.q.toPlace,
			mode, time, searchWindow,
			vias: this.q.vias,
			maxPreTransitTime: budget,
			maxPostTransitTime: budget,
			// Full 2-h transfer table rides along with the wide walking
			// budget — both mark "sparse service, search exhaustively"
			// (transfer-point-optimization.md § Two-tier transfer table).
			fullTransfers: budget === WIDE_PRE_POST_SEC,
			options: this.optionParams
		}, this.callSignal());
	}

	/** Take the baseline off a plan response. Absent fields (station
	 * endpoints report 0; an unreachable endpoint or an older server
	 * omits them) contribute nothing. A property of the query — every hop
	 * returns the same number. */
	private static walkBaseline(res: PlanResponse): number {
		return (res.koraMinWalkFrom ?? 0) + (res.koraMinWalkTo ?? 0);
	}

	/** True when any transit leg in the itinerary is preceded by a wait
	 * longer than LONG_WAIT_THRESHOLD_SEC. Signals that expanding the
	 * walking budget might reach a nearer stop with better-timed service. */
	private hasLongWait(it: Itinerary): boolean {
		const viaWaits = this.viaWaitByStop;
		const legs = it.legs;
		for (let i = 0; i < legs.length; i++) {
			const leg = legs[i];
			if (leg.mode === 'WALK') continue;
			const prev = i > 0 ? legs[i - 1] : null;
			const prevEnd = prev ? Date.parse(prev.endTime) : Date.parse(it.startTime);
			// A wait the user asked for at a via is not a signal that the
			// walking radius is too narrow — only its excess is
			// (via-stops.md § Planned dwell).
			const planned = viaWaits && prev
				? (viaWaits.get(prev.to?.parentId ?? '') ?? viaWaits.get(prev.to?.stopId ?? '') ?? 0)
				: 0;
			const wait = (Date.parse(leg.startTime) - prevEnd) / 1000 - planned;
			if (wait > LONG_WAIT_THRESHOLD_SEC) return true;
		}
		return false;
	}

	/** Where the next hop in `dir` (+1 forward, −1 backward) starts: the
	 * end of the coverage on that axis. Without coverage there yet — an
	 * extension that switches direction ("later" on an arrive-by query,
	 * "earlier" on a leave-at one) — the known list's edge on that axis
	 * seeds it: a journey beyond the edge on the other side (departing
	 * before the latest known departure yet arriving after the arrive-by
	 * time, or the mirror) is Pareto-dominated by the edge journey. */
	private frontier(state: CascadeState, dir: 1 | -1): number {
		const cov = dir === 1 ? state.departures : state.arrivals;
		if (cov) return dir === 1 ? cov.to : cov.from;
		if (state.combined.length === 0) return Date.parse(this.q.time);
		const anchors = state.combined.map((i) => anchorMs(i, dir === 1 ? 'leave' : 'arrive'));
		return dir === 1 ? Math.max(...anchors) + 60_000 : Math.min(...anchors);
	}

	/** Extend the coverage on `dir`'s axis by `span`. Assigns a new
	 * object so a cached previous state is never mutated. */
	private cover(state: CascadeState, dir: 1 | -1, span: Span) {
		if (dir === 1) state.departures = unionSpan(state.departures, span);
		else state.arrivals = unionSpan(state.arrivals, span);
	}

	/** Hop in `dir` (+1 forward, −1 backward) from the coverage frontier
	 * and merge fresh itineraries into `state.combined` until the
	 * published list on that side reaches its quota, MAX_SPAN_MS of
	 * coverage has been added, or the deadline runs out. Mutates `state`.
	 *
	 * Hops are direction-native point queries, independent of the panel's
	 * mode (which keeps governing pruning / sorting / display): forward
	 * hops are leave-at queries bounding departures, backward hops are
	 * arrive-by queries bounding arrivals. A forward hop asks for
	 * [frontier, frontier + W); a backward hop for [frontier − 1 min − W,
	 * frontier) — the fork's arrive-by interval includes the query minute
	 * itself, so the query time sits one minute before the frontier.
	 *
	 * Coverage advances by the span the fork reports having searched: the
	 * requested window, or further when MOTIS extended on its own to find
	 * enough journeys (an empty night is crossed in one call). Everything
	 * inside that span is merged.
	 *
	 * `shouldEscalate` (when provided) is called with the new frontier
	 * after every iteration; `true` returns 'escalate' so the caller can
	 * redo the pipeline with the wide walking budget. */
	private async runHopCascade(
		state: CascadeState,
		dir: 1 | -1,
		shouldEscalate?: (frontierMs: number) => boolean
	): Promise<'done' | 'escalate'> {
		const hopMode: TimeMode = dir === 1 ? 'leave' : 'arrive';
		const startEpoch = this.frontier(state, dir);
		const target = this.sideTarget(state, dir);
		while (this.sideCount(state, dir) < target) {
			const frontier = this.frontier(state, dir);
			if (Math.abs(frontier - startEpoch) > MAX_SPAN_MS) break;
			if (this.remainingMs() < MIN_HOP_BUDGET_MS) {
				state.truncated = true;
				break;
			}
			const queryEpoch = dir === 1 ? frontier : frontier - 60_000;
			let res: PlanResponse;
			try {
				res = await this.query(hopMode, new Date(queryEpoch).toISOString(), HOP_SEARCH_WINDOW_SEC, state.budget);
			} catch (e) {
				// Out of time mid-cascade: keep what was found so far.
				if (e instanceof PlanDeadlineError || isAbortLike(e)) {
					state.truncated = true;
					break;
				}
				throw e;
			}
			state.walkBaselineSec = Cascade.walkBaseline(res);
			const span = searchedSpan(res);
			const fresh = insideSpan(res, hopMode, span)
				.filter((it) => !state.seen.has(itineraryFingerprint(it)));
			for (const it of fresh) state.seen.add(itineraryFingerprint(it));
			this.cover(state, dir, span);
			if (fresh.length > 0) state.combined = [...state.combined, ...fresh];
			if (shouldEscalate?.(this.frontier(state, dir))) return 'escalate';
		}
		return 'done';
	}

	/** Stages 1 → 2 → 3 → 2c for the query's own time. */
	async runInitial(): Promise<CascadeState> {
		const q = this.q;
		const state: CascadeState = {
			combined: [], seen: new Set(), departures: null, arrivals: null,
			budget: NARROW_PRE_POST_SEC,
			forwardTarget: q.mode === 'arrive' ? 0 : TARGET_RESULT_COUNT,
			backwardTarget: q.mode === 'arrive' ? TARGET_RESULT_COUNT : 0,
			walkBaselineSec: 0, truncated: false
		};
		// Adopt a stage 1 / 2 response as the candidate set: only the
		// journeys inside the span it searched, and that span as the
		// coverage on the query mode's axis.
		const adopt = (r: PlanResponse) => {
			state.walkBaselineSec = Cascade.walkBaseline(r);
			const span = searchedSpan(r);
			state.combined = insideSpan(r, q.mode, span);
			state.departures = q.mode === 'leave' ? span : null;
			state.arrivals = q.mode === 'arrive' ? span : null;
		};
		// Share verification must not depend on the narrow-radius
		// heuristics: a shared connection with a long first/last-mile walk
		// would be invisible to the narrow query and read as expired. Go
		// wide from the start.
		if (q.share) state.budget = WIDE_PRE_POST_SEC;

		// Stage 1 — narrow initial query (fast for typical cases).
		let res = await this.query(q.mode, q.time, INITIAL_SEARCH_WINDOW_SEC, state.budget);
		adopt(res);

		// Stage 2 — escalate walking budget on trigger:
		//   (a) narrow query returned no TRANSIT itinerary — a direct walk
		//       alone must not mask "nothing found": MOTIS always returns
		//       the walk, so testing for emptiness alone let walk-only
		//       results suppress the wide retry that would have found
		//       transit, or
		//   (b) any returned itinerary has a >1 h wait at start or between
		//       transit legs, or
		//   (c) the narrow results leave a ≥4 h daytime service gap after
		//       the requested time — MOTIS extends its search interval
		//       until it has 5 itineraries, so a narrow query can "succeed"
		//       with next-morning connections only; those must not suppress
		//       the wide retry that finds same-day ones, or
		//   (d) the best option for the requested timing (earliest arrival
		//       for leave-at, latest departure for arrive-by) is a walk-only
		//       itinerary of more than 30 min — a long walk "winning" is a
		//       strong hint that reachable transit sits beyond the narrow
		//       radius.
		// Escalation replaces `combined` (different candidate set with a
		// wider walking radius, not comparable via merge).
		const initialEpoch = Date.parse(q.time);
		if (state.budget === NARROW_PRE_POST_SEC) {
			const c = state.combined;
			const best = c.length === 0 ? null : c.reduce((a, b) =>
				q.mode === 'arrive'
					? (Date.parse(b.startTime) > Date.parse(a.startTime) ? b : a)
					: (Date.parse(b.endTime) < Date.parse(a.endTime) ? b : a));
			const bestIsLongWalk = best !== null
				&& boardingCount(best) === 0 && walkSeconds(best) > 1800;
			const escalate =
				!c.some((it) => boardingCount(it) > 0)
				|| bestIsLongWalk
				|| c.some((it) => this.hasLongWait(it))
				|| hasSparseServiceGap(c, initialEpoch, initialEpoch, q.mode);
			if (escalate) {
				state.budget = WIDE_PRE_POST_SEC;
				res = await this.query(q.mode, q.time, INITIAL_SEARCH_WINDOW_SEC, state.budget);
				adopt(res);
			}
		}
		// Seed the dedupe set now that `combined` has stabilised for stages
		// 1 + 2 — stage 3 (and any later extension) then filters against it.
		state.seen = new Set(state.combined.map(itineraryFingerprint));

		// Stage 3 — time-advance cascade from the coverage frontier.
		const advanceDir: 1 | -1 = q.mode === 'arrive' ? -1 : 1;
		// Only arm the sparse-gap escalation check while the narrow budget
		// is still in effect. If stage 2 already went wide there is no
		// wider budget to retry with.
		const shouldEscalate = state.budget === NARROW_PRE_POST_SEC
			? (frontier: number) => hasSparseServiceGap(state.combined, initialEpoch, frontier, q.mode)
			: undefined;
		const outcome = await this.runHopCascade(state, advanceDir, shouldEscalate);

		// Stage 2c — sparse-service gap discovered mid-cascade. Redo the
		// full narrow flow (stage 1 + stage 3) with the wide walking budget;
		// the wider candidate set is not merge-comparable with the narrow
		// one.
		if (outcome === 'escalate') {
			state.budget = WIDE_PRE_POST_SEC;
			adopt(await this.query(q.mode, q.time, INITIAL_SEARCH_WINDOW_SEC, state.budget));
			state.seen = new Set(state.combined.map(itineraryFingerprint));
			await this.runHopCascade(state, advanceDir);
		}
		return state;
	}

	/** Extend the list in one chronological direction: bumps that side's
	 * display quota by TARGET_RESULT_COUNT and hops past the coverage
	 * frontier until that many settled survivors show there. Returns a
	 * new state; `prev` is left untouched (it may be cached). */
	async runExtension(prev: CascadeState, direction: Extension): Promise<CascadeState> {
		const state: CascadeState = {
			combined: [...prev.combined],
			seen: new Set(prev.seen),
			departures: prev.departures,
			arrivals: prev.arrivals,
			budget: prev.budget,
			forwardTarget: prev.forwardTarget + (direction === 'later' ? TARGET_RESULT_COUNT : 0),
			backwardTarget: prev.backwardTarget + (direction === 'earlier' ? TARGET_RESULT_COUNT : 0),
			walkBaselineSec: prev.walkBaselineSec,
			truncated: false
		};
		const dir: 1 | -1 = direction === 'later' ? 1 : -1;
		// Hops continue from the coverage frontier on this direction's axis
		// (see runHopCascade). Extend with the budget the visible list was
		// built with. While that is narrow, arm the same sparse-gap
		// escalation as the initial cascade: service can thin out past the
		// list's edge (e.g. extending into the night) even when the original
		// window was dense.
		const startEpoch = this.frontier(state, dir);
		const outcome = await this.runHopCascade(
			state, dir,
			state.budget === NARROW_PRE_POST_SEC
				? (frontier) => hasSparseServiceGap(state.combined, startEpoch, frontier, this.q.mode)
				: undefined
		);
		// Sparse service past the edge — continue wide (full transfer table
		// rides along). Unlike stage 2c the shown results are NOT replaced:
		// only the span beyond the current coverage is searched wide, and
		// the wide budget sticks for further extensions.
		if (outcome === 'escalate') {
			state.budget = WIDE_PRE_POST_SEC;
			await this.runHopCascade(state, dir);
		}
		return state;
	}
}

// ---------------------------------------------------------------------------
// Replay + cache.

const CACHE_TTL_MS = 10 * 60_000;
const CACHE_MAX = 40;
const cache = new Map<string, { state: CascadeState; at: number }>();

function cacheKey(q: PlanQuery, extensions: Extension[]): string {
	return JSON.stringify([
		q.fromPlace, q.toPlace, q.mode, q.time, q.vias, q.options, q.share, extensions
	]);
}

function cacheGet(key: string): CascadeState | null {
	const hit = cache.get(key);
	if (!hit) return null;
	if (Date.now() - hit.at > CACHE_TTL_MS) {
		cache.delete(key);
		return null;
	}
	return hit.state;
}

function cachePut(key: string, state: CascadeState) {
	// A truncated state is what the deadline allowed, not what the search
	// would find — never freeze it for later extensions.
	if (state.truncated) return;
	cache.set(key, { state, at: Date.now() });
	if (cache.size > CACHE_MAX) {
		const oldest = cache.keys().next().value;
		if (oldest !== undefined) cache.delete(oldest);
	}
}

/** Run the cascade for `q`, then replay `extensions` in order, and return
 * the list the panel shows. Finished states are cached per (query,
 * extension prefix) so a click-through replays nothing it already has. */
export async function planCascade(
	q: PlanQuery, extensions: Extension[], signal?: AbortSignal
): Promise<PlanOutcome> {
	const engine = new Cascade(q, Date.now() + REQUEST_DEADLINE_MS, signal);
	// Longest cached prefix wins; everything after it is replayed.
	let state: CascadeState | null = null;
	let done = 0;
	for (let n = extensions.length; n >= 0; n--) {
		const hit = cacheGet(cacheKey(q, extensions.slice(0, n)));
		if (hit) {
			state = hit;
			done = n;
			break;
		}
	}
	if (!state) {
		state = await engine.runInitial();
		cachePut(cacheKey(q, []), state);
	}
	for (let i = done; i < extensions.length; i++) {
		state = await engine.runExtension(state, extensions[i]);
		cachePut(cacheKey(q, extensions.slice(0, i + 1)), state);
	}
	const outcome: PlanOutcome = {
		itineraries: engine.publish(state),
		walkBaselineSec: state.walkBaselineSec,
		budget: state.budget === WIDE_PRE_POST_SEC ? 'wide' : 'narrow',
		truncated: state.truncated
	};
	// Share reconciliation runs against the raw `combined` set, not the
	// pruned display list — dominance pruning must never turn a
	// still-running connection into a false expiry.
	if (q.share) {
		const wanted = q.share;
		outcome.shareMatch = state.combined.find((it) => shareFingerprint(it) === wanted) ?? null;
	}
	return outcome;
}
