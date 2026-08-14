<script lang="ts">
	import EndpointInput from './EndpointInput.svelte';
	import TimeSelector from './TimeSelector.svelte';
	import ResultCard from './ResultCard.svelte';
	import { computeCardStates } from './ranking';
	import { routingState } from './state.svelte';
	import type { Leg } from './types';

	let { onFocusLeg }: { onFocusLeg?: (leg: Leg) => void } = $props();

	let cardStates = $derived(computeCardStates(routingState.results));

	// Main routing shell. Replaces the map menu / stop search top-controls
	// while open (Map.svelte decides visibility). Runs a query whenever
	// both endpoints are set and any input changes.
	let lastKey = '';
	$effect(() => {
		const from = routingState.from;
		const to = routingState.to;
		const mode = routingState.mode;
		const time = routingState.time;
		if (!from || !to) return;
		const key = JSON.stringify({ from, to, mode, time });
		if (key === lastKey) return;
		lastKey = key;
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
			/>
			<EndpointInput
				label="To"
				endpoint={routingState.to}
				placeholder="Destination"
				onChange={(ep) => routingState.setTo(ep)}
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

	{#if routingState.hasQueried}
		<div class="rp-results">
			{#if routingState.loading}
				<div class="rp-status">Searching…</div>
			{:else if routingState.error}
				<div class="rp-status rp-error">{routingState.error}</div>
			{:else if routingState.results.length === 0}
				<div class="rp-status">No connections found</div>
			{:else}
				{#if routingState.selectionInvalid}
					<div class="rp-status rp-error">
						The saved route is no longer valid. Pick one below.
					</div>
				{/if}
				<button
					type="button"
					class="rp-load-more rp-load-more-top"
					onclick={() => routingState.loadMoreEarlier()}
					disabled={!!routingState.loadingMore || routingState.loading}
				>
					<span class="rp-load-more-icon rp-load-more-icon-up material-symbols-outlined" aria-hidden="true">chevron_right</span>
					<span>{routingState.loadingMore === 'earlier' ? 'Loading…' : 'Earlier connections'}</span>
				</button>
				{#each routingState.results as it, i (i)}
					{@const prevIso = i === 0 ? baselineIso() : routingState.results[i - 1].startTime}
					{#if i === 0 || dayKey(it.startTime) !== dayKey(prevIso)}
						<div class="rp-day-marker">{fmtDay(it.startTime)}</div>
					{/if}
					<ResultCard
						itinerary={it}
						badge={cardStates[i]?.badge ?? null}
						warnings={cardStates[i]?.warnings ?? []}
						{onFocusLeg}
					/>
				{/each}
				<button
					type="button"
					class="rp-load-more rp-load-more-bottom"
					onclick={() => routingState.loadMoreLater()}
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
	@media (max-width: 600px) {
		.routing-panel { width: auto; flex: 1 1 auto; min-width: 0; }
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

	.rp-results {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		overflow-y: auto;
		padding-top: 0.35rem;
		border-top: 1px solid #eee;
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
