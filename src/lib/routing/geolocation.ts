// One shared geolocation cache — the first `resolveCurrent` triggers the
// browser permission prompt; subsequent calls reuse the fix while it's fresh.
// If the user denied the prompt, the option stays selectable (transit-routing.md
// § Endpoint inputs / § Constraints) and the next call re-triggers the prompt.

const MAX_AGE_MS = 60_000;
const TIMEOUT_MS = 8_000;

let cache: { coord: [number, number]; at: number } | null = null;

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
		// enableHighAccuracy: false — GPS-precise fixes fail more often on
		// desktops (CoreLocation returns kCLErrorLocationUnknown when a
		// fresh fix isn't available), and city-block accuracy is fine for
		// routing to the nearest street/stop.
		navigator.geolocation.getCurrentPosition(
			(pos) => {
				const coord: [number, number] = [pos.coords.longitude, pos.coords.latitude];
				cache = { coord, at: Date.now() };
				resolve(coord);
			},
			(err) => reject(err),
			{ enableHighAccuracy: false, timeout: TIMEOUT_MS, maximumAge: MAX_AGE_MS }
		);
	});
}
