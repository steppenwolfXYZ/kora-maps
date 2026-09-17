<script lang="ts">
	// The "Options" expander button (routing-options.md § UI): sits at the
	// end of the transit tab's leave-at / arrive-by row and of the cycling
	// tab's control row. The open state is the active state — gradient
	// fill, white glyph and text (per ux-guidelines.md); while collapsed a
	// small gradient dot on the tune glyph marks a non-default setting.
	// The label is per host: the transit row is tight (it shares the
	// line with the leave-at / arrive-by toggle and the swap), so it
	// keeps the short "Options"; the cycling row has room for "More
	// options".
	let { open, modified, onToggle, label = 'Options' }: {
		open: boolean;
		modified: boolean;
		onToggle: () => void;
		label?: string;
	} = $props();
</script>

<button
	class="ob icon-btn"
	class:open
	onclick={onToggle}
	title="More options"
	aria-expanded={open}
>
	<span class="ob-icon">
		<span class="material-symbols-outlined" aria-hidden="true">tune</span>
		{#if !open && modified}
			<span class="ob-dot" aria-hidden="true"></span>
		{/if}
	</span>
	<span class="ob-label">{label}</span>
</button>

<style>
	/* Base look + hover from .icon-btn (app.css); sizing only here. Icon
	   plus a text label — the bare glyph was hard to spot and a
	   small target on touch, so the button is a labelled pill with a
	   comfortable hit area. The row height comes from the host row
	   (--ts-row-h, inherited), so the button matches its neighbours. */
	.ob {
		flex: 0 0 auto;
		gap: 0.25rem;
		min-height: var(--ts-row-h, 2rem);
		padding: 0 0.7rem 0 0.55rem;
		font-family: var(--font-ui);
		font-size: 0.8rem;
		line-height: 1.2;
	}
	.ob :global(.material-symbols-outlined) { font-size: 1.15rem; line-height: 1; }
	.ob.open,
	.ob.open:hover {
		background: var(--gradient-brand);
		color: var(--white);
	}
	.ob.open :global(.material-symbols-outlined) { color: var(--white); }
	/* Non-default indicator while collapsed: small gradient dot badged onto
	   the tune glyph (badges belong on the icon, not on the label), sitting
	   mostly outside the glyph box at its top-right so it covers as little
	   of it as possible. The white ring keeps the two apart. */
	.ob-icon {
		position: relative;
		display: inline-flex;
		align-items: center;
	}
	.ob-dot {
		position: absolute;
		top: -0.15rem;
		right: -0.25rem;
		width: 0.6rem;
		height: 0.6rem;
		border-radius: 50%;
		background: var(--gradient-brand);
		border: 1px solid var(--white);
	}
</style>
