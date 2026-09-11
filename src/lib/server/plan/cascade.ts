import { boardingCount, pruneDominated, walkSeconds, type RankOptions } from '$lib/routing/ranking';
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
// Stage 3 time-advance cascade — MOTIS's nextPageCursor stalls on remote
// destinations (returns 0 with the same cursor value), so instead of
// paging via cursor we advance `time` past the last returned itinerary
// and re-query fresh.
const HOP_MS = 2 * 3600 * 1000;         // 2 h step when a hop returns empty
const HOP_SEARCH_WINDOW_SEC = 7200;     // matches HOP_MS so windows don't gap
const MAX_SPAN_MS = 5 * 24 * 3600 * 1000; // stop after 5 days of advance
const MAX_EMPTY_STREAK = 3;             // stop after N consecutive empty hops
// Merge cap (server-side-transit-planning.md § Hop merge cap): a hop
// merges `needed / pruneRatio × margin` itineraries, so the frontier
// moves in proportion to what pruning keeps. The cap used to be `needed`
// alone, which crawled a minute per hop whenever pruning retired nearly
// everything (minimize walking: 24 hops for 55 min of coverage).
const MERGE_MARGIN = 1.5;

// Wall-clock guard: a pathological search must not hold a server worker
// indefinitely. Past the ceiling the hop loop stops and returns what it
// has; a single MOTIS call gets at most the call timeout.
const REQUEST_DEADLINE_MS = 25_000;
const MOTIS_CALL_TIMEOUT_MS = 20_000;
// Below this much remaining time no further hop is started.
const MIN_HOP_BUDGET_MS = 1_500;

/** Everything a finished stage leaves behind — the input to the next
 * extension. Treated as immutable once cached: an extension clones
 * before it merges. */
interface CascadeState {
	combined: Itinerary[];
	seen: Set<string>;
	/** Walking budget (pre/post seconds) the list was built with. */
	budget: number;
	/** The display cap — bumped by TARGET_RESULT_COUNT per extension. */
	resultTarget: number;
	walkBaselineSec: number;
	/** kept / returned after the initial stage — the merge cap's basis. */
	pruneRatio: number;
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

	/** The list as the panel shows it: minimize-walking suppression,
	 * dominance pruning, chronological sort, display cap. */
	publish(state: CascadeState): Itinerary[] {
		// Minimize walking: direct walk itineraries beyond 30 min are never
		// shown (routing-options.md § Minimize walking — suppression rules).
		const candidates = this.q.options.minimizeWalking
			? state.combined.filter((it) => boardingCount(it) > 0 || walkSeconds(it) <= 1800)
			: state.combined;
		const pruned = pruneDominated(candidates, this.q.mode, this.rankOptions(state))
			.sort(this.sortFn());
		// The cap must keep the end nearest the query time: leave-at sorts by
		// arrival ascending and keeps the head (earliest arrivals after the
		// departure time); arrive-by sorts by departure ascending and must
		// keep the tail (latest departures before the arrival time).
		return this.q.mode === 'arrive'
			? pruned.slice(-state.resultTarget)
			: pruned.slice(0, state.resultTarget);
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

	/** Hop `time` in `dir` (+1 forward, −1 backward) starting at
	 * `startEpoch` and merge fresh itineraries into `state.combined` until
	 * the published list reaches `state.resultTarget`, MAX_EMPTY_STREAK
	 * consecutive empty hops fire, MAX_SPAN_MS from `startEpoch` is
	 * exceeded, or the deadline runs out. Mutates `state`.
	 *
	 * Hops are direction-native point queries, independent of the panel's
	 * mode (which keeps governing pruning / sorting / display): MOTIS
	 * effectively treats arrive-by as "the N connections arriving closest
	 * before `time`" — its arrive-by searchWindow handling is unreliable,
	 * so window-coverage hops would leave gaps. Forward hops therefore
	 * always query leave-at anchored just past the latest known departure;
	 * backward hops always query arrive-by anchored just before the
	 * earliest known arrival.
	 *
	 * `shouldEscalate` (when provided) is called with the current search
	 * frontier after every iteration; `true` returns 'escalate' so the
	 * caller can redo the pipeline with the wide walking budget. */
	private async runHopCascade(
		state: CascadeState,
		dir: 1 | -1,
		startEpoch: number,
		shouldEscalate?: (frontierMs: number) => boolean
	): Promise<'done' | 'escalate'> {
		const hopMode: TimeMode = dir === 1 ? 'leave' : 'arrive';
		const anchorOf = (i: Itinerary) => Date.parse(dir === 1 ? i.startTime : i.endTime);
		let queryEpoch = startEpoch;
		let emptyStreak = 0;
		let results = this.publish(state);
		while (results.length < state.resultTarget) {
			if (Math.abs(queryEpoch - startEpoch) > MAX_SPAN_MS) break;
			if (emptyStreak >= MAX_EMPTY_STREAK) break;
			if (this.remainingMs() < MIN_HOP_BUDGET_MS) {
				state.truncated = true;
				break;
			}
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
			const unseen = allItineraries(res)
				.filter((it) => !state.seen.has(itineraryFingerprint(it)))
				.sort((a, b) => dir === 1 ? anchorOf(a) - anchorOf(b) : anchorOf(b) - anchorOf(a));
			// Merge only the adjacent-most items: leave-at hops honor the
			// search window and can return the full 2 h of connections at
			// once — merging all of them would let the display slice (head
			// for leave-at, tail for arrive-by) jump to the batch's far end
			// and replace the visible list instead of extending it. The cap
			// scales with how selective pruning proved to be for this search,
			// so the survivors land near `needed` and the frontier still
			// advances by a useful stretch. Items beyond the cap stay
			// unmarked in `seen`, so a later hop re-fetches them as fresh.
			const needed = Math.max(1, state.resultTarget - results.length);
			const cap = state.pruneRatio > 0
				? Math.min(unseen.length, Math.max(needed, Math.ceil((needed / state.pruneRatio) * MERGE_MARGIN)))
				: unseen.length;
			const fresh = unseen.slice(0, cap);
			// Never split a same-minute anchor group across the merge cap:
			// the next hop starts one minute past this batch's last anchor,
			// so an unmerged sibling departing (arriving) in the same minute
			// would sit behind every later hop window and vanish for good.
			if (fresh.length > 0) {
				const edge = anchorOf(fresh[fresh.length - 1]);
				for (const it of unseen.slice(fresh.length)) {
					if (anchorOf(it) !== edge) break;
					fresh.push(it);
				}
			}
			for (const it of fresh) state.seen.add(itineraryFingerprint(it));
			if (fresh.length === 0) {
				emptyStreak++;
				queryEpoch += dir * HOP_MS;
			} else {
				emptyStreak = 0;
				state.combined = [...state.combined, ...fresh];
				results = this.publish(state);
				// Advance along the axis the hop mode bounds: leave-at queries
				// bound departures (startTime), arrive-by queries bound
				// arrivals (endTime). Anchoring backward hops on startTime
				// would skip ~a trip duration of connections per hop.
				const anchors = fresh.map(anchorOf);
				queryEpoch = (dir === 1 ? Math.max(...anchors) : Math.min(...anchors))
					+ dir * 60_000;
			}
			if (shouldEscalate?.(queryEpoch)) return 'escalate';
		}
		return 'done';
	}

	/** Record how selective pruning is for this search: the merge cap's
	 * basis. A ratio of zero (nothing survived) makes hops merge whole
	 * batches. */
	private notePruneRatio(state: CascadeState) {
		const kept = this.publish(state).length;
		state.pruneRatio = state.combined.length > 0 ? kept / state.combined.length : 0;
	}

	/** Stages 1 → 2 → 3 → 2c for the query's own time. */
	async runInitial(): Promise<CascadeState> {
		const q = this.q;
		const state: CascadeState = {
			combined: [], seen: new Set(), budget: NARROW_PRE_POST_SEC,
			resultTarget: TARGET_RESULT_COUNT, walkBaselineSec: 0, pruneRatio: 0,
			truncated: false
		};
		// Share verification must not depend on the narrow-radius
		// heuristics: a shared connection with a long first/last-mile walk
		// would be invisible to the narrow query and read as expired. Go
		// wide from the start.
		if (q.share) state.budget = WIDE_PRE_POST_SEC;

		// Stage 1 — narrow initial query (fast for typical cases).
		let res = await this.query(q.mode, q.time, INITIAL_SEARCH_WINDOW_SEC, state.budget);
		state.walkBaselineSec = Cascade.walkBaseline(res);
		state.combined = allItineraries(res);

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
				state.walkBaselineSec = Cascade.walkBaseline(res);
				state.combined = allItineraries(res);
			}
		}
		// Seed the dedupe set now that `combined` has stabilised for stages
		// 1 + 2 — stage 3 (and any later extension) then filters against it.
		state.seen = new Set(state.combined.map(itineraryFingerprint));
		this.notePruneRatio(state);

		// Stage 3 — time-advance cascade.
		const advanceDir: 1 | -1 = q.mode === 'arrive' ? -1 : 1;
		// Anchor on the axis the hop mode bounds (see runHopCascade):
		// departures for forward/leave-at hops, arrivals for backward/
		// arrive-by hops.
		const startEpochFrom = (its: Itinerary[]): number => {
			if (!its.length) return initialEpoch + advanceDir * HOP_MS;
			const anchors = its.map((i) =>
				Date.parse(advanceDir === 1 ? i.startTime : i.endTime));
			return (advanceDir === 1 ? Math.max(...anchors) : Math.min(...anchors))
				+ advanceDir * 60_000;
		};
		// Only arm the sparse-gap escalation check while the narrow budget
		// is still in effect. If stage 2 already went wide there is no
		// wider budget to retry with.
		const shouldEscalate = state.budget === NARROW_PRE_POST_SEC
			? (frontier: number) => hasSparseServiceGap(state.combined, initialEpoch, frontier, q.mode)
			: undefined;
		const outcome = await this.runHopCascade(
			state, advanceDir, startEpochFrom(state.combined), shouldEscalate
		);

		// Stage 2c — sparse-service gap discovered mid-cascade. Redo the
		// full narrow flow (stage 1 + stage 3) with the wide walking budget;
		// the wider candidate set is not merge-comparable with the narrow
		// one.
		if (outcome === 'escalate') {
			state.budget = WIDE_PRE_POST_SEC;
			const wideRes = await this.query(q.mode, q.time, INITIAL_SEARCH_WINDOW_SEC, state.budget);
			state.walkBaselineSec = Cascade.walkBaseline(wideRes);
			state.combined = allItineraries(wideRes);
			state.seen = new Set(state.combined.map(itineraryFingerprint));
			this.notePruneRatio(state);
			await this.runHopCascade(state, advanceDir, startEpochFrom(state.combined));
		}
		return state;
	}

	/** Extend the list in one chronological direction: bumps the display
	 * cap by TARGET_RESULT_COUNT and hops past the current edge until that
	 * many more results survive pruning. Returns a new state; `prev` is
	 * left untouched (it may be cached). */
	async runExtension(prev: CascadeState, direction: Extension): Promise<CascadeState> {
		const state: CascadeState = {
			combined: [...prev.combined],
			seen: new Set(prev.seen),
			budget: prev.budget,
			resultTarget: prev.resultTarget + TARGET_RESULT_COUNT,
			walkBaselineSec: prev.walkBaselineSec,
			pruneRatio: prev.pruneRatio,
			truncated: false
		};
		const dir: 1 | -1 = direction === 'later' ? 1 : -1;
		// Direction-native seed (see runHopCascade): forward hops are
		// leave-at queries anchored just past the latest known departure,
		// backward hops are arrive-by queries anchored just before the
		// earliest known arrival. Recomputed for the escalation retry —
		// merged results move the edge.
		const seedEpoch = () => {
			if (state.combined.length === 0) return Date.parse(this.q.time) + dir * HOP_MS;
			const anchors = state.combined.map((i) =>
				Date.parse(dir === 1 ? i.startTime : i.endTime));
			return dir === 1
				? Math.max(...anchors) + 60_000
				: Math.min(...anchors) - 60_000;
		};
		// Extend with the budget the visible list was built with. While that
		// is narrow, arm the same sparse-gap escalation as the initial
		// cascade: service can thin out past the list's edge (e.g. extending
		// into the night) even when the original window was dense.
		const startEpoch = seedEpoch();
		const outcome = await this.runHopCascade(
			state, dir, startEpoch,
			state.budget === NARROW_PRE_POST_SEC
				? (frontier) => hasSparseServiceGap(state.combined, startEpoch, frontier, this.q.mode)
				: undefined
		);
		// Sparse service past the edge — continue wide (full transfer table
		// rides along). Unlike stage 2c the shown results are NOT replaced:
		// only the extension beyond the current edge is re-searched, so the
		// seed is recomputed from the merged set and the wide budget sticks
		// for further extensions.
		if (outcome === 'escalate') {
			state.budget = WIDE_PRE_POST_SEC;
			await this.runHopCascade(state, dir, seedEpoch());
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
