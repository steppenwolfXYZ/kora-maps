import { browser } from '$app/environment';
import {
	DEFAULT_OPTIONS, isSafetyMode, isWalkSpeedTier, pedestrianSpeedMs, tierKmh,
	type RoutingOptionValues, type SafetyMode, type WalkSpeedTier
} from './optionParams';

// Routing search options (routing-options.md): walking speed tiers,
// connection-safety modes and the minimize-walking toggle. localStorage-
// backed under a single key. The value model, the tier tables and the
// MOTIS parameter derivation live in optionParams.ts (pure — shared with
// the server-side planning engine); this module only holds the reactive
// store. The "reckless" safety mode and the step-free toggle are deferred
// (separately shippable per the concept) and deliberately absent here.

// Re-exported so existing importers (panel, URL round-trip) keep one
// import site for the option model.
export {
	BASE_WALK_KMH, DEFAULT_OPTIONS, SAFETY_MODES, WALK_SPEED_TIERS,
	type RoutingOptionValues, type SafetyMode, type WalkSpeedTier
} from './optionParams';

const STORAGE_KEY = 'kora_routing_prefs';
const DEFAULTS = DEFAULT_OPTIONS;

function readStorage(): RoutingOptionValues {
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (!raw) return { ...DEFAULTS };
		const p = JSON.parse(raw) as Partial<RoutingOptionValues>;
		return {
			walkSpeed: isWalkSpeedTier(p.walkSpeed) ? p.walkSpeed : DEFAULTS.walkSpeed,
			safety: isSafetyMode(p.safety) ? p.safety : DEFAULTS.safety,
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

	/** Walking pace for the direct walking tab's Valhalla call (m/s) —
	 * null at the normal tier so the default query stays byte-identical.
	 * The transit query's option params are derived server-side from the
	 * value snapshot (optionParams.ts). */
	get pedestrianSpeedMs(): number | null {
		return pedestrianSpeedMs(values);
	},

	/** Plain copy of the current values — ridden along on every routing
	 * URL write (url.ts serialises only the non-default fields) and sent
	 * with every /api/plan request. */
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
