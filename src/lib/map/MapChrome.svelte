<script lang="ts">
	// Everything overlaid on the map: the top-controls row (menu, route
	// button, stop search — or the routing panel), the line-detail bar,
	// the mobile route header, the context menu, toast, brand overlay
	// and the dev zoom badge. Rendered inside Map.svelte's .map-wrap;
	// rules that depend on .map-wrap's state classes use :global
	// ancestors since that element lives in the parent component.
	import { PUBLIC_ENVIRONMENT } from '$env/static/public';
	import StopSearch from '../StopSearch.svelte';
	import MapMenu from '../MapMenu.svelte';
	import RoutingPanel from '../routing/RoutingPanel.svelte';
	import RouteMapHeader from '../routing/RouteMapHeader.svelte';
	import MapContextMenu from '../routing/MapContextMenu.svelte';
	import LineDetailBar from '../linedetail/LineDetailBar.svelte';
	import NavigationOverlay from '../navigation/NavigationOverlay.svelte';
	import { navigation } from '../navigation/state.svelte';

	// Start-navigation entry point (bicycle-navigation.md § Entering and
	// leaving): shown while a cycling route is selected and no ride is
	// running. The narrow-screen full panel covers this spot, so the
	// expanded direct sheet hides it via CSS.
	let navStartRoute = $derived(
		!navigation.active && routingState.open && !lineDetailState.selection
			&& routingState.selectedDirectRoute?.mode === 'bike'
			? routingState.selectedDirectRoute
			: null
	);
	import { routingState } from '../routing/state.svelte';
	import { lineDetailState } from '../linedetail/state.svelte';
	import { isNarrow } from '../routing/layout';
	import { mapUi } from './uiState.svelte';
	import {
		exitLineDetailView, focusSelectedLeg, frameSelectedDirect,
		frameSelectedItinerary, frameShownDirectRoutes
	} from './orchestration.svelte';
</script>

<svelte:window
	onkeydown={(e) => {
		if (e.key !== 'Escape') return;
		if (navigation.active) navigation.stop();
		else if (routingState.mapMode) routingState.exitMapMode();
		else if (lineDetailState.selection) exitLineDetailView();
	}}
	onresize={() => { if (!isNarrow() && routingState.mapMode) routingState.exitMapMode(); }}
/>

{#if navigation.active}
	<!-- Bicycle navigation (bicycle-navigation.md): banner, summary and
	     re-center control own the screen; the planning chrome below
	     stays mounted but hidden, so the panel's state survives the
	     ride and reappears on exit. -->
	<NavigationOverlay />
{/if}

<div class="planning-chrome" class:navigating={navigation.active}>
<LineDetailBar onClose={exitLineDetailView} />

{#if navStartRoute}
	{@const route = navStartRoute}
	<button
		class="nav-start"
		class:sheet-expanded={routingState.directSheetExpanded}
		type="button"
		disabled={navigation.starting}
		onclick={() => void navigation.start(route, routingState.directRoutes.filter((r) => r !== route))}
	>
		<span class="material-symbols-outlined" aria-hidden="true">navigation</span>
		Navigate
	</button>
{/if}

{#if routingState.mapMode}
	<!-- Mobile fullscreen map mode: the summary header owns the top of
	     the map. The RoutingPanel below stays mounted (display:none) so
	     the list's scroll position and any transient DOM state survive
	     the toggle. -->
	<RouteMapHeader />
{/if}
{#if routingState.open}
	<!-- direct-sheet: narrow-screen bottom sheet for cycling / walking
	     results — the wrapper flips to the bottom edge so the map above
	     stays visible and interactive (rule in the narrow media query;
	     desktop layout is unaffected by the class). -->
	<div
		class="top-controls"
		class:hidden-in-map-mode={routingState.mapMode}
		class:direct-sheet={
			routingState.travelMode !== 'transit'
			&& routingState.hasQueried
			&& !routingState.directSheetExpanded
		}
	>
		<RoutingPanel
			onFocusLeg={focusSelectedLeg}
			onEnterMapMode={frameSelectedItinerary}
			onFrameRoute={frameSelectedItinerary}
			onFrameDirectRoutes={frameShownDirectRoutes}
			onFrameDirectRoute={frameSelectedDirect}
			getMapCenter={() => {
				const c = mapUi.mapRef?.getCenter();
				return c ? ([c.lng, c.lat] as [number, number]) : null;
			}}
		/>
	</div>
{:else if !lineDetailState.selection}
	<div class="top-controls">
		<MapMenu
			viewMode={mapUi.viewMode}
			setView={mapUi.setView}
			contoursEnabled={mapUi.contoursEnabled}
			toggleContours={mapUi.toggleContours}
			bind:open={mapUi.menuOpen}
		/>
		<button
			class="control-disc route-button"
			type="button"
			aria-label="Plan a route"
			onclick={() => routingState.openPanel()}
		>
			<span class="material-symbols-outlined" aria-hidden="true">directions</span>
		</button>
		{#if mapUi.viewMode === 'transit-focus'}
			<StopSearch map={mapUi.mapRef} />
		{/if}
	</div>
{/if}

<MapContextMenu anchor={mapUi.contextAnchor} onClose={() => (mapUi.contextAnchor = null)} />

<a class="brand-overlay" href="/about" aria-label="About Kora Maps">
	<img src="/icon.svg" alt="" draggable="false" />
	<span class="beta-pill">Beta</span>
</a>
</div>

{#if mapUi.toast}
	<div class="map-toast" class:error={mapUi.toastLevel === 'error'} role="alert">
		{#if mapUi.toastTitle}<strong class="map-toast-title">{mapUi.toastTitle}</strong>{/if}
		{mapUi.toast}
	</div>
{/if}

{#if PUBLIC_ENVIRONMENT !== 'production'}
	<div class="zoom-badge" aria-label="Current zoom level">
		z&thinsp;{mapUi.zoom}
	</div>
{/if}

<style>
	/* Wrapper only — no box of its own, so the absolutely positioned
	   chrome inside keeps .map-wrap as its containing block. Hidden as
	   a whole while navigating. */
	.planning-chrome {
		display: contents;
	}
	.planning-chrome.navigating {
		display: none;
	}

	.top-controls {
		position: absolute;
		top: 1rem;
		left: 1rem;
		/* Keep the search bar clear of the top-right pill (~2.1rem wide
		   + 1rem right margin, plus a bit of visual breathing room). */
		right: 4rem;
		z-index: 2;
		display: flex;
		gap: 0.5rem;
		align-items: flex-start;
		/* The absolute box covers a strip across the top of the map even
		   when the visible children are narrow; disable pointer events
		   here and re-enable them per child so map pan/click through the
		   empty flex area still works. */
		pointer-events: none;
	}
	.top-controls > :global(*) {
		pointer-events: auto;
	}
	.top-controls.hidden-in-map-mode {
		display: none;
	}

	/* Start navigation: a labelled pill left of the zoom bar (the top-right
	   MapLibre column: 1rem margin + one control width + 0.5rem gap).
	   Primary action — red at rest, red fill on hover. */
	.nav-start {
		position: absolute;
		top: 1rem;
		right: calc(1rem + var(--control-size) + 0.5rem);
		z-index: 2;
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		height: var(--control-size);
		padding: 0 0.85rem 0 0.6rem;
		border: none;
		border-radius: var(--radius-pill);
		background: var(--white);
		box-shadow: var(--shadow-control);
		color: var(--brand);
		font-family: var(--font-ui);
		font-size: 0.85rem;
		font-weight: 600;
		cursor: pointer;
	}
	.nav-start .material-symbols-outlined {
		font-size: 1.2rem;
		line-height: 1;
	}
	.nav-start:hover {
		background: var(--brand);
		color: var(--white);
	}
	.nav-start:disabled {
		opacity: 0.6;
		cursor: progress;
	}
	/* Narrow screens: the top-right column is hidden while routing, so
	   the pill takes the corner itself; the expanded full-height panel
	   covers it, so it hides there. */
	@media (max-width: 699px) {
		:global(.map-wrap.routing-active:not(.routing-map-mode)) .nav-start {
			right: 1rem;
		}
		:global(.map-wrap.routing-active) .nav-start.sheet-expanded {
			display: none;
		}
		/* Fullscreen map mode: below the summary header, like the
		   MapLibre column (app.css § MapLibre controls). */
		:global(.map-wrap.routing-map-mode) .nav-start {
			top: 5.2rem;
		}
	}

	/* Round routing entry point — base disc styling from .control-disc
	   (app.css § Shared patterns), same family as MapMenu's toggle.
	   Brand red marks it as the primary action on the map. */
	.route-button {
		color: var(--brand);
	}
	.route-button .material-symbols-outlined {
		font-size: 1.25rem;
		line-height: 1;
	}
	.route-button:hover {
		background: var(--brand);
		color: var(--white);
	}

	/* Narrow screens (keep in sync with NARROW_BREAKPOINT in
	   routing/layout.ts): the routing panel becomes a full-bleed page —
	   no margins, no rounding (panel CSS handles the rounding). */
	@media (max-width: 699px) {
		:global(.map-wrap.routing-active) .top-controls {
			top: 0;
			left: 0;
			right: 0;
		}
		/* Direct-mode bottom sheet: the wrapper hugs the bottom edge (its
		   height is the sheet's own), leaving the map above it live. */
		:global(.map-wrap.routing-active) .top-controls.direct-sheet {
			top: auto;
			bottom: 0;
		}
	}

	.map-toast {
		position: absolute;
		bottom: 4rem;
		left: 50%;
		transform: translateX(-50%);
		background: rgba(0, 0, 0, 0.75);
		color: var(--white);
		font-family: var(--font-ui);
		font-size: 0.85rem;
		padding: 0.45rem 0.9rem;
		border-radius: var(--radius-pill);
		pointer-events: none;
		backdrop-filter: blur(4px);
		-webkit-backdrop-filter: blur(4px);
		max-width: min(85vw, 24rem);
		text-align: center;
		z-index: 40;
	}
	/* Error level: brand red, larger, higher up, a bold title over the
	   plain message — must not be missed. */
	.map-toast.error {
		bottom: 6rem;
		background: var(--brand);
		font-size: 0.92rem;
		padding: 0.7rem 1.1rem;
		border-radius: 0.8rem;
		box-shadow: var(--shadow-popover);
		max-width: min(90vw, 26rem);
	}
	.map-toast-title {
		display: block;
		font-weight: 700;
		font-size: 1rem;
		margin-bottom: 0.15rem;
	}

	.zoom-badge {
		position: absolute;
		bottom: 2.2rem;
		left: 50%;
		transform: translateX(-50%);
		background: rgba(0, 0, 0, 0.55);
		color: var(--white);
		font-family: var(--font-mono);
		font-size: 0.75rem;
		letter-spacing: 0.05em;
		padding: 0.25rem 0.6rem;
		border-radius: var(--radius-pill);
		pointer-events: none;
		backdrop-filter: blur(4px);
		-webkit-backdrop-filter: blur(4px);
		user-select: none;
	}

	/* Narrow routing-active: the full-width routing page owns the
	   viewport, so the zoom badge hides alongside the map controls
	   (kept in sync with the ctrl rules in Map.svelte). */
	@media (max-width: 699px) {
		:global(.map-wrap.routing-active:not(.routing-map-mode)) .zoom-badge {
			display: none;
		}
	}

	.brand-overlay {
		position: absolute;
		bottom: 1rem;
		left: 1rem;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.4rem;
		z-index: 1;
		text-decoration: none;
		pointer-events: auto;
	}

	.brand-overlay img {
		height: 2.6rem;
		width: 2.6rem;
		filter: drop-shadow(0 1px 2px rgba(0, 0, 0, 0.35));
	}

	.beta-pill {
		background: var(--brand);
		color: var(--white);
		font-family: var(--font-ui);
		font-weight: 700;
		font-size: 0.68rem;
		line-height: 1;
		letter-spacing: 0.04em;
		padding: 0.22rem 0.5rem;
		border-radius: var(--radius-pill);
		box-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
	}

	/* Narrow routing-active: the full-width routing page owns the
	   viewport, so the brand overlay hides alongside the other controls
	   (kept in sync with the zoom-badge / ctrl rules above). In
	   fullscreen map mode it stays visible. */
	@media (max-width: 699px) {
		:global(.map-wrap.routing-active:not(.routing-map-mode)) .brand-overlay {
			display: none;
		}
	}
</style>
