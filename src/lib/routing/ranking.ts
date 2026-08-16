import type { Itinerary, Leg, TimeMode } from './types';

// Quality ranking for the merged cascade results — see transit-routing.md
// § Ranking. A single rule drops an itinerary A in favour of some B when
// B time-beats A on the query's primary axis (arrival for leave-at,
// departure for arrive-by) AND A's comfort penalty over B exceeds an
// allowance that scales with the pair's temporal gap. The score's only
// role is as this filter's escape hatch — it is never used for sorting.

const TRANSFER_PENALTY_SEC = 600;    // one transfer ≈ 5 min of walking
const WALK_PER_SEC = 2;              // full linear rate for the first 30 min
// Walking cost is soft-capped: small walking differences (0–30 min) stay
// as sensitive as before, but each further second is worth a quarter as
// much — a 30-min-vs-3-h walking difference no longer overwhelms every
// realistic temporal-gap allowance.
const WALK_SOFT_CAP_SEC = 30 * 60;   // linear knee-point (30 min)
const WALK_TAIL_PER_SEC = 0.5;       // shallow slope past the knee
const T_SLACK_MS = 60 * 1000;        // start/end jitter that still counts as "same time"
// Comfort-edge requirement at zero temporal gap: A must be MORE
// comfortable than B by more than MARGIN to survive when B time-beats
// it — matches the old tier-1 semantics. Positive allowance kicks in as
// the gap grows.
const MARGIN = 300;
// Cube-root curve for the allowance: rises fast at short gaps so a rare
// fast option can't nuke its neighbours (2 min gap already needs ≥15 min
// extra walking to prune), saturates gracefully at long gaps (2 h → ~8000
// ≈ 65 min extra walking under the soft cap). Linear couldn't hit both
// "steep at 2 min" and "sane at 2 h" simultaneously.
const PENALTY_K = 430;

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
	let transit = 0;
	for (const l of it.legs) if (l.mode !== 'WALK' && l.mode !== 'BIKE' && l.mode !== 'CAR') transit++;
	return Math.max(0, transit - 1);
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
	return TRANSFER_PENALTY_SEC * transferCount(it) + walkCost(walkSeconds(it));
}

interface Entry {
	it: Itinerary;
	start: number;
	end: number;
	score: number;
}

/** True when B time-beats A on the query's primary axis (arrival for
 * leave-at, departure for arrive-by, within T_SLACK) AND A's comfort
 * penalty over B exceeds the allowance for the pair's temporal gap.
 *
 * `gap = min(|Δstart|, |Δend|)` — the tighter axis distance. Two options
 * far apart on one axis but near-identical on the other are treated as
 * near-ties: the tight axis limits how much comfort penalty the worse
 * one can afford. `allowed = −MARGIN + PENALTY_K · gap^(1/3)` — at zero
 * gap A must be MORE comfy than B by MARGIN (old tier-1 semantics); the
 * cube-root rises fast so even a 2 min gap already tolerates a fairly
 * steep comfort difference, and saturates gracefully so a 2 h gap sits
 * around a "dramatic" allowance rather than an absurd one. */
function beatenBy(a: Entry, b: Entry, mode: TimeMode): boolean {
	const primaryBeats = mode === 'arrive'
		? b.start >= a.start - T_SLACK_MS
		: b.end <= a.end + T_SLACK_MS;
	if (!primaryBeats) return false;
	const gapSec = Math.min(
		Math.abs(a.start - b.start),
		Math.abs(a.end - b.end)
	) / 1000;
	const allowed = -MARGIN + PENALTY_K * Math.cbrt(gapSec);
	return a.score - b.score > allowed;
}

/** Drop each itinerary that some other beats under the temporal-gap-
 * scaled comfort rule. Input order is preserved; the caller sorts
 * chronologically afterwards. */
export function pruneDominated(its: Itinerary[], mode: TimeMode): Itinerary[] {
	const entries: Entry[] = its.map((it) => ({
		it,
		start: Date.parse(it.startTime),
		end: Date.parse(it.endTime),
		score: itineraryScore(it)
	}));
	return entries
		.filter((a) => !entries.some((b) => b !== a && beatenBy(a, b, mode)))
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
// Transfer malus: 1 − (1 − r)^n, r = 0.3. First transfer 30%, then
// gently saturating: 0 / 30 / 51 / 66 / 76 / … % toward 100%.
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
const VERY_SLOW_FACTOR     = 2;
const MEDIUM_SLOW_FACTOR   = 3;
const STRONG_SLOW_FACTOR   = 4;

function transferMalus(transfers: number): number {
	return 1 - Math.pow(1 - TRANSFER_STEP_R, transfers);
}

function walkMalus(walkSec: number): number {
	const t = walkSec / 60;
	return (t * t) / (t * t + WALK_HALF_MIN * WALK_HALF_MIN);
}

/** Multiplier applied to duration to get effective time. In [1.0, 1.2]. */
export function comfortFactor(it: Itinerary): number {
	const w = walkMalus(walkSeconds(it));
	const x = transferMalus(transferCount(it));
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

	// Best = lowest worseness (i.e. lowest effective time). Tie-break by
	// earliest arrival so only one itinerary ever wears the crown.
	const bestWorseness = Math.min(...worseness);
	let bestIdx = -1;
	let bestArrival = Infinity;
	worseness.forEach((w, i) => {
		if (w > bestWorseness) return;
		const arrival = Date.parse(itins[i].endTime);
		if (arrival < bestArrival) {
			bestArrival = arrival;
			bestIdx = i;
		}
	});

	// Only needed for the very-slow warnings, which compare raw duration
	// against the raw fastest — not the comfort-adjusted one.
	const minDur = Math.min(...itins.map((i) => i.duration));

	return itins.map((it, i) => {
		let badge: Badge | null = null;
		if (i === bestIdx) badge = 'best';
		else if (worseness[i] <= GOOD_MAX_PCT) badge = 'good';
		else if (worseness[i] >= BAD_MIN_PCT) badge = 'bad';

		const warnings: Warning[] = [];
		const walk = longestWalkLeg(it);
		if (walk > STRONG_WALK_SEC) warnings.push({ kind: 'long-walk', severity: 'strong' });
		else if (walk > MEDIUM_WALK_SEC) warnings.push({ kind: 'long-walk', severity: 'medium' });
		else if (walk > LONG_WALK_SEC) warnings.push({ kind: 'long-walk', severity: 'standard' });
		const wait = longestTransferWait(it);
		if (wait >= STRONG_WAIT_SEC) warnings.push({ kind: 'long-wait', severity: 'strong' });
		else if (wait >= MEDIUM_WAIT_SEC) warnings.push({ kind: 'long-wait', severity: 'medium' });
		else if (wait >= LONG_WAIT_SEC) warnings.push({ kind: 'long-wait', severity: 'standard' });
		if (it.duration >= STRONG_SLOW_FACTOR * minDur) warnings.push({ kind: 'very-slow', severity: 'strong' });
		else if (it.duration >= MEDIUM_SLOW_FACTOR * minDur) warnings.push({ kind: 'very-slow', severity: 'medium' });
		else if (it.duration >= VERY_SLOW_FACTOR * minDur) warnings.push({ kind: 'very-slow', severity: 'standard' });

		return { badge, warnings };
	});
}
