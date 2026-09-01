<script lang="ts">
	import { slide } from 'svelte/transition';
	import type { DirectRoute } from './types';
	import { fmtDistance, fmtDuration } from './itineraryFormat';
	import { isNarrow } from './layout';
	import { routingState } from './state.svelte';

	// One card per direct cycling / walking route
	// (pedestrian-bicycle-routing.md § Result cards): duration, distance,
	// ascent / descent; the selected card adds the elevation profile
	// graph. Selection is two-way with the map (card click ↔ route line
	// click). The stats render as a row of uniform stat items, so future
	// additions (surface quality, dedicated-path share, …) are one more
	// item, not a redesign.

	interface Props {
		route: DirectRoute;
		index: number;
		/** Frame the whole alternatives set (mobile map-mode entry /
		 * desktop reframe). */
		onFrameRoutes?: () => void;
	}

	let { route, index, onFrameRoutes }: Props = $props();

	let selected = $derived(routingState.directSelected === index);

	function pick() {
		routingState.selectDirectRoute(index);
	}

	// Map icon: on mobile enter fullscreen map mode (the full-width list
	// hides the map otherwise); on desktop reframe the overview.
	function showOnMap(e: Event) {
		e.stopPropagation();
		routingState.selectDirectRoute(index);
		if (isNarrow()) routingState.enterMapMode();
		onFrameRoutes?.();
	}

	let hasElevation = $derived(route.ascentM !== null && route.descentM !== null);

	// ── Elevation profile (selected card only) ─────────────────────────
	// Inline SVG polyline over the profile samples; light area fill under
	// the line. Y spans the route's own min/max with a small floor so a
	// flat route doesn't blow jitter up to full height.
	const PROFILE_W = 280;
	const PROFILE_H = 56;
	const MIN_SPAN_M = 30;

	let profileData = $derived.by(() => {
		const p = route.profile;
		if (!p || p.length < 2) return null;
		const min = Math.min(...p);
		const max = Math.max(...p);
		const span = Math.max(max - min, MIN_SPAN_M);
		const mid = (max + min) / 2;
		const lo = mid - span / 2;
		const pts = p.map((v, i) => {
			const x = (i / (p.length - 1)) * PROFILE_W;
			const y = PROFILE_H - ((v - lo) / span) * PROFILE_H;
			return `${x.toFixed(1)},${y.toFixed(1)}`;
		});
		return {
			line: pts.join(' '),
			area: `0,${PROFILE_H} ${pts.join(' ')} ${PROFILE_W},${PROFILE_H}`,
			minLabel: `${Math.round(min)} m`,
			maxLabel: `${Math.round(max)} m`
		};
	});
</script>

<div
	class="card drc"
	class:selected
	role="button"
	tabindex="0"
	aria-pressed={selected}
	onclick={pick}
	onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } }}
>
	<div class="drc-head">
		<span class="drc-title">
			{index === 0 ? 'Suggested route' : `Alternative ${index}`}
		</span>
		<div class="card-actions">
			<button
				class="card-map"
				type="button"
				aria-label="Show this route on the map"
				title="Show this route on the map"
				onclick={showOnMap}
			>
				<span class="material-symbols-outlined" aria-hidden="true">map</span>
			</button>
		</div>
	</div>
	<!-- Stat row: uniform items so later additions (surface quality,
	     dedicated-path share, …) slot in without redesign. -->
	<div class="drc-stats">
		<span class="drc-stat drc-dur">{fmtDuration(route.durationSec)}</span>
		<span class="drc-stat">{fmtDistance(route.distanceM)}</span>
		{#if hasElevation}
			<span class="drc-stat" title="{route.ascentM} m ascent · {route.descentM} m descent">
				&#8593;&nbsp;{route.ascentM}&thinsp;m &#8595;&nbsp;{route.descentM}&thinsp;m
			</span>
		{/if}
		{#if route.stairsM > 0}
			<span
				class="drc-stat drc-stairs"
				title={route.mode === 'bike'
					? `Includes ${fmtDistance(route.stairsM)} of stairs — push or carry the bike`
					: `Includes ${fmtDistance(route.stairsM)} of stairs`}
			>
				stairs {fmtDistance(route.stairsM)}
			</span>
		{/if}
	</div>
	{#if selected && profileData}
		<div class="drc-profile" transition:slide={{ duration: 160 }}>
			<svg
				viewBox="0 0 {PROFILE_W} {PROFILE_H}"
				preserveAspectRatio="none"
				aria-label="Elevation profile"
				role="img"
			>
				<polygon points={profileData.area} class="drc-profile-area" />
				<polyline points={profileData.line} class="drc-profile-line" />
			</svg>
			<div class="drc-profile-labels" aria-hidden="true">
				<span>low {profileData.minLabel}</span>
				<span>high {profileData.maxLabel}</span>
			</div>
		</div>
	{/if}
</div>

<style>
	/* Same card family as the transit result cards (ResultCard.svelte):
	   white card, hairline border; selection = gradient strip on the left
	   edge below an overlay border ring (ux-guidelines.md § Usage rules). */
	.card {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		padding: 0.55rem 0.7rem;
		background: var(--white);
		border: 1px solid var(--gray-100);
		border-radius: 0.6rem;
		font-family: var(--font-ui);
		text-align: left;
		width: 100%;
		cursor: pointer;
		color: inherit;
		transition: border-color 0.12s, background 0.12s;
	}
	.card:hover { border-color: var(--gray-250); background: #fafafa; }
	.card.selected {
		border-color: transparent;
		background: var(--gray-75);
	}
	.card.selected::before {
		content: '';
		position: absolute;
		left: 0;
		top: 0;
		bottom: 0;
		width: 4px;
		border-radius: 0.6rem 0 0 0.6rem;
		background: var(--gradient-brand);
	}
	.card.selected::after {
		content: '';
		position: absolute;
		inset: -1px;
		border: 2px solid var(--gray-900);
		border-radius: 0.6rem;
		pointer-events: none;
	}

	.drc-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.4rem;
	}
	.drc-title {
		font-size: 0.72rem;
		font-weight: 600;
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--gray-500);
	}
	.card.selected .drc-title { color: var(--anthracite); }

	.card-actions {
		flex: 0 0 auto;
		display: inline-flex;
		align-items: center;
		margin: -0.3rem -0.15rem -0.3rem 0;
	}
	/* Same red map accent as the transit cards. */
	.card-map {
		flex: 0 0 auto;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 1.6rem;
		height: 1.6rem;
		border: none;
		border-radius: var(--radius-pill);
		background: transparent;
		cursor: pointer;
	}
	.card-map :global(.material-symbols-outlined) {
		font-size: 1.25rem;
		line-height: 1;
		color: var(--brand);
	}
	.card-map:hover { background: #f3e2e5; }
	@media (min-width: 700px) {
		.card.selected .card-map { background: var(--brand); }
		.card.selected .card-map :global(.material-symbols-outlined) { color: var(--white); }
		.card.selected .card-map:hover { background: var(--brand-hover); }
	}

	.drc-stats {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 0.25rem 0.9rem;
		font-size: 0.85rem;
		color: var(--gray-700);
	}
	.drc-dur {
		font-size: 1.05rem;
		font-weight: 700;
		color: var(--gray-850);
	}
	.drc-stairs { color: var(--warn); font-size: 0.78rem; }

	.drc-profile svg {
		display: block;
		width: 100%;
		height: 3.5rem;
	}
	.drc-profile-area { fill: var(--gray-100); }
	.drc-profile-line {
		fill: none;
		stroke: var(--anthracite);
		stroke-width: 1.5;
		vector-effect: non-scaling-stroke;
	}
	.drc-profile-labels {
		display: flex;
		justify-content: space-between;
		font-size: 0.68rem;
		color: var(--gray-400);
	}
</style>
