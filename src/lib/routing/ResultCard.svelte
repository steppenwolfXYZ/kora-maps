<script lang="ts">
	import { slide } from 'svelte/transition';
	import type { Itinerary, Leg } from './types';
	import { legBadgeColor, loadRouteColorIndex } from './legColor';
	import { legDuration, transferCount, walkSeconds } from './ranking';
	import type { Badge, Warning, WarningKind, WarningSeverity } from './ranking';
	import {
		badgeTextColor, displayLegs, fmtDuration, fmtTime,
		iconFor, isTransitMode
	} from './itineraryFormat';
	import { isNarrow } from './layout';
	import { itineraryFingerprint } from './fingerprint';
	import { routingState } from './state.svelte';

	interface Props {
		itinerary: Itinerary;
		badge?: Badge | null;
		warnings?: Warning[];
		/** Camera-focus one leg on the map (Map.svelte wires this through). */
		onFocusLeg?: (leg: Leg) => void;
		/** Frame the whole route when entering mobile map mode. */
		onEnterMapMode?: (it: Itinerary) => void;
	}

	let { itinerary, badge = null, warnings = [], onFocusLeg, onEnterMapMode }: Props = $props();

	function headsign(leg: Leg): string {
		return leg.headsign ?? leg.tripHeadsign ?? '';
	}

	// Primary click (card body or chevron): toggle details. On desktop,
	// opening a card also puts it on the map (open implies select); on
	// mobile the map is only reachable via the map icon. See
	// routing-map-details-split.md.
	function toggleCard() {
		routingState.toggleExpanded(itinerary);
		if (!isNarrow() && routingState.expandedFingerprint === fingerprint) {
			routingState.selectItinerary(itinerary);
		}
	}

	// Map icon: select on the map without opening/closing any card. On
	// mobile this enters fullscreen map mode; on desktop it just re-aims
	// the overlay at this connection (peek while another card stays open).
	function showOnMap(e: Event) {
		e.stopPropagation();
		routingState.selectItinerary(itinerary);
		if (isNarrow()) {
			routingState.enterMapMode();
			onEnterMapMode?.(itinerary);
		}
	}

	// Clicking a leg row focuses it on the map. If the card isn't on the
	// map yet, select it first so the overlay is there to look at. On
	// mobile also enter map mode — a camera move behind the full-width
	// list would be invisible otherwise.
	function focusLeg(e: Event, leg: Leg) {
		e.stopPropagation();
		if (!selected) routingState.selectItinerary(itinerary);
		if (isNarrow()) routingState.enterMapMode();
		onFocusLeg?.(leg);
	}

	const BADGE_ICON: Record<Badge, string> = {
		best: 'crown',
		good: 'thumb_up',
		bad: 'thumb_down'
	};
	const BADGE_LABEL: Record<Badge, string> = {
		best: 'Best route',
		good: 'Good route',
		bad: 'Slower or less comfortable than the best options'
	};

	const WARNING_ICON: Record<WarningKind, string> = {
		'long-walk':       'directions_walk',
		'long-wait':       'hourglass_top',
		'very-slow':       'hourglass_bottom'
	};
	const WARNING_LABEL: Record<WarningKind, Record<WarningSeverity, string>> = {
		'long-walk': {
			standard: 'Includes a walk longer than 20 minutes',
			medium:   'Includes a walk longer than 40 minutes',
			strong:   'Includes a walk longer than an hour'
		},
		'long-wait': {
			standard: 'Includes a transfer wait of an hour or more',
			medium:   'Includes a transfer wait of two hours or more',
			strong:   'Includes a transfer wait of three hours or more'
		},
		'very-slow': {
			standard: 'Takes at least twice as long as the fastest option',
			medium:   'Takes at least three times as long as the fastest option',
			strong:   'Takes at least four times as long as the fastest option'
		}
	};

	let fingerprint = $derived(itineraryFingerprint(itinerary));
	let selected = $derived(routingState.selectedFingerprint === fingerprint);
	let expanded = $derived(routingState.expandedFingerprint === fingerprint);

	let colorIndex = $state<Map<string, string> | null>(null);
	$effect(() => {
		let cancelled = false;
		loadRouteColorIndex().then((m) => { if (!cancelled) colorIndex = m; });
		return () => { cancelled = true; };
	});

	// First/last transit station of the trip, with departure / arrival times.
	// Null for walk-only itineraries (direct walking options).
	function transitEndpoints(it: Itinerary): { fromName: string; fromTime: string; toName: string; toTime: string } | null {
		const transit = it.legs.filter((l) => l.mode !== 'WALK' && l.mode !== 'BIKE' && l.mode !== 'CAR');
		if (!transit.length) return null;
		const first = transit[0];
		const last = transit[transit.length - 1];
		return {
			fromName: first.from?.name ?? '',
			fromTime: first.startTime,
			toName: last.to?.name ?? '',
			toTime: last.endTime
		};
	}
</script>

<div
	class="card"
	class:selected
	role="button"
	tabindex="0"
	aria-expanded={expanded}
	onclick={toggleCard}
	onkeydown={(e) => {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			toggleCard();
		}
	}}
>
	{#if badge}
		<span class="card-badge card-badge-{badge}" title={BADGE_LABEL[badge]} aria-label={BADGE_LABEL[badge]}>
			<span class="material-symbols-outlined" aria-hidden="true">{BADGE_ICON[badge]}</span>
		</span>
	{/if}
	<div class="card-head">
		{#if warnings.length}
			<span class="card-warnings">
				{#each warnings as w}
					<span
						class="card-warning card-warning-{w.severity}"
						title={WARNING_LABEL[w.kind][w.severity]}
						aria-label={WARNING_LABEL[w.kind][w.severity]}
					>
						<span class="material-symbols-outlined" aria-hidden="true">{WARNING_ICON[w.kind]}</span>
					</span>
				{/each}
			</span>
		{/if}
		<span class="card-time">{fmtTime(itinerary.startTime)} – {fmtTime(itinerary.endTime)}</span>
		<span class="card-dur">{fmtDuration(itinerary.duration)}</span>
		{#if selected}
			<button
				class="card-clear"
				type="button"
				aria-label="Clear route from map"
				onclick={(e) => {
					e.stopPropagation();
					routingState.dismissSelectedItinerary();
				}}
			>×</button>
		{/if}
	</div>
	{#if transitEndpoints(itinerary)}
		{@const endpoints = transitEndpoints(itinerary)!}
		<div class="card-route">
			<strong>{endpoints.fromName}</strong> {fmtTime(endpoints.fromTime)} – <strong>{endpoints.toName}</strong> {fmtTime(endpoints.toTime)}
		</div>
	{/if}
	<div class="card-legs">
		{#each displayLegs(itinerary) as { leg, dur, isWalk }, i}
			{#if i > 0}<span class="card-sep material-symbols-outlined" aria-hidden="true">chevron_right</span>{/if}
			<span class="card-leg" class:walk={isWalk}>
				{#if isWalk}
					<span class="card-mode material-symbols-outlined" aria-hidden="true">{iconFor(leg.mode)}</span>
					<span class="card-leg-dur">{fmtDuration(dur)}</span>
				{:else if leg.routeShortName}
					{@const bg = legBadgeColor(colorIndex, leg)}
					<span
						class="card-ref"
						style="background:{bg};color:{badgeTextColor(bg)}"
					>{leg.routeShortName}</span>
				{:else}
					<span class="card-mode material-symbols-outlined" aria-hidden="true">{iconFor(leg.mode)}</span>
				{/if}
			</span>
		{/each}
	</div>
	<div class="card-meta">
		<span class="card-meta-text">
			{transferCount(itinerary)} transfer{transferCount(itinerary) === 1 ? '' : 's'}
			· {fmtDuration(walkSeconds(itinerary))} walking
		</span>
		<button
			class="card-map"
			type="button"
			title="Show on map"
			aria-label="Show on map"
			onclick={showOnMap}
		>
			<span class="material-symbols-outlined" aria-hidden="true">map</span>
		</button>
	</div>
	{#if expanded}
		<div class="leg-list" transition:slide>
			{#each itinerary.legs as leg, i}
				{#if isTransitMode(leg.mode)}
					<button class="leg-item" type="button" onclick={(e) => focusLeg(e, leg)}>
						<span class="leg-stop-row">
							<span class="leg-time">{fmtTime(leg.startTime)}</span>
							<span class="leg-stop-name">{leg.from?.name ?? ''}</span>
							{#if leg.from?.track}<span class="leg-pf">Pl. {leg.from.track}</span>{/if}
						</span>
						<span class="leg-line-row">
							{#if leg.routeShortName}
								{@const bg = legBadgeColor(colorIndex, leg)}
								<span
									class="card-ref"
									style="background:{bg};color:{badgeTextColor(bg)}"
								>{leg.routeShortName}</span>
							{:else}
								<span class="card-mode material-symbols-outlined" aria-hidden="true">{iconFor(leg.mode)}</span>
							{/if}
							{#if headsign(leg)}<span class="leg-dir">→ {headsign(leg)}</span>{/if}
							<span class="leg-dur">{fmtDuration(legDuration(leg))}</span>
						</span>
						<span class="leg-stop-row">
							<span class="leg-time">{fmtTime(leg.endTime)}</span>
							<span class="leg-stop-name">{leg.to?.name ?? ''}</span>
							{#if leg.to?.track}<span class="leg-pf">Pl. {leg.to.track}</span>{/if}
						</span>
					</button>
				{:else}
					<button class="leg-item walk" type="button" onclick={(e) => focusLeg(e, leg)}>
						<span class="card-mode material-symbols-outlined" aria-hidden="true">{iconFor(leg.mode)}</span>
						<span class="leg-walk-dur">{fmtDuration(legDuration(leg))}</span>
					</button>
				{/if}
			{/each}
		</div>
	{/if}
	<button
		class="card-expand"
		type="button"
		aria-label={expanded ? 'Hide connection details' : 'Show connection details'}
		aria-expanded={expanded}
		onclick={(e) => { e.stopPropagation(); toggleCard(); }}
	><span class="card-expand-chevron" class:flipped={expanded}>▾</span></button>
</div>

<style>
	.card {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		padding: 0.55rem 0.7rem;
		background: #ffffff;
		border: 1px solid #eee;
		border-radius: 0.6rem;
		font-family: 'Saira', sans-serif;
		text-align: left;
		width: 100%;
		cursor: pointer;
		color: inherit;
		transition: border-color 0.12s, background 0.12s, box-shadow 0.12s;
	}

	.card-map {
		flex: 0 0 auto;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 1.6rem;
		height: 1.6rem;
		margin: -0.3rem -0.15rem -0.3rem 0;
		border: none;
		border-radius: 999px;
		background: transparent;
		cursor: pointer;
	}
	/* Filled map glyph in the brand red — the one coloured accent on the
	   otherwise monochrome card. */
	.card-map :global(.material-symbols-outlined) {
		font-size: 1.25rem;
		line-height: 1;
		color: #740013;
		font-variation-settings: 'FILL' 1;
	}
	.card-map:hover { background: #f3e2e5; }
	/* This card is the one on the map: invert to a red disc with a white
	   glyph so the active state reads at a glance. Desktop only — on
	   mobile the map is never visible while the list is, so the icon
	   never shows the active state there. */
	@media (min-width: 700px) {
		.card.selected .card-map { background: #740013; }
		.card.selected .card-map :global(.material-symbols-outlined) { color: #fff; }
		.card.selected .card-map:hover { background: #8a0418; }
	}

	.card-badge {
		position: absolute;
		top: -0.7rem;
		right: 0.6rem;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 1.35rem;
		height: 1.35rem;
		border-radius: 999px;
		border: 1px solid #eee;
		background: #ffffff;
		box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
		color: #444;
		z-index: 1;
	}
	.card-badge :global(.material-symbols-outlined) { font-size: 0.95rem; line-height: 1; }
	.card-badge-best { color: #b58a00; border-color: #e2c26a; background: #fff8e1; }
	.card-badge-good { color: #2f7a2f; border-color: #cde7cd; background: #f1faf1; }
	.card-badge-bad  { color: #a33; border-color: #eecdcd; background: #fbf1f1; }

	.card-warnings {
		display: inline-flex;
		align-items: center;
		gap: 0.15rem;
		margin-right: 0.15rem;
		flex: 0 0 auto;
	}
	.card-warning {
		display: inline-flex;
		align-items: center;
		justify-content: center;
	}
	.card-warning :global(.material-symbols-outlined) { font-size: 1rem; line-height: 1; }
	/* standard: plain red icon */
	.card-warning-standard { color: #b83232; }
	/* medium / strong: white icon inside a coloured circle */
	.card-warning-medium,
	.card-warning-strong {
		width: 1.2rem;
		height: 1.2rem;
		border-radius: 999px;
		color: #fff;
	}
	.card-warning-medium { background: #d9a400; }
	.card-warning-strong { background: #b83232; }
	.card-warning-medium :global(.material-symbols-outlined),
	.card-warning-strong :global(.material-symbols-outlined) { font-size: 0.85rem; }
	.card:hover { border-color: #ccc; background: #fafafa; }
	.card.selected {
		border-color: #1a1a1a;
		background: #f2f2f2;
		box-shadow: 0 0 0 1px #1a1a1a inset;
	}
	.card + :global(.card) { margin-top: 0.4rem; }

	.card-clear {
		border: none;
		background: transparent;
		color: #444;
		font-size: 1.1rem;
		line-height: 1;
		cursor: pointer;
		padding: 0 0.35rem;
		margin-left: 0.15rem;
		border-radius: 999px;
		flex: 0 0 auto;
	}
	.card-clear:hover { background: #ddd; color: #000; }

	.card-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.card-time { font-weight: 700; font-size: 0.95rem; color: #222; }
	.card-dur  { font-size: 0.85rem; color: #555; }

	.card-route {
		font-size: 0.8rem;
		color: #444;
	}

	.card-legs {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.2rem 0.25rem;
	}
	.card-leg {
		display: inline-flex;
		align-items: center;
		gap: 0.15rem;
	}
	.card-mode {
		font-size: 1.05rem;
		line-height: 1;
		color: #444;
	}
	.card-leg.walk .card-mode { color: #888; }
	.card-leg-dur {
		font-size: 0.72rem;
		color: #666;
		white-space: nowrap;
	}
	.card-ref {
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
	.card-sep { font-size: 0.9rem; color: #ccc; }

	.card-meta {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.3rem;
		font-size: 0.75rem;
		color: #777;
	}

	.card-expand {
		border: none;
		background: transparent;
		width: 100%;
		padding: 0.05rem 0;
		margin-top: 0.05rem;
		cursor: pointer;
		display: flex;
		justify-content: center;
		border-radius: 0.4rem;
		color: #888;
		line-height: 1.2;
	}
	/* No hover background: the whole card is the toggle target now, so the
	   chevron is purely an affordance/indicator — a second hover effect on
	   top of the card's own hover read as redundant. */
	.card-expand-chevron {
		display: inline-block;
		font-size: 0.8rem;
		transition: transform 0.15s ease;
	}
	.card-expand-chevron.flipped { transform: rotate(180deg); }

	.leg-list {
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		border-top: 1px solid #eee;
		padding-top: 0.35rem;
	}
	.leg-item {
		display: flex;
		flex-direction: column;
		align-items: stretch;
		gap: 0.12rem;
		width: 100%;
		text-align: left;
		border: none;
		background: transparent;
		border-radius: 0.4rem;
		padding: 0.35rem 0.4rem;
		font-family: 'Saira', sans-serif;
		font-size: inherit;
		cursor: pointer;
		color: inherit;
	}
	.leg-item:hover,
	.leg-item:focus-visible { background: #f0f0f0; outline: none; }
	/* On a selected card (#f2f2f2) the default hover is nearly
	 * invisible — darken the inner-element hover there. */
	.card.selected .leg-item:hover,
	.card.selected .leg-item:focus-visible { background: #e0e0e0; }
	.leg-item.walk {
		flex-direction: row;
		align-items: center;
		gap: 0.35rem;
		color: #777;
	}
	.leg-walk-dur { font-size: 0.78rem; color: #666; }

	.leg-line-row {
		display: flex;
		align-items: center;
		gap: 0.35rem;
		min-width: 0;
		/* Align under the station-name column (time column + gap). */
		margin-left: 2.8rem;
	}
	.leg-dir {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.78rem;
		color: #444;
	}
	.leg-dur {
		flex: 0 0 auto;
		font-size: 0.78rem;
		color: #555;
		white-space: nowrap;
	}
	.leg-stop-row {
		display: flex;
		align-items: baseline;
		gap: 0.4rem;
		font-size: 0.78rem;
	}
	.leg-time {
		font-weight: 600;
		color: #222;
		width: 2.4rem;
		flex: 0 0 auto;
	}
	.leg-stop-name {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: #333;
	}
	.leg-pf {
		flex: 0 0 auto;
		font-size: 0.72rem;
		color: #666;
		white-space: nowrap;
	}
</style>
