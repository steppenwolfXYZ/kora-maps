<script lang="ts">
	// Expanded "more options" area of the cycling tab
	// (bicycle-route-options.md § 1, § 2, § 4): the bike type dropdown,
	// the pace ruler (pedal bikes only — an e-bike's flat speed is its
	// cap, not a preference) and the fast ↔ nice ruler. Values live in
	// options.svelte.ts; every change re-runs the query.
	import BikeTypeSelect from './BikeTypeSelect.svelte';
	import RulerSelect from './RulerSelect.svelte';
	import { isMotorBike } from './optionParams';
	import {
		BIKE_PACES, BIKE_ROADS, routingOptions, type BikePace, type BikeRoads, type BikeType
	} from './options.svelte';
	import { routingState } from './state.svelte';

	function setType(t: BikeType) {
		routingOptions.setBikeType(t);
		routingState.optionsChanged();
	}

	function setPace(id: string) {
		routingOptions.setBikePace(id as BikePace);
		routingState.optionsChanged();
	}
	function setRoads(id: string) {
		routingOptions.setBikeRoads(id as BikeRoads);
		routingState.optionsChanged();
	}
</script>

<div class="br">
	<div class="br-group br-type">
		<span class="br-label">Bike</span>
		<BikeTypeSelect value={routingOptions.bikeType} onChange={setType} />
	</div>
	{#if !isMotorBike(routingOptions.bikeType)}
		<div class="br-group">
			<RulerSelect
				label="Riding pace"
				icon="directions_bike"
				stops={BIKE_PACES}
				value={routingOptions.bikePace}
				onChange={setPace}
			/>
		</div>
	{/if}
	<div class="br-group">
		<RulerSelect
			label="Route character"
			icon="route"
			stops={BIKE_ROADS}
			value={routingOptions.bikeRoads}
			onChange={setRoads}
		/>
	</div>
</div>

<style>
	/* Same group-card language as the transit tab's options area. */
	.br {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		padding: 0.15rem 0 0.2rem;
	}
	.br-group {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		background: var(--gray-50);
		border-radius: 0.55rem;
		padding: 0.45rem 0.6rem 0.5rem;
	}
	/* Uppercase micro-title like the rulers', the dropdown beneath. */
	.br-label {
		font-size: 0.62rem;
		font-weight: 600;
		letter-spacing: 0.07em;
		text-transform: uppercase;
		color: var(--anthracite);
	}
	.br-type { gap: 0.25rem; }
</style>
