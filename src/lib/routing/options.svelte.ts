import { browser } from '$app/environment';

// Routing search options (routing-options.md): walking speed tiers,
// connection-safety modes and the minimize-walking ranking toggle.
// localStorage-backed under a single key; defaults produce byte-identical
// queries to the pre-options behavior (no extra params sent). The
// "reckless" safety mode and the step-free toggle are deferred
// (separately shippable per the concept) and deliberately absent here.

const STORAGE_KEY = 'kora_routing_prefs';

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
const DEFAULTS = DEFAULT_OPTIONS;

function readStorage(): RoutingOptionValues {
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (!raw) return { ...DEFAULTS };
		const p = JSON.parse(raw) as Partial<RoutingOptionValues>;
		return {
			walkSpeed: WALK_SPEED_TIERS.some((t) => t.id === p.walkSpeed)
				? p.walkSpeed as WalkSpeedTier : DEFAULTS.walkSpeed,
			safety: SAFETY_MODES.some((m) => m.id === p.safety)
				? p.safety as SafetyMode : DEFAULTS.safety,
			minimizeWalking: p.minimizeWalking === true
		};
	} catch {
		return { ...DEFAULTS };
	}
}

let values = $state<RoutingOptionValues>(browser ? readStorage() : { ...DEFAULTS });

function writeStorage() {
	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify(values));
	} catch {
		// Storage unavailable — the choice still holds this session.
	}
}

function tierKmh(id: WalkSpeedTier): number {
	return WALK_SPEED_TIERS.find((t) => t.id === id)!.kmh;
}

export const routingOptions = {
	get walkSpeed() { return values.walkSpeed; },
	get safety() { return values.safety; },
	get minimizeWalking() { return values.minimizeWalking; },

	get walkSpeedKmh() { return tierKmh(values.walkSpeed); },

	/** Any option off its default → the collapsed more-options button
	 * shows the indicator dot. */
	get isDefault() {
		return values.walkSpeed === DEFAULTS.walkSpeed
			&& values.safety === DEFAULTS.safety
			&& !values.minimizeWalking;
	},

	/** `pedestrianSpeed` plan param (m/s) — null at the normal tier so
	 * the default query stays byte-identical to today's. */
	get pedestrianSpeedMs(): number | null {
		if (values.walkSpeed === 'normal') return null;
		return Math.round((tierKmh(values.walkSpeed) / 3.6) * 1000) / 1000;
	},

	/** `transferTimeFactor` plan param: walking-speed scaling of the
	 * imported transfer matrix, composed with daring's halving. Null when
	 * it would be 1.0. */
	get transferTimeFactor(): number | null {
		const f = (BASE_WALK_KMH / tierKmh(values.walkSpeed))
			* (values.safety === 'daring' ? 0.5 : 1);
		const rounded = Math.round(f * 10000) / 10000;
		return rounded === 1 ? null : rounded;
	},

	/** `additionalTransferTime` plan param (MINUTES) — cautious only. */
	get additionalTransferMin(): number {
		return values.safety === 'cautious' ? 5 : 0;
	},

	/** `minTransferTime` plan param (MINUTES): a one-minute floor on
	 * every transfer whenever the transfer-time factor drops below 1
	 * (daring, and the brisk / running tiers on their own). The transfer
	 * table is quantised to whole minutes, so a factor below 1 truncates
	 * a one-minute transfer to ZERO — the engine then offers connections
	 * where alighting and boarding happen at the same instant. That is
	 * the reckless tier by definition; daring may demand a sprint but
	 * always leaves a minute (routing-options.md § Connection safety). */
	get minTransferMin(): number {
		const f = this.transferTimeFactor;
		return f != null && f < 1 ? 1 : 0;
	},

	/** Minimize-walking server params (routing-options.md § Minimize
	 * walking): the fork's steeper walk-point table plus widened
	 * ε-alternates so more low-walk variants come back. */
	get koraWalkPoints(): 'minwalk' | null {
		return values.minimizeWalking ? 'minwalk' : null;
	},
	get alternativesEpsilon(): number {
		return values.minimizeWalking ? 900 : 540;
	},
	get alternativesMax(): number {
		return values.minimizeWalking ? 5 : 3;
	},

	/** Plain copy of the current values — ridden along on every routing
	 * URL write (url.ts serialises only the non-default fields). */
	snapshot(): RoutingOptionValues {
		return { ...values };
	},

	/** Session-only override from a URL restore: the link's options apply
	 * to this tab's queries but are NOT persisted — the recipient's saved
	 * prefs survive. Always the full set (absent URL params = the sender
	 * was at the defaults, so defaults apply). */
	applySession(v: RoutingOptionValues) {
		values = { ...v };
	},

	setWalkSpeed(t: WalkSpeedTier) {
		if (values.walkSpeed === t) return;
		values = { ...values, walkSpeed: t };
		writeStorage();
	},

	setSafety(m: SafetyMode) {
		if (values.safety === m) return;
		values = { ...values, safety: m };
		writeStorage();
	},

	setMinimizeWalking(v: boolean) {
		if (values.minimizeWalking === v) return;
		values = { ...values, minimizeWalking: v };
		writeStorage();
	}
};

export type RoutingOptions = typeof routingOptions;
