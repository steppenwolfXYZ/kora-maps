<script lang="ts">
	import { fmtDuration } from './itineraryFormat';
	import { MAX_VIA_WAIT_MIN, VIA_WAIT_PRESETS } from './types';

	// Wait control of a via row (via-stops.md § Panel UI). The value is a
	// MINIMUM stay in whole minutes, so the label reads as a request, not
	// as a measured dwell. 0 is the default and the corridor case ("route
	// through here"), rendered muted so a via without an errand stays
	// visually quiet.

	interface Props {
		wait: number;
		onChange: (minutes: number) => void;
		/** Station name, for the control's accessible label. */
		stationName?: string;
	}

	let { wait, onChange, stationName = '' }: Props = $props();

	let open = $state(false);
	let btnEl: HTMLButtonElement | null = $state(null);
	let custom = $state('');
	let menuStyle = $state('');

	function label(m: number): string {
		return m <= 0 ? 'no wait' : fmtDuration(m * 60);
	}

	// The menu escapes the routing panel's `overflow: hidden` the same way
	// the endpoint dropdown does: fixed position, rect from the button,
	// re-measured on resize / scroll.
	function updatePos() {
		if (!btnEl) return;
		const r = btnEl.getBoundingClientRect();
		// Right-aligned to the button; the menu is wider than the chip.
		const width = 10.5 * 16;
		const left = Math.max(8, Math.min(r.right - width, window.innerWidth - width - 8));
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

	function toggle() {
		open = !open;
		if (open) {
			custom = wait > 0 && !VIA_WAIT_PRESETS.includes(wait) ? String(wait) : '';
		}
	}

	function pick(m: number) {
		open = false;
		onChange(m);
	}

	function commitCustom() {
		const m = Number(custom);
		if (!Number.isFinite(m)) return;
		pick(Math.min(MAX_VIA_WAIT_MIN, Math.max(0, Math.round(m))));
	}

	function onCustomKey(e: KeyboardEvent) {
		if (e.key === 'Enter') {
			e.preventDefault();
			commitCustom();
		}
	}

	function onBlur(e: FocusEvent) {
		// Keep the menu up while focus moves inside it (preset row → custom
		// field). Only a move outside the whole control closes it.
		const next = e.relatedTarget as Node | null;
		if (next && (e.currentTarget as HTMLElement).contains(next)) return;
		setTimeout(() => { open = false; }, 120);
	}
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="vw" onfocusout={onBlur}>
	<button
		bind:this={btnEl}
		class="vw-chip"
		class:set={wait > 0}
		type="button"
		aria-haspopup="listbox"
		aria-expanded={open}
		aria-label="Wait time{stationName ? ` at ${stationName}` : ''}: {label(wait)}"
		onclick={toggle}
	>
		<!-- The glyph shows at rest too, so a via row reads as carrying a
		     wait control rather than a stray grey label. -->
		<span class="material-symbols-outlined vw-icon" aria-hidden="true">hourglass_top</span>
		<span class="vw-text">{label(wait)}</span>
	</button>
	{#if open}
		<div class="vw-menu" role="listbox" style={menuStyle}>
			<div class="vw-head">Minimum time at this stop</div>
			{#each VIA_WAIT_PRESETS as m (m)}
				<button
					class="vw-opt"
					class:selected={wait === m}
					type="button"
					role="option"
					aria-selected={wait === m}
					onclick={() => pick(m)}
				>{label(m)}</button>
			{/each}
			<div class="vw-custom">
				<input
					bind:value={custom}
					type="number"
					min="0"
					max={MAX_VIA_WAIT_MIN}
					step="1"
					placeholder="Custom"
					aria-label="Custom wait in minutes"
					onkeydown={onCustomKey}
				/>
				<span class="vw-unit">min</span>
				<button class="vw-ok" type="button" onclick={commitCustom}>Set</button>
			</div>
		</div>
	{/if}
</div>

<style>
	.vw {
		flex: 0 0 auto;
		position: relative;
		display: flex;
		align-items: center;
	}

	/* Muted at rest (no wait requested), a solid little pill once a wait
	   is set — the row should look quiet until the user asks for time. */
	.vw-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.15rem;
		border: none;
		background: var(--gray-100);
		font-family: inherit;
		font-size: 0.72rem;
		line-height: 1.2;
		color: var(--gray-500);
		padding: 0.15rem 0.4rem 0.15rem 0.3rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
		white-space: nowrap;
	}
	.vw-chip:hover { color: var(--brand); }
	.vw-chip.set {
		background: var(--anthracite);
		color: var(--white);
		font-weight: 600;
	}
	.vw-chip.set:hover { background: var(--brand); color: var(--white); }
	.vw-icon {
		font-size: 0.85rem;
		line-height: 1;
	}

	.vw-menu {
		position: fixed;
		display: flex;
		flex-direction: column;
		padding: 0.25rem 0;
		background: var(--white);
		border-radius: 0.55rem;
		box-shadow: var(--shadow-popover);
		z-index: 30;
	}

	.vw-head {
		padding: 0.3rem 0.7rem 0.35rem;
		font-size: 0.65rem;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--anthracite);
	}

	.vw-opt {
		border: none;
		background: transparent;
		text-align: left;
		font-family: inherit;
		font-size: 0.85rem;
		color: var(--gray-850);
		padding: 0.3rem 0.7rem;
		cursor: pointer;
	}
	.vw-opt:hover { background: var(--gray-100); }
	/* Selected state = brand gradient with white text (ux-guidelines.md). */
	.vw-opt.selected {
		background: var(--gradient-brand-input);
		color: var(--white);
		font-weight: 600;
	}

	.vw-custom {
		display: flex;
		align-items: center;
		gap: 0.3rem;
		padding: 0.35rem 0.7rem 0.2rem;
		border-top: 1px solid var(--gray-100);
		margin-top: 0.2rem;
	}
	.vw-custom input {
		flex: 1 1 auto;
		min-width: 0;
		border: 1px solid var(--gray-200);
		border-radius: 0.4rem;
		background: var(--white);
		font-family: inherit;
		font-size: 0.85rem;
		color: var(--gray-850);
		padding: 0.2rem 0.35rem;
		outline: none;
	}
	.vw-custom input:focus { border-color: var(--gray-400); }
	.vw-unit {
		font-size: 0.75rem;
		color: var(--gray-500);
	}
	.vw-ok {
		border: none;
		background: var(--gray-100);
		font-family: inherit;
		font-size: 0.75rem;
		color: var(--gray-850);
		padding: 0.22rem 0.5rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
	}
	.vw-ok:hover { background: var(--brand); color: var(--white); }
</style>
