<script lang="ts">
	import type { TimeMode } from './types';

	interface Props {
		mode: TimeMode;
		time: string | null;
		onMode: (m: TimeMode) => void;
		onTime: (t: string | null) => void;
	}

	let { mode, time, onMode, onTime }: Props = $props();

	// Local <input type="datetime-local"> value (needs local time without Z).
	// If `time` is null → "now" and we render the current wall time; edits
	// convert back to ISO before pushing to state.

	function toLocalInput(iso: string | null): string {
		const d = iso ? new Date(iso) : new Date();
		const pad = (n: number) => String(n).padStart(2, '0');
		return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
	}

	function fromLocalInput(v: string): string | null {
		if (!v) return null;
		// Interpret as local time.
		const d = new Date(v);
		if (Number.isNaN(d.getTime())) return null;
		return d.toISOString();
	}

	const localValue = $derived(toLocalInput(time));
</script>

<div class="ts">
	<div class="ts-mode" role="group" aria-label="Time mode">
		<button class:active={mode === 'leave'} onclick={() => onMode('leave')}>Leave at</button>
		<button class:active={mode === 'arrive'} onclick={() => onMode('arrive')}>Arrive by</button>
	</div>
	<div class="ts-time">
		<input
			type="datetime-local"
			value={localValue}
			onchange={(e) => onTime(fromLocalInput((e.currentTarget as HTMLInputElement).value))}
		/>
		<button
			class="ts-now"
			onclick={() => onTime(null)}
			disabled={time === null}
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
	.ts-time input {
		flex: 1 1 auto;
		border: 1px solid #ddd;
		border-radius: 0.4rem;
		padding: 0.25rem 0.4rem;
		font-family: 'Saira', sans-serif;
		font-size: 0.85rem;
		color: #222;
	}
	.ts-now {
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
	.ts-now :global(.material-symbols-outlined) { font-size: 1.15rem; line-height: 1; }
	.ts-now:hover:not(:disabled) { background: #eee; color: #000; }
	.ts-now:disabled { color: #bbb; cursor: default; }
</style>
