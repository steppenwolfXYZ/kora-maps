// Cross-feature orchestration around the map. This is the ONE place
// where the features riding on the map are coordinated: line-detail ↔
// routing mutual exclusion, browser history back/forward for both
// views, the route-overlay ↔ selection reconciliation, popup actions,
// and deep-link / cold-load restores. The feature modules themselves
// (linedetail/, routing/routeOverlay.ts, map/popups/) never import each
// other's state — everything meets here.

import type maplibregl from 'maplibre-gl';
import { page } from '$app/state';
import { routingState } from '../routing/state.svelte';
import { readRoutingQuery, urlHasRoutingQuery } from '../routing/url';
import { loadStationIndex, type StationEntry } from '../routing/stationIndex';
import { loadRouteColorIndex } from '../routing/legColor';
import { itineraryFingerprint } from '../routing/fingerprint';
import {
	enterRouteOverlay, exitRouteOverlay, disposeRouteOverlay,
	focusRouteLeg, frameItinerary
} from '../routing/routeOverlay';
import type { Endpoint, Itinerary, Leg } from '../routing/types';
import { installClickPopups, type RouteEndpointRequest } from './popups/handlers';
import { lineDetailState } from '../linedetail/state.svelte';
import {
	readLineDeepLinkFromUrl, clearLineDeepLinkFromUrl, resolveLineDeepLink,
	type LineDetailSelection
} from '../linedetail/lineIndex';
import { mapUi } from './uiState.svelte';

let routeColorIndex: Map<string, string> | null = null;
let routeStationIndex: Map<string, StationEntry> | null = null;
// Guards double-firing history.back() while a route-view close is in
// flight — mirrors lineDetailState.closingViaBack so the hashchange
// listener skips the camera jump for that step.
let closingRouteViaBack = false;

/** Fed to createKoraMap so its hashchange listener knows when a
 * feature's history.back() close is consuming the hash step. */
export const suppressHashJump = () =>
	lineDetailState.closingViaBack || closingRouteViaBack;

// ── Line detail (line-detail-view.md) ───────────────────────────────────

export function enterLineDetail(
	sel: LineDetailSelection,
	source: 'user' | 'deeplink' | 'history' = 'user'
) {
	const map = mapUi.mapRef;
	if (!map || !sel.keys?.length || !sel.bbox || sel.bbox.length !== 4) return;

	// Line-detail and routing are mutually exclusive shells (both
	// occupy the top-left slot and demand the map's foreground).
	// Two coordinated moves so the paint/filter capture in enter()
	// sees the true pre-route baseline, not the route overlay's
	// overrides:
	//   1. Tear the route overlay down synchronously first — the
	//      reactive $effect that watches selectedItinerary would run
	//      later (microtask), by which time line-detail has already
	//      captured its saved paints from the still-desaturated map
	//      and applied its own edits; the delayed revert would then
	//      overwrite them with the pre-route baseline.
	//   2. Then closePanel so the "routing open → close line-detail"
	//      effect doesn't fire mid-entry and tear us back down.
	if (routingState.open) {
		if (routingState.selectedItinerary) exitRouteOverlay(map);
		routingState.closePanel();
	}

	lineDetailState.enter(map, sel, source);
}

export function exitLineDetailView() {
	lineDetailState.exit(mapUi.mapRef);
}

// ── Route overlay actions (route-display.md) ────────────────────────────

/** Camera-focus a clicked leg row in the expanded result card. */
export const focusSelectedLeg = (leg: Leg) =>
	focusRouteLeg(() => mapUi.mapRef, leg);

/** Frame the whole selected itinerary — serves both the mobile map-mode
 * entry and the reset to the overview after a leg focus zoomed in. */
export const frameSelectedItinerary = (it: Itinerary) =>
	frameItinerary(() => mapUi.mapRef, it, routeColorIndex, routeStationIndex);

/** Popup Route from/to button → routing endpoint. Station endpoints
 * need the feed's parent stop id (`pid`, SLOID scheme) for the MOTIS
 * place id — the legacy `ch_Parent<uic>` fallback in client.ts
 * formatPlace no longer resolves (404) since the SLOID migration.
 * Resolve it from the station index; without a hit, route from the
 * coord. */
function handleRouteEndpoint(side: 'from' | 'to', req: RouteEndpointRequest) {
	const hit = req.uic ? routeStationIndex?.get(String(req.uic)) : undefined;
	const ep: Endpoint = hit
		? { type: 'station', uic: hit.u, name: String(req.name || hit.n), coord: req.coord, mode: hit.m, pid: hit.p }
		: { type: 'point', coord: req.coord, displayName: String(req.name ?? '') || undefined };
	if (side === 'from') routingState.setFrom(ep);
	else routingState.setTo(ep);
	if (!routingState.open) routingState.openPanel({ prefillCurrent: false });
}

// ── Reactive glue ───────────────────────────────────────────────────────

/** Register the cross-feature $effects. Must be called during component
 * initialization (Map.svelte's script body). */
export function setupMapOrchestration() {
	$effect(() => {
		void loadRouteColorIndex().then((idx) => { routeColorIndex = idx; });
	});
	$effect(() => {
		void loadStationIndex().then((idx) => { routeStationIndex = idx; });
	});

	// Browser back/forward ↔ line detail. Back while open pops the pushed
	// record → page.state loses `lineDetail` → teardown. Forward while
	// closed restores the record → reopen from the selection stored in
	// its state. Guarded on a loaded style so a page.state restored on
	// reload can't fire before the map is ready (the deep-link path owns
	// that case).
	$effect(() => {
		const histSel = page.state.lineDetail;
		if (!histSel && lineDetailState.selection) {
			lineDetailState.teardown(mapUi.mapRef);
		} else if (histSel && !lineDetailState.selection && mapUi.mapRef?.isStyleLoaded()) {
			enterLineDetail(histSel, 'history');
		}
	});

	// Routing panel opens → close any open line-detail (transit-routing.md
	// § Routing panel: "Opening the routing panel closes any open
	// line-detail-view").
	$effect(() => {
		if (routingState.open && lineDetailState.selection) exitLineDetailView();
	});

	// Route selection reconciled with the map: install / update the
	// overlay when a selection exists, tear it down when it clears. The
	// effect fans out both to fresh in-session picks and to the delayed
	// cold-load restore (state.svelte.ts sets selectedItinerary only once
	// runQuery has matched the pending fingerprint).
	$effect(() => {
		const it = routingState.selectedItinerary;
		const map = mapUi.mapRef;
		if (!map || !map.isStyleLoaded()) return;
		if (it) enterRouteOverlay(map, it, routeColorIndex, routeStationIndex);
		else exitRouteOverlay(map);
	});

	// Browser back / forward ↔ route selection. The pushed history entry
	// (state.svelte.ts § selectItinerary) carries `routeSelection`; on
	// back it disappears from page.state, so we clear the selection to
	// match. Forward-restore: if the entry reappears while results still
	// hold the matching itinerary, re-select it silently (no fresh push).
	$effect(() => {
		const histFp = page.state.routeSelection;
		const curFp = routingState.selectedFingerprint;
		if (!histFp && curFp && !routingState.selectionInvalid) {
			closingRouteViaBack = true;
			routingState.clearSelectedItineraryFromHistory();
			// One frame later — long enough for the hashchange listener
			// to have skipped its jump.
			queueMicrotask(() => { closingRouteViaBack = false; });
		} else if (histFp && !curFp && routingState.results.length > 0) {
			const match = routingState.results.find(
				(r) => itineraryFingerprint(r) === histFp);
			if (match) routingState.selectItinerary(match);
		}
	});
}

// ── Per-map wiring ──────────────────────────────────────────────────────

/** Wire the feature entry points onto a freshly created map: click
 * popups, the ?line= deep link, the routing cold-load restore, and the
 * on-load feature applications. Called from Map.svelte's init effect
 * right after createKoraMap. */
export function wireMapFeatures(map: maplibregl.Map) {
	installClickPopups(map, {
		onEnterLineDetail: (sel) => enterLineDetail(sel),
		onRouteEndpoint: handleRouteEndpoint
	});

	// Deep-link resolution runs in parallel with style load; the fetch
	// runs alongside tile/glyph loading and is awaited inside the
	// map.on('load') handler below.
	const deepLinkKeys = readLineDeepLinkFromUrl();
	const deepLinkPromise: Promise<LineDetailSelection | null> = deepLinkKeys
		? resolveLineDeepLink(deepLinkKeys)
		: Promise.resolve(null);

	// Routing cold-load restore (transit-routing.md § Deep link). If
	// ?from / ?to is on the URL, hydrate routing state from it — this
	// opens the panel and reissues the query. `station` endpoints need
	// the search index to resolve UIC → name/coord, so we await it.
	const routingUrl = new URL(window.location.href);
	if (urlHasRoutingQuery(routingUrl)) {
		void loadStationIndex().then((idx) => {
			const parsed = readRoutingQuery(routingUrl, (uic) => {
				const e = idx?.get(uic);
				// pid is required — without it formatPlace falls back to the
				// legacy ch_Parent<uic> id, which 404s post-SLOID-migration.
				return e ? { name: e.n, coord: e.c, mode: e.m, pid: e.p } : null;
			});
			routingState.hydrate(parsed);
		});
	}

	map.on('load', () => {
		// Deep link (line-detail-view.md § Deep link): once the style
		// is loaded, apply the pre-fetched selection. Unknown / malformed
		// keys drop the param silently.
		if (deepLinkKeys) {
			deepLinkPromise.then((sel) => {
				if (sel) enterLineDetail(sel, 'deeplink');
				else clearLineDeepLinkFromUrl();
			});
		}

		// Route overlay (route-display.md § Lifecycle): the reactive
		// $effect on selectedItinerary may have fired before this
		// point with an unloaded style — apply it here now that the
		// style is ready. If runQuery hasn't returned yet, the effect
		// will run again once it does; both paths converge on the
		// same enterRouteOverlay call.
		if (routingState.selectedItinerary) {
			enterRouteOverlay(map, routingState.selectedItinerary,
				routeColorIndex, routeStationIndex);
		}
	});
}

/** Unmount path: the map is being destroyed — drop all feature state
 * without touching the map. */
export function resetMapFeatures() {
	lineDetailState.reset();
	disposeRouteOverlay();
	closingRouteViaBack = false;
}
