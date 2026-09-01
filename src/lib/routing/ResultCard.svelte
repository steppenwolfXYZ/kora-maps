<script lang="ts">
	import { slide } from 'svelte/transition';
	import type { FilledVia, Itinerary, Leg } from './types';
	import { legBadgeColor, loadHfGondolaRoutes, loadRouteColorIndex } from './legColor';
	import {
		assessTransfers, legDuration, transferCount, walkElevation, walkMetres, walkSeconds
	} from './ranking';
	import { stationPlaceId } from './client';
	import type { Badge, TransferAssessment, Warning, WarningKind } from './ranking';
	import {
		badgeTextColor, displayLegs, fmtDistance, fmtDuration, fmtElevation, fmtTime,
		iconFor, isTransitMode
	} from './itineraryFormat';
	import { isNarrow } from './layout';
	import { itineraryFingerprint } from './fingerprint';
	import { rankOptionsFor, routingState } from './state.svelte';
	import { buildSharePayload, createShare } from './share';
	import SharePopup from './SharePopup.svelte';

	interface Props {
		itinerary: Itinerary;
		badge?: Badge | null;
		warnings?: Warning[];
		/** Camera-focus one leg on the map (Map.svelte wires this through). */
		onFocusLeg?: (leg: Leg) => void;
		/** Frame the whole route when entering mobile map mode. */
		onEnterMapMode?: (it: Itinerary) => void;
		/** Reset the camera to the whole-route overview (after a leg focus). */
		onFrameRoute?: (it: Itinerary) => void;
	}

	let { itinerary, badge = null, warnings = [], onFocusLeg, onEnterMapMode, onFrameRoute }: Props = $props();

	function headsign(leg: Leg): string {
		return leg.headsign ?? leg.tripHeadsign ?? '';
	}

	// Primary click (card body), identical on both platforms
	// (routing-map-details-split.md § Result card anatomy): the first
	// click makes the connection the active one (highlight, and on the
	// map where the map is visible); clicking the already-active card
	// opens its details, and closes them again on the next click. Double-
	// clicking a fresh card therefore selects and opens it in one go.
	// Never enters map mode — that stays on the map icon.
	function toggleCard() {
		if (!selected) {
			routingState.selectItinerary(itinerary);
			return;
		}
		// Already active: same as the chevron — open / close the details
		// (which on desktop also resets the camera to the route overview).
		toggleExpandOnly();
	}

	// Chevron-only toggle: open/close details without selecting on the
	// map. Lets users inspect a connection without activating it.
	function toggleExpandOnly() {
		const willExpand = !expanded;
		routingState.toggleExpanded(itinerary);
		if (!isNarrow() && selected) onFrameRoute?.(itinerary);
		if (willExpand) scrollIntoViewSoon();
	}

	// After the slide transition finishes (~400 ms default), smooth-
	// scroll the card into view so the expanded leg list is fully
	// visible. No-op when the card already fits.
	function scrollIntoViewSoon() {
		setTimeout(() => cardEl?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }), 400);
	}

	// Map icon: select on the map without opening/closing any card. On
	// mobile this enters fullscreen map mode; on desktop it just re-aims
	// the overlay at this connection (peek while another card stays open).
	function showOnMap(e: Event) {
		e.stopPropagation();
		const wasSelected = selected;
		routingState.selectItinerary(itinerary);
		if (isNarrow()) {
			routingState.enterMapMode();
			onEnterMapMode?.(itinerary);
		} else if (wasSelected) {
			// Re-clicking the icon of the already-shown connection: the overlay
			// effect won't re-run, so reset the overview framing here (e.g.
			// after a leg focus zoomed into one segment).
			onFrameRoute?.(itinerary);
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
		'very-slow':       'snail',
		'tight-transfer':  'transfer_within_a_station',
		'lucky-transfer':  'sprint'
	};
	// The four transfer tiers escalate glyph-first, colour-second: the
	// transfer glyph (the same one the safety ruler wears, so the warning
	// points back at the setting that governs it) for the two tiers that
	// need no running, the sprint glyph for the two that do; inside each
	// pair yellow is the milder and red the worse. A runner glyph on the
	// mild tiers would read as the long-walk warning at this size.
	function warningIcon(w: Warning): string {
		if (w.kind === 'tight-transfer' && w.severity === 'strong') return 'sprint';
		return WARNING_ICON[w.kind];
	}
	// Sub-minute spares deserve seconds, not a rounded "0 min".
	function fmtSpare(secs: number): string {
		const s = Math.abs(secs);
		return s < 60 ? `${Math.round(s)} s` : fmtDuration(s);
	}
	// Tooltip text incorporates the connection's actual measured value
	// (carried on Warning.value) instead of just naming the threshold tier.
	function warningLabel(w: Warning): string {
		switch (w.kind) {
			case 'long-walk': return `Includes a ${fmtDuration(w.value)} walk`;
			case 'long-wait': return `Includes a ${fmtDuration(w.value)} transfer wait`;
			case 'very-slow': return `${fmtDuration(w.value)} slower than the fastest route`;
			case 'tight-transfer': return w.value < 0
				? `Tight transfer — needs slightly faster walking than your set speed`
				: `Tight transfer — ${fmtSpare(w.value)} to spare at your walking speed`;
			case 'lucky-transfer':
				return 'Transfer only works out if you are lucky — not makeable at your walking speed';
		}
	}
	// Per-transfer marks for the expanded leg list (routing-options.md
	// § Connection warnings): keyed by the boarded transit leg's index.
	const TRANSFER_MARK_LABEL: Record<TransferAssessment['tier'], string> = {
		'tight': 'Tight transfer',
		'very-tight': 'Very tight transfer',
		'extremely-tight': 'Extremely tight transfer',
		'lucky': 'Only if you are lucky'
	};
	const TRANSFER_MARK_ICON: Record<TransferAssessment['tier'], string> = {
		'tight': 'transfer_within_a_station',
		'very-tight': 'transfer_within_a_station',
		'extremely-tight': 'sprint',
		'lucky': 'sprint'
	};
	function transferMarkLabel(a: TransferAssessment): string {
		const base = TRANSFER_MARK_LABEL[a.tier];
		return a.spare >= 0
			? `${base} — ${fmtSpare(a.spare)} to spare at your walking speed`
			: `${base} — ${fmtSpare(a.spare)} short at your walking speed`;
	}
	let hfGondolas = $state<Set<string> | null>(null);
	$effect(() => {
		void loadHfGondolaRoutes().then((s) => { hfGondolas = s; });
	});
	let transferMarks = $derived(new Map(
		assessTransfers(itinerary, { hfGondolaRoutes: hfGondolas })
			.map((a) => [a.legIndex, a])
	));

	// Per-walk-row elevation: shown on long walks only, where the profile
	// is the difference between a stroll and a climb. The summary line
	// stays free of it — it only carries the total as a tooltip on the
	// walked distance, so the row itself doesn't grow.
	const LEG_ELEVATION_MIN_SEC = 10 * 60;
	let walkTotalM = $derived(walkMetres(itinerary));
	let walkElevationLabel = $derived.by(() => {
		const e = walkElevation(itinerary);
		return e ? `${Math.round(e.up)} m ascent · ${Math.round(e.down)} m descent` : undefined;
	});
	function showLegElevation(leg: Leg): boolean {
		return (leg.elevationUp != null || leg.elevationDown != null)
			&& legDuration(leg) > LEG_ELEVATION_MIN_SEC;
	}

	// Via stops of the current query (via-stops.md). A via shows up in the
	// connection in one of two shapes: as a JUNCTION — the traveller
	// alights and boards again, which is where a requested wait lands — or
	// as a PASS-THROUGH, one of a transit leg's intermediate stops, which
	// is what a 0-wait "route through here" via normally produces.
	let queryVias = $derived(routingState.vias
		.filter((v) => v.station !== null)
		.map((v) => ({
			id: stationPlaceId(v.station!),
			name: v.station!.name,
			wait: v.wait
		})));
	let viaById = $derived(new Map(queryVias.map((v) => [v.id, v])));

	function viaFor(place: { parentId?: string; stopId?: string } | undefined) {
		if (!place || viaById.size === 0) return null;
		return viaById.get(place.parentId ?? '') ?? viaById.get(place.stopId ?? '') ?? null;
	}

	interface ViaStay {
		name: string;
		/** Requested minimum, in minutes. */
		wait: number;
		/** What the timetable actually gives, in seconds. */
		actual: number;
	}

	/** Via stays keyed by the index of the transit leg the traveller
	 * alights from. Only junctions produce a stay — a via passed on board
	 * has no time to show. */
	let viaStays = $derived.by(() => {
		const out = new Map<number, ViaStay>();
		if (viaById.size === 0) return out;
		let prevIdx = -1;
		itinerary.legs.forEach((leg, i) => {
			const isTransit = isTransitMode(leg.mode);
			if (!isTransit) return;
			if (prevIdx >= 0) {
				const prev = itinerary.legs[prevIdx];
				const v = viaFor(prev.to);
				if (v) {
					out.set(prevIdx, {
						name: prev.to?.name || v.name,
						wait: v.wait,
						actual: Math.max(0,
							(Date.parse(leg.startTime) - Date.parse(prev.endTime)) / 1000)
					});
				}
			}
			prevIdx = i;
		});
		return out;
	});

	/** Leg indices a via is passed through on board — the marker rides in
	 * the strip after that leg, since there is no stop row to hang it on. */
	let viaPassthroughs = $derived.by(() => {
		const out = new Map<number, string>();
		if (viaById.size === 0) return out;
		itinerary.legs.forEach((leg, i) => {
			if (!isTransitMode(leg.mode)) return;
			if (viaStays.has(i)) return;
			for (const stop of leg.intermediateStops ?? []) {
				const v = viaFor(stop);
				if (v) { out.set(i, stop.name || v.name); return; }
			}
		});
		return out;
	});

	// A change the traveller makes because they wanted to stop there is not
	// a transfer (via-stops.md § Planned dwell) — rankOptionsFor carries
	// the via waits that let transferCount see the difference.
	let transfers = $derived(transferCount(itinerary, rankOptionsFor()));

	/** Marker text for the collapsed legs strip: the requested wait when
	 * there is one, otherwise just the pin. */
	function viaChipLabel(idx: number): string | null {
		const stay = viaStays.get(idx);
		if (stay) return stay.wait > 0 ? fmtDuration(stay.wait * 60) : '';
		return viaPassthroughs.has(idx) ? '' : null;
	}

	// Card title (transit-routing.md § Results): the ride's own boarding /
	// alighting times are what a glance is looking for, so they carry the
	// large type and the station names ride along small. The door-to-door
	// times demote to the "leave … · there …" line above. The title row is
	// collapsed-only: an expanded card shows the same stops in full detail
	// in its leg list, so keeping it would print them twice. A walk-only
	// itinerary has no ride to name — it drops the title row entirely and
	// puts its own endpoint times in the head line instead.
	let endpoints = $derived(transitEndpoints(itinerary));

	let fingerprint = $derived(itineraryFingerprint(itinerary));
	let selected = $derived(routingState.selectedFingerprint === fingerprint);
	let expanded = $derived(routingState.expandedFingerprint === fingerprint);
	let firstTransitIdx = $derived(itinerary.legs.findIndex((l) => isTransitMode(l.mode)));
	let lastTransitIdx = $derived(itinerary.legs.findLastIndex((l) => isTransitMode(l.mode)));

	let cardEl: HTMLElement | null = $state(null);

	let colorIndex = $state<Map<string, string> | null>(null);
	$effect(() => {
		let cancelled = false;
		loadRouteColorIndex().then((m) => { if (!cancelled) colorIndex = m; });
		return () => { cancelled = true; };
	});

	// Share button (connection-sharing.md § Share button): create a
	// server-stored share of this connection, then open the share dialog
	// (SharePopup) with the link, a copy button, and the native share sheet
	// where the browser offers one. The button shows an exclamation mark
	// for a moment when creation fails.
	let shareState = $state<'idle' | 'busy' | 'error'>('idle');
	let shareUrl = $state<string | null>(null);
	let shareAnchor: DOMRect | null = $state(null);
	let shareResetTimer: ReturnType<typeof setTimeout> | null = null;

	async function shareConnection(e: Event) {
		e.stopPropagation();
		if (shareState === 'busy') return;
		const from = routingState.from;
		const to = routingState.to;
		if (!from || !to) return;
		// Anchor for the speech bubble — captured now; the layout doesn't
		// shift while the share is being created.
		shareAnchor = (e.currentTarget as HTMLElement).getBoundingClientRect();
		shareState = 'busy';
		if (shareResetTimer) clearTimeout(shareResetTimer);
		try {
			const legColors = itinerary.legs.map((leg) => legBadgeColor(colorIndex, leg));
			// The via chain travels with the share — the re-verification
			// query has to repeat it or a via-forced connection reads as
			// expired (via-stops.md § Persistence and sharing).
			const { url } = await createShare(
				buildSharePayload(itinerary, from, to, legColors,
					routingState.vias.filter((v) => v.station !== null) as FilledVia[])
			);
			shareUrl = url;
			shareState = 'idle';
		} catch (err) {
			console.error('[share] failed:', err);
			shareState = 'error';
			shareResetTimer = setTimeout(() => { shareState = 'idle'; }, 2500);
		}
	}

	const SHARE_TITLE: Record<typeof shareState, string> = {
		idle: 'Share this connection',
		busy: 'Creating share link…',
		error: 'Sharing failed'
	};

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

{#snippet transferWarn(mark: TransferAssessment)}
	<span
		class="leg-transfer-warn leg-transfer-warn-{mark.tier}"
		title={transferMarkLabel(mark)}
		aria-label={transferMarkLabel(mark)}
	>
		<span class="material-symbols-outlined" aria-hidden="true"
		>{TRANSFER_MARK_ICON[mark.tier]}</span>
		<span class="leg-transfer-warn-text">{mark.spare >= 0 ? fmtSpare(mark.spare) : `−${fmtSpare(mark.spare)}`}</span>
	</span>
{/snippet}

<div
	bind:this={cardEl}
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
	<div class="card-top">
		{#if warnings.length}
			<span class="card-warnings">
				{#each warnings as w}
					<span
					class="card-warning card-warning-{w.severity} card-warning-kind-{w.kind}"
					title={warningLabel(w)}
					aria-label={warningLabel(w)}
					>
						<span class="material-symbols-outlined" aria-hidden="true">{warningIcon(w)}</span>
					</span>
				{/each}
			</span>
		{/if}
		<div class="card-top-body">
			<div class="card-head">
				{#if endpoints}
					<span class="card-time">leave <strong>{fmtTime(itinerary.startTime)}</strong> · there <strong>{fmtTime(itinerary.endTime)}</strong></span>
				{:else}
					<!-- Walk-only: no ride to name, so the big times ARE the head
					     line — a separate title row below would leave the head
					     holding nothing but the duration. -->
					<span class="card-walk-times">
						<span class="cr-time">{fmtTime(itinerary.startTime)}</span>
						<span class="cr-arrow" aria-hidden="true">→</span>
						<span class="cr-time">{fmtTime(itinerary.endTime)}</span>
					</span>
				{/if}
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
			{#if endpoints && !expanded}
				<div class="card-route" transition:slide>
					<span class="cr-stop">
						<span class="cr-time">{fmtTime(endpoints.fromTime)}</span>
						{#if endpoints.fromName}<span class="cr-name">{endpoints.fromName}</span>{/if}
					</span>
					<span class="cr-arrow" aria-hidden="true">→</span>
					<span class="cr-stop">
						<span class="cr-time">{fmtTime(endpoints.toTime)}</span>
						{#if endpoints.toName}<span class="cr-name">{endpoints.toName}</span>{/if}
					</span>
				</div>
			{/if}
		</div>
	</div>
	{#if !expanded}
		<div class="card-summary" transition:slide>
			<div class="card-legs">
				{#each displayLegs(itinerary) as { leg, dur, isWalk, index }, i}
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
					{#if viaChipLabel(index) !== null}
						{@const stay = viaStays.get(index)}
						<span class="card-sep material-symbols-outlined" aria-hidden="true">chevron_right</span>
						<span
							class="card-via"
							class:card-via-wait={!!stay && stay.wait > 0}
							title={stay
								? (stay.wait > 0
									? `Stop at ${stay.name} — ${fmtDuration(stay.wait * 60)} planned, ${fmtDuration(stay.actual)} actual`
									: `Change at ${stay.name} (your stop)`)
								: `Passes through ${viaPassthroughs.get(index)}`}
						>
							<span class="material-symbols-outlined" aria-hidden="true">location_on</span>
							{#if viaChipLabel(index)}<span>{viaChipLabel(index)}</span>{/if}
						</span>
					{/if}
				{/each}
			</div>
		</div>
	{/if}
	<div class="card-meta">
		<span class="card-meta-text">
			{transfers} transfer{transfers === 1 ? '' : 's'}
			· <strong>{fmtDuration(walkSeconds(itinerary))}</strong>{#if walkTotalM > 0}{' '}<span
				class="card-meta-dist"
				title={walkElevationLabel}
			>{fmtDistance(walkTotalM)}</span>{/if} walking
		</span>
		<span class="card-actions">
			<button
				class="card-share"
				class:share-error={shareState === 'error'}
				type="button"
				title={SHARE_TITLE[shareState]}
				aria-label={SHARE_TITLE[shareState]}
				disabled={shareState === 'busy'}
				onclick={shareConnection}
			>
				{#if shareState === 'error'}
					<span class="card-share-error-mark" aria-hidden="true">!</span>
				{:else}
					<span class="material-symbols-outlined" aria-hidden="true">share</span>
				{/if}
			</button>
			<button
				class="card-map"
				type="button"
				title="Show on map"
				aria-label="Show on map"
				onclick={showOnMap}
			>
				<span class="material-symbols-outlined" aria-hidden="true">map</span>
			</button>
		</span>
	</div>
	{#if expanded}
		<div class="leg-list" transition:slide>
			{#each itinerary.legs as leg, i}
				{#if isTransitMode(leg.mode)}
					<button class="leg-item" type="button" onclick={(e) => focusLeg(e, leg)}>
						<span class="leg-stop-row" class:leg-stop-end={i === firstTransitIdx}>
							<span class="leg-time">{fmtTime(leg.startTime)}</span>
							<span class="leg-stop-name">{leg.from?.name ?? ''}</span>
							{#if leg.from?.track}<span class="leg-pf">Pl. {leg.from.track}</span>{/if}
							{#if transferMarks.has(i) && itinerary.legs[i - 1]?.mode !== 'WALK'}
								<!-- Fallback only: normally the chip renders on the
								     transfer walk row above; without one (direct
								     interline) it stays on the boarding row. -->
								{@render transferWarn(transferMarks.get(i)!)}
							{/if}
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
						<span class="leg-stop-row" class:leg-stop-end={i === lastTransitIdx}>
							<span class="leg-time">{fmtTime(leg.endTime)}</span>
							<span class="leg-stop-name">{leg.to?.name ?? ''}</span>
							{#if leg.to?.track}<span class="leg-pf">Pl. {leg.to.track}</span>{/if}
						</span>
					</button>
					{#if viaStays.has(i)}
						{@const stay = viaStays.get(i)!}
						<!-- The stay the traveller asked for, kept visually apart
						     from a transfer wait: this time is the point of the
						     trip, not a cost (via-stops.md § Result display). -->
						<div class="leg-via">
							<span class="material-symbols-outlined leg-via-icon" aria-hidden="true">location_on</span>
							<span class="leg-via-text">
								Your stop at <strong>{stay.name}</strong>
								— <strong>{fmtDuration(stay.actual)}</strong>
								{#if stay.wait > 0}
									<span class="leg-via-req">({fmtDuration(stay.wait * 60)} asked for)</span>
								{/if}
							</span>
						</div>
					{:else if viaPassthroughs.has(i)}
						<!-- A via passed on board: no stop row exists for it in the
						     leg list, so the ride names it here instead. -->
						<div class="leg-via leg-via-pass">
							<span class="material-symbols-outlined leg-via-icon" aria-hidden="true">location_on</span>
							<span class="leg-via-text">
								Passes through <strong>{viaPassthroughs.get(i)}</strong>
							</span>
						</div>
					{/if}
				{:else}
					<button class="leg-item walk" type="button" onclick={(e) => focusLeg(e, leg)}>
						<span class="card-mode material-symbols-outlined" aria-hidden="true">{iconFor(leg.mode)}</span>
						<span class="leg-walk-dur"><strong>{fmtDuration(legDuration(leg))}</strong>{#if leg.distance != null}{' '}{fmtDistance(leg.distance)}{/if}{#if showLegElevation(leg)}{' '}({fmtElevation(leg.elevationUp ?? 0, leg.elevationDown ?? 0)}){/if}</span>
						{#if transferMarks.has(i + 1)}
							<!-- The tightness belongs to the transfer itself, so the
							     chip sits on the transfer walk row, not on the
							     boarding row below (where it would collide with the
							     platform info). -->
							{@render transferWarn(transferMarks.get(i + 1)!)}
						{/if}
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
		onclick={(e) => { e.stopPropagation(); toggleExpandOnly(); }}
	><span class="card-expand-chevron" class:flipped={expanded}>▾</span></button>
	{#if shareUrl && shareAnchor}
		<SharePopup url={shareUrl} anchor={shareAnchor} onClose={() => { shareUrl = null; }} />
	{/if}
</div>

<style>
	.card {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		padding: 0.55rem 0.7rem;
		background: var(--white);
		border: 1px solid var(--gray-100);
		border-radius: 0.6rem;
		font-family: var(--font-ui);
		text-align: left;
		width: 100%;
		cursor: pointer;
		color: inherit;
		transition: border-color 0.12s, background 0.12s, box-shadow 0.12s;
	}

	.card-actions {
		flex: 0 0 auto;
		display: inline-flex;
		align-items: center;
		gap: 0.15rem;
		margin: -0.3rem -0.15rem -0.3rem 0;
	}

	.card-map,
	.card-share {
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

	/* Share stays monochrome next to the red map accent. */
	.card-share :global(.material-symbols-outlined) {
		font-size: 1.1rem;
		line-height: 1;
		color: var(--gray-600);
	}
	.card-share:hover { background: var(--gray-100); }
	.card-share:disabled { cursor: default; opacity: 0.6; }
	.card-share-error-mark {
		font-size: 1rem;
		font-weight: 800;
		line-height: 1;
		color: var(--warn);
	}
	/* Map glyph in the brand red — the one coloured accent on the
	   otherwise monochrome card. */
	.card-map :global(.material-symbols-outlined) {
		font-size: 1.25rem;
		line-height: 1;
		color: var(--brand);
	}
	.card-map:hover { background: #f3e2e5; }
	/* This card is the one on the map: invert to a red disc with a white
	   glyph so the active state reads at a glance. Desktop only — on
	   mobile the map is never visible while the list is, so the icon
	   never shows the active state there. */
	@media (min-width: 700px) {
		.card.selected .card-map { background: var(--brand); }
		.card.selected .card-map :global(.material-symbols-outlined) { color: var(--white); }
		.card.selected .card-map:hover { background: var(--brand-hover); }
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
		border-radius: var(--radius-pill);
		border: 1px solid var(--gray-100);
		background: var(--white);
		box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
		color: var(--gray-700);
		z-index: 1;
	}
	.card-badge :global(.material-symbols-outlined) { font-size: 0.95rem; line-height: 1; }
	.card-badge-best { color: #b58a00; border-color: #e2c26a; background: #fff8e1; }
	.card-badge-good { color: #2f7a2f; border-color: #cde7cd; background: #f1faf1; }
	.card-badge-bad  { color: #a33; border-color: #eecdcd; background: #fbf1f1; }

	/* Warning marks are a COLUMN down the card's left edge; the head line
	   and the title row sit in their own column beside it, so both are
	   indented by exactly the same amount and stay flush with each other.
	   Extra warnings grow downwards instead of shoving the times right. */
	.card-top {
		display: flex;
		align-items: flex-start;
		gap: 0.35rem;
	}
	.card-top-body {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		flex: 1 1 auto;
		min-width: 0;
	}
	.card-warnings {
		display: inline-flex;
		flex-direction: column;
		align-items: center;
		gap: 0.15rem;
		padding-top: 0.1rem;
		flex: 0 0 auto;
	}
	.card-warning {
		display: inline-flex;
		align-items: center;
		justify-content: center;
	}
	.card-warning :global(.material-symbols-outlined) {
		font-size: 1rem;
		line-height: 1;
	}
	/* standard: plain red icon */
	.card-warning-standard { color: var(--warn); }
	/* medium / strong: white icon inside a coloured circle */
	.card-warning-medium,
	.card-warning-strong {
		width: 1.2rem;
		height: 1.2rem;
		border-radius: var(--radius-pill);
		color: var(--white);
	}
	.card-warning-medium { background: #d9a400; }
	.card-warning-strong { background: var(--warn); }
	.card-warning-medium :global(.material-symbols-outlined),
	.card-warning-strong :global(.material-symbols-outlined) { font-size: 0.85rem; }
	/* All four transfer tiers are white-on-colour discs, escalating
	   glyph-first (transfer → sprint) and colour-second (yellow → red)
	   inside each glyph pair — see warningIcon() above. */
	.card-warning-kind-tight-transfer,
	.card-warning-kind-lucky-transfer {
		width: 1.2rem;
		height: 1.2rem;
		border-radius: var(--radius-pill);
		color: var(--white);
	}
	.card-warning-kind-tight-transfer :global(.material-symbols-outlined),
	.card-warning-kind-lucky-transfer :global(.material-symbols-outlined) { font-size: 0.85rem; }
	.card-warning-kind-tight-transfer.card-warning-standard,
	.card-warning-kind-tight-transfer.card-warning-strong { background: #d9a400; }
	.card-warning-kind-tight-transfer.card-warning-medium,
	.card-warning-kind-lucky-transfer { background: var(--warn); }
	.card:hover { border-color: var(--gray-250); background: #fafafa; }
	.card.selected {
		border-color: transparent;
		background: var(--gray-75);
	}
	/* Brand-gradient strip along the selected card's left edge. Painted
	   before the ::after ring so the border stays on top of it. */
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
	/* The selected border as an overlay ring ABOVE the card content
	   (a real border paints below positioned children, so the strip
	   would overlap it). Inset -1px covers the card's own 1px border
	   slot, keeping the outer geometry identical to the unselected
	   card. */
	.card.selected::after {
		content: '';
		position: absolute;
		inset: -1px;
		border: 2px solid var(--gray-900);
		border-radius: 0.6rem;
		pointer-events: none;
	}
	.card + :global(.card) { margin-top: 0.4rem; }

	.card-clear {
		border: none;
		background: transparent;
		color: var(--gray-700);
		font-size: 1.1rem;
		line-height: 1;
		cursor: pointer;
		padding: 0 0.35rem;
		margin-left: 0.15rem;
		border-radius: var(--radius-pill);
		flex: 0 0 auto;
	}
	.card-clear:hover { background: var(--gray-200); color: var(--black); }

	.card-head {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
	}
	.card-time { font-size: 0.75rem; color: var(--gray-600); flex: 0 0 auto; }
	.card-time strong { font-weight: 700; color: var(--gray-800); }
	.card-walk-times {
		display: inline-flex;
		align-items: baseline;
		gap: 0.35rem;
		flex: 0 0 auto;
	}
	.card-dur  { font-size: 0.85rem; color: var(--gray-600); flex: 0 0 auto; margin-left: auto; }

	.card-summary {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	/* Card title. Times large and bold, station names small alongside —
	   the pair wraps as a unit so a long name never splits off its time. */
	.card-route {
		display: flex;
		align-items: baseline;
		gap: 0 0.4rem;
		min-width: 0;
		/* Separates the title from the line-badge strip below; the card's
		   own 0.25rem gap alone read as cramped. */
		margin-bottom: 0.2rem;
	}
	/* Time over name, so the two stops sit side by side instead of the
	   pair wrapping onto a second line. A column flex item takes its
	   baseline from its first line, so the arrow still lines up with the
	   times. */
	/* flex: 1 1 0 — both stops claim the same width, so the arrival column
	   starts at a fixed x on every card (departure-board alignment) and a
	   long name ellipsizes instead of stealing the other stop's room. */
	.cr-stop {
		display: inline-flex;
		flex-direction: column;
		gap: 0.05rem;
		min-width: 0;
		flex: 1 1 0;
	}
	.cr-time {
		font-size: 0.95rem;
		font-weight: 700;
		line-height: 1.15;
		color: var(--gray-850);
		flex: 0 0 auto;
	}
	.cr-name {
		font-size: 0.78rem;
		line-height: 1.15;
		color: var(--gray-600);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.cr-arrow {
		font-size: 0.9rem;
		color: var(--gray-400);
		flex: 0 0 auto;
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
		color: var(--gray-700);
	}
	.card-leg.walk .card-mode { color: var(--gray-400); }
	.card-leg-dur {
		font-size: 0.72rem;
		color: var(--gray-500);
		white-space: nowrap;
	}
	.card-ref {
		display: inline-block;
		padding: 1px 5px;
		border-radius: 3px;
		font-size: 0.72rem;
		font-weight: 800;
		letter-spacing: 0.02em;
		background: var(--gray-300);
		color: var(--white);
		white-space: nowrap;
	}
	.card-sep { font-size: 0.9rem; color: var(--gray-250); }

	/* Via marker in the collapsed strip (via-stops.md § Result display).
	   A pass-through via is a quiet pin; one the traveller waits at gets
	   the anthracite pill with its requested minutes, so a glance tells
	   the errand stop from a corridor stop. */
	.card-via {
		display: inline-flex;
		align-items: center;
		gap: 0.1rem;
		font-size: 0.7rem;
		font-weight: 600;
		color: var(--gray-500);
		white-space: nowrap;
	}
	.card-via :global(.material-symbols-outlined) {
		font-size: 0.95rem;
		line-height: 1;
	}
	.card-via-wait {
		background: var(--anthracite);
		color: var(--white);
		padding: 1px 5px 1px 3px;
		border-radius: var(--radius-pill);
	}

	/* The via stay row in the expanded leg list. Deliberately not styled
	   like a transfer wait: a left gradient rule marks it as a chosen part
	   of the journey rather than a cost (via-stops.md § Result display). */
	.leg-via {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		padding: 0.3rem 0.45rem 0.3rem calc(0.45rem + 3px);
		margin: 0.15rem 0;
		/* Layered background, not a border-image: the 3px gradient strip
		   then follows the box exactly (same construction as the selected
		   connection card's left strip — ux-guidelines.md). */
		background:
			var(--gradient-brand) left / 3px 100% no-repeat,
			var(--gray-50);
		border-radius: 0.3rem;
		font-size: 0.8rem;
		color: var(--gray-850);
	}
	/* A via merely passed on board carries no time of its own — quieter. */
	.leg-via-pass {
		color: var(--gray-600);
	}
	.leg-via-icon {
		font-size: 1rem;
		line-height: 1;
		color: var(--anthracite);
		flex: 0 0 auto;
	}
	.leg-via-text {
		min-width: 0;
	}
	.leg-via-req {
		color: var(--gray-500);
	}

	/* Walking time / distance in the meta row read like the walk rows of
	   the leg list: duration bold and a shade darker, distance plain.
	   The distance carries the ascent / descent tooltip. */
	.card-meta-text strong { font-weight: 600; color: var(--gray-700); }
	.card-meta-dist { cursor: help; }

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
		color: var(--gray-400);
		line-height: 1.2;
	}
	.card-expand-chevron {
		display: inline-block;
		font-size: 0.8rem;
		transition: transform 0.15s ease;
	}
	.card-expand-chevron.flipped { transform: rotate(180deg); }
	/* Hover signals the chevron is a separate control: opening/closing
	   the card without activating it on the map (click stops propagation,
	   so the card's own click handler never fires). */
	.card-expand:hover { background: #f0f0f0; color: var(--gray-700); }
	/* On a selected card (var(--gray-75)) the default hover is nearly
	   invisible — darken it there, matching the leg-item hover. */
	.card.selected .card-expand:hover { background: #e0e0e0; }

	.leg-list {
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		border-top: 1px solid var(--gray-100);
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
		font-family: var(--font-ui);
		font-size: inherit;
		cursor: pointer;
		color: inherit;
	}
	.leg-item:hover,
	.leg-item:focus-visible { background: #f0f0f0; outline: none; }
	/* On a selected card (var(--gray-75)) the default hover is nearly
	 * invisible — darken the inner-element hover there. */
	.card.selected .leg-item:hover,
	.card.selected .leg-item:focus-visible { background: #e0e0e0; }
	.leg-item.walk {
		flex-direction: row;
		align-items: center;
		gap: 0.35rem;
		color: #777;
	}
	.leg-walk-dur { font-size: 0.78rem; color: var(--gray-500); }
	.leg-walk-dur strong { font-weight: 600; color: var(--gray-700); }

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
		color: var(--gray-700);
	}
	.leg-dur {
		flex: 0 0 auto;
		font-size: 0.78rem;
		color: var(--gray-600);
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
		color: var(--gray-850);
		width: 2.4rem;
		flex: 0 0 auto;
	}
	.leg-stop-name {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--gray-800);
	}
	.leg-stop-end .leg-stop-name { font-weight: 700; color: #111; }
	.leg-stop-end .leg-time { font-weight: 700; color: #111; }
	.leg-pf {
		flex: 0 0 auto;
		font-size: 0.72rem;
		color: var(--gray-500);
		white-space: nowrap;
	}

	/* Tight-transfer mark on the boarding row (routing-options.md):
	   compact icon + spare-time chip. Same escalation as the card badge —
	   glyph says whether you must run, colour says how bad it is inside
	   that pair. */
	.leg-transfer-warn {
		flex: 0 0 auto;
		display: inline-flex;
		align-items: center;
		gap: 0.15rem;
		align-self: center;
		font-size: 0.68rem;
		font-weight: 600;
		line-height: 1;
		padding: 0.12rem 0.3rem;
		border-radius: var(--radius-pill);
		white-space: nowrap;
	}
	.leg-transfer-warn :global(.material-symbols-outlined) {
		font-size: 0.85rem;
		line-height: 1;
	}
	.leg-transfer-warn-tight,
	.leg-transfer-warn-very-tight,
	.leg-transfer-warn-extremely-tight,
	.leg-transfer-warn-lucky { color: var(--white); }
	.leg-transfer-warn-tight,
	.leg-transfer-warn-extremely-tight { background: #d9a400; }
	.leg-transfer-warn-very-tight,
	.leg-transfer-warn-lucky { background: var(--warn); }
</style>
