<script lang="ts">
	// Title bar of the line-detail view (line-detail-view.md): badge +
	// route, the service summary line, and the expandable per-variant
	// rows. Reads its data from lineDetailState; the close action is
	// injected because closing may involve history.back() coordination
	// owned by Map.svelte.
	import { slide } from 'svelte/transition';
	import { lineDetailState } from './state.svelte';
	import type { LineServiceInfo } from './lineIndex';

	let { onClose }: { onClose: () => void } = $props();

	function badgeTextColor(hexColor: string): string {
		const lum = parseInt(hexColor.slice(1, 3), 16) * 0.299
			+ parseInt(hexColor.slice(3, 5), 16) * 0.587
			+ parseInt(hexColor.slice(5, 7), 16) * 0.114;
		return lum > 140 ? '#000' : '#fff';
	}

	const SERVICE_DAY_ABBR = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'];

	function fmtServiceDays(mask: string): string {
		if (mask === '1111111') return 'daily';
		const runs: string[] = [];
		let i = 0;
		while (i < 7) {
			if (mask[i] !== '1') { i++; continue; }
			let j = i;
			while (j + 1 < 7 && mask[j + 1] === '1') j++;
			runs.push(j > i
				? `${SERVICE_DAY_ABBR[i]}–${SERVICE_DAY_ABBR[j]}`
				: SERVICE_DAY_ABBR[i]);
			i = j + 1;
		}
		return runs.join(', ') || '–';
	}

	/** Format a departure time rounded to the nearest quarter hour. */
	function fmtDep(secs: number): string {
		const q = Math.round(secs / 900) * 900;
		const h = Math.floor(q / 3600) % 24;
		const m = Math.floor((q % 3600) / 60);
		return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
	}

	/** Cadence on a day the line runs: regular service reads as a rate
	 * (headway / ×-per-hour / every 2 h); rarer than ~every 2 h or an
	 * irregular pattern falls back to runs per day. */
	function fmtCadence(rpd: number, dep?: [number, number], irr?: boolean): string {
		if (rpd <= 0) return '–';
		const perDay = `≈${Math.max(1, Math.round(rpd))}×/day`;
		if (irr || rpd < 3) return perDay;
		const spanMin = dep ? (dep[1] - dep[0]) / 60 : 17 * 60;
		const headway = spanMin / Math.max(1, rpd - 1);
		if (headway > 130) return perDay;
		if (headway < 24) return `every ~${Math.max(1, Math.round(headway))} min`;
		if (headway < 40) return '≈2×/h';
		if (headway < 80) return '≈1×/h';
		return 'every ~2 h';
	}

	function fmtDateShort(iso: string): string {
		return new Date(iso + 'T12:00:00')
			.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
	}

	function serviceSummary(svc: LineServiceInfo): string {
		const parts: string[] = [];
		if (svc.from && svc.to) {
			parts.push(`${fmtDateShort(svc.from)} – ${fmtDateShort(svc.to)}`);
		}
		parts.push(fmtServiceDays(svc.days));
		if (svc.dep) parts.push(`${fmtDep(svc.dep[0])}–${fmtDep(svc.dep[1])}`);
		parts.push(fmtCadence(svc.rpd, svc.dep, svc.irr));
		return parts.join(' · ');
	}
</script>

{#if lineDetailState.selection}
	{@const sel = lineDetailState.selection}
	{@const svc = lineDetailState.service}
	<div class="line-detail-bar" role="status">
		<div class="line-detail-head">
			<span
				class="line-detail-badge"
				style="background:{sel.color};color:{badgeTextColor(sel.color)}"
			>{sel.ref || sel.mode}</span>
			{#if sel.route}
				<span class="line-detail-route">{sel.route}</span>
			{/if}
			<button
				class="line-detail-close"
				onclick={onClose}
				aria-label="Close line detail view"
			>×</button>
		</div>
		{#if svc}
			<div class="line-detail-summary">{serviceSummary(svc)}</div>
			{#if lineDetailState.serviceExpanded}
				<div class="line-detail-details" transition:slide={{ duration: 250 }}>
					<div class="line-detail-variants">
						{#each svc.variants as v (v.route)}
							<div class="line-detail-variant">
								<span class="line-detail-variant-route">{v.route}</span>
								<span class="line-detail-variant-meta">
									{fmtServiceDays(v.days)}{#if v.dep}
										&nbsp;· {fmtDep(v.dep[0])}–{fmtDep(v.dep[1])}{/if}
									· {fmtCadence(v.rpd, v.dep, v.irr)}{#if v.from && v.to}
										&nbsp;· {fmtDateShort(v.from)} – {fmtDateShort(v.to)}{/if}
								</span>
							</div>
						{/each}
					</div>
				</div>
			{/if}
			{#if svc.variants.length > 1}
				<button
					class="line-detail-toggle"
					onclick={() => (lineDetailState.serviceExpanded = !lineDetailState.serviceExpanded)}
					aria-label={lineDetailState.serviceExpanded ? 'Hide line details' : 'Show line details'}
					aria-expanded={lineDetailState.serviceExpanded}
				><span class="line-detail-chevron" class:flipped={lineDetailState.serviceExpanded}>▾</span></button>
			{/if}
		{/if}
	</div>
{/if}

<style>
	.line-detail-bar {
		position: absolute;
		top: 1rem;
		left: 50%;
		transform: translateX(-50%);
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		max-width: min(85vw, 34rem);
		background: var(--white);
		border-radius: 1.1rem;
		box-shadow: var(--shadow-control);
		padding: 0.6rem 0.9rem 0.6rem 1rem;
		font-family: var(--font-ui);
		z-index: 5;
	}

	.line-detail-head {
		display: flex;
		align-items: center;
		gap: 0.65rem;
	}

	/* Push the action buttons to the right edge even when there is no
	   route text to grow into the gap. */
	.line-detail-head > button:first-of-type {
		margin-left: auto;
	}

	.line-detail-summary {
		font-size: 0.75rem;
		color: var(--gray-500);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.line-detail-details {
		border-top: 1px solid var(--gray-100);
		margin-top: 0.35rem;
		padding-top: 0.45rem;
		max-height: 45vh;
		overflow-y: auto;
		font-size: 0.8rem;
		color: var(--gray-800);
	}

	.line-detail-variants {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}

	.line-detail-variant {
		display: flex;
		flex-direction: column;
	}

	.line-detail-variant-route {
		font-weight: 600;
		font-size: 0.78rem;
	}

	.line-detail-variant-meta {
		color: var(--gray-500);
		font-size: 0.72rem;
	}

	.line-detail-toggle {
		border: none;
		background: transparent;
		color: var(--gray-600);
		font-size: 0.8rem;
		line-height: 1;
		cursor: pointer;
		/* Span the card's horizontal padding so the strip runs edge to
		   edge and closes off the bottom of the bar. */
		margin: 0.25rem -0.9rem -0.6rem -1rem;
		padding: 0.3rem 0 0.4rem;
		border-top: 1px solid var(--gray-100);
		border-radius: 0 0 1.1rem 1.1rem;
	}

	.line-detail-toggle:hover {
		background: var(--gray-50);
		color: var(--black);
	}

	.line-detail-chevron {
		display: inline-block;
		transition: transform 0.25s ease;
	}

	.line-detail-chevron.flipped {
		transform: rotate(180deg);
	}

	.line-detail-badge {
		border-radius: 3px;
		padding: 2px 8px;
		font-size: 0.8rem;
		font-weight: 800;
		letter-spacing: 0.02em;
		white-space: nowrap;
	}

	.line-detail-route {
		font-size: 0.85rem;
		color: var(--gray-800);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.line-detail-close {
		border: none;
		background: transparent;
		color: var(--gray-600);
		font-size: 1.1rem;
		line-height: 1;
		cursor: pointer;
		padding: 0.15rem 0.35rem;
		border-radius: var(--radius-pill);
		flex: 0 0 auto;
	}

	.line-detail-close:hover {
		background: var(--gray-100);
		color: var(--black);
	}
</style>
