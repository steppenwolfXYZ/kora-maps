// Routing search options (routing-options.md) — the value model, the
// tier tables and the derivation of the MOTIS query parameters from a
// value set. Pure (no runes, no storage): shared by the browser store
// (options.svelte.ts), the URL round-trip (url.ts) and the server-side
// planning engine, which re-derives the same parameters from the option
// values the client sends with each /api/plan request.

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

export interface RoutingOptionValues {
	walkSpeed: WalkSpeedTier;
	safety: SafetyMode;
	minimizeWalking: boolean;
}

export const DEFAULT_OPTIONS: RoutingOptionValues = {
	walkSpeed: 'normal',
	safety: 'balanced',
	minimizeWalking: false
};

export function isWalkSpeedTier(v: unknown): v is WalkSpeedTier {
	return WALK_SPEED_TIERS.some((t) => t.id === v);
}

export function isSafetyMode(v: unknown): v is SafetyMode {
	return SAFETY_MODES.some((m) => m.id === v);
}

export function tierKmh(id: WalkSpeedTier): number {
	return WALK_SPEED_TIERS.find((t) => t.id === id)!.kmh;
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
