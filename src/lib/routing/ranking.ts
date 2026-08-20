import type { Itinerary, Leg, TimeMode } from './types';

// Quality ranking for the merged cascade results — see transit-routing.md
// § Ranking. Each pair (A, B) falls into one of two shapes and is judged
// by a case-specific rule:
//   Case 1 (overlapping): B Pareto-time-dominates A — A takes strictly
//     more of the user's day for no time benefit. A survives only when
//     BOTH the time gap AND the comfort gap are marginal.
//   Case 2 (non-overlapping): neither Pareto-dominates in time — legitimate
//     different time slots. A survives unless it's meaningfully worse in
//     comfort than the gap-scaled allowance permits. Score is Case 2's
//     escape hatch and is never used for sorting.

const TRANSFER_PENALTY_SEC = 600;    // one boarding ≈ 5 min of walking
const WALK_PER_SEC = 2;              // full linear rate for the first 30 min
// Walking cost is soft-capped: small walking differences (0–30 min) stay
// as sensitive as before, but each further second is worth a quarter as
// much — a 30-min-vs-3-h walking difference no longer overwhelms every
// realistic temporal-gap allowance.
const WALK_SOFT_CAP_SEC = 30 * 60;   // linear knee-point (30 min)
const WALK_TAIL_PER_SEC = 0.5;       // shallow slope past the knee
const T_SLACK_MS = 60 * 1000;        // start/end jitter that still counts as "same time"
// Case 1 (overlapping) marginality thresholds.
const OVERLAP_TIME_MAX_MS = 9 * 60 * 1000;   // both endpoints must be within 9 min
const OVERLAP_COMFORT_MAX_PCT = 0.20;        // effective-time worseness ≤ 20%
// Case 2 (non-overlapping) calibration.
const MARGIN = 300;
// Cube-root curve for the allowance: rises fast at short gaps so a rare
// fast option can't nuke its neighbours (2 min gap already needs ≥15 min
// extra walking to prune), saturates gracefully at long gaps (2 h → ~8000
// ≈ 65 min extra walking under the soft cap). Linear couldn't hit both
// "steep at 2 min" and "sane at 2 h" simultaneously.
const PENALTY_K = 430;
// The gap is floored at 2 min: Case 2 removes only clearly worse
// connections, so the allowance never drops below the 2-min value
// (~1820 ≈ 15 min extra walking). Without the floor the curve went
// negative at near-zero gaps, and two identical-time near-ties (e.g. a
// direct walk vs. a walk + one-stop bus hybrid) mutually dropped each
// other, leaving neither.
const GAP_FLOOR_SEC = 120;

export function legDuration(leg: Leg): number {
	return leg.duration ?? Math.max(0, (Date.parse(leg.endTime) - Date.parse(leg.startTime)) / 1000);
}

/** Seconds walking across all WALK legs, including inter-station transfer
 * walks (same total the result card shows). */
export function walkSeconds(it: Itinerary): number {
	let s = 0;
	for (const l of it.legs) if (l.mode === 'WALK') s += legDuration(l);
	return s;
}

/** Number of transit legs − 1 (same count the result card shows). */
export function transferCount(it: Itinerary): number {
	if (typeof it.transfers === 'number') return it.transfers;
	return Math.max(0, boardingCount(it) - 1);
}

/** Number of transit legs — one boarding per vehicle, walk-only = 0.
 * Unlike transferCount (display-facing), this is what the score and
 * comfort factor penalise: the first vehicle you must catch costs like
 * any later transfer, pricing in schedule-dependence so a pure walk
 * outranks a walk + one-stop hop at similar walking time. */
export function boardingCount(it: Itinerary): number {
	let transit = 0;
	for (const l of it.legs) if (l.mode !== 'WALK' && l.mode !== 'BIKE' && l.mode !== 'CAR') transit++;
	return transit;
}

/** Walking cost with a soft cap at 30 min: full linear rate below the
 * knee, quarter rate above. Keeps small walking differences meaningful
 * while bounding the score inflation from multi-hour hikes. */
function walkCost(walkSec: number): number {
	const base = WALK_PER_SEC * Math.min(walkSec, WALK_SOFT_CAP_SEC);
	const tail = WALK_TAIL_PER_SEC * Math.max(0, walkSec - WALK_SOFT_CAP_SEC);
	return base + tail;
}

/** Comfort score — lower is better. Only used as the dominance escape
 * hatch, never for sorting. */
export function itineraryScore(it: Itinerary): number {
	return TRANSFER_PENALTY_SEC * boardingCount(it) + walkCost(walkSeconds(it));
}

interface Entry {
	it: Itinerary;
	start: number;
	end: number;
	score: number;
	effTime: number;
}

/** True when B Pareto-dominates A in time: B departs later-or-equal AND
 * arrives earlier-or-equal (both within T_SLACK), with at least one
 * endpoint strictly better beyond T_SLACK. In this case A takes strictly
 * more of the user's day for no time benefit. */
function paretoTimeDominates(b: Entry, a: Entry): boolean {
	const laterOrEqualStart = b.start >= a.start - T_SLACK_MS;
	const earlierOrEqualEnd = b.end <= a.end + T_SLACK_MS;
	if (!laterOrEqualStart || !earlierOrEqualEnd) return false;
	const strictStart = b.start > a.start + T_SLACK_MS;
	const strictEnd = b.end < a.end - T_SLACK_MS;
	return strictStart || strictEnd;
}

/** Case 1 (overlapping): B Pareto-time-dominates A. A survives only if
 * BOTH the time gap and the comfort gap are marginal. Returns true when
 * A fails either test (i.e. B causes A's drop). */
function droppedByOverlap(a: Entry, b: Entry): boolean {
	const timeGapMs = Math.max(Math.abs(a.start - b.start), Math.abs(a.end - b.end));
	if (timeGapMs > OVERLAP_TIME_MAX_MS) return true;
	if (b.effTime <= 0) return false;
	return a.effTime / b.effTime - 1 > OVERLAP_COMFORT_MAX_PCT;
}

/** Case 2 (non-overlapping): B time-beats A on the query's primary axis
 * (arrival for leave-at, departure for arrive-by, within T_SLACK) AND A's
 * comfort penalty over B exceeds the gap-scaled allowance.
 *
 * `gap = min(|Δstart|, |Δend|)` — the tighter axis distance, floored at
 * GAP_FLOOR_SEC. Two options far apart on one axis but near-identical on
 * the other are treated as near-ties: the tight axis limits how much
 * comfort penalty the worse one can afford. `allowed = −MARGIN +
 * PENALTY_K · max(gap, floor)^(1/3)` — the cube-root rises fast so even
 * a small gap only tolerates a fairly steep comfort difference, and
 * saturates gracefully so a 2 h gap sits around a "dramatic" allowance
 * rather than an absurd one. */
function droppedByNonOverlap(a: Entry, b: Entry, mode: TimeMode): boolean {
	const primaryBeats = mode === 'arrive'
		? b.start >= a.start - T_SLACK_MS
		: b.end <= a.end + T_SLACK_MS;
	if (!primaryBeats) return false;
	const gapSec = Math.max(GAP_FLOOR_SEC, Math.min(
		Math.abs(a.start - b.start),
		Math.abs(a.end - b.end)
	) / 1000);
	const allowed = -MARGIN + PENALTY_K * Math.cbrt(gapSec);
	return a.score - b.score > allowed;
}

/** Dispatches (a, b) to the case-specific rule. When B Pareto-time-
 * dominates A the pair is overlapping (Case 1). When A dominates B the
 * pair is B's problem, not A's — return false. Otherwise apply Case 2. */
function droppedBy(a: Entry, b: Entry, mode: TimeMode): boolean {
	if (paretoTimeDominates(b, a)) return droppedByOverlap(a, b);
	if (paretoTimeDominates(a, b)) return false;
	return droppedByNonOverlap(a, b, mode);
}

/** Drop each itinerary that some other beats under the two-case rule
 * (Case 1 overlapping, Case 2 non-overlapping). Input order is preserved;
 * the caller sorts chronologically afterwards. */
export function pruneDominated(its: Itinerary[], mode: TimeMode): Itinerary[] {
	const entries: Entry[] = its.map((it) => ({
		it,
		start: Date.parse(it.startTime),
		end: Date.parse(it.endTime),
		score: itineraryScore(it),
		effTime: it.duration * comfortFactor(it)
	}));
	return entries
		.filter((a) => !entries.some((b) => b !== a && droppedBy(a, b, mode)))
		.map((e) => e.it);
}

// Per-card badge + warning derivation — see transit-routing.md § Badges,
// § Warnings.

export type Badge = 'best' | 'good' | 'bad';
export type WarningKind = 'long-walk' | 'long-wait' | 'very-slow';
// standard = plain red icon; medium = white icon in a yellow circle;
// strong = white icon in a red circle. One icon per kind, highest
// severity wins.
export type WarningSeverity = 'standard' | 'medium' | 'strong';
export interface Warning {
	kind: WarningKind;
	severity: WarningSeverity;
	// The actual measured value for this connection — surfaced in the
	// tooltip so the user sees the real number, not just the threshold
	// tier. All kinds carry seconds: long-walk = longest walk leg,
	// long-wait = longest transfer wait, very-slow = duration gap to the
	// fastest surviving itinerary.
	value: number;
}

export interface CardState {
	badge: Badge | null;
	warnings: Warning[];
}

// Comfort factor multiplies the trip's duration to produce an
// "effective time". Each malus ∈ [0, 1]; they add and share a fixed cap,
// so the factor lives in [1.0, 1.0 + 2·COMFORT_FACTOR_SLOPE] = [1.0, 1.2].
// Worseness is then a single ratio (this_eff / min_eff − 1), and the 80/20
// speed-vs-comfort intuition is baked into the factor's shape — no
// separate weight to tune, no unbounded ratios.
const COMFORT_FACTOR_SLOPE = 0.1;
// Boarding malus: 1 − (1 − r)^n over transit legs, r = 0.3. Walk-only
// 0%, first boarding 30%, then gently saturating: 51 / 66 / 76 / … %
// toward 100%. Counting boardings (not transfers) prices in schedule-
// dependence — a walk-only trip needs no vehicle at all.
const TRANSFER_STEP_R = 0.3;
// Walking malus: t² / (t² + T²) with t in minutes, T = 30.
// 10 min → 10%, 20 → 31%, 30 → 50%, 40 → 64%, 60 → 80%.
const WALK_HALF_MIN = 30;

// Absolute worseness thresholds — no dependency on the surviving set's
// spread. Adding or removing another itinerary never re-ranks the rest.
const GOOD_MAX_PCT = 0.07;   // within 7% of fastest → thumbs up
const BAD_MIN_PCT  = 0.25;   // ≥ 25% worse → thumbs down (tunable)

const LONG_WALK_SEC        = 20 * 60;
const MEDIUM_WALK_SEC      = 40 * 60;
const STRONG_WALK_SEC      = 60 * 60;
const LONG_WAIT_SEC        = 60 * 60;
const MEDIUM_WAIT_SEC      = 2 * 60 * 60;
const STRONG_WAIT_SEC      = 3 * 60 * 60;
const VERY_SLOW_FACTOR     = 1.5;
const MEDIUM_SLOW_FACTOR   = 2;
const STRONG_SLOW_FACTOR   = 2.5;
// Minimum absolute duration gap before any very-slow warning fires. A
// 1.5× ratio on a 6-min trip is only 3 min — not worth flagging. The gate
// keeps the warning off short connections where the ratio looks dramatic
// but the absolute difference is trivial.
const VERY_SLOW_MIN_DIFF_SEC = 10 * 60;

function transferMalus(boardings: number): number {
	return 1 - Math.pow(1 - TRANSFER_STEP_R, boardings);
}

function walkMalus(walkSec: number): number {
	const t = walkSec / 60;
	return (t * t) / (t * t + WALK_HALF_MIN * WALK_HALF_MIN);
}

/** Multiplier applied to duration to get effective time. In [1.0, 1.2]. */
export function comfortFactor(it: Itinerary): number {
	const w = walkMalus(walkSeconds(it));
	const x = transferMalus(boardingCount(it));
	return 1 + COMFORT_FACTOR_SLOPE * (w + x);
}

/** Longest single WALK leg in seconds. */
function longestWalkLeg(it: Itinerary): number {
	let max = 0;
	for (const l of it.legs) if (l.mode === 'WALK') max = Math.max(max, legDuration(l));
	return max;
}

/** Longest wait between two consecutive transit legs in seconds. Walking
 * time between them is subtracted — a 55 min walk with a 5 min stop
 * counts as a 5 min wait, not 60. */
function longestTransferWait(it: Itinerary): number {
	let prevTransitEnd: number | null = null;
	let walkBetween = 0;
	let maxWait = 0;
	for (const l of it.legs) {
		const isTransit = l.mode !== 'WALK' && l.mode !== 'BIKE' && l.mode !== 'CAR';
		if (isTransit) {
			if (prevTransitEnd !== null) {
				const wait = (Date.parse(l.startTime) - prevTransitEnd) / 1000 - walkBetween;
				if (wait > maxWait) maxWait = wait;
			}
			prevTransitEnd = Date.parse(l.endTime);
			walkBetween = 0;
		} else if (l.mode === 'WALK' && prevTransitEnd !== null) {
			walkBetween += legDuration(l);
		}
	}
	return maxWait;
}

/** Rate every itinerary against the best effective time in the set and
 * derive its badge + warnings. Returns one CardState per input in the
 * same order. Thresholds are absolute — adding or removing an itinerary
 * never re-ranks the ones that remain. */
export function computeCardStates(itins: Itinerary[]): CardState[] {
	if (itins.length === 0) return [];

	const effTimes = itins.map((it) => it.duration * comfortFactor(it));
	const minEff = Math.min(...effTimes);
	const worseness = effTimes.map((e) => (minEff > 0 ? e / minEff - 1 : 0));

	// Best = lowest worseness (i.e. lowest effective time). All itineraries
	// tied on the minimum share the crown — an arbitrary tie-break would
	// crown one and demote its identical siblings to Good.
	const bestWorseness = Math.min(...worseness);
	const isBest = worseness.map((w) => w === bestWorseness);

	// Only needed for the very-slow warnings, which compare raw duration
	// against the raw fastest — not the comfort-adjusted one.
	const minDur = Math.min(...itins.map((i) => i.duration));

	return itins.map((it, i) => {
		let badge: Badge | null = null;
		if (isBest[i]) badge = 'best';
		else if (worseness[i] <= GOOD_MAX_PCT) badge = 'good';
		else if (worseness[i] >= BAD_MIN_PCT) badge = 'bad';

		const warnings: Warning[] = [];
		const walk = longestWalkLeg(it);
		if (walk > STRONG_WALK_SEC) warnings.push({ kind: 'long-walk', severity: 'strong', value: walk });
		else if (walk > MEDIUM_WALK_SEC) warnings.push({ kind: 'long-walk', severity: 'medium', value: walk });
		else if (walk > LONG_WALK_SEC) warnings.push({ kind: 'long-walk', severity: 'standard', value: walk });
		const wait = longestTransferWait(it);
		if (wait >= STRONG_WAIT_SEC) warnings.push({ kind: 'long-wait', severity: 'strong', value: wait });
		else if (wait >= MEDIUM_WAIT_SEC) warnings.push({ kind: 'long-wait', severity: 'medium', value: wait });
		else if (wait >= LONG_WAIT_SEC) warnings.push({ kind: 'long-wait', severity: 'standard', value: wait });
		const slowGap = it.duration - minDur;
		// Gate the whole chain on the minimum absolute difference so the
		// ratio thresholds don't fire on short trips with small gaps.
		if (slowGap >= VERY_SLOW_MIN_DIFF_SEC) {
			if (it.duration >= STRONG_SLOW_FACTOR * minDur) warnings.push({ kind: 'very-slow', severity: 'strong', value: slowGap });
			else if (it.duration >= MEDIUM_SLOW_FACTOR * minDur) warnings.push({ kind: 'very-slow', severity: 'medium', value: slowGap });
			else if (it.duration >= VERY_SLOW_FACTOR * minDur) warnings.push({ kind: 'very-slow', severity: 'standard', value: slowGap });
		}

		return { badge, warnings };
	});
}
