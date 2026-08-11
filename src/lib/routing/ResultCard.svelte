<script lang="ts">
	import type { Itinerary, LegMode } from './types';
	import { legBadgeColor, loadRouteColorIndex } from './legColor';

	interface Props {
		itinerary: Itinerary;
	}

	let { itinerary }: Props = $props();

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

	function walkSecs(it: Itinerary): number {
		let s = 0;
		for (const l of it.legs) {
			if (l.mode === 'WALK') {
				const dur = l.duration ?? Math.max(0, (new Date(l.endTime).getTime() - new Date(l.startTime).getTime()) / 1000);
				s += dur;
			}
		}
		return s;
	}

	function transferCount(it: Itinerary): number {
		if (typeof it.transfers === 'number') return it.transfers;
		let transit = 0;
		for (const l of it.legs) if (l.mode !== 'WALK' && l.mode !== 'BIKE' && l.mode !== 'CAR') transit++;
		return Math.max(0, transit - 1);
	}

	function iconFor(mode: LegMode): string {
		return MODE_ICON[mode] ?? 'directions_transit';
	}
</script>

<article class="card">
	<div class="card-head">
		<span class="card-time">{fmtTime(itinerary.startTime)} – {fmtTime(itinerary.endTime)}</span>
		<span class="card-dur">{fmtDuration(itinerary.duration)}</span>
	</div>
	<div class="card-legs">
		{#each itinerary.legs as leg, i}
			{#if i > 0}<span class="card-sep material-symbols-outlined" aria-hidden="true">chevron_right</span>{/if}
			<span class="card-leg" class:walk={leg.mode === 'WALK'}>
				<span class="card-mode material-symbols-outlined" aria-hidden="true">{iconFor(leg.mode)}</span>
				{#if leg.mode !== 'WALK' && leg.routeShortName}
					{@const bg = legBadgeColor(colorIndex, leg)}
					<span
						class="card-ref"
						style="background:{bg};color:{badgeTextColor(bg)}"
					>{leg.routeShortName}</span>
				{/if}
			</span>
		{/each}
	</div>
	<div class="card-meta">
		<span>{transferCount(itinerary)} transfer{transferCount(itinerary) === 1 ? '' : 's'}</span>
		<span>·</span>
		<span>{fmtDuration(walkSecs(itinerary))} walking</span>
	</div>
</article>

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
	}
	.card + :global(.card) { margin-top: 0.4rem; }

	.card-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.card-time { font-weight: 700; font-size: 0.95rem; color: #222; }
	.card-dur  { font-size: 0.85rem; color: #555; }

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
