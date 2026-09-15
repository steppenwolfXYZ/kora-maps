// Browser sensors for bicycle navigation (bicycle-navigation.md § Battery
// and sensors / § Keeping the screen on): the continuous high-accuracy
// position watch, the device compass, and the screen wake lock. Thin
// wrappers with a single teardown each, so the tracker can start and
// stop them as a unit. Nothing here runs before navigation starts —
// location is only ever requested on the rider's explicit action.

import type { LonLat } from './geometry';

export interface PositionFix {
	coord: LonLat;
	/** Reported horizontal accuracy radius in metres. */
	accuracyM: number;
	/** Ground speed in m/s, null when the platform gives none. */
	speedMs: number | null;
	/** GPS course in degrees (0 = north), null when unavailable — the
	 * platform reports none while standing still. */
	courseDeg: number | null;
	at: number;
}

const FIRST_FIX_TIMEOUT_MS = 8000;
const WATCH_TIMEOUT_MS = 20000;

function toFix(pos: GeolocationPosition): PositionFix {
	const c = pos.coords;
	return {
		coord: [c.longitude, c.latitude],
		accuracyM: Number.isFinite(c.accuracy) ? c.accuracy : 1000,
		speedMs: typeof c.speed === 'number' && Number.isFinite(c.speed) ? c.speed : null,
		courseDeg: typeof c.heading === 'number' && Number.isFinite(c.heading) ? c.heading : null,
		at: Date.now()
	};
}

/** One fresh high-accuracy fix, rejecting with the raw
 * GeolocationPositionError (geolocationErrorMessage turns it into text)
 * after the same timeout the routing endpoints use. */
export function getFirstFix(): Promise<PositionFix> {
	if (typeof navigator === 'undefined' || !navigator.geolocation) {
		return Promise.reject(new Error('Geolocation not available in this browser'));
	}
	return new Promise((resolve, reject) => {
		navigator.geolocation.getCurrentPosition(
			(pos) => resolve(toFix(pos)),
			reject,
			{ enableHighAccuracy: true, timeout: FIRST_FIX_TIMEOUT_MS, maximumAge: 5000 }
		);
	});
}

/** Continuous position updates; returns the stop function. Errors
 * after the first fix are reported but do not end the watch — a
 * temporary signal loss recovers on its own. */
export function watchPosition(
	onFix: (fix: PositionFix) => void,
	onError: (err: GeolocationPositionError) => void
): () => void {
	if (typeof navigator === 'undefined' || !navigator.geolocation) return () => {};
	const id = navigator.geolocation.watchPosition(
		(pos) => onFix(toFix(pos)),
		onError,
		{ enableHighAccuracy: true, timeout: WATCH_TIMEOUT_MS, maximumAge: 0 }
	);
	return () => navigator.geolocation.clearWatch(id);
}

// ── Compass ─────────────────────────────────────────────────────────────

/** iOS requires an explicit permission for device orientation, and the
 * request must run inside a user gesture — call this synchronously from
 * the start button's handler. Resolves to what happened: `implicit`
 * (no permission API — Android, desktop: events just fire), `granted`
 * / `denied` from the prompt, `no-api`, or `error:<name>` when the
 * call itself threw (typically: not inside a user gesture). Only iOS
 * withholds events without a grant, so the caller listens regardless
 * unless the API is missing altogether. */
export function requestCompassPermission(): Promise<string> {
	if (typeof window === 'undefined') return Promise.resolve('no-api');
	const DOE = (window as any).DeviceOrientationEvent;
	if (!DOE) return Promise.resolve('no-api');
	if (typeof DOE.requestPermission !== 'function') return Promise.resolve('implicit');
	try {
		return DOE.requestPermission()
			.then((state: string) => (state === 'granted' ? 'granted' : 'denied'))
			.catch((e: unknown) => `error:${(e as Error)?.name ?? 'unknown'}`);
	} catch (e) {
		return Promise.resolve(`error:${(e as Error)?.name ?? 'unknown'}`);
	}
}

export interface CompassSample {
	type: string;
	alpha: number | null;
	absolute: boolean;
	webkitHeading: number | null;
}

/** Compass heading updates in degrees (0 = north, clockwise); returns
 * the stop function. Prefers the absolute event where the platform
 * offers it; iOS exposes its heading as webkitCompassHeading on the
 * plain event. Fires nothing on devices without a magnetometer. */
export function watchCompass(
	onHeading: (deg: number) => void,
	/** TEMPORARY diagnostic (bicycle-navigation.md): every raw event,
	 * usable or not, so a phone test can show what the platform
	 * delivers. */
	onSample?: (s: CompassSample) => void
): () => void {
	if (typeof window === 'undefined') return () => {};
	const handler = (ev: DeviceOrientationEvent) => {
		const wk = (ev as any).webkitCompassHeading;
		onSample?.({
			type: ev.type,
			alpha: typeof ev.alpha === 'number' ? ev.alpha : null,
			absolute: ev.absolute === true,
			webkitHeading: typeof wk === 'number' ? wk : null
		});
		if (typeof wk === 'number' && Number.isFinite(wk)) {
			onHeading(wk);
			return;
		}
		if (ev.absolute !== true && (ev.type !== 'deviceorientationabsolute')) return;
		if (typeof ev.alpha === 'number' && Number.isFinite(ev.alpha)) {
			onHeading((360 - ev.alpha) % 360);
		}
	};
	const type = 'ondeviceorientationabsolute' in window
		? 'deviceorientationabsolute'
		: 'deviceorientation';
	window.addEventListener(type, handler as EventListener, true);
	return () => window.removeEventListener(type, handler as EventListener, true);
}

// ── Screen wake lock ─────────────────────────────────────────────────────

/** Keeps the screen on while held. Foreground only by platform design:
 * the lock is released when the tab is hidden and re-acquired when it
 * returns. `supported` is false on browsers without the API — the
 * caller tells the rider once that the screen may lock. */
export class ScreenWakeLock {
	readonly supported: boolean;
	private sentinel: WakeLockSentinel | null = null;
	private held = false;
	private onVisibility = () => {
		if (this.held && document.visibilityState === 'visible') void this.request();
	};

	constructor() {
		this.supported = typeof navigator !== 'undefined' && 'wakeLock' in navigator;
	}

	async acquire(): Promise<void> {
		this.held = true;
		if (!this.supported) return;
		document.addEventListener('visibilitychange', this.onVisibility);
		await this.request();
	}

	private async request(): Promise<void> {
		if (this.sentinel && !this.sentinel.released) return;
		try {
			this.sentinel = await navigator.wakeLock.request('screen');
			this.sentinel.addEventListener('release', () => { this.sentinel = null; });
		} catch {
			// Low battery, hidden tab, or a policy refusal — the lock simply
			// isn't held; navigation continues regardless.
			this.sentinel = null;
		}
	}

	release(): void {
		this.held = false;
		if (!this.supported) return;
		document.removeEventListener('visibilitychange', this.onVisibility);
		void this.sentinel?.release().catch(() => {});
		this.sentinel = null;
	}
}
