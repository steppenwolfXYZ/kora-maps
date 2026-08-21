<script lang="ts">
	import { untrack } from 'svelte';
	import EndpointInput from './EndpointInput.svelte';
	import TimeSelector from './TimeSelector.svelte';
	import ResultCard from './ResultCard.svelte';
	import { computeCardStates } from './ranking';
	import { routingState } from './state.svelte';
	import { itineraryFingerprint } from './fingerprint';
	import type { Itinerary, Leg } from './types';

	let { onFocusLeg, onEnterMapMode }: {
		onFocusLeg?: (leg: Leg) => void;
		onEnterMapMode?: (it: Itinerary) => void;
	} = $props();

	// Shared-only mode (connection-sharing.md § Shared view) renders just the
	// verified shared connection; ranking badges are suppressed there — a
	// single card comparing against itself would always wear the crown.
	let displayed = $derived(routingState.displayedResults);
	let cardStates = $derived(computeCardStates(displayed));

	let resultsEl: HTMLDivElement | null = $state(null);
	// After a query finishes (loading false→true→false), scroll the
	// selected card into view — arrive-by auto-selects the last result,
	// which sits at the bottom and would otherwise be off-screen. No-op
	// for leave-at (first card is already at the top) and for user-clicked
	// selections (the card is already visible, block:'nearest' won't
	// scroll). loadMore doesn't toggle `loading`, so it never fires here.
	let wasLoading = false;
	$effect(() => {
		const isLoading = routingState.loading;
		if (wasLoading && !isLoading && resultsEl) {
			const fp = untrack(() => routingState.selectedFingerprint);
			if (fp) {
				const results = untrack(() => routingState.displayedResults);
				const idx = results.findIndex((it) => itineraryFingerprint(it) === fp);
				if (idx >= 0) {
					const card = resultsEl.querySelectorAll('.card')[idx] as HTMLElement | undefined;
					card?.scrollIntoView({ block: 'nearest' });
				}
			}
		}
		wasLoading = isLoading;
	});

	// Main routing shell. Replaces the map menu / stop search top-controls
	// while open (Map.svelte decides visibility). Runs a query whenever
	// both endpoints are set and any input changes. Dedup lives in the
	// store (see `lastQueryKey` in state.svelte.ts) so a bare remount —
	// e.g. exiting mobile map mode — doesn't refetch.
	$effect(() => {
		const from = routingState.from;
		const to = routingState.to;
		void routingState.mode;
		void routingState.time;
		void routingState.timeVersion;
		if (!from || !to) return;
		void routingState.runQuery();
	});

	// Local-calendar day key so day-boundary markers respect the viewer's TZ.
	function dayKey(iso: string): string {
		const d = new Date(iso);
		return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
	}
	const dayFmt = new Intl.DateTimeFormat(undefined, {
		weekday: 'short', day: 'numeric', month: 'short'
	});
	function fmtDay(iso: string): string {
		return dayFmt.format(new Date(iso));
	}
	// Reference day for the first result: the requested query time (arrive-by
	// or leave-at), or now if none set. If the first itinerary already sits
	// on a later day, it gets a marker too.
	function baselineIso(): string {
		return routingState.time ?? new Date().toISOString();
	}
</script>

<div class="routing-panel" role="dialog" aria-label="Route planning">
	<div class="rp-head">
		<span class="rp-title">
			<span class="material-symbols-outlined rp-title-icon" aria-hidden="true">directions</span>
			Route
		</span>
		<button
			class="rp-close"
			onclick={() => routingState.closePanel()}
			aria-label="Close route planning"
		>×</button>
	</div>

	<div class="rp-endpoints">
		<div class="rp-inputs">
			<EndpointInput
				label="From"
				endpoint={routingState.from}
				placeholder="Start"
				onChange={(ep) => routingState.setFrom(ep)}
				otherIsCurrent={routingState.to?.type === 'current'}
			/>
			<EndpointInput
				label="To"
				endpoint={routingState.to}
				placeholder="Destination"
				onChange={(ep) => routingState.setTo(ep)}
				otherIsCurrent={routingState.from?.type === 'current'}
			/>
		</div>
		<button
			class="rp-swap"
			onclick={() => routingState.swap()}
			aria-label="Swap start and destination"
		>
			<span class="material-symbols-outlined">swap_vert</span>
		</button>
	</div>

	<div class="rp-when">
		<TimeSelector
			mode={routingState.mode}
			time={routingState.time}
			onMode={(m) => routingState.setMode(m)}
			onTime={(t) => routingState.setTime(t)}
		/>
	</div>

	{#if routingState.hasQueried || routingState.sharedExpired}
	<div class="rp-results-sep" aria-hidden="true"></div>
	<div class="rp-results" bind:this={resultsEl}>
			{#if routingState.sharedExpired}
				<div class="rp-status rp-error">
					This shared connection is no longer available — the timetable
					has likely changed since it was shared.
				</div>
			{/if}
			{#if routingState.loading}
				<div class="rp-status">Searching…</div>
			{:else if routingState.error}
				<div class="rp-status rp-error">{routingState.error}</div>
			{:else if displayed.length === 0}
				{#if routingState.hasQueried}
					<div class="rp-status">No connections found</div>
				{/if}
			{:else}
				{#if routingState.selectionInvalid}
					<div class="rp-status rp-error">
						The saved route is no longer valid. Pick one below.
					</div>
				{/if}
				<button
					type="button"
					class="rp-load-more rp-load-more-top"
					onclick={() => { routingState.exitSharedOnly(); routingState.loadMoreEarlier(); }}
					disabled={!!routingState.loadingMore || routingState.loading}
				>
					<span class="rp-load-more-icon rp-load-more-icon-up material-symbols-outlined" aria-hidden="true">chevron_right</span>
					<span>{routingState.loadingMore === 'earlier' ? 'Loading…' : 'Earlier connections'}</span>
				</button>
				{#each displayed as it, i (i)}
					{@const prevIso = i === 0 ? baselineIso() : displayed[i - 1].startTime}
					{#if i === 0 || dayKey(it.startTime) !== dayKey(prevIso)}
						<div class="rp-day-marker">{fmtDay(it.startTime)}</div>
					{/if}
					<ResultCard
						itinerary={it}
						badge={routingState.sharedOnly ? null : cardStates[i]?.badge ?? null}
						warnings={cardStates[i]?.warnings ?? []}
						{onFocusLeg}
						{onEnterMapMode}
					/>
				{/each}
				<button
					type="button"
					class="rp-load-more rp-load-more-bottom"
					onclick={() => { routingState.exitSharedOnly(); routingState.loadMoreLater(); }}
					disabled={!!routingState.loadingMore || routingState.loading}
				>
					<span class="rp-load-more-icon rp-load-more-icon-down material-symbols-outlined" aria-hidden="true">chevron_right</span>
					<span>{routingState.loadingMore === 'later' ? 'Loading…' : 'Later connections'}</span>
				</button>
			{/if}
		</div>
	{/if}
</div>

<style>
	.routing-panel {
		width: 22rem;
		max-height: calc(100vh - 2rem);
		max-height: calc(100dvh - 2rem);
		background: #ffffff;
		border-radius: 0.9rem;
		box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3);
		padding: 0.7rem 0.85rem 0.85rem;
		font-family: 'Saira', sans-serif;
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		overflow: hidden;
	}
	/* Narrow breakpoint: keep in sync with NARROW_BREAKPOINT in
	   ./layout.ts — the routing panel becomes a full-bleed page. */
	@media (max-width: 699px) {
		.routing-panel {
			width: 100%;
			flex: 1 1 auto;
			min-width: 0;
			max-height: 100vh;
			max-height: 100dvh;
			border-radius: 0;
		}
	}

	.rp-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.rp-title {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		font-size: 0.7rem;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: #666;
	}
	.rp-title-icon {
		font-size: 1rem;
		line-height: 1;
		color: #444;
	}
	.rp-close {
		border: none;
		background: transparent;
		font-size: 1.25rem;
		line-height: 1;
		color: #555;
		padding: 0.15rem 0.4rem;
		border-radius: 999px;
		cursor: pointer;
	}
	.rp-close:hover { background: #eee; color: #000; }

	.rp-endpoints {
		display: flex;
		flex-direction: row;
		align-items: center;
		gap: 0.35rem;
		position: relative;
	}
	.rp-inputs {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		flex: 1 1 auto;
		min-width: 0;
	}
	.rp-swap {
		flex: 0 0 auto;
		border: none;
		background: transparent;
		color: #666;
		padding: 0.25rem 0.35rem;
		border-radius: 999px;
		cursor: pointer;
	}
	.rp-swap :global(.material-symbols-outlined) { font-size: 1.15rem; line-height: 1; }
	.rp-swap:hover { background: #eee; color: #000; }

	.rp-results-sep {
		/* Sits outside the scroll container so it never scrolls — the line
		   stays pinned between the search criteria and the results. As a
		   panel flex child it spans the panel content box, so its edges
		   align with the cards (the scrollbar gutter is carved out only
		   on .rp-results via its negative margin). */
		border-top: 1px solid #eee;
		height: 0;
		/* Tighten the panel gap below so the first card sits where it did
		   when the line was a border-top on .rp-results with padding-top. */
		margin-bottom: -0.25rem;
	}
	.rp-results {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		overflow-y: auto;
		/* Pull the scroll container into the panel's right padding so the
		   overlay scrollbar paints there instead of over the cards, then
		   inset the cards by the same amount so their right edge stays
		   aligned with the panel content box (symmetric with the left).
		   Negative margin + matching padding keeps the card width
		   unchanged. */
		margin-right: -0.75rem;
		padding-right: 0.75rem;
	}
	.rp-status {
		font-size: 0.85rem;
		color: #666;
		padding: 0.35rem 0.15rem;
	}
	.rp-error { color: #a11; }

	.rp-day-marker {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.72rem;
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: #888;
		padding: 0.25rem 0.1rem 0.1rem;
	}
	.rp-day-marker::before,
	.rp-day-marker::after {
		content: '';
		flex: 1 1 auto;
		height: 1px;
		background: #e5e5e5;
	}

	.rp-load-more {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 0.35rem;
		border: 1px solid #ddd;
		background: #f5f5f5;
		color: #222;
		font-family: inherit;
		font-size: 0.8rem;
		font-weight: 600;
		letter-spacing: 0.01em;
		padding: 0.45rem 0.6rem;
		border-radius: 0.5rem;
		cursor: pointer;
		transition: border-color 0.12s, background 0.12s, color 0.12s;
	}
	.rp-load-more-icon {
		font-size: 1.1rem;
		line-height: 1;
	}
	.rp-load-more-icon-up { transform: rotate(-90deg); }
	.rp-load-more-icon-down { transform: rotate(90deg); }
	.rp-load-more:hover:not(:disabled) {
		background: #ebebeb;
		border-color: #bbb;
	}
	.rp-load-more:active:not(:disabled) {
		background: #e0e0e0;
	}
	.rp-load-more:disabled {
		opacity: 0.55;
		cursor: default;
	}
</style>
