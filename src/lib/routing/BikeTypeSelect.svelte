<script lang="ts">
	// Bike type dropdown (bicycle-route-options.md § 1): a chip showing
	// the selected type's icon and name, opening a menu with every type's
	// icon, name and one-line description. Same fixed-position menu
	// mechanics as the via-wait control, so it escapes the routing
	// panel's overflow clip.
	import RacingBikeIcon from './RacingBikeIcon.svelte';
	import { BIKE_TYPES, type BikeType } from './options.svelte';

	let { value, onChange }: {
		value: BikeType;
		onChange: (t: BikeType) => void;
	} = $props();

	let open = $state(false);
	let btnEl: HTMLButtonElement | null = $state(null);
	let menuStyle = $state('');

	let selected = $derived(BIKE_TYPES.find((t) => t.id === value) ?? BIKE_TYPES[0]);

	function updatePos() {
		if (!btnEl) return;
		const r = btnEl.getBoundingClientRect();
		const width = 15 * 16;
		const left = Math.max(8, Math.min(r.left, window.innerWidth - width - 8));
		menuStyle = `left:${left}px; top:${r.bottom + 4}px; width:${width}px;`;
	}

	$effect(() => {
		if (!open) return;
		updatePos();
		const handler = () => updatePos();
		window.addEventListener('resize', handler);
		window.addEventListener('scroll', handler, true);
		return () => {
			window.removeEventListener('resize', handler);
			window.removeEventListener('scroll', handler, true);
		};
	});

	function pick(t: BikeType) {
		open = false;
		if (t !== value) onChange(t);
	}

	function onBlur(e: FocusEvent) {
		const next = e.relatedTarget as Node | null;
		if (next && (e.currentTarget as HTMLElement).contains(next)) return;
		setTimeout(() => { open = false; }, 120);
	}

	function onKey(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			e.preventDefault();
			open = false;
			btnEl?.focus();
		}
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="bt" onfocusout={onBlur} onkeydown={onKey}>
	<button
		bind:this={btnEl}
		class="bt-chip"
		class:open
		type="button"
		aria-haspopup="listbox"
		aria-expanded={open}
		aria-label="Bike type: {selected.label}"
		onclick={() => (open = !open)}
	>
		<span class="bt-glyph">
			{#if selected.svg}
				<RacingBikeIcon />
			{:else}
				<span class="material-symbols-outlined" aria-hidden="true">{selected.icon}</span>
			{/if}
			{#if selected.badge}<span class="bt-badge" aria-hidden="true">{selected.badge}</span>{/if}
		</span>
		<span class="bt-text">{selected.label}</span>
		<!-- chevron_right turned downward: the subset carries no
		     expand_more glyph, and one rotated chevron is enough. -->
		<span class="material-symbols-outlined bt-chevron" aria-hidden="true">chevron_right</span>
	</button>
	{#if open}
		<div class="bt-menu" role="listbox" aria-label="Bike type" style={menuStyle}>
			{#each BIKE_TYPES as t (t.id)}
				<button
					class="bt-opt"
					class:selected={t.id === value}
					type="button"
					role="option"
					aria-selected={t.id === value}
					onclick={() => pick(t.id)}
				>
					<span class="bt-glyph">
						{#if t.svg}
							<RacingBikeIcon />
						{:else}
							<span class="material-symbols-outlined" aria-hidden="true">{t.icon}</span>
						{/if}
						{#if t.badge}<span class="bt-badge" aria-hidden="true">{t.badge}</span>{/if}
					</span>
					<span class="bt-opt-text">
						<span class="bt-opt-label">{t.label}</span>
						<span class="bt-opt-desc">{t.desc}</span>
					</span>
				</button>
			{/each}
		</div>
	{/if}
</div>

<style>
	.bt {
		flex: 0 0 auto;
		position: relative;
		display: flex;
		align-items: center;
	}
	/* Pill in the segmented control's colours: gray at rest, gradient
	   with white text while the menu is open (the open state is the
	   active state, per ux-guidelines.md). */
	.bt-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		height: var(--bo-row-h, 2rem);
		border: none;
		background: var(--gray-100);
		font-family: var(--font-ui);
		font-size: 0.85rem;
		color: var(--gray-700);
		padding: 0 0.5rem 0 0.6rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
		white-space: nowrap;
	}
	.bt-chip:hover { color: var(--anthracite); }
	.bt-chip.open,
	.bt-chip.open:hover { background: var(--gradient-brand); color: var(--white); }
	.bt-chip :global(.material-symbols-outlined),
	.bt-chip :global(.rbi) { font-size: 1.25rem; line-height: 1; }
	.bt-chevron { transform: rotate(90deg); font-size: 1.1rem !important; margin-left: -0.15rem; }
	.bt-glyph {
		position: relative;
		display: inline-flex;
		align-items: center;
	}
	/* Assist-cap badge on the e-bike glyphs: tiny bold figure at the
	   glyph's lower right, its colour following the host's. */
	.bt-badge {
		position: absolute;
		right: -0.45rem;
		bottom: -0.3rem;
		font-size: 0.5rem;
		font-weight: 700;
		line-height: 1;
		letter-spacing: -0.02em;
	}

	.bt-menu {
		position: fixed;
		display: flex;
		flex-direction: column;
		padding: 0.25rem 0;
		background: var(--white);
		border-radius: 0.55rem;
		box-shadow: var(--shadow-popover);
		z-index: 30;
	}
	.bt-opt {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		border: none;
		background: transparent;
		text-align: left;
		font-family: inherit;
		color: var(--gray-850);
		padding: 0.4rem 0.8rem 0.4rem 0.7rem;
		cursor: pointer;
	}
	.bt-opt :global(.material-symbols-outlined),
	.bt-opt :global(.rbi) { font-size: 1.4rem; line-height: 1; }
	.bt-opt:hover { background: var(--gray-100); }
	/* Selected state = brand gradient with white text (ux-guidelines.md). */
	.bt-opt.selected {
		background: var(--gradient-brand-input);
		color: var(--white);
	}
	.bt-opt-text {
		display: flex;
		flex-direction: column;
		gap: 0.05rem;
		min-width: 0;
	}
	.bt-opt-label { font-size: 0.85rem; font-weight: 600; }
	.bt-opt-desc { font-size: 0.72rem; color: var(--gray-500); }
	.bt-opt.selected .bt-opt-desc { color: var(--white); opacity: 0.85; }
</style>
