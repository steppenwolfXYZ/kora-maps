<script lang="ts">
	import type { TimeMode } from './types';

	interface Props {
		mode: TimeMode;
		time: string | null;
		onMode: (m: TimeMode) => void;
		onTime: (t: string | null) => void;
	}

	let { mode, time, onMode, onTime }: Props = $props();

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
	<div class="ts-mode" role="group" aria-label="Time mode">
		<button class:active={mode === 'leave'} onclick={() => onMode('leave')}>Leave at</button>
		<button class:active={mode === 'arrive'} onclick={() => onMode('arrive')}>Arrive by</button>
	</div>
	<div class="ts-time">
		<input
			class="ts-date"
			type="date"
			value={dateValue}
			aria-label="Date"
			onchange={(e) => onDateChange((e.currentTarget as HTMLInputElement).value)}
		/>
		<button
			class="ts-day-step"
			onclick={() => shiftDay(-1)}
			title="Previous day"
			aria-label="Previous day"
		><span class="material-symbols-outlined ts-day-step-back" aria-hidden="true">chevron_right</span></button>
		<button
			class="ts-day-step"
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
			class="ts-now"
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
	.ts-mode {
		display: flex;
		border: 1px solid #ddd;
		border-radius: 0.55rem;
		overflow: hidden;
		width: fit-content;
	}
	.ts-mode button {
		border: none;
		background: transparent;
		font-family: 'Saira', sans-serif;
		font-size: 0.85rem;
		padding: 0.35rem 0.8rem;
		cursor: pointer;
		color: #444;
	}
	.ts-mode button.active { background: #333; color: #fff; }

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
	.ts-day-step {
		flex: 0 0 auto;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border: none;
		background: transparent;
		color: #666;
		cursor: pointer;
		padding: 0.1rem;
		border-radius: 0.3rem;
	}
	.ts-day-step :global(.material-symbols-outlined) { font-size: 1.15rem; line-height: 1; }
	/* chevron_left isn't in the icon subset — mirror chevron_right instead.
	   scaleX (not rotate): the glyph sits slightly off-center vertically,
	   so a 180° rotation would shift it down relative to its twin. */
	.ts-day-step :global(.ts-day-step-back) { transform: scaleX(-1); display: inline-block; }
	.ts-day-step:hover { background: #eee; color: #000; }
	.ts-date {
		flex: 1 1 auto;
		max-width: 9rem;
		border: none;
		background: #f5f5f5;
		border-radius: 0.55rem;
		padding: 0.35rem 0.5rem;
		font-family: 'Saira', sans-serif;
		font-size: 0.85rem;
		color: #222;
	}
	.ts-timebox {
		flex: 0 1 auto;
		width: 5.2rem;
		margin-left: auto;
	}
	.ts-timein {
		width: 100%;
		border: none;
		background: #f5f5f5;
		border-radius: 0.55rem;
		padding: 0.35rem 0.5rem;
		font-family: 'Saira', sans-serif;
		font-size: 0.85rem;
		color: #222;
		text-align: left;
		font-variant-numeric: tabular-nums;
	}
	.ts-menu {
		position: fixed;
		margin: 0;
		padding: 0.2rem 0;
		list-style: none;
		background: #ffffff;
		border-radius: 0.55rem;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
		max-height: 13rem;
		overflow-y: auto;
		z-index: 30;
	}
	.ts-row {
		padding: 0.25rem 0.7rem;
		font-size: 0.85rem;
		color: #222;
		text-align: left;
		font-variant-numeric: tabular-nums;
		cursor: pointer;
	}
	.ts-row.full-hour { font-weight: 600; }
	.ts-row.now { color: #1565c0; font-weight: 600; }
	.ts-row:hover { background: #eee; }
	.ts-row.selected { background: #333; color: #fff; }

	.ts-now {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border: none;
		background: transparent;
		color: #666;
		cursor: pointer;
		/* Sized and spaced like the swap button next to the from/to rows
		   (0.35rem gap + 1.85rem button) so the time field's right edge
		   aligns with the endpoint fields' right edge. */
		padding: 0.25rem 0.35rem;
		margin-left: -0.15rem;
		border-radius: 999px;
	}
	.ts-now :global(.material-symbols-outlined) { font-size: 1.15rem; line-height: 1; }
	.ts-now:hover { background: #eee; color: #000; }
</style>
