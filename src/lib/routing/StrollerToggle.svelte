<script lang="ts">
	// The walking tab's always-visible option (routing-options.md
	// § Stroller mode): the stroller switch on the direct control row —
	// the same value the transit tab's more-options area shows, so both
	// tabs describe one walker. Value in options.svelte.ts
	// (localStorage-persisted); a change re-runs the query.
	import { routingOptions } from './options.svelte';
	import { routingState } from './state.svelte';

	function toggleStroller() {
		routingOptions.setStroller(!routingOptions.stroller);
		routingState.optionsChanged();
	}
</script>

<!-- Same switch pattern as the cycling tab's avoid-stairs toggle. -->
<button
	class="st-toggle"
	class:active={routingOptions.stroller}
	onclick={toggleStroller}
	aria-pressed={routingOptions.stroller}
	title="Avoid stairs where possible — short flights are carried, long ones only when nothing else connects"
>
	<span class="material-symbols-outlined st-icon" aria-hidden="true">stroller</span>
	<span class="st-label">Stroller</span>
	<span class="switch" aria-hidden="true"></span>
</button>

<style>
	.st-toggle {
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
	.st-toggle:hover .st-label { color: var(--anthracite); }
	.st-toggle :global(.st-icon) {
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
	.st-toggle.active .switch { background: var(--gradient-brand); }
	.st-toggle.active .switch::after { left: calc(100% - 1rem + 2px); }
</style>
