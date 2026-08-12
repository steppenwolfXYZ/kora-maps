<script lang="ts">
	import EndpointInput from './EndpointInput.svelte';
	import TimeSelector from './TimeSelector.svelte';
	import ResultCard from './ResultCard.svelte';
	import { routingState } from './state.svelte';

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
		<EndpointInput
			label="From"
			endpoint={routingState.from}
			placeholder="Start"
			onChange={(ep) => routingState.setFrom(ep)}
		/>
		<button
			class="rp-swap"
			onclick={() => routingState.swap()}
			aria-label="Swap start and destination"
		>
			<span class="material-symbols-outlined">swap_vert</span>
		</button>
		<EndpointInput
			label="To"
			endpoint={routingState.to}
			placeholder="Destination"
			onChange={(ep) => routingState.setTo(ep)}
		/>
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
				{#each routingState.results as it, i (i)}
					<ResultCard itinerary={it} />
				{/each}
			{/if}
		</div>
	{/if}
</div>

<style>
	.routing-panel {
		width: 20rem;
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
		flex-direction: column;
		gap: 0.35rem;
		position: relative;
	}
	.rp-swap {
		align-self: flex-end;
		border: none;
		background: transparent;
		color: #666;
		padding: 0.1rem 0.35rem;
		border-radius: 999px;
		cursor: pointer;
		margin: -0.15rem 0.2rem;
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
</style>
