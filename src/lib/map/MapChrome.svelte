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
	import { routingState } from '../routing/state.svelte';
	import { lineDetailState } from '../linedetail/state.svelte';
	import { isNarrow } from '../routing/layout';
	import { mapUi } from './uiState.svelte';
	import {
		exitLineDetailView, focusSelectedLeg, frameSelectedItinerary
	} from './orchestration.svelte';
</script>

<svelte:window
	onkeydown={(e) => {
		if (e.key !== 'Escape') return;
		if (routingState.mapMode) routingState.exitMapMode();
		else if (lineDetailState.selection) exitLineDetailView();
	}}
	onresize={() => { if (!isNarrow() && routingState.mapMode) routingState.exitMapMode(); }}
/>

<LineDetailBar onClose={exitLineDetailView} />

{#if routingState.mapMode}
	<!-- Mobile fullscreen map mode: the summary header owns the top of
	     the map. The RoutingPanel below stays mounted (display:none) so
	     the list's scroll position and any transient DOM state survive
	     the toggle. -->
	<RouteMapHeader />
{/if}
{#if routingState.open}
	<div class="top-controls" class:hidden-in-map-mode={routingState.mapMode}>
		<RoutingPanel
			onFocusLeg={focusSelectedLeg}
			onEnterMapMode={frameSelectedItinerary}
			onFrameRoute={frameSelectedItinerary}
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

{#if mapUi.toast}
	<div class="map-toast" role="alert">{mapUi.toast}</div>
{/if}

<a class="brand-overlay" href="/about" aria-label="About Kora Maps">
	<img src="/icon.svg" alt="" draggable="false" />
	<span class="beta-pill">Beta</span>
</a>

{#if PUBLIC_ENVIRONMENT !== 'production'}
	<div class="zoom-badge" aria-label="Current zoom level">
		z&thinsp;{mapUi.zoom}
	</div>
{/if}

<style>
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
