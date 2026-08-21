// One shared geolocation cache — the first `resolveCurrent` triggers the
// browser permission prompt; subsequent calls reuse the fix while it's fresh.
// Location is only ever requested on an explicit user action (locate button,
// running a query with a "Current location" endpoint) — never on startup.

const MAX_AGE_MS = 60_000;
const TIMEOUT_MS = 8_000;

let cache: { coord: [number, number]; at: number } | null = null;

// Reactive denied flag: once the user rejects the permission prompt, the
// "Current location" suggestion disappears from the routing dropdowns.
// Synced from the Permissions API where available (covers a pre-denied
// permission on load, and a re-grant via browser settings un-denies it);
// markGeolocationDenied() covers rejections on browsers without it.
let denied = $state(false);

export function geolocationDenied(): boolean {
	return denied;
}

export function markGeolocationDenied(): void {
	denied = true;
}

if (typeof navigator !== 'undefined' && navigator.permissions?.query) {
	navigator.permissions
		.query({ name: 'geolocation' })
		.then((status) => {
			const sync = () => { denied = status.state === 'denied'; };
			sync();
			status.onchange = sync;
		})
		.catch(() => {});
}

export function hasGeolocation(): boolean {
	return typeof navigator !== 'undefined' && !!navigator.geolocation;
}

/** Turn a raw GeolocationPositionError (which is NOT an Error instance and
 * stringifies to "[object GeolocationPositionError]") into a human-readable
 * message. `code` is the W3C constant: 1=PERMISSION_DENIED, 2=POSITION_
 * UNAVAILABLE (macOS reports this as kCLErrorLocationUnknown when
 * CoreLocation can't get a fix), 3=TIMEOUT. */
export function geolocationErrorMessage(err: unknown): string {
	if (typeof GeolocationPositionError !== 'undefined' && err instanceof GeolocationPositionError) {
		if (err.code === 1) return 'Location permission denied. Pick a start manually.';
		if (err.code === 2) return 'Location unavailable. Pick a start manually.';
		if (err.code === 3) return 'Location request timed out. Pick a start manually.';
		return err.message || 'Location unavailable.';
	}
	if (err instanceof Error) return err.message;
	return String(err);
}

export function resolveCurrent(): Promise<[number, number]> {
	if (!hasGeolocation()) {
		return Promise.reject(new Error('Geolocation not available in this browser'));
	}
	if (cache && Date.now() - cache.at < MAX_AGE_MS) {
		return Promise.resolve(cache.coord);
	}
	return new Promise((resolve, reject) => {
		// enableHighAccuracy: true — matches the map's GeolocateControl.
		// Counter-intuitively the low-accuracy request is the flaky one on
		// desktop: without an existing recent fix it can sit on CoreLocation
		// waiting until the timeout, while a high-accuracy request triggers
		// an actual lookup and resolves.
		navigator.geolocation.getCurrentPosition(
			(pos) => {
				const coord: [number, number] = [pos.coords.longitude, pos.coords.latitude];
				cache = { coord, at: Date.now() };
				resolve(coord);
			},
			(err) => {
				if (err.code === 1) denied = true;
				reject(err);
			},
			{ enableHighAccuracy: true, timeout: TIMEOUT_MS, maximumAge: MAX_AGE_MS }
		);
	});
}
