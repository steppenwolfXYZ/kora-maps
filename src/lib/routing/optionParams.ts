// Routing search options (routing-options.md, bicycle-route-options.md) —
// the value model, the tier tables and the derivation of the engine
// parameters from a value set. Pure (no runes, no storage): shared by
// the browser store (options.svelte.ts), the URL round-trip (url.ts),
// the Valhalla request builder (valhalla.ts) and the server-side
// planning engine, which re-derives the same transit parameters from the
// option values the client sends with each /api/plan request.

// Base speed baked into the Valhalla matrix + live calls (kWalkSpeedKmh
// in the MOTIS fork). The normal tier IS this speed — it sends no params.
export const BASE_WALK_KMH = 5.1;

export type WalkSpeedTier = 'slow' | 'leisurely' | 'normal' | 'brisk' | 'running';
export type SafetyMode = 'cautious' | 'balanced' | 'daring';

export const WALK_SPEED_TIERS: {
	id: WalkSpeedTier; label: string; kmh: number; desc: string; icon: string;
}[] = [
	{ id: 'slow',      label: 'Slow',      kmh: 2,             desc: '2 km/h',   icon: 'assist_walker' },
	{ id: 'leisurely', label: 'Leisurely', kmh: 4,             desc: '4 km/h',   icon: 'nature_people' },
	{ id: 'normal',    label: 'Normal',    kmh: BASE_WALK_KMH, desc: '5 km/h',   icon: 'directions_walk' },
	{ id: 'brisk',     label: 'Brisk',     kmh: 7.5,           desc: '7.5 km/h', icon: 'directions_run' },
	{ id: 'running',   label: 'Running',   kmh: 11,            desc: '11 km/h',  icon: 'sprint' }
];

export const SAFETY_MODES: {
	id: SafetyMode; label: string; desc: string; icon: string;
}[] = [
	{ id: 'cautious', label: 'Cautious', desc: '5 extra minutes to spare',  icon: 'shield' },
	{ id: 'balanced', label: 'Balanced', desc: 'Normal transfer times',     icon: 'balance' },
	{ id: 'daring',   label: 'Daring',   desc: 'You may have to run. Small delays may be an issue.', icon: 'local_fire_department' }
];

// ── Bicycle route options (bicycle-route-options.md) ─────────────────────

export type BikeType = 'bicycle' | 'racing' | 'ebike' | 'sbike';
export type BikePace = 'leisurely' | 'normal' | 'fast' | 'pro';
/** Fast ↔ nice ruler stops, in ruler order (calm on the left). */
export type BikeRoads = 'quiet' | 'relaxed' | 'balanced' | 'fast' | 'road';

export const BIKE_TYPES: {
	id: BikeType; label: string; desc: string;
	/** Material Symbols glyph — or, for the racing bike, the inline
	 * `RacingBikeIcon` component (Material has no bent-over rider). */
	icon: string;
	svg?: 'racing';
	/** Small text badge on the icon (the assist cap of the e-bike types). */
	badge?: string;
	/** Motor-assisted: no pace ruler, avoid-stairs switches on when selected. */
	motor: boolean;
	/** Engine `bicycle_type` (the fork adds `ebike` / `sbike`). */
	engineType: string;
}[] = [
	{ id: 'bicycle', label: 'Bicycle',        desc: 'Normal bicycle',                    icon: 'directions_bike', motor: false, engineType: 'hybrid' },
	{ id: 'racing',  label: 'Racing bicycle', desc: 'Avoids gravel and rough surfaces',  icon: '', svg: 'racing', motor: false, engineType: 'road' },
	{ id: 'ebike',   label: 'E-Bike',         desc: 'Motor assist up to 25 km/h',        icon: 'electric_bike',   motor: true,  engineType: 'ebike', badge: '25' },
	{ id: 'sbike',   label: 'Fast E-Bike',    desc: 'Motor assist up to 45 km/h',        icon: 'electric_bike',   motor: true,  engineType: 'sbike', badge: '45' }
];

/** Flat-ground speed per pace stop; the engine derives the rider's
 * sustained power from it, so the whole grade→speed curve follows. */
export const BIKE_PACES: {
	id: BikePace; label: string; kmh: number; desc: string; icon: string; svg?: 'racing';
}[] = [
	{ id: 'leisurely', label: 'Leisurely',    kmh: 15, desc: '15 km/h on the flat', icon: 'nature_people' },
	{ id: 'normal',    label: 'Normal',       kmh: 20, desc: '20 km/h on the flat', icon: 'directions_bike' },
	{ id: 'fast',      label: 'Fast',         kmh: 25, desc: '25 km/h on the flat', icon: 'speed' },
	{ id: 'pro',       label: 'Professional', kmh: 30, desc: '30 km/h on the flat', icon: '', svg: 'racing' }
];

/** The fast ↔ nice ruler (bicycle-route-options.md § 4). Each stop goes
 * to the engine as its `route_character` value; the per-stop numbers
 * (traffic-penalty scale, infrastructure and cycle-route bonuses, quiet
 * boost, surface tables and relief) live in the fork's tuning block
 * (valhalla/fork/README.md). Ruler order: calm on the left. */
export const BIKE_ROADS: {
	id: BikeRoads; label: string; desc: string; icon: string;
}[] = [
	{ id: 'quiet',    label: 'Quiet',    desc: 'Quiet lanes and cycle routes, gravel welcome', icon: 'forest' },
	{ id: 'relaxed',  label: 'Relaxed',  desc: 'Prefers calm streets and cycle routes',       icon: 'self_improvement' },
	{ id: 'balanced', label: 'Balanced', desc: 'Direct, but away from busy roads',            icon: 'balance' },
	{ id: 'fast',     label: 'Fast',     desc: 'More direct, main roads tolerated',           icon: 'speed' },
	{ id: 'road',     label: 'Road',     desc: 'Shortest ride, traffic ignored',              icon: 'road' }
];

export interface BikeOptionValues {
	bikeType: BikeType;
	bikePace: BikePace;
	bikeRoads: BikeRoads;
	avoidStairs: boolean;
}

export interface RoutingOptionValues extends BikeOptionValues {
	walkSpeed: WalkSpeedTier;
	safety: SafetyMode;
	minimizeWalking: boolean;
}

export const DEFAULT_BIKE_OPTIONS: BikeOptionValues = {
	bikeType: 'bicycle',
	bikePace: 'normal',
	bikeRoads: 'balanced',
	avoidStairs: false
};

export const DEFAULT_OPTIONS: RoutingOptionValues = {
	walkSpeed: 'normal',
	safety: 'balanced',
	minimizeWalking: false,
	...DEFAULT_BIKE_OPTIONS
};

export function isWalkSpeedTier(v: unknown): v is WalkSpeedTier {
	return WALK_SPEED_TIERS.some((t) => t.id === v);
}

export function isSafetyMode(v: unknown): v is SafetyMode {
	return SAFETY_MODES.some((m) => m.id === v);
}

export function isBikeType(v: unknown): v is BikeType {
	return BIKE_TYPES.some((t) => t.id === v);
}

export function isBikePace(v: unknown): v is BikePace {
	return BIKE_PACES.some((p) => p.id === v);
}

export function isBikeRoads(v: unknown): v is BikeRoads {
	return BIKE_ROADS.some((r) => r.id === v);
}

export function isMotorBike(t: BikeType): boolean {
	return BIKE_TYPES.find((b) => b.id === t)!.motor;
}

export function tierKmh(id: WalkSpeedTier): number {
	return WALK_SPEED_TIERS.find((t) => t.id === id)!.kmh;
}

/** Bicycle costing options one bike option set maps to (the fork's
 * request surface — valhalla/fork/README.md). Always explicit, so a
 * request states the full rider model it was computed with. */
export function bikeCostingOptions(v: BikeOptionValues): Record<string, unknown> {
	const type = BIKE_TYPES.find((b) => b.id === v.bikeType)!;
	const out: Record<string, unknown> = {
		bicycle_type: type.engineType,
		route_character: v.bikeRoads
	};
	if (!type.motor) {
		// The rider's flat speed → their sustained power (e-bike types
		// ignore it: fixed Normal effort plus the motor).
		out.cycling_speed = BIKE_PACES.find((p) => p.id === v.bikePace)!.kmh;
	}
	if (v.bikeType === 'racing') {
		// The engine's road type penalises everything rougher than
		// compacted gravel; near its maximum weight so gravel is taken only
		// where nothing else reaches the destination (an outright refusal
		// would fail the query on a gravel driveway).
		out.avoid_bad_surfaces = 0.9;
	}
	if (v.avoidStairs) out.exclude_steps = true;
	return out;
}

/** The MOTIS plan parameters one option set maps to. Every field is at
 * its "send nothing" value for the defaults, so a default query stays
 * byte-identical to the pre-options behavior. */
export interface PlanOptionParams {
	/** `pedestrianSpeed` (m/s) — null at the normal tier. */
	pedestrianSpeedMs: number | null;
	/** `transferTimeFactor`: walking-speed scaling of the imported
	 * transfer matrix, composed with daring's halving. Null when 1.0. */
	transferTimeFactor: number | null;
	/** `additionalTransferTime` (MINUTES) — cautious only. */
	additionalTransferMin: number;
	/** `minTransferTime` (MINUTES): a one-minute floor on every transfer
	 * whenever the transfer-time factor drops below 1 (daring, and the
	 * brisk / running tiers on their own). The transfer table is quantised
	 * to whole minutes, so a factor below 1 truncates a one-minute
	 * transfer to ZERO — the engine then offers connections where
	 * alighting and boarding happen at the same instant. That is the
	 * reckless tier by definition; daring may demand a sprint but always
	 * leaves a minute (routing-options.md § Connection safety). */
	minTransferMin: number;
	/** Minimize-walking server params (routing-options.md § Minimize
	 * walking): the fork's steeper walk-point table plus widened
	 * ε-alternates so more low-walk variants come back. */
	koraWalkPoints: 'minwalk' | null;
	alternativesEpsilon: number;
	alternativesMax: number;
}

export function pedestrianSpeedMs(v: RoutingOptionValues): number | null {
	if (v.walkSpeed === 'normal') return null;
	return Math.round((tierKmh(v.walkSpeed) / 3.6) * 1000) / 1000;
}

export function transferTimeFactor(v: RoutingOptionValues): number | null {
	const f = (BASE_WALK_KMH / tierKmh(v.walkSpeed))
		* (v.safety === 'daring' ? 0.5 : 1);
	const rounded = Math.round(f * 10000) / 10000;
	return rounded === 1 ? null : rounded;
}

export function planOptionParams(v: RoutingOptionValues): PlanOptionParams {
	const factor = transferTimeFactor(v);
	return {
		pedestrianSpeedMs: pedestrianSpeedMs(v),
		transferTimeFactor: factor,
		additionalTransferMin: v.safety === 'cautious' ? 5 : 0,
		minTransferMin: factor != null && factor < 1 ? 1 : 0,
		koraWalkPoints: v.minimizeWalking ? 'minwalk' : null,
		alternativesEpsilon: v.minimizeWalking ? 900 : 540,
		alternativesMax: v.minimizeWalking ? 5 : 3
	};
}
