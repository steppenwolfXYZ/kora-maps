import { browser } from '$app/environment';
import {
	DEFAULT_OPTIONS, isBikePace, isBikeRoads, isBikeType, isMotorBike, isSafetyMode,
	isWalkSpeedTier, pedestrianSpeedMs, tierKmh,
	type BikeOptionValues, type BikePace, type BikeRoads, type BikeType,
	type RoutingOptionValues, type SafetyMode, type WalkSpeedTier
} from './optionParams';

// Routing options: the transit search's walking speed tiers, connection-
// safety modes and minimize-walking toggle (routing-options.md), plus the
// cycling tab's bike type, pace, fast ↔ nice ruler and avoid-stairs
// toggle (bicycle-route-options.md). localStorage-backed under a single
// key. The value model, the tier tables and the engine parameter
// derivations live in optionParams.ts (pure — shared with the server-side
// planning engine and the Valhalla request builder); this module only
// holds the reactive store. The "reckless" safety mode and the step-free
// toggle are deferred (separately shippable per the concept) and
// deliberately absent here.

// Re-exported so existing importers (panel, URL round-trip) keep one
// import site for the option model.
export {
	BASE_WALK_KMH, BIKE_PACES, BIKE_ROADS, BIKE_TYPES, DEFAULT_BIKE_OPTIONS, DEFAULT_OPTIONS,
	SAFETY_MODES, WALK_SPEED_TIERS,
	type BikeOptionValues, type BikePace, type BikeRoads, type BikeType,
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
			minimizeWalking: p.minimizeWalking === true,
			bikeType: isBikeType(p.bikeType) ? p.bikeType : DEFAULTS.bikeType,
			bikePace: isBikePace(p.bikePace) ? p.bikePace : DEFAULTS.bikePace,
			bikeRoads: isBikeRoads(p.bikeRoads) ? p.bikeRoads : DEFAULTS.bikeRoads,
			avoidStairs: p.avoidStairs === true
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
	get bikeType() { return values.bikeType; },
	get bikePace() { return values.bikePace; },
	get bikeRoads() { return values.bikeRoads; },
	get avoidStairs() { return values.avoidStairs; },

	get walkSpeedKmh() { return tierKmh(values.walkSpeed); },

	/** Any TRANSIT option off its default → the transit tab's collapsed
	 * more-options button shows the indicator dot. */
	get isDefault() {
		return values.walkSpeed === DEFAULTS.walkSpeed
			&& values.safety === DEFAULTS.safety
			&& !values.minimizeWalking;
	},

	/** The cycling tab's expander holds the bike type and the two rulers
	 * (avoid-stairs is always visible), so its indicator dot follows
	 * those. The pace ruler does not apply to e-bikes, so a remembered
	 * non-default pace shows no dot while one is selected. */
	get bikeOptionsDefault() {
		return values.bikeType === DEFAULTS.bikeType
			&& values.bikeRoads === DEFAULTS.bikeRoads
			&& (isMotorBike(values.bikeType) || values.bikePace === DEFAULTS.bikePace);
	},

	/** Walking pace for the direct walking tab's Valhalla call (m/s) —
	 * null at the normal tier so the default query stays byte-identical.
	 * The transit query's option params are derived server-side from the
	 * value snapshot (optionParams.ts). */
	get pedestrianSpeedMs(): number | null {
		return pedestrianSpeedMs(values);
	},

	/** Plain copy of the current values — ridden along on every routing
	 * URL write (url.ts serialises only the non-default fields of the
	 * active tab) and sent with every /api/plan request. */
	snapshot(): RoutingOptionValues {
		return { ...values };
	},

	/** The cycling tab's option set — sent with every bicycle query and
	 * stored on the resulting routes so navigation recalculates with the
	 * same rider model (bicycle-route-options.md § 6). */
	bikeSnapshot(): BikeOptionValues {
		return {
			bikeType: values.bikeType,
			bikePace: values.bikePace,
			bikeRoads: values.bikeRoads,
			avoidStairs: values.avoidStairs
		};
	},

	/** Session-only override from a URL restore: the link's options apply
	 * to this tab's queries but are NOT persisted — the recipient's saved
	 * prefs survive. The link carries only its own tab's group (url.ts
	 * paramsToOptions), always complete for that group: absent params =
	 * the sender was at the defaults, so defaults apply. The other tab's
	 * values stay as they were. */
	applySession(v: Partial<RoutingOptionValues>) {
		values = { ...values, ...v };
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
	},

	/** Selecting an e-bike type switches avoid-stairs ON (stairs are a
	 * no-go with a motor); the user may switch it off again afterwards.
	 * Switching back to a pedal type leaves the toggle alone. The pace
	 * stays untouched throughout, so it comes back with the pedal types. */
	setBikeType(t: BikeType) {
		if (values.bikeType === t) return;
		const toMotor = isMotorBike(t) && !isMotorBike(values.bikeType);
		values = { ...values, bikeType: t, avoidStairs: toMotor ? true : values.avoidStairs };
		writeStorage();
	},

	setBikePace(p: BikePace) {
		if (values.bikePace === p) return;
		values = { ...values, bikePace: p };
		writeStorage();
	},

	setBikeRoads(r: BikeRoads) {
		if (values.bikeRoads === r) return;
		values = { ...values, bikeRoads: r };
		writeStorage();
	},

	setAvoidStairs(v: boolean) {
		if (values.avoidStairs === v) return;
		values = { ...values, avoidStairs: v };
		writeStorage();
	}
};

export type RoutingOptions = typeof routingOptions;
