<script lang="ts">
	import { fly } from 'svelte/transition';
	// Menu button + panel: holds the view-mode switch and the map legend.
	// The legend explains the three transit-line encodings: hue = mode,
	// darkness = speed, width = frequency. Colors mirror speed_to_color
	// (scripts/transit/gtfs/frequency.py) at mid speed; the gradient bar
	// shows the train ramp as the representative example.
	type ViewMode = 'standard' | 'transit-focus';

	// `open` is bindable so Map.svelte can close the panel on map
	// interaction (small screens only — see the handlers there). The panel
	// is non-modal: no backdrop, the map stays usable while it's open.
	let {
		viewMode,
		setView,
		contoursEnabled,
		toggleContours,
		open = $bindable(false)
	}: {
		viewMode: ViewMode;
		setView: (mode: ViewMode) => void;
		contoursEnabled: boolean;
		toggleContours: () => void;
		open?: boolean;
	} = $props();

	// Mid-speed (score 0.5) colors from speed_to_color, per mode.
	const MODES: { label: string; color: string; note?: string }[] = [
		{ label: 'Train', color: '#c94040' },
		{ label: 'Metro', color: '#40c940' },
		{ label: 'Tram', color: '#40c9c9' },
		{ label: 'City bus', color: '#406dc9' },
		{ label: 'Regional bus', color: '#fc9247' },
		{ label: 'Ferry', color: '#406dc9' },
		{ label: 'Mountain', color: '#b440cb', note: 'cablecar, funicular, cog railway' }
	];

	// Train speed ramp endpoints/midpoint (score 0 / 0.5 / 1).
	const SPEED_RAMP = ['#d0b8b8', '#c94040', '#840505'];
</script>

<svelte:window onkeydown={(e) => { if (e.key === 'Escape' && open) open = false; }} />

<div class="menu">
	<button
		class="menu-toggle"
		class:active={open}
		onclick={() => (open = !open)}
		aria-expanded={open}
		aria-label="Menu and legend"
	>
		<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
			<path d="M3 5.5h14M3 10h14M3 14.5h14" fill="none" stroke="currentColor"
				stroke-width="1.8" stroke-linecap="round" />
		</svg>
	</button>

	{#if open}
		<div
			class="panel"
			role="dialog"
			aria-label="Map menu"
			transition:fly={{ y: -12, duration: 180 }}
		>
			<div class="section">
				<div class="section-title">View</div>
				<div class="view-toggle" role="group" aria-label="Map view">
					<button
						class:active={viewMode === 'standard'}
						onclick={() => setView('standard')}
					>Standard</button>
					<button
						class:active={viewMode === 'transit-focus'}
						onclick={() => setView('transit-focus')}
					>Transit</button>
				</div>
			</div>

			<div class="section">
				<div class="section-title">Layers</div>
				<button
					class="row-toggle"
					class:active={contoursEnabled}
					onclick={toggleContours}
					aria-pressed={contoursEnabled}
				>
					<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
						<path d="M2 15 Q 6 8, 10 11 T 18 6" fill="none" stroke="currentColor" stroke-width="1.4" />
						<path d="M2 17 Q 6 12, 10 14 T 18 10" fill="none" stroke="currentColor" stroke-width="1.4" />
						<path d="M2 13 Q 6 5, 10 8 T 18 3" fill="none" stroke="currentColor" stroke-width="1.4" />
					</svg>
					<span class="row-label">Contour lines</span>
					<span class="switch" aria-hidden="true"></span>
				</button>
			</div>

			<div class="section">
				<div class="section-title">Transit lines</div>
				<ul class="mode-list">
					{#each MODES as m}
						<li>
							<span class="swatch" style="background:{m.color}"></span>
							<span class="mode-label">
								{m.label}{#if m.note}<span class="mode-note"> — {m.note}</span>{/if}
							</span>
						</li>
					{/each}
				</ul>

				<div class="encoding">
					<div
						class="speed-bar"
						style="background:linear-gradient(to right, {SPEED_RAMP.join(', ')})"
					></div>
					<div class="encoding-label">
						<span>slow</span>
						<span>fast</span>
					</div>
				</div>

				<div class="encoding">
					<svg class="freq-wedge" viewBox="0 0 64 12" preserveAspectRatio="none" aria-hidden="true">
						<path d="M0 6.6 L64 2 L64 10 L0 7.4 Z" fill="#c94040" />
					</svg>
					<div class="encoding-label">
						<span>infrequent</span>
						<span>frequent</span>
					</div>
				</div>
			</div>

			<div class="section about">
				Contact: <a href="mailto:info@steppenwolfx.ch">info@steppenwolfx.ch</a>
			</div>
		</div>
	{/if}
</div>

<style>
	.menu {
		position: relative;
	}

	.menu-toggle {
		background: #ffffff;
		border: none;
		border-radius: 999px;
		box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3);
		width: 2.1rem;
		height: 2.1rem;
		display: flex;
		align-items: center;
		justify-content: center;
		cursor: pointer;
		color: #333;
		padding: 0;
	}

	.menu-toggle.active {
		background: #333;
		color: #fff;
	}

	.panel {
		position: absolute;
		top: calc(2.1rem + 0.5rem);
		left: 0;
		z-index: 10;
		width: 15rem;
		max-height: calc(100vh - 5rem);
		max-height: calc(100dvh - 5rem);
		overflow-y: auto;
		background: #ffffff;
		border-radius: 0.9rem;
		box-shadow: 0 1px 4px rgba(0, 0, 0, 0.3);
		padding: 0.85rem 1rem;
		font-family: 'Saira', 'Helvetica Neue', Arial, sans-serif;
		user-select: none;
	}

	.section + .section {
		margin-top: 0.9rem;
		padding-top: 0.8rem;
		border-top: 1px solid #eee;
	}

	.section-title {
		font-size: 0.7rem;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: #999;
		margin-bottom: 0.45rem;
	}

	.view-toggle {
		display: flex;
		border-radius: 0.6rem;
		overflow: hidden;
		border: 1px solid #ddd;
		width: fit-content;
	}

	.view-toggle button {
		border: none;
		background: transparent;
		font-family: inherit;
		font-size: 0.85rem;
		line-height: 1.2;
		padding: 0.35rem 0.8rem;
		cursor: pointer;
		color: #333;
	}

	.view-toggle button.active {
		background: #333;
		color: #fff;
	}

	.row-toggle {
		display: flex;
		align-items: center;
		gap: 0.55rem;
		width: 100%;
		border: none;
		background: transparent;
		padding: 0.15rem 0;
		cursor: pointer;
		font-family: inherit;
		font-size: 0.85rem;
		color: #222;
	}

	.row-toggle svg {
		flex: 0 0 auto;
		color: #6a4a24;
	}

	.row-label {
		flex: 1 1 auto;
		text-align: left;
		line-height: 1.25;
	}

	.switch {
		flex: 0 0 auto;
		position: relative;
		width: 1.7rem;
		height: 1rem;
		border-radius: 999px;
		background: #ccc;
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
		background: #fff;
		transition: left 0.15s ease;
	}

	.row-toggle.active .switch {
		background: #333;
	}

	.row-toggle.active .switch::after {
		left: calc(100% - 1rem + 2px);
	}

	.mode-list {
		list-style: none;
		margin: 0;
		padding: 0;
	}

	.mode-list li {
		display: flex;
		align-items: center;
		gap: 0.55rem;
		padding: 0.14rem 0;
	}

	.swatch {
		flex: 0 0 auto;
		width: 1.6rem;
		height: 5px;
		border-radius: 999px;
	}

	.mode-label {
		font-size: 0.85rem;
		color: #222;
		line-height: 1.25;
	}

	.mode-note {
		color: #888;
		font-size: 0.75rem;
	}

	.encoding {
		margin-top: 0.65rem;
	}

	.speed-bar {
		width: 100%;
		height: 5px;
		border-radius: 999px;
	}

	.freq-wedge {
		width: 100%;
		height: 12px;
		display: block;
	}

	.encoding-label {
		display: flex;
		justify-content: space-between;
		font-size: 0.8rem;
		color: #444;
	}

	.about {
		font-size: 0.8rem;
		color: #888;
	}

	.about a {
		color: #666;
	}
</style>
