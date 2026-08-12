<script lang="ts">
	import type { Itinerary, Leg, LegMode } from './types';
	import { legBadgeColor, loadRouteColorIndex } from './legColor';
	import { itineraryFingerprint } from './fingerprint';
	import { routingState } from './state.svelte';

	interface Props {
		itinerary: Itinerary;
	}

	let { itinerary }: Props = $props();

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

	function legDuration(leg: Leg): number {
		return leg.duration ?? Math.max(0, (new Date(leg.endTime).getTime() - new Date(leg.startTime).getTime()) / 1000);
	}

	function walkSecs(it: Itinerary): number {
		let s = 0;
		for (const l of it.legs) {
			if (l.mode === 'WALK') s += legDuration(l);
		}
		return s;
	}

	function transferCount(it: Itinerary): number {
		if (typeof it.transfers === 'number') return it.transfers;
		let transit = 0;
		for (const l of it.legs) if (l.mode !== 'WALK' && l.mode !== 'BIKE' && l.mode !== 'CAR') transit++;
		return Math.max(0, transit - 1);
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
	<div class="card-head">
		<span class="card-time">{fmtTime(itinerary.startTime)} – {fmtTime(itinerary.endTime)}</span>
		<span class="card-dur">{fmtDuration(itinerary.duration)}</span>
		{#if selected}
			<button
				class="card-clear"
				type="button"
				aria-label="Clear route from map"
				onclick={(e) => { e.stopPropagation(); routingState.dismissSelectedItinerary(); }}
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
		<span>{fmtDuration(walkSecs(itinerary))} walking</span>
	</div>
</div>

<style>
	.card {
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
</style>
