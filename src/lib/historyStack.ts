// Shared history-entry plumbing for the app's back-closable layers
// (routing panel / route / map mode in routing/state.svelte.ts, the map
// menu in map/orchestration.svelte.ts). Every layer that back should
// close sits on its own history entry; this module owns the two rules
// all of them must follow.
//
// 1. At most one pushed entry per user gesture. Chrome's back-button
//    intervention marks an entry skippable when the page pushes on top of
//    it without a fresh user activation — a second push from the same tap
//    (or one from an effect) makes back silently jump over a layer. So
//    every push goes through pushEntry, which refuses once the current
//    gesture already pushed; the caller then replaces instead. The flag
//    resets on the next pointerdown / keydown (capture phase, before any
//    handler runs).
// 2. A UI close (×, Clear route, menu button) consumes its own entry via
//    popEntries rather than leaving it behind, and the map skips the
//    position-hash camera jump for that step — a UI close closes in place,
//    only a real back restores the previous viewport.
//
// `routingDepth` in page.state counts the entries pushed through here up
// to and including the current one, so a close can pop its layer together
// with everything stacked above it.

import { browser } from '$app/environment';
import { pushState } from '$app/navigation';
import { page } from '$app/state';

let gesturePushed = false;
if (browser) {
	const reset = () => { gesturePushed = false; };
	window.addEventListener('pointerdown', reset, { capture: true });
	window.addEventListener('keydown', reset, { capture: true });
}

export function historyDepth(): number {
	return page.state.routingDepth ?? 0;
}

/** Push `state` (+ the next routingDepth) as a new entry, unless this
 * gesture already pushed one. Returns whether it pushed. */
export function pushEntry(url: URL | '', state: App.PageState): boolean {
	if (gesturePushed) return false;
	gesturePushed = true;
	pushState(url, { ...state, routingDepth: historyDepth() + 1 });
	return true;
}

/** Push the current state plus a layer marker holding the new entry's
 * depth. */
export function pushMarker(key: 'routingPanel' | 'routingQuery' | 'routeMapMode' | 'mapMenu'): boolean {
	return pushEntry('', { ...page.state, [key]: historyDepth() + 1 });
}

// A popEntries / traverse is in flight: further pops are no-ops until its
// popstate arrives (resize can fire exitMapMode repeatedly).
let popping = false;
let suppressHashUntil = 0;

/** Step `delta` entries through history (negative = back) for a UI-driven
 * close or a back interception, with the camera jump suppressed. Returns
 * false while another such step is still in flight. */
export function traverse(delta: number): boolean {
	if (popping) return false;
	popping = true;
	suppressHashUntil = Date.now() + 600;
	window.addEventListener('popstate', () => { popping = false; }, { once: true });
	// Safety net: a popstate that never comes must not leave every later
	// close a silent no-op.
	setTimeout(() => { popping = false; }, 1000);
	history.go(delta);
	return true;
}

export function traverseInFlight(): boolean {
	return popping;
}

/** True for a moment after traverse — the map's hashchange listener
 * skips the position-hash camera jump for that step. */
export function hashJumpSuppressed(): boolean {
	return Date.now() < suppressHashUntil;
}
