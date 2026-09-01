<script lang="ts">
	// Expanded "more options" area of the routing panel
	// (routing-options.md): walking-speed ruler, minimize-walking
	// checkbox, transfer-safety ruler. Values live in options.svelte.ts
	// (localStorage-persisted); speed/safety changes re-run the query,
	// minimize-walking only re-ranks the fetched results.
	import RulerSelect from './RulerSelect.svelte';
	import {
		routingOptions, SAFETY_MODES, WALK_SPEED_TIERS,
		type SafetyMode, type WalkSpeedTier
	} from './options.svelte';
	import { routingState } from './state.svelte';

	function setSpeed(id: string) {
		routingOptions.setWalkSpeed(id as WalkSpeedTier);
		routingState.optionsChanged();
	}
	function setSafety(id: string) {
		routingOptions.setSafety(id as SafetyMode);
		routingState.optionsChanged();
	}
	function toggleMinimizeWalking() {
		routingOptions.setMinimizeWalking(!routingOptions.minimizeWalking);
		// A query param since the server-side minwalk work
		// (routing-options.md § Minimize walking) — full re-query, like
		// the rulers.
		routingState.optionsChanged();
	}
</script>

<div class="ro">
	<!-- Soft group cards so belonging reads at a glance: one per topic —
	     walking speed, transfer safety, minimize walking. -->
	<div class="ro-group">
		<RulerSelect
			label="Walking speed"
			icon="directions_walk"
			stops={WALK_SPEED_TIERS}
			value={routingOptions.walkSpeed}
			onChange={setSpeed}
		/>
	</div>
	<div class="ro-group">
		<RulerSelect
			label="Transfer safety"
			icon="transfer_within_a_station"
			stops={SAFETY_MODES}
			value={routingOptions.safety}
			onChange={setSafety}
		/>
	</div>
	<div class="ro-group">
		<!-- Same switch pattern as MapMenu's layer toggles: label + pill
		     switch, gradient when on. -->
		<button
			class="ro-toggle"
			class:active={routingOptions.minimizeWalking}
			onclick={toggleMinimizeWalking}
			aria-pressed={routingOptions.minimizeWalking}
		>
			<span class="ro-toggle-label">Minimize walking</span>
			<span class="switch" aria-hidden="true"></span>
		</button>
	</div>
</div>

<style>
	/* Sits between the leave-at/arrive-by row and the timing row (inside
	   TimeSelector's column). The group cards carry the visual weight,
	   so no hairline frame of its own. */
	.ro {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		padding: 0.15rem 0 0.2rem;
	}
	/* Soft card per topic group — light fill, no border, so the cards
	   read as grouping, not as more controls. */
	.ro-group {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		background: var(--gray-50);
		border-radius: 0.55rem;
		padding: 0.45rem 0.6rem 0.5rem;
	}
	/* Switch row — same pattern as MapMenu's layer toggles: text label,
	   pill switch pinned right, gradient fill when on. */
	.ro-toggle {
		display: flex;
		align-items: center;
		gap: 0.45rem;
		width: fit-content;
		border: none;
		background: transparent;
		font-family: inherit;
		font-size: 0.8rem;
		color: var(--gray-800);
		padding: 0;
		cursor: pointer;
	}
	.ro-toggle:hover .ro-toggle-label {
		color: var(--anthracite);
	}
	.switch {
		flex: 0 0 auto;
		position: relative;
		width: 1.7rem;
		height: 1rem;
		border-radius: var(--radius-pill);
		background: var(--gray-250);
		transition: background 0.15s ease;
	}
	.switch::after {
		content: '';
		position: absolute;
		top: 2px;
		left: 2px;
		width: calc(1rem - 4px);
		height: calc(1rem - 4px);
		border-radius: 50%;
		background: var(--white);
		transition: left 0.15s ease;
	}
	.ro-toggle.active .switch {
		background: var(--gradient-brand);
	}
	.ro-toggle.active .switch::after {
		left: calc(100% - 1rem + 2px);
	}
</style>
