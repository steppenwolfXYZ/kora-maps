<script lang="ts">
	// The chrome of bicycle navigation (bicycle-navigation.md): the
	// maneuver banner at the top (with the × that ends the ride), the
	// trip summary at the bottom, and the re-center control while
	// following is suspended.
	// Rendered by MapChrome in place of all other chrome while a ride
	// is active; everything it shows derives from navigation state.
	import { navigation } from './state.svelte';
	import { fmtNavDistance, instructionText } from './guidance';
	import { maneuverIconSvg } from './maneuverIcons';
	import { fmtDistance, fmtDuration } from '../routing/itineraryFormat';

	let g = $derived(navigation.guidance);
	let arrived = $derived(navigation.arrived);

	function fmtClock(ms: number): string {
		const d = new Date(ms);
		return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
	}

	// One status line at a time, most urgent first.
	let status = $derived.by(() => {
		if (navigation.arrived) return null;
		if (navigation.recalculating) return { text: 'Recalculating the route…', warn: false };
		if (navigation.updateFailed)
			return { text: 'Route could not be updated — guiding along the previous route', warn: true };
		if (navigation.offRoute) return { text: 'Off route', warn: true };
		if (navigation.positionStale) return { text: 'Waiting for a position fix…', warn: true };
		return null;
	});

	// Guidance is relative to the old route while off it — the summary
	// marks its numbers as approximate (concept § Off-route detection).
	let approximate = $derived(navigation.offRoute);
</script>

<div class="nav-banner" role="status" aria-live="polite">
	<button
		class="nb-close icon-btn"
		type="button"
		aria-label="End navigation"
		title="End navigation"
		onclick={() => navigation.stop()}
	>×</button>
	{#if arrived}
		<div class="nb-row">
			<div class="nb-icon">{@html maneuverIconSvg('destination')}</div>
			<div class="nb-main">
				<div class="nb-dist">You have arrived</div>
				<div class="nb-text">Navigation ends in a moment</div>
			</div>
		</div>
	{:else if g}
		<div class="nb-row">
			<div class="nb-icon" class:pushed={g.next.pushed}>
				{@html maneuverIconSvg(g.nextKind)}
			</div>
			<div class="nb-main">
				<div class="nb-dist">{fmtNavDistance(g.distanceToNextM)}</div>
				<div class="nb-text">{instructionText(g.next)}</div>
				{#if g.current?.pushed}
					<span class="nb-chip">
						<span class="material-symbols-outlined" aria-hidden="true">directions_walk</span>
						Push your bike
					</span>
				{/if}
			</div>
		</div>
		{#if g.then}
			<div class="nb-then">
				<span class="nb-then-label">then</span>
				<span class="nb-then-icon">{@html maneuverIconSvg(g.thenKind ?? 'straight')}</span>
				<span class="nb-then-text">{instructionText(g.then)}</span>
			</div>
		{/if}
	{:else}
		<div class="nb-row">
			<div class="nb-icon">{@html maneuverIconSvg('straight')}</div>
			<div class="nb-main">
				<div class="nb-text">Follow the route</div>
			</div>
		</div>
	{/if}
	{#if status}
		<div class="nb-status" class:warn={status.warn}>{status.text}</div>
	{/if}
	<!-- TEMPORARY compass diagnostic for phone tests — remove once the
	     compass behaviour is understood. -->
	{#if true}
		{@const c = navigation.compassDebug}
		<div class="nb-status nb-debug">
			compass {c.permission} · {c.samples} samples
			{#if c.last}
				· {c.last.type} · α {c.last.alpha === null ? 'null' : c.last.alpha.toFixed(0)}
				· abs {c.last.absolute ? 'y' : 'n'}
				· wk {c.last.webkitHeading === null ? 'null' : c.last.webkitHeading.toFixed(0)}
			{/if}
			· heading {navigation.heading === null ? 'null' : navigation.heading.toFixed(0)}
		</div>
	{/if}
</div>

{#if !navigation.following}
	<button
		class="nav-recenter"
		type="button"
		title="Re-center on your position"
		onclick={() => navigation.resumeFollow()}
	>
		<span class="material-symbols-outlined" aria-hidden="true">my_location</span>
		Re-center
	</button>
{/if}

<div class="nav-summary">
	{#if g && !arrived}
		<span class="ns-time">{approximate ? '~' : ''}{fmtDuration(g.remainingSec)}</span>
		<span class="ns-meta">
			{fmtDistance(g.remainingM)} · arrive {fmtClock(g.etaMs)}
		</span>
	{:else if arrived}
		<span class="ns-time">Arrived</span>
	{/if}
</div>

<style>
	/* Floating panel family (ux-guidelines.md): white, gradient hairline
	   along the top edge as a layered background so it follows the
	   corner radius. */
	.nav-banner {
		position: absolute;
		top: calc(0.75rem + env(safe-area-inset-top, 0px));
		left: 0.75rem;
		right: 0.75rem;
		max-width: 30rem;
		margin: 0 auto;
		z-index: 3;
		display: flex;
		flex-direction: column;
		background: var(--gradient-brand) top / 100% 3px no-repeat, var(--white);
		border-radius: 0.9rem;
		box-shadow: var(--shadow-popover);
		font-family: var(--font-ui);
		overflow: hidden;
	}
	/* Base look + hover from .icon-btn (app.css); placement and sizing
	   only here. Top-right corner of the banner, clear of the text. */
	.nb-close {
		position: absolute;
		top: 0.55rem;
		right: 0.55rem;
		width: 2rem;
		height: 2rem;
		padding: 0;
		font-size: 1.4rem;
		line-height: 1;
	}
	.nb-row {
		display: flex;
		align-items: center;
		gap: 0.85rem;
		/* Right padding keeps the instruction text out from under the ×. */
		padding: 0.85rem 2.9rem 0.75rem 1rem;
	}
	/* Turn glyph: red disc, white glyph — the routing panel's title-icon
	   treatment (ux-guidelines.md § Brand red). */
	.nb-icon {
		flex: 0 0 auto;
		width: 3.4rem;
		height: 3.4rem;
		border-radius: var(--radius-pill);
		background: var(--brand);
		color: var(--white);
		display: flex;
		align-items: center;
		justify-content: center;
	}
	.nb-icon :global(svg) {
		width: 2.3rem;
		height: 2.3rem;
	}
	/* A walked step ahead: anthracite disc, so the rider sees the change
	   of pace before the instruction text. */
	.nb-icon.pushed {
		background: var(--anthracite);
	}
	.nb-main {
		flex: 1 1 auto;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
	}
	.nb-dist {
		font-size: 1.7rem;
		font-weight: 700;
		line-height: 1.1;
		color: var(--anthracite);
		font-variant-numeric: tabular-nums;
	}
	.nb-text {
		font-size: 1rem;
		line-height: 1.25;
		color: var(--gray-700);
		overflow-wrap: anywhere;
	}
	.nb-chip {
		align-self: flex-start;
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		margin-top: 0.3rem;
		padding: 0.15rem 0.55rem 0.15rem 0.4rem;
		border-radius: var(--radius-pill);
		background: var(--anthracite);
		color: var(--white);
		font-size: 0.75rem;
		font-weight: 600;
	}
	.nb-chip .material-symbols-outlined {
		font-size: 1rem;
	}
	.nb-then {
		display: flex;
		align-items: center;
		gap: 0.45rem;
		padding: 0.45rem 1rem;
		border-top: 1px solid var(--gray-100);
		font-size: 0.85rem;
		color: var(--gray-600);
	}
	.nb-then-label {
		text-transform: uppercase;
		letter-spacing: 0.05em;
		font-size: 0.7rem;
		font-weight: 600;
		color: var(--gray-500);
	}
	.nb-then-icon {
		display: inline-flex;
		color: var(--anthracite);
	}
	.nb-then-icon :global(svg) {
		width: 1.3rem;
		height: 1.3rem;
	}
	.nb-then-text {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.nb-status {
		padding: 0.4rem 1rem;
		background: var(--gray-75);
		color: var(--gray-600);
		font-size: 0.8rem;
		text-align: center;
	}
	.nb-status.warn {
		background: color-mix(in srgb, var(--warn) 12%, var(--white));
		color: var(--warn);
	}
	.nb-debug {
		font-family: var(--font-mono);
		font-size: 0.68rem;
		text-align: left;
	}

	/* Left side, above the summary — a labelled pill in the map-control
	   family (white, shadow, red glyph; red fill on hover). */
	.nav-recenter {
		position: absolute;
		left: 1rem;
		bottom: calc(4.6rem + env(safe-area-inset-bottom, 0px));
		z-index: 3;
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
	.nav-recenter .material-symbols-outlined {
		font-size: 1.2rem;
		line-height: 1;
	}
	.nav-recenter:hover {
		background: var(--brand);
		color: var(--white);
	}

	.nav-summary {
		position: absolute;
		left: 50%;
		bottom: calc(1rem + env(safe-area-inset-bottom, 0px));
		transform: translateX(-50%);
		z-index: 3;
		display: flex;
		align-items: center;
		gap: 0.6rem;
		max-width: calc(100vw - 1.5rem);
		padding: 0.5rem 1.1rem;
		background: var(--white);
		border-radius: var(--radius-pill);
		box-shadow: var(--shadow-control);
		font-family: var(--font-ui);
		white-space: nowrap;
	}
	.ns-time {
		font-size: 1.15rem;
		font-weight: 700;
		color: var(--anthracite);
		font-variant-numeric: tabular-nums;
	}
	.ns-meta {
		font-size: 0.85rem;
		color: var(--gray-600);
		font-variant-numeric: tabular-nums;
	}
</style>
