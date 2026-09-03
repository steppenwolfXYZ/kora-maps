import type { Itinerary, Leg, LegPlace, TimeMode } from './types';

// Quality ranking for the merged cascade results — see transit-routing.md
// § Ranking. Three unconditional prunes run on Pareto-time-dominated
// pairs first: Rule 0 (same route minus a vehicle, slower AND more
// walking), Rule 0b (prefix/suffix dominance — the distinct trains are
// provably catchable from B's own legs with less walking), Rule 0c
// (shared endpoint + more walking = no benefit anywhere). Every other
// pair (A, B) falls into one of two shapes and is judged by a
// case-specific rule:
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
// Absorbs seconds-granular walk-offset jitter (Valhalla walk legs shift
// itinerary endpoints by seconds) — but must stay below 60 s: transit
// times are minute-granular, and a full minute is a real difference on
// short connections (a 60 s slack let a bus arriving a minute later
// Pareto-dominate a tram and prune it via the Case 1 comfort test).
const T_SLACK_MS = 50 * 1000;        // start/end jitter that still counts as "same time"
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
// Minimize-walking reverse displacement (a slower low-walk connection
// dropping a faster walk-heavy one) only fires within this primary-axis
// distance — beyond it, the faster option is "the only one around" and
// stays regardless of walking.
const REVERSE_DISPLACE_MAX_GAP_MS = 3 * 3600 * 1000;

export function legDuration(leg: Leg): number {
	return leg.duration ?? Math.max(0, (Date.parse(leg.endTime) - Date.parse(leg.startTime)) / 1000);
}

/** Seconds walking across all WALK legs, including inter-station transfer
 * walks (same total the result card shows). Same-stop "walk" legs are
 * excluded: MOTIS renders the mandatory change-time buffer of a
 * same-platform transfer as a WALK leg from a stop to itself — that's
 * waiting, not walking, and counting it distorts the comfort rating. */
export function walkSeconds(it: Itinerary): number {
	let s = 0;
	for (const l of it.legs) {
		if (l.mode !== 'WALK') continue;
		if (l.from?.stopId != null && l.from.stopId === l.to?.stopId) continue;
		s += legDuration(l);
	}
	return s;
}

/** Metres walked across the same legs `walkSeconds` counts. Legs whose
 * distance MOTIS omitted contribute 0. */
export function walkMetres(it: Itinerary): number {
	let m = 0;
	for (const l of it.legs) {
		if (l.mode !== 'WALK') continue;
		if (l.from?.stopId != null && l.from.stopId === l.to?.stopId) continue;
		m += l.distance ?? 0;
	}
	return m;
}

/** Ascent / descent in metres across the same legs `walkSeconds` counts.
 * `null` when no walk leg carried elevation (older MOTIS build, or no
 * elevation data on the Valhalla side). */
export function walkElevation(it: Itinerary): { up: number; down: number } | null {
	let up = 0;
	let down = 0;
	let any = false;
	for (const l of it.legs) {
		if (l.mode !== 'WALK') continue;
		if (l.from?.stopId != null && l.from.stopId === l.to?.stopId) continue;
		if (l.elevationUp == null && l.elevationDown == null) continue;
		any = true;
		up += l.elevationUp ?? 0;
		down += l.elevationDown ?? 0;
	}
	return any ? { up, down } : null;
}

/** Number of transit legs − 1 (same count the result card shows). Vehicle
 * changes forced by a via the traveller asked to wait at are subtracted —
 * getting off where you meant to get off is not a transfer
 * (via-stops.md § Planned dwell). */
export function transferCount(it: Itinerary, opts?: RankOptions): number {
	const base = typeof it.transfers === 'number'
		? it.transfers
		: Math.max(0, boardingCount(it) - 1);
	return Math.max(0, base - viaForcedChanges(it, opts));
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

// Minimize-walking mode (routing-options.md § Minimize walking): the
// relative importance shifts from ~timing 80 / transfers 10 / walking 10
// to ~timing 40 / transfers 10 / walking 50 — expressed here as a flat
// multiplier on the walking cost terms (transfer terms untouched).
const MINIMIZE_WALK_MULT = 4;

/** Ranking knobs derived from the user's routing options. All optional —
 * absent means today's behavior. */
export interface RankOptions {
	/** Weight walking ~5x heavier in pruning, badges and comfort. */
	minimizeWalking?: boolean;
	/** Route ids of "continuous" gondolas (short frequencies.txt headways,
	 * from route_color_index.json via loadHfGondolaRoutes) — boarding them
	 * never warns: missing one departure means taking the next a minute
	 * later. */
	hfGondolaRoutes?: Set<string> | null;
	/** via-stops.md § Planned dwell: the sum of the REQUESTED via waits in
	 * seconds. Subtracted from duration before every quality judgement
	 * (effective time, worseness, very-slow) — time the traveller asked
	 * for is theirs, not travel time. Deliberately the request, not the
	 * realised stay: a via where the next departure is an hour out still
	 * costs real dead time and must stay visible to the comparison. */
	plannedDwellSec?: number;
	/** MOTIS parent-station id of each via → its REQUESTED wait in
	 * seconds. Lets the long-wait warning and the displayed transfer count
	 * see which stop-time is deliberate. */
	viaWaitByStop?: Map<string, number> | null;
}

/** Judged duration: the trip's duration minus the planned via dwell. Every
 * quality comparison uses this; nothing the user reads as a clock does
 * (via-stops.md § Planned dwell). Floored at a minute so a dwell that
 * swallows the whole trip can't produce a zero or negative ratio. */
export function judgedDuration(it: Itinerary, opts?: RankOptions): number {
	return Math.max(60, it.duration - (opts?.plannedDwellSec ?? 0));
}

/** The via wait requested at the station a leg place sits in, in seconds —
 * 0 when the place is not a via. Legs carry the parent station as
 * `parentId` (the same id shape a via is sent to the engine as); `stopId`
 * is a fallback for places the feed gives no parent. */
function viaWaitAt(place: LegPlace | undefined, opts?: RankOptions): number {
	const map = opts?.viaWaitByStop;
	if (!map || !place) return 0;
	const byParent = place.parentId ? map.get(place.parentId) : undefined;
	if (byParent != null) return byParent;
	return (place.stopId ? map.get(place.stopId) : undefined) ?? 0;
}

/** Vehicle changes the traveller makes only because they asked to stop
 * there — a junction between two transit legs at a via with a non-zero
 * requested wait. Not transfers in any sense the traveller cares about
 * (via-stops.md), so the displayed transfer count excludes them. A change
 * at a `wait = 0` via is an ordinary transfer and is not counted here. */
function viaForcedChanges(it: Itinerary, opts?: RankOptions): number {
	if (!opts?.viaWaitByStop?.size) return 0;
	let n = 0;
	let prev: Leg | null = null;
	for (const l of it.legs) {
		const isTransit = l.mode !== 'WALK' && l.mode !== 'BIKE' && l.mode !== 'CAR';
		if (isTransit) {
			if (prev && viaWaitAt(prev.to, opts) > 0) n++;
			prev = l;
		} else if (l.mode !== 'WALK') {
			prev = null;
		}
	}
	return n;
}

/** Walking cost with a soft cap at 30 min: full linear rate below the
 * knee, quarter rate above. Keeps small walking differences meaningful
 * while bounding the score inflation from multi-hour hikes. */
function walkCost(walkSec: number, opts?: RankOptions): number {
	if (opts?.minimizeWalking) {
		// No soft cap in minimize-walking: discounting long walking is
		// exactly what this mode must not do. The capped cost shrank the
		// walk-heavy-vs-low-walk score gaps to ~the pruning allowance,
		// so near-identical connections fell on opposite sides of the
		// boundary (routing-options.md § Minimize walking).
		return MINIMIZE_WALK_MULT * WALK_PER_SEC * walkSec;
	}
	const base = WALK_PER_SEC * Math.min(walkSec, WALK_SOFT_CAP_SEC);
	const tail = WALK_TAIL_PER_SEC * Math.max(0, walkSec - WALK_SOFT_CAP_SEC);
	return base + tail;
}

/** Comfort score — lower is better. Only used as the dominance escape
 * hatch, never for sorting. */
export function itineraryScore(it: Itinerary, opts?: RankOptions): number {
	return TRANSFER_PENALTY_SEC * boardingCount(it) + walkCost(walkSeconds(it), opts);
}

interface Entry {
	it: Itinerary;
	start: number;
	end: number;
	score: number;
	effTime: number;
	walk: number;
	transitKeys: Set<string>;
	vehicles: string;
}

/** Identity keys of an itinerary's transit legs: same vehicle (trip),
 * boarded and left at the same stops. Legs without a tripId fall back to
 * route + departure time, which is equally stable within one result set. */
function transitLegKeys(it: Itinerary): Set<string> {
	const keys = new Set<string>();
	for (const l of it.legs) {
		if (l.mode === 'WALK' || l.mode === 'BIKE' || l.mode === 'CAR') continue;
		const vehicle = l.tripId ?? `${l.routeId ?? l.routeShortName ?? ''}@${l.startTime}`;
		keys.add(`${vehicle}|${l.from?.stopId ?? l.from?.name ?? ''}|${l.to?.stopId ?? l.to?.name ?? ''}`);
	}
	return keys;
}

/** Vehicle-only identity: the ordered trips ridden, ignoring where they
 * are boarded and left. Two itineraries with equal vehicle keys are pure
 * endpoint-walk variants of one another (enter/exit the same runs at
 * different stations). */
function vehicleKeys(it: Itinerary): string {
	const keys: string[] = [];
	for (const l of it.legs) {
		if (l.mode === 'WALK' || l.mode === 'BIKE' || l.mode === 'CAR') continue;
		keys.push(l.tripId ?? `${l.routeId ?? l.routeShortName ?? ''}@${l.startTime}`);
	}
	return keys.join('|');
}

// Walk-difference slack for the same-vehicles rule: below this, two
// endpoint choices count as equally good on the walking axis.
const SAME_VEHICLE_WALK_SLACK_SEC = 30;

/** Rule 0d — same vehicles, worse endpoints: A and B ride exactly the
 * same runs and differ only in where they enter/exit (and thus in the
 * endpoint walks). Such pairs are compared ONLY on (arrival, total
 * walking) — the departure axis is ignored, because leaving home a
 * minute earlier or later to reach a different stop of the same vehicle
 * is not a real alternative. A is dropped when B is equal-or-better on
 * both axes and strictly better on one; on a full tie, the later
 * departure (then input order) wins so exactly one of the pair
 * survives. */
function sameVehiclesDropped(a: Entry, b: Entry, aIdx: number, bIdx: number): boolean {
	if (a.vehicles.length === 0 || a.vehicles !== b.vehicles) return false;
	const arrWorse = a.end >= b.end - T_SLACK_MS;
	const walkWorse = a.walk >= b.walk - SAME_VEHICLE_WALK_SLACK_SEC;
	if (!arrWorse || !walkWorse) return false;
	const arrStrict = a.end > b.end + T_SLACK_MS;
	const walkStrict = a.walk > b.walk + SAME_VEHICLE_WALK_SLACK_SEC;
	if (arrStrict || walkStrict) return true;
	// Full tie on both axes — deterministic tie-break, one survivor.
	if (a.start !== b.start) return a.start < b.start;
	return aIdx > bIdx;
}

/** True when A is "same route minus a vehicle": A rides at least one
 * transit leg, every one of them is also in B (same trip, same board/
 * alight stops), and B rides strictly more — so A differs from B only by
 * replacing vehicles with walking. */
function vehicleSubset(a: Entry, b: Entry): boolean {
	if (a.transitKeys.size === 0 || a.transitKeys.size >= b.transitKeys.size) return false;
	for (const k of a.transitKeys) if (!b.transitKeys.has(k)) return false;
	return true;
}

function isTransitLeg(l: Leg): boolean {
	return l.mode !== 'WALK' && l.mode !== 'BIKE' && l.mode !== 'CAR';
}

/** Station identity for the prefix/suffix dominance checks — parent
 * station preferred so different platforms of one station compare equal. */
function stationKey(p?: LegPlace): string | null {
	return p?.parentId ?? p?.stopId ?? p?.name ?? null;
}

// Walking-difference guard for the unconditional prunes (Rules 0b/0c):
// Valhalla walk durations are seconds-granular, so two similar accesses
// can differ by jitter alone. The dominated side must walk meaningfully
// more before an unconditional drop fires.
const WALK_DOM_SLACK_SEC = 60;

/** Prefix dominance (access side): S = the station where A boards its
 * first transit leg. If B's own legs are provably at S ready to board at
 * or before A's departure from S, having walked meaningfully less than A
 * up to that point, A's access is pointless — B's actual prefix departs
 * home later, walks less, and still catches A's trains. Only B's real
 * legs are consulted; no timetable speculation. */
function accessDominated(a: Itinerary, b: Itinerary): boolean {
	const iA = a.legs.findIndex(isTransitLeg);
	if (iA < 0) return false;
	const s = stationKey(a.legs[iA].from);
	if (!s) return false;
	const depA = Date.parse(a.legs[iA].startTime);
	let walkA = 0;
	for (let k = 0; k < iA; k++) if (a.legs[k].mode === 'WALK') walkA += legDuration(a.legs[k]);

	let walkB = 0;
	for (let j = 0; j < b.legs.length; j++) {
		const l = b.legs[j];
		if (isTransitLeg(l)) {
			const lessWalk = walkA - walkB > WALK_DOM_SLACK_SEC;
			if (stationKey(l.from) === s) {
				const ready = j === 0 ? Date.parse(l.startTime) : Date.parse(b.legs[j - 1].endTime);
				if (ready <= depA && lessWalk) return true;
			}
			if (stationKey(l.to) === s && Date.parse(l.endTime) <= depA && lessWalk) return true;
		} else if (l.mode === 'WALK') {
			walkB += legDuration(l);
		}
	}
	return false;
}

/** Suffix dominance (egress side), mirror of accessDominated: S = the
 * station where A alights its last transit leg. Fires when B's own legs
 * depart S at or after A's arrival there with meaningfully less walking
 * left, or when B alights at S and only walks from there (a pure walk is
 * time-shiftable, so A could always follow it). */
function egressDominated(a: Itinerary, b: Itinerary): boolean {
	let iA = -1;
	for (let k = a.legs.length - 1; k >= 0; k--) if (isTransitLeg(a.legs[k])) { iA = k; break; }
	if (iA < 0) return false;
	const s = stationKey(a.legs[iA].to);
	if (!s) return false;
	const arrA = Date.parse(a.legs[iA].endTime);
	let walkA = 0;
	for (let k = iA + 1; k < a.legs.length; k++) if (a.legs[k].mode === 'WALK') walkA += legDuration(a.legs[k]);

	const walkAfter = (j: number) => {
		let sum = 0;
		for (let k = j + 1; k < b.legs.length; k++) if (b.legs[k].mode === 'WALK') sum += legDuration(b.legs[k]);
		return sum;
	};
	for (let j = 0; j < b.legs.length; j++) {
		const l = b.legs[j];
		if (!isTransitLeg(l)) continue;
		if (stationKey(l.from) === s && Date.parse(l.startTime) >= arrA
			&& walkA - walkAfter(j) > WALK_DOM_SLACK_SEC) return true;
		if (stationKey(l.to) === s
			&& b.legs.slice(j + 1).every((r) => r.mode === 'WALK')
			&& walkA - walkAfter(j) > WALK_DOM_SLACK_SEC) return true;
	}
	return false;
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
function droppedByOverlap(a: Entry, b: Entry, opts?: RankOptions): boolean {
	const timeGapMs = Math.max(Math.abs(a.start - b.start), Math.abs(a.end - b.end));
	// Minimize-walking (routing-options.md § Minimize walking): the
	// marginality window is a pure TIME rule — past 9 min A is dropped
	// for "costing more of your day for no time benefit", which ignores
	// the one axis this mode exists to weigh. An A that walks
	// meaningfully LESS therefore skips the window and is judged by the
	// comfort test alone (minimize-walking's effectiveTime already
	// prices walking linearly, so a genuinely bad A still fails it).
	// Case 2 has carried the mirror-image exception all along; without
	// this one a walk-heavier connection departing a few minutes later
	// silently deleted the low-walk option it happens to dominate in
	// time (canonical: same 18:32 arrival, 15:35/113 min walking
	// dropping both 15:22/80 min and 15:25/87 min).
	const walkExempt = !!opts?.minimizeWalking && b.walk - a.walk > WALK_DOM_SLACK_SEC;
	if (timeGapMs > OVERLAP_TIME_MAX_MS && !walkExempt) return true;
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
function droppedByNonOverlap(
	a: Entry, b: Entry, mode: TimeMode, opts?: RankOptions
): boolean {
	// Minimize-walking (routing-options.md § Minimize walking): Case 2
	// becomes direction-blind — the score-vs-allowance test applies even
	// when A is the FASTER one, so a much-lower-walk B can displace a
	// fast walk-heavy A. This reverse direction carries a HARD gap
	// ceiling on the primary axis: with the uncapped minwalk walk costs,
	// score gaps outgrow the cube-root allowance at any distance, and
	// without the ceiling next-morning low-walk connections wiped out
	// every same-day option the moment a "later" load brought them in.
	// Within the ceiling, drops are decisive (uncapped costs); beyond
	// it, a slower low-walk option never displaces a faster one.
	// Mutual drops stay impossible (the score difference has one sign),
	// and Pareto-dominating pairs never reach this rule (Case 1's
	// territory).
	const timeBeats = mode === 'arrive'
		? b.start >= a.start - T_SLACK_MS
		: b.end <= a.end + T_SLACK_MS;
	if (!timeBeats) {
		if (!opts?.minimizeWalking) return false;
		const primaryGapMs = mode === 'arrive'
			? Math.abs(a.start - b.start)
			: Math.abs(a.end - b.end);
		if (primaryGapMs > REVERSE_DISPLACE_MAX_GAP_MS) return false;
	}
	const gapSec = Math.max(GAP_FLOOR_SEC, Math.min(
		Math.abs(a.start - b.start),
		Math.abs(a.end - b.end)
	) / 1000);
	const allowed = -MARGIN + PENALTY_K * Math.cbrt(gapSec);
	return a.score - b.score > allowed;
}

/** Dispatches (a, b) to the case-specific rule. Two unconditional prunes
 * run first, both only for Pareto-time-dominated A (no marginality
 * allowance applies):
 *   Rule 0 — A is the same route as B minus one or more vehicles (walked
 *     instead) and the trade also cost walking: pure noise. If the trade
 *     wins on either axis (faster, or less walking), A falls through.
 *   Rule 0b — prefix/suffix dominance: A's distinct trains buy nothing,
 *     because B's actual legs reach A's first boarding station in time to
 *     catch them (or leave A's last alighting station after A arrives)
 *     with meaningfully less walking.
 *   Rule 0c — shared endpoint: A arrives (or departs) together with B,
 *     is dominated, and walks meaningfully more — no benefit anywhere.
 * Then: when B Pareto-time-dominates A the pair is overlapping (Case 1).
 * When A dominates B the pair is B's problem, not A's — return false.
 * Otherwise apply Case 2. */
function droppedBy(a: Entry, b: Entry, mode: TimeMode, opts?: RankOptions): boolean {
	if (paretoTimeDominates(b, a)) {
		if (vehicleSubset(a, b) && a.walk > b.walk) return true;
		if (accessDominated(a.it, b.it) || egressDominated(a.it, b.it)) return true;
		// Rule 0c — shared endpoint: A and B arrive together (or leave
		// together), so A's whole time claim collapses onto its one worse
		// endpoint. Had both started at that shared point, A would simply
		// be slower for no benefit; extra walking must not rescue it. A
		// with meaningfully LESS walking still offers a real trade and
		// falls through to Case 1's marginality.
		if ((Math.abs(a.end - b.end) <= T_SLACK_MS || Math.abs(a.start - b.start) <= T_SLACK_MS)
			&& a.walk - b.walk > WALK_DOM_SLACK_SEC) return true;
		return droppedByOverlap(a, b, opts);
	}
	if (paretoTimeDominates(a, b)) return false;
	return droppedByNonOverlap(a, b, mode, opts);
}

/** Drop each itinerary that some other beats under the two-case rule
 * (Case 1 overlapping, Case 2 non-overlapping). Input order is preserved;
 * the caller sorts chronologically afterwards. */
export function pruneDominated(
	its: Itinerary[], mode: TimeMode, opts?: RankOptions
): Itinerary[] {
	const entries: Entry[] = its.map((it) => ({
		it,
		start: Date.parse(it.startTime),
		end: Date.parse(it.endTime),
		score: itineraryScore(it, opts),
		effTime: judgedDuration(it, opts) * comfortFactor(it, opts),
		walk: walkSeconds(it),
		transitKeys: transitLegKeys(it),
		vehicles: vehicleKeys(it)
	}));
	return entries
		.filter((a, ai) => !entries.some((b, bi) =>
			b !== a && (sameVehiclesDropped(a, b, ai, bi) || droppedBy(a, b, mode, opts))))
		.map((e) => e.it);
}

// Per-card badge + warning derivation — see transit-routing.md § Badges,
// § Warnings.

export type Badge = 'best' | 'good' | 'bad';
export type WarningKind =
	'long-walk' | 'long-wait' | 'very-slow' | 'tight-transfer' | 'lucky-transfer';
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
	// fastest surviving itinerary, tight-transfer / lucky-transfer =
	// spare seconds of the worst transfer (may be negative).
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
const BAD_MIN_PCT  = 0.3;    // ≥ 30% worse → thumbs down (tunable)
// Minimum absolute effective-time gap before a thumbs down fires — on
// short trips a small gap crosses the percentage threshold too easily
// (2 min on a 7-min trip is already +29%).
const BAD_MIN_DIFF_SEC = 5 * 60;

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

// Minimize-walking: the walking malus gets its own, much steeper slope
// (0.5 instead of 0.1) so walking-heavy connections rate clearly worse;
// the boarding malus keeps the standard slope. Factor range grows from
// [1.0, 1.2] to [1.0, 1.6].
const MINIMIZE_WALK_SLOPE = 0.5;

/** Effective time driving badges and auto-select. Normal mode:
 * duration x comfortFactor (multiplicative, bounded malus). Minimize
 * walking: duration + penalty score (additive) — the multiplicative
 * walk malus SATURATES (t^2 curve), so an 11-min walking difference
 * between two 80-90-min-walk connections registers as ~1% and a
 * 3-min duration edge outvotes it; the additive score keeps walking
 * differences linear (routing-options.md § Minimize walking). */
export function effectiveTime(it: Itinerary, opts?: RankOptions): number {
	if (opts?.minimizeWalking) {
		return judgedDuration(it, opts) + itineraryScore(it, opts);
	}
	return judgedDuration(it, opts) * comfortFactor(it, opts);
}

/** Multiplier applied to duration to get effective time. In [1.0, 1.2]
 * (up to [1.0, 1.6] with minimize-walking). */
export function comfortFactor(it: Itinerary, opts?: RankOptions): number {
	const w = walkMalus(walkSeconds(it));
	const x = transferMalus(boardingCount(it));
	const walkSlope = opts?.minimizeWalking ? MINIMIZE_WALK_SLOPE : COMFORT_FACTOR_SLOPE;
	return 1 + walkSlope * w + COMFORT_FACTOR_SLOPE * x;
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
function longestTransferWait(it: Itinerary, opts?: RankOptions): number {
	let prevTransitEnd: number | null = null;
	let prevTransitTo: LegPlace | undefined;
	let walkBetween = 0;
	let maxWait = 0;
	for (const l of it.legs) {
		const isTransit = l.mode !== 'WALK' && l.mode !== 'BIKE' && l.mode !== 'CAR';
		if (isTransit) {
			if (prevTransitEnd !== null) {
				// At a via only the EXCESS over the requested wait counts —
				// a 15 min stay on a 15 min request is the errand, not a
				// warning (via-stops.md § Planned dwell).
				const wait = (Date.parse(l.startTime) - prevTransitEnd) / 1000
					- walkBetween - viaWaitAt(prevTransitTo, opts);
				if (wait > maxWait) maxWait = wait;
			}
			prevTransitEnd = Date.parse(l.endTime);
			prevTransitTo = l.to;
			walkBetween = 0;
		} else if (l.mode === 'WALK' && prevTransitEnd !== null) {
			walkBetween += legDuration(l);
		}
	}
	return maxWait;
}

// Tight-transfer ladder (routing-options.md § Connection warnings). All
// feasibility math runs at the user's SET walking speed — the fork
// reports every WALK leg's duration as Valhalla's own walking seconds at
// that speed (never the leg's safety-scaled time span), so leg durations
// can be taken at face value in every safety mode.
export type TransferTier = 'tight' | 'very-tight' | 'extremely-tight' | 'lucky';
export interface TransferAssessment {
	/** Index (into it.legs) of the transit leg BOARDED at this transfer —
	 * the detail view marks its departure row. */
	legIndex: number;
	tier: TransferTier;
	/** Spare seconds after walking at the set speed (negative = not
	 * makeable at that speed). */
	spare: number;
}

// The ladder is deliberately pitched so the tier maps onto the safety
// mode (routing-options.md § Connection warnings). Balanced only ever
// returns transfers the set speed makes (spare >= 0), so it can reach
// no tier above "tight" — and that one only inside the last 20 s of
// margin, i.e. rarely. Everything above needs a MEANINGFULLY negative
// spare, which only daring's halved transfer times produce. Calibrated
// on CH, where a positive spare on the Valhalla matrix genuinely means
// makeable; other countries will want their own thresholds.
const TIGHT_SPARE_SEC = 20;
// Rounding guard: walk-leg durations arrive as whole seconds while the
// schedule window is exact, so a walk that exactly fills its window can
// come back as a spare of −1 s. Anything inside this band counts as
// "makeable, no time to spare" rather than escalating a tier — walking
// times are not second-accurate to begin with.
const SPARE_EPSILON_SEC = 5;
const EXTREMELY_TIGHT_SPEEDUP = 1.2;  // needs > 20% faster walking
const LUCKY_SPEEDUP = 1.5;            // needs > 50% faster walking

// Timed-transfer exception (routing-options.md § Connection warnings):
// train → bus/tram and tram → bus transfers in CH are typically
// Anschluss-timed — the receiving vehicle waits for a late feeder — so
// a nominally short buffer is not actually tight. Such pairs only warn
// when the spare goes negative beyond the rounding guard at the set
// walking speed (even a waiting bus only holds a few minutes); the
// ladder applies unchanged
// then. tram → bus is an interim blanket rule — the per-line/
// per-station refinement is planned (regio-tram-timed-transfers.md);
// tram → tram is deliberately NOT exempt.
const RAIL_MODES = new Set([
	'RAIL', 'HIGHSPEED_RAIL', 'LONG_DISTANCE', 'NIGHT_RAIL',
	'REGIONAL_RAIL', 'REGIONAL_FAST_RAIL'
]);
const BUS_MODES = new Set(['BUS', 'COACH']);
function isTimedFeederPair(fromMode: string, toMode: string): boolean {
	if (RAIL_MODES.has(fromMode)) return BUS_MODES.has(toMode) || toMode === 'TRAM';
	if (fromMode === 'TRAM') return BUS_MODES.has(toMode);
	return false;
}

/** Strip the MOTIS dataset prefix ("ch_") so leg route ids match the
 * pipeline's raw GTFS route_ids (same rule as legColor.ts). */
function stripDatasetPrefix(routeId: string): string {
	return routeId.replace(/^[a-z]+_/, '');
}

/** Judge every transfer of the itinerary: available window = next
 * departure − previous transit arrival (schedule times, factor-agnostic);
 * needed = the walking between them at the set speed. Same-stop pseudo
 * walk legs (MOTIS renders the change-time buffer as a stop-to-itself
 * WALK) count as waiting, not walking — identical to walkSeconds(). */
export function assessTransfers(it: Itinerary, opts?: RankOptions): TransferAssessment[] {
	const hf = opts?.hfGondolaRoutes;
	const out: TransferAssessment[] = [];
	let prevTransitEnd: number | null = null;
	let prevTransitMode: string | null = null;
	let walkBetween = 0;
	for (let i = 0; i < it.legs.length; i++) {
		const l = it.legs[i];
		if (!isTransitLeg(l)) {
			if (l.mode === 'WALK' && prevTransitEnd !== null
				&& !(l.from?.stopId != null && l.from.stopId === l.to?.stopId)) {
				walkBetween += legDuration(l);
			}
			continue;
		}
		if (prevTransitEnd !== null) {
			const available = (Date.parse(l.startTime) - prevTransitEnd) / 1000;
			const needed = walkBetween;
			const spare = available - needed;
			let tier: TransferTier | null = null;
			if (spare >= -SPARE_EPSILON_SEC) {
				// Makeable (or short by rounding noise only).
				if (spare < TIGHT_SPARE_SEC) tier = 'tight';
			} else if (available <= 0 || (needed > 0 && needed > available * LUCKY_SPEEDUP)) {
				tier = 'lucky';
			} else if (needed > available * EXTREMELY_TIGHT_SPEEDUP) {
				tier = 'extremely-tight';
			} else {
				tier = 'very-tight';
			}
			// Continuous gondolas: no warning at all — a missed departure
			// just means the next one a minute later.
			const hfExempt = !!(hf && l.routeId
				&& (hf.has(l.routeId) || hf.has(stripDatasetPrefix(l.routeId))));
			// Timed train → bus/tram feeders: a non-negative spare is fine
			// (the vehicle waits); only a physically unmakeable transfer
			// still warns.
			const timedExempt = tier !== 'lucky' && spare >= -SPARE_EPSILON_SEC
				&& prevTransitMode !== null
				&& isTimedFeederPair(prevTransitMode, l.mode);
			if (tier && !hfExempt && !timedExempt) {
				out.push({ legIndex: i, tier, spare: Math.round(spare) });
			}
		}
		prevTransitEnd = Date.parse(l.endTime);
		prevTransitMode = l.mode;
		walkBetween = 0;
	}
	return out;
}

const TIGHT_SEVERITY: Record<Exclude<TransferTier, 'lucky'>, WarningSeverity> = {
	'tight': 'standard',
	'very-tight': 'medium',
	'extremely-tight': 'strong'
};
const TIER_RANK: Record<TransferTier, number> = {
	'tight': 0, 'very-tight': 1, 'extremely-tight': 2, 'lucky': 3
};

/** Rate every itinerary against the best effective time in the set and
 * derive its badge + warnings. Returns one CardState per input in the
 * same order. Thresholds are absolute — adding or removing an itinerary
 * never re-ranks the ones that remain. */
export function computeCardStates(itins: Itinerary[], opts?: RankOptions): CardState[] {
	if (itins.length === 0) return [];

	const effTimes = itins.map((it) => effectiveTime(it, opts));
	const minEff = Math.min(...effTimes);
	const worseness = effTimes.map((e) => (minEff > 0 ? e / minEff - 1 : 0));

	// Best = lowest worseness (i.e. lowest effective time). All itineraries
	// tied on the minimum share the crown — an arbitrary tie-break would
	// crown one and demote its identical siblings to Good.
	const bestWorseness = Math.min(...worseness);
	const isBest = worseness.map((w) => w === bestWorseness);

	// Only needed for the very-slow warnings, which compare raw duration
	// against the raw fastest — not the comfort-adjusted one. "Raw" still
	// means minus the planned via dwell: an errand the user asked for must
	// not make every connection look slow.
	const durations = itins.map((i) => judgedDuration(i, opts));
	const minDur = Math.min(...durations);

	return itins.map((it, i) => {
		let badge: Badge | null = null;
		if (isBest[i]) badge = 'best';
		else if (worseness[i] <= GOOD_MAX_PCT) badge = 'good';
		else if (worseness[i] >= BAD_MIN_PCT && effTimes[i] - minEff >= BAD_MIN_DIFF_SEC) badge = 'bad';

		const warnings: Warning[] = [];
		const walk = longestWalkLeg(it);
		if (walk > STRONG_WALK_SEC) warnings.push({ kind: 'long-walk', severity: 'strong', value: walk });
		else if (walk > MEDIUM_WALK_SEC) warnings.push({ kind: 'long-walk', severity: 'medium', value: walk });
		else if (walk > LONG_WALK_SEC) warnings.push({ kind: 'long-walk', severity: 'standard', value: walk });
		const wait = longestTransferWait(it, opts);
		if (wait >= STRONG_WAIT_SEC) warnings.push({ kind: 'long-wait', severity: 'strong', value: wait });
		else if (wait >= MEDIUM_WAIT_SEC) warnings.push({ kind: 'long-wait', severity: 'medium', value: wait });
		else if (wait >= LONG_WAIT_SEC) warnings.push({ kind: 'long-wait', severity: 'standard', value: wait });
		const dur = durations[i];
		const slowGap = dur - minDur;
		// Gate the whole chain on the minimum absolute difference so the
		// ratio thresholds don't fire on short trips with small gaps.
		if (slowGap >= VERY_SLOW_MIN_DIFF_SEC) {
			if (dur >= STRONG_SLOW_FACTOR * minDur) warnings.push({ kind: 'very-slow', severity: 'strong', value: slowGap });
			else if (dur >= MEDIUM_SLOW_FACTOR * minDur) warnings.push({ kind: 'very-slow', severity: 'medium', value: slowGap });
			else if (dur >= VERY_SLOW_FACTOR * minDur) warnings.push({ kind: 'very-slow', severity: 'standard', value: slowGap });
		}

		// Tight-transfer ladder: one icon for the worst tight transfer,
		// plus the visually distinct "if you're lucky" icon when any
		// transfer is beyond rescue at 1.5x walking speed. Both carry the
		// worst transfer's spare seconds for the tooltip.
		const transfers = assessTransfers(it, opts);
		const lucky = transfers.filter((t) => t.tier === 'lucky');
		if (lucky.length) {
			const worst = lucky.reduce((a, b) => (b.spare < a.spare ? b : a));
			warnings.push({ kind: 'lucky-transfer', severity: 'strong', value: worst.spare });
		}
		const tight = transfers.filter((t) => t.tier !== 'lucky');
		if (tight.length) {
			const worst = tight.reduce((a, b) =>
				TIER_RANK[b.tier] > TIER_RANK[a.tier] || (b.tier === a.tier && b.spare < a.spare) ? b : a);
			warnings.push({
				kind: 'tight-transfer',
				severity: TIGHT_SEVERITY[worst.tier as Exclude<TransferTier, 'lucky'>],
				value: worst.spare
			});
		}

		return { badge, warnings };
	});
}
