import type { Itinerary, Leg, TimeMode } from './types';

// Quality ranking for the merged cascade results — see transit-routing.md
// § Ranking. A single rule drops an itinerary A in favour of some B when
// B time-beats A on the query's primary axis (arrival for leave-at,
// departure for arrive-by) AND A's comfort penalty over B exceeds an
// allowance that scales with the pair's temporal gap. The score's only
// role is as this filter's escape hatch — it is never used for sorting.

const TRANSFER_PENALTY_SEC = 600;  // one transfer ≈ 5 min of walking
const WALK_PER_SEC = 2;            // linear: +5 min walking = +600 at any baseline
const T_SLACK_MS = 60 * 1000;      // start/end jitter that still counts as "same time"
const MARGIN = 300;                // allowed comfort penalty at zero gap (≈ 2.5 min walking)
// Linear slope so the anchor calibration matches the previous two-tier
// system's boundary: gap = 2 h → allowed = MARGIN + 7200·slope ≈ 3000
// (the old BIG_MARGIN). Grows without cap beyond that so genuinely
// temporally-distinct options can absorb larger comfort differences.
const PENALTY_PER_SEC = 0.375;

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

/** Comfort score — lower is better. Only used as the dominance escape
 * hatch, never for sorting. */
export function itineraryScore(it: Itinerary): number {
	return TRANSFER_PENALTY_SEC * transferCount(it) + WALK_PER_SEC * walkSeconds(it);
}

interface Entry {
	it: Itinerary;
	start: number;
	end: number;
	score: number;
}

/** True when B time-beats A on the query's primary axis (arrival for
 * leave-at, departure for arrive-by, within T_SLACK) AND A's comfort
 * penalty over B exceeds the allowance scaled by the pair's temporal gap.
 *
 * `gap = min(|Δstart|, |Δend|)` — the tighter axis distance. Two options
 * far apart on one axis but near-identical on the other are treated as
 * near-ties: the tight axis limits how much comfort penalty the worse
 * one can afford. Options genuinely temporally distinct on both axes get
 * a large allowance and survive even with big comfort differences. */
function beatenBy(a: Entry, b: Entry, mode: TimeMode): boolean {
	const primaryBeats = mode === 'arrive'
		? b.start >= a.start - T_SLACK_MS
		: b.end <= a.end + T_SLACK_MS;
	if (!primaryBeats) return false;
	const gapSec = Math.min(
		Math.abs(a.start - b.start),
		Math.abs(a.end - b.end)
	) / 1000;
	const allowed = MARGIN + gapSec * PENALTY_PER_SEC;
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

const SPEED_WEIGHT = 0.8;
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

/** Rate every itinerary against the fastest / most-comfortable in the set
 * and derive its badge + warnings. Returns one CardState per input in the
 * same order. Thresholds are absolute — adding or removing an itinerary
 * never re-ranks the ones that remain. */
export function computeCardStates(itins: Itinerary[]): CardState[] {
	if (itins.length === 0) return [];

	const durations = itins.map((i) => i.duration);
	const scores = itins.map(itineraryScore);
	const minDur = Math.min(...durations);
	const minScore = Math.min(...scores);

	// Worseness = weighted sum of the two "how much worse than best" ratios.
	// 0 for the fastest with the best comfort; grows with each axis.
	const worseness = itins.map((_, i) => {
		const speedPct = minDur > 0 ? durations[i] / minDur - 1 : 0;
		let comfortPct: number;
		if (minScore > 0) {
			comfortPct = scores[i] / minScore - 1;
		} else {
			// Degenerate case: fastest itinerary has score 0. Anything with a
			// non-zero score is treated as infinitely worse on that axis so it
			// can never earn Good; effectively drops it to Bad unless duration
			// lifts it into Best.
			comfortPct = scores[i] === 0 ? 0 : Infinity;
		}
		return SPEED_WEIGHT * speedPct + (1 - SPEED_WEIGHT) * comfortPct;
	});

	// Best = lowest worseness. Tie-break by earliest arrival so only one
	// itinerary ever wears the crown.
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
