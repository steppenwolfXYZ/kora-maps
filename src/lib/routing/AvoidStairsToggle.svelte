<script lang="ts">
	// The always-visible cycling option (bicycle-route-options.md § 5,
	// § 6): the avoid-stairs switch, living on the cycling tab's control
	// row beside the More-options button. Value in options.svelte.ts
	// (localStorage-persisted); a change re-runs the query.
	import { routingOptions } from './options.svelte';
	import { routingState } from './state.svelte';

	function toggleStairs() {
		routingOptions.setAvoidStairs(!routingOptions.avoidStairs);
		routingState.optionsChanged();
	}
</script>

<!-- Same switch pattern as the transit tab's minimize-walking toggle. -->
<button
	class="as-toggle"
	class:active={routingOptions.avoidStairs}
	onclick={toggleStairs}
	aria-pressed={routingOptions.avoidStairs}
	title="Never route over stairs"
>
	<span class="material-symbols-outlined as-icon" aria-hidden="true">stairs</span>
	<span class="as-label">Avoid stairs</span>
	<span class="switch" aria-hidden="true"></span>
</button>

<style>
	.as-toggle {
		display: flex;
		align-items: center;
		gap: 0.35rem;
		flex: 0 0 auto;
		min-height: var(--ts-row-h, 2rem);
		border: none;
		background: transparent;
		font-family: inherit;
		font-size: 0.8rem;
		color: var(--gray-800);
		padding: 0;
		cursor: pointer;
	}
	.as-toggle:hover .as-label { color: var(--anthracite); }
	.as-toggle :global(.as-icon) {
		font-size: 1.15rem;
		line-height: 1;
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
	.as-toggle.active .switch { background: var(--gradient-brand); }
	.as-toggle.active .switch::after { left: calc(100% - 1rem + 2px); }
</style>
