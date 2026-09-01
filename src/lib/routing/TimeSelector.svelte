<script lang="ts">
	import type { Snippet } from 'svelte';
	import { slide } from 'svelte/transition';
	import type { TimeMode } from './types';

	interface Props {
		mode: TimeMode;
		time: string | null;
		onMode: (m: TimeMode) => void;
		onTime: (t: string | null) => void;
		/** More-options expander (routing-options.md § UI): the button
		 * lives right of the leave-at/arrive-by toggle; the expanded area
		 * (the `options` snippet, provided by RoutingPanel) renders
		 * between the mode row and the timing row. `optionsModified`
		 * shows the non-default indicator dot while collapsed. */
		optionsOpen?: boolean;
		optionsModified?: boolean;
		onToggleOptions?: () => void;
		options?: Snippet;
	}

	let {
		mode, time, onMode, onTime,
		optionsOpen = false, optionsModified = false, onToggleOptions,
		options
	}: Props = $props();

	// Local tick so the displayed wall clock re-evaluates on every refresh
	// click — including when `time` is already null and the prop doesn't
	// change. Without it the fields would stay frozen at the first null
	// evaluation and drift stale as the clock moves.
	let nowTick = $state(0);

	const pad = (n: number) => String(n).padStart(2, '0');

	// Displayed date/time parts; `time === null` means "now".
	const shown = $derived.by(() => {
		nowTick;
		return time ? new Date(time) : new Date();
	});
	const dateValue = $derived(
		`${shown.getFullYear()}-${pad(shown.getMonth() + 1)}-${pad(shown.getDate())}`
	);
	const timeValue = $derived(`${pad(shown.getHours())}:${pad(shown.getMinutes())}`);

	// Combine the two field values (interpreted as local time) back into the
	// ISO string the store keeps. Editing either field pins the query time;
	// "now" only returns via the refresh button.
	function commitParts(date: string, hm: string) {
		const d = new Date(`${date}T${hm}`);
		if (!Number.isNaN(d.getTime())) onTime(d.toISOString());
	}

	function onDateChange(v: string) {
		if (v) commitParts(v, timeValue);
	}

	function shiftDay(delta: number) {
		const d = new Date(shown);
		d.setDate(d.getDate() + delta);
		commitParts(
			`${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
			timeValue
		);
	}

	function refresh() {
		nowTick++;
		onTime(null);
	}

	// --- Time combobox ---------------------------------------------------
	// Editable text input + quarter-hour dropdown. The 24 h list is repeated
	// three times so the user can scroll backward past 00:00 or forward past
	// 23:45 without hitting a wall; picking a row sets the time only and
	// never changes the date field. Hand-typed values allow any minute.

	const QUARTERS: string[] = [];
	for (let h = 0; h < 24; h++)
		for (let m = 0; m < 60; m += 15) QUARTERS.push(`${pad(h)}:${pad(m)}`);
	const ROWS: string[] = [...QUARTERS, ...QUARTERS, ...QUARTERS];

	let timeOpen = $state(false);
	let timeDraft = $state('');
	// Quarter-hour slot nearest the wall clock at open time, tinted blue in
	// the list as an orientation anchor.
	let nowQuarter = $state('');
	let timeInputEl: HTMLInputElement | null = $state(null);
	let timeWrapEl: HTMLDivElement | null = $state(null);
	let menuEl: HTMLUListElement | null = $state(null);
	let menuStyle = $state('');

	// Accepts "9" → 09:00, "9:30", "09:30", "930", "0930", "9.30".
	function parseTime(raw: string): string | null {
		const m = raw.trim().match(/^(\d{1,2})(?:[:.]?([0-5]\d))?$/);
		if (!m) return null;
		const h = Number(m[1]);
		if (h > 23) return null;
		return `${pad(h)}:${m[2] ?? '00'}`;
	}

	function openTime() {
		timeOpen = true;
		timeDraft = timeValue;
		const n = new Date();
		nowQuarter = QUARTERS[Math.round((n.getHours() * 60 + n.getMinutes()) / 15) % 96];
		queueMicrotask(() => timeInputEl?.select());
	}

	function commitDraft() {
		const hm = parseTime(timeDraft);
		// Skip the no-op commit: it would pin a "now" (null) time without
		// the user having changed anything.
		if (hm && hm !== timeValue) commitParts(dateValue, hm);
		timeOpen = false;
	}

	// Clicking the input while the dropdown is open closes it (toggle).
	// mousedown fires before focus, so on a fresh focus timeOpen is still
	// false here and only the focus handler opens.
	function onTimeMouseDown() {
		if (timeOpen) commitDraft();
		else openTime();
	}

	function pickRow(hm: string) {
		commitParts(dateValue, hm);
		timeOpen = false;
		timeInputEl?.blur();
	}

	function onTimeKey(e: KeyboardEvent) {
		if (e.key === 'Enter') {
			e.preventDefault();
			commitDraft();
			timeInputEl?.blur();
		} else if (e.key === 'Escape') {
			timeOpen = false;
			timeInputEl?.blur();
		}
	}

	// Menu positioning: the routing panel clips overflow, so the dropdown is
	// position: fixed, anchored to the input's bounding rect and re-anchored
	// on resize/scroll (same pattern as EndpointInput).
	function updateMenuPos() {
		if (!timeWrapEl) return;
		const r = timeWrapEl.getBoundingClientRect();
		menuStyle = `left:${r.left}px; top:${r.bottom + 4}px; width:${r.width}px;`;
	}

	$effect(() => {
		if (!timeOpen) return;
		updateMenuPos();
		const handler = () => updateMenuPos();
		window.addEventListener('resize', handler);
		window.addEventListener('scroll', handler, true);
		return () => {
			window.removeEventListener('resize', handler);
			window.removeEventListener('scroll', handler, true);
		};
	});

	// On open, scroll the middle copy's slot nearest the shown time to the
	// top of the list, one row of context above.
	$effect(() => {
		if (!timeOpen || !menuEl) return;
		const [h, m] = timeValue.split(':').map(Number);
		const slot = QUARTERS.length + h * 4 + Math.floor(m / 15);
		const target = menuEl.children[Math.max(0, slot - 1)] as HTMLElement | undefined;
		if (target) menuEl.scrollTop = target.offsetTop;
	});
</script>

<div class="ts">
	<div class="ts-mode-row">
		<div class="ts-mode" role="group" aria-label="Time mode">
			<button class:active={mode === 'leave'} onclick={() => onMode('leave')}>Leave at</button>
			<button class:active={mode === 'arrive'} onclick={() => onMode('arrive')}>Arrive by</button>
		</div>
		{#if onToggleOptions}
			<button
				class="ts-options icon-btn"
				class:open={optionsOpen}
				onclick={onToggleOptions}
				title="More options"
				aria-expanded={optionsOpen}
			>
				<span class="ts-options-icon">
					<span class="material-symbols-outlined" aria-hidden="true">tune</span>
					{#if !optionsOpen && optionsModified}
						<span class="ts-options-dot" aria-hidden="true"></span>
					{/if}
				</span>
				<span class="ts-options-label">Options</span>
			</button>
		{/if}
	</div>
	{#if optionsOpen && options}
		<div class="ts-options-area" transition:slide={{ duration: 180 }}>
			{@render options()}
		</div>
	{/if}
	<div class="ts-time">
		<input
			class="ts-date"
			type="date"
			value={dateValue}
			aria-label="Date"
			onchange={(e) => onDateChange((e.currentTarget as HTMLInputElement).value)}
		/>
		<button
			class="ts-day-step icon-btn"
			onclick={() => shiftDay(-1)}
			title="Previous day"
			aria-label="Previous day"
		><span class="material-symbols-outlined ts-day-step-back" aria-hidden="true">chevron_right</span></button>
		<button
			class="ts-day-step icon-btn"
			onclick={() => shiftDay(1)}
			title="Next day"
			aria-label="Next day"
		><span class="material-symbols-outlined" aria-hidden="true">chevron_right</span></button>
		<div class="ts-timebox" bind:this={timeWrapEl}>
			<input
				bind:this={timeInputEl}
				class="ts-timein"
				type="text"
				inputmode="numeric"
				autocomplete="off"
				aria-label="Time"
				value={timeOpen ? timeDraft : timeValue}
				oninput={(e) => (timeDraft = (e.currentTarget as HTMLInputElement).value)}
				onmousedown={onTimeMouseDown}
				onfocus={() => { if (!timeOpen) openTime(); }}
				onblur={() => { if (timeOpen) commitDraft(); }}
				onkeydown={onTimeKey}
			/>
			{#if timeOpen}
				<ul class="ts-menu" bind:this={menuEl} role="listbox" aria-label="Time suggestions" style={menuStyle}>
					{#each ROWS as hm, i (i)}
						<li
							class="ts-row"
							class:full-hour={hm.endsWith(':00')}
							class:now={hm === nowQuarter}
							class:selected={hm === timeValue}
							role="option"
							aria-selected={hm === timeValue}
							onmousedown={(e) => { e.preventDefault(); pickRow(hm); }}
						>{hm}</li>
					{/each}
				</ul>
			{/if}
		</div>
		<button
			class="ts-now icon-btn"
			onclick={refresh}
			title="Reset to now"
			aria-label="Reset to now"
		><span class="material-symbols-outlined" aria-hidden="true">refresh</span></button>
	</div>
</div>

<style>
	.ts {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.ts-mode-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		/* One height for both controls in the row — the mode toggle and
		   the options button read as a pair, and it doubles as a
		   comfortable touch target. */
		--ts-row-h: 2rem;
	}
	/* Fully rounded ends like every other pill in the app (segmented
	   control per ux-guidelines.md § Toggles). */
	.ts-mode {
		display: flex;
		border-radius: var(--radius-pill);
		overflow: hidden;
		width: fit-content;
		height: var(--ts-row-h);
	}
	/* Base look + hover from .icon-btn (app.css); sizing only here. Icon
	   plus a "Options" label — the bare glyph was hard to spot and a
	   small target on touch, so the button is a labelled pill with a
	   comfortable hit area. The open state is the active state —
	   gradient fill, white glyph and text (per ux-guidelines.md). */
	.ts-options {
		flex: 0 0 auto;
		gap: 0.25rem;
		/* Shared row height with the mode toggle beside it. */
		min-height: var(--ts-row-h);
		padding: 0 0.7rem 0 0.55rem;
		font-family: var(--font-ui);
		font-size: 0.8rem;
		line-height: 1.2;
	}
	.ts-options :global(.material-symbols-outlined) { font-size: 1.15rem; line-height: 1; }
	.ts-options.open,
	.ts-options.open:hover {
		background: var(--gradient-brand);
		color: var(--white);
	}
	.ts-options.open :global(.material-symbols-outlined) { color: var(--white); }
	/* Non-default indicator while collapsed: small gradient dot badged onto
	   the tune glyph (badges belong on the icon, not on the label), sitting
	   mostly outside the glyph box at its top-right so it covers as little
	   of it as possible. The white ring keeps the two apart. */
	.ts-options-icon {
		position: relative;
		display: inline-flex;
		align-items: center;
	}
	.ts-options-dot {
		position: absolute;
		top: -0.15rem;
		right: -0.25rem;
		width: 0.6rem;
		height: 0.6rem;
		border-radius: 50%;
		background: var(--gradient-brand);
		border: 1px solid var(--white);
	}
	.ts-mode button {
		display: inline-flex;
		align-items: center;
		border: none;
		background: var(--gray-100);
		font-family: var(--font-ui);
		font-size: 0.85rem;
		/* A touch more side padding than a square-cornered segment needs —
		   the pill's curve eats into the ends. */
		padding: 0 0.9rem;
		cursor: pointer;
		color: var(--gray-700);
	}
	.ts-mode button.active { background: var(--gradient-brand); color: var(--white); }

	.ts-time {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	/* Pull the chevron pair toward the date field (≈0.1rem) so it visually
	   belongs to it, and tighten the pair itself; the flex slack stays
	   between the chevrons and the time input. */
	.ts-date + .ts-day-step { margin-left: -0.4rem; }
	.ts-day-step + .ts-day-step { margin-left: -0.25rem; }
	/* Base look + hover from .icon-btn (app.css); sizing only here. */
	.ts-day-step {
		flex: 0 0 auto;
		padding: 0.1rem;
	}
	.ts-day-step :global(.material-symbols-outlined) { font-size: 1.15rem; line-height: 1; }
	/* chevron_left isn't in the icon subset — mirror chevron_right instead.
	   scaleX (not rotate): the glyph sits slightly off-center vertically,
	   so a 180° rotation would shift it down relative to its twin. */
	.ts-day-step :global(.ts-day-step-back) { transform: scaleX(-1); display: inline-block; }
	.ts-date {
		flex: 1 1 auto;
		max-width: 9rem;
		/* Permanent transparent border so the gradient focus ring can
		   appear without a layout shift (padding compensates). */
		border: 2px solid transparent;
		background: var(--gray-50);
		border-radius: 0.55rem;
		padding: calc(0.35rem - 2px) calc(0.5rem - 2px);
		font-family: var(--font-ui);
		font-size: 0.85rem;
		color: var(--gray-850);
	}
	.ts-date:focus {
		outline: none;
		background: linear-gradient(var(--gray-50), var(--gray-50)) padding-box, var(--gradient-brand-input) border-box;
	}
	.ts-timebox {
		flex: 0 1 auto;
		width: 5.2rem;
		margin-left: auto;
	}
	.ts-timein {
		width: 100%;
		/* Same transparent-border trick as .ts-date. */
		border: 2px solid transparent;
		background: var(--gray-50);
		border-radius: 0.55rem;
		padding: calc(0.35rem - 2px) calc(0.5rem - 2px);
		font-family: var(--font-ui);
		font-size: 0.85rem;
		color: var(--gray-850);
		text-align: left;
		font-variant-numeric: tabular-nums;
	}
	.ts-timein:focus {
		outline: none;
		background: linear-gradient(var(--gray-50), var(--gray-50)) padding-box, var(--gradient-brand-input) border-box;
	}
	.ts-menu {
		position: fixed;
		margin: 0;
		padding: 0.2rem 0;
		list-style: none;
		background: var(--white);
		border-radius: 0.55rem;
		box-shadow: var(--shadow-popover);
		max-height: 13rem;
		overflow-y: auto;
		z-index: 30;
	}
	.ts-row {
		padding: 0.25rem 0.7rem;
		font-size: 0.85rem;
		color: var(--gray-850);
		text-align: left;
		font-variant-numeric: tabular-nums;
		cursor: pointer;
	}
	.ts-row.full-hour { font-weight: 600; }
	.ts-row.now { color: var(--brand); font-weight: 600; }
	.ts-row:hover { background: var(--gray-100); }
	.ts-row.selected { background: var(--gradient-brand); color: var(--white); }

	/* Base look + hover from .icon-btn (app.css). Sized and spaced like
	   the swap button next to the from/to rows (0.35rem gap + 1.85rem
	   button) so the time field's right edge aligns with the endpoint
	   fields' right edge. */
	.ts-now {
		padding: 0.25rem 0.35rem;
		margin-left: -0.15rem;
	}
	.ts-now :global(.material-symbols-outlined) { font-size: 1.15rem; line-height: 1; }
</style>
