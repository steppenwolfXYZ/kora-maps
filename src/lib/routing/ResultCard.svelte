<script lang="ts">
	import { slide } from 'svelte/transition';
	import type { Itinerary, Leg, LegMode } from './types';
	import { legBadgeColor, loadRouteColorIndex } from './legColor';
	import { legDuration, transferCount, walkSeconds } from './ranking';
	import type { Badge, Warning, WarningKind, WarningSeverity } from './ranking';
	import { itineraryFingerprint } from './fingerprint';
	import { routingState } from './state.svelte';

	interface Props {
		itinerary: Itinerary;
		badge?: Badge | null;
		warnings?: Warning[];
		/** Camera-focus one leg on the map (Map.svelte wires this through). */
		onFocusLeg?: (leg: Leg) => void;
	}

	let { itinerary, badge = null, warnings = [], onFocusLeg }: Props = $props();

	// Expanded leg detail (chevron at the card bottom). Per-card local
	// state — every result card expands independently.
	let expanded = $state(false);

	function isTransitMode(mode: LegMode): boolean {
		return mode !== 'WALK' && mode !== 'BIKE' && mode !== 'CAR';
	}

	function headsign(leg: Leg): string {
		return leg.headsign ?? leg.tripHeadsign ?? '';
	}

	function toggleExpand(e: Event) {
		e.stopPropagation();
		expanded = !expanded;
	}

	// Clicking a leg row focuses it on the map. If the card isn't on the
	// map yet, select it first so the overlay is there to look at.
	function focusLeg(e: Event, leg: Leg) {
		e.stopPropagation();
		if (!selected) routingState.selectItinerary(itinerary);
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

	let colorIndex = $state<Map<string, string> | null>(null);
	$effect(() => {
		let cancelled = false;
		loadRouteColorIndex().then((m) => { if (!cancelled) colorIndex = m; });
		return () => { cancelled = true; };
	});

	function badgeTextColor(hex: string): string {
		const h = hex.replace(/^#/, '');
		const r = parseInt(h.slice(0, 2), 16);
		const g = parseInt(h.slice(2, 4), 16);
		const b = parseInt(h.slice(4, 6), 16);
		const lum = r * 0.299 + g * 0.587 + b * 0.114;
		return lum > 140 ? '#000' : '#fff';
	}

	// Map MOTIS mode strings to Material Symbols icon names — parallel to
	// StopSearch's MODE_ICON.
	const MODE_ICON: Record<string, string> = {
		WALK:          'directions_walk',
		BIKE:          'directions_bike',
		CAR:           'directions_car',
		TRANSIT:       'directions_transit',
		TRAM:          'tram',
		SUBWAY:        'subway',
		METRO:         'subway',
		RAIL:          'train',
		HIGHSPEED_RAIL:'train',
		LONG_DISTANCE: 'train',
		NIGHT_RAIL:    'train',
		REGIONAL_RAIL: 'train',
		REGIONAL_FAST_RAIL: 'train',
		BUS:           'directions_bus',
		COACH:         'directions_bus',
		FERRY:         'directions_boat',
		CABLE_CAR:     'gondola_lift',
		GONDOLA:       'gondola_lift',
		FUNICULAR:     'gondola_lift',
		AIRPLANE:      'flight'
	};

	function fmtTime(iso: string): string {
		const d = new Date(iso);
		return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
	}

	function fmtDuration(secs: number): string {
		const m = Math.round(secs / 60);
		if (m < 60) return `${m} min`;
		const h = Math.floor(m / 60);
		const rem = m % 60;
		return rem ? `${h} h ${rem} min` : `${h} h`;
	}

	// Legs to render in the strip: short inter-transit transfer walks (≤ 6 min
	// between two transit rides) are dropped entirely; first/last-mile walks
	// and longer inter-transit walks are kept, always with their minutes.
	function displayLegs(it: Itinerary): { leg: Leg; dur: number; isWalk: boolean }[] {
		const transitIdx = it.legs
			.map((l, i) => (l.mode !== 'WALK' && l.mode !== 'BIKE' && l.mode !== 'CAR' ? i : -1))
			.filter((i) => i >= 0);
		const firstT = transitIdx[0] ?? -1;
		const lastT = transitIdx[transitIdx.length - 1] ?? -1;
		const out: { leg: Leg; dur: number; isWalk: boolean }[] = [];
		it.legs.forEach((leg, i) => {
			const dur = legDuration(leg);
			const isWalk = leg.mode === 'WALK';
			if (isWalk && i > firstT && i < lastT && dur <= 6 * 60) return;
			out.push({ leg, dur, isWalk });
		});
		return out;
	}

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

	function iconFor(mode: LegMode): string {
		return MODE_ICON[mode] ?? 'directions_transit';
	}
</script>

<div
	class="card"
	class:selected
	role="button"
	tabindex="0"
	aria-pressed={selected}
	onclick={() => routingState.selectItinerary(itinerary)}
	onkeydown={(e) => {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			routingState.selectItinerary(itinerary);
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
					expanded = false;
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
		<span>{transferCount(itinerary)} transfer{transferCount(itinerary) === 1 ? '' : 's'}</span>
		<span>·</span>
		<span>{fmtDuration(walkSeconds(itinerary))} walking</span>
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
		onclick={toggleExpand}
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
	.card-expand:hover { background: #f0f0f0; color: #333; }
	.card.selected .card-expand:hover { background: #e0e0e0; }
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
