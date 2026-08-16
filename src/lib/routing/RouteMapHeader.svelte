<script lang="ts">
	import type { Itinerary } from './types';
	import { legBadgeColor, loadRouteColorIndex } from './legColor';
	import { transferCount } from './ranking';
	import {
		badgeTextColor, displayLegs, fmtDuration, fmtTime, iconFor
	} from './itineraryFormat';
	import { routingState } from './state.svelte';

	// Summary header for the mobile fullscreen map mode
	// (routing-map-details-split.md): the connection's times / duration /
	// transfers / leg badges, plus an × that returns to the list
	// (selection persists).
	let it: Itinerary | null = $derived(routingState.selectedItinerary);

	let colorIndex = $state<Map<string, string> | null>(null);
	$effect(() => {
		let cancelled = false;
		loadRouteColorIndex().then((m) => { if (!cancelled) colorIndex = m; });
		return () => { cancelled = true; };
	});
</script>

{#if it}
	<div class="route-map-header" role="status">
		<div class="rmh-body">
			<div class="rmh-title">
				<span class="rmh-time">{fmtTime(it.startTime)} – {fmtTime(it.endTime)}</span>
				<span class="rmh-meta">
					{fmtDuration(it.duration)}
					· {transferCount(it)} transfer{transferCount(it) === 1 ? '' : 's'}
				</span>
			</div>
			<div class="rmh-legs">
				{#each displayLegs(it) as { leg, dur, isWalk }, i}
					{#if i > 0}<span class="rmh-sep material-symbols-outlined" aria-hidden="true">chevron_right</span>{/if}
					<span class="rmh-leg" class:walk={isWalk}>
						{#if isWalk}
							<span class="rmh-mode material-symbols-outlined" aria-hidden="true">{iconFor(leg.mode)}</span>
							<span class="rmh-leg-dur">{fmtDuration(dur)}</span>
						{:else if leg.routeShortName}
							{@const bg = legBadgeColor(colorIndex, leg)}
							<span
								class="rmh-ref"
								style="background:{bg};color:{badgeTextColor(bg)}"
							>{leg.routeShortName}</span>
						{:else}
							<span class="rmh-mode material-symbols-outlined" aria-hidden="true">{iconFor(leg.mode)}</span>
						{/if}
					</span>
				{/each}
			</div>
		</div>
		<button
			class="rmh-btn"
			type="button"
			aria-label="Back to connection list"
			title="Back to connection list"
			onclick={() => routingState.exitMapMode()}
		>×</button>
	</div>
{/if}

<style>
	/* Same visual pattern as the line-detail view's top bar. */
	.route-map-header {
		position: absolute;
		top: 0.6rem;
		left: 50%;
		transform: translateX(-50%);
		display: flex;
		align-items: center;
		gap: 0.5rem;
		width: min(94vw, 34rem);
		background: #ffffff;
		border-radius: 1.1rem;
		box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3);
		padding: 0.55rem 0.7rem;
		font-family: 'Saira', sans-serif;
		z-index: 5;
	}

	.rmh-btn {
		flex: 0 0 auto;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 2rem;
		height: 2rem;
		border: none;
		border-radius: 999px;
		background: transparent;
		color: #444;
		font-size: 1.3rem;
		line-height: 1;
		cursor: pointer;
	}
	.rmh-btn:hover { background: #eee; color: #000; }

	.rmh-body {
		flex: 1 1 auto;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
	.rmh-title {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.rmh-time { font-weight: 700; font-size: 0.95rem; color: #222; }
	.rmh-meta { font-size: 0.8rem; color: #555; white-space: nowrap; }

	.rmh-legs {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.15rem 0.25rem;
	}
	.rmh-leg { display: inline-flex; align-items: center; gap: 0.15rem; }
	.rmh-mode { font-size: 1rem; line-height: 1; color: #444; }
	.rmh-leg.walk .rmh-mode { color: #888; }
	.rmh-leg-dur { font-size: 0.7rem; color: #666; white-space: nowrap; }
	.rmh-ref {
		display: inline-block;
		padding: 1px 5px;
		border-radius: 3px;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.02em;
		background: #999;
		color: #fff;
		white-space: nowrap;
	}
	.rmh-sep { font-size: 0.9rem; color: #ccc; }
</style>
