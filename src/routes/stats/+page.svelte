<script lang="ts">
	import { onMount } from 'svelte';
	import { loadStationIndex, type StationEntry } from '$lib/routing/stationIndex';

	let { data } = $props();
	const stats = $derived(data.stats);

	// Station index for resolving route-pair tokens to names. Loaded
	// client-side — map assets are nginx-served and never exist inside
	// the server build (deployment.md § SSR constraints).
	let stationIndex = $state<Map<string, StationEntry> | null>(null);

	onMount(() => {
		// Splash lives in app.html and is only hidden by Map.svelte —
		// hide it here too (same pattern as the about page).
		const s = document.getElementById('kora-splash');
		if (s) {
			s.classList.add('kora-splash-hidden');
			setTimeout(() => s.remove(), 400);
		}
		loadStationIndex().then((idx) => (stationIndex = idx));
	});

	/** Nearest station to a coord, within maxM metres. */
	function nearestStation(lat: number, lon: number, maxM: number): StationEntry | null {
		if (!stationIndex) return null;
		const cosLat = Math.cos((lat * Math.PI) / 180);
		let best: StationEntry | null = null;
		let bestD = Infinity;
		for (const e of stationIndex.values()) {
			const dLat = (e.c[1] - lat) * 111_320;
			const dLon = (e.c[0] - lon) * 111_320 * cosLat;
			const d = dLat * dLat + dLon * dLon;
			if (d < bestD) {
				bestD = d;
				best = e;
			}
		}
		return best && bestD <= maxM * maxM ? best : null;
	}

	/** Reverse lookup parent stop id ("Parentch:1:sloid:7000") → index
	 *  entry, for the SLOID-scheme "p:" tokens (sloid-stop-identity.md). */
	const byParentId = $derived.by(() => {
		const m = new Map<string, StationEntry>();
		if (stationIndex) for (const e of stationIndex.values()) if (e.p) m.set(e.p, e);
		return m;
	});

	/** "u:<uic>" | "p:<parent stop id>" | "c:<lat>,<lon>" | "?:<raw>" →
	 *  display name. A label logged with the request (fromName/toName)
	 *  wins over any guess. */
	function resolveToken(token: string, loggedName?: string): string {
		if (token.startsWith('u:')) {
			const uic = token.slice(2);
			return stationIndex?.get(uic)?.n ?? `UIC ${uic}`;
		}
		if (token.startsWith('p:')) {
			const pid = token.slice(2);
			return byParentId.get(pid)?.n ?? pid.replace(/^Parent/, '');
		}
		if (loggedName) return loggedName;
		if (token.startsWith('c:')) {
			const [lat, lon] = token.slice(2).split(',').map(Number);
			const near = nearestStation(lat, lon, 300);
			return near ? `≈ ${near.n}` : `${lat.toFixed(3)}, ${lon.toFixed(3)}`;
		}
		return token.slice(2);
	}

	/** App deep link for a route pair, or null when a "p:" token cannot be
	 *  resolved to its UIC (index not loaded / unknown station). */
	function routeLink(r: { linkFrom?: string; linkTo?: string }): string | null {
		if (!r.linkFrom || !r.linkTo) return null;
		const tok = (t: string) => (t.startsWith('p:') ? byParentId.get(t.slice(2))?.u ?? null : t);
		const from = tok(r.linkFrom);
		const to = tok(r.linkTo);
		if (!from || !to) return null;
		return `/?${new URLSearchParams({ from, to }).toString()}`;
	}

	const fmt = new Intl.NumberFormat('de-CH');
</script>

<svelte:head>
	<meta name="robots" content="noindex" />
</svelte:head>

<div class="page">
	<nav class="topbar">
		<a class="back" href="/">
			<span class="material-symbols-outlined back-arrow">chevron_right</span>
			back to map
		</a>
		<a class="brandmark" href="/" aria-label="Kora Maps">
			<img src="/kora_maps_landscape.svg" alt="Kora Maps" />
		</a>
	</nav>

	<header class="head">
		<h1>Usage</h1>
		{#if stats.available}
			<p class="sub">
				Over the retained log window ({stats.days.length}
				{stats.days.length === 1 ? 'day' : 'days'}): {fmt.format(stats.totalHits)} requests,
				{fmt.format(stats.totalPlans)} routing queries, {fmt.format(stats.totalUniqueIps)} unique
				IPs. Bots excluded from all figures.
			</p>
		{/if}
	</header>

	{#if !stats.available}
		<p class="notice">
			No access log found at <code>{stats.logPath}</code>. On the server this needs the per-site
			<code>access_log</code> nginx directive and read permission for the app user; locally there is
			nothing to show.
		</p>
	{:else}
		<section>
			<h2>Per day</h2>
			<table>
				<thead>
					<tr>
						<th>Day</th>
						<th class="num">Requests</th>
						<th class="num">Routing queries</th>
						<th class="num">Unique IPs</th>
						<th class="num muted">Bots</th>
					</tr>
				</thead>
				<tbody>
					{#each stats.days as d (d.day)}
						<tr>
							<td>{d.day}</td>
							<td class="num">{fmt.format(d.hits)}</td>
							<td class="num">{fmt.format(d.planRequests)}</td>
							<td class="num">{fmt.format(d.uniqueIps)}</td>
							<td class="num muted">{fmt.format(d.botHits)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</section>

		<section>
			<h2>Recent route requests</h2>
			{#if stats.recentRoutes.length === 0}
				<p class="notice">No routing queries in the retained log window.</p>
			{:else}
				<table>
					<thead>
						<tr>
							<th>Time</th>
							<th>From</th>
							<th>To</th>
							<th></th>
						</tr>
					</thead>
					<tbody>
						{#each stats.recentRoutes as r, i (i)}
							{@const link = routeLink(r)}
							<tr>
								<td class="time">{r.time}</td>
								<td>{resolveToken(r.from, r.fromName)}</td>
								<td>{resolveToken(r.to, r.toName)}</td>
								<td class="num">
									{#if link}
										<a class="route-link" href={link} target="_blank" rel="noopener">open ↗</a>
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
				<p class="legend">
					≈ marks a map-picked point shown as its nearest station (within 300 m); raw coordinates
					appear when no station is close.
				</p>
			{/if}
		</section>
	{/if}
</div>

<style>
	/* The map page locks html/body to overflow:hidden; on this scrolling
	   page, re-enable normal document flow (same as the about page). */
	:global(html),
	:global(body) {
		overflow: auto;
		height: auto;
		background: #f6f1e6;
	}

	.page {
		font-family: var(--font-ui);
		color: #2a241c;
		max-width: 46rem;
		margin: 0 auto;
		padding: 0 1.5rem 4rem;
	}

	.topbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 1.1rem 0;
	}
	.back {
		display: inline-flex;
		align-items: center;
		gap: 0.15rem;
		font-size: 0.9rem;
		font-weight: 600;
		color: #6e6155;
		text-decoration: none;
		transition: color 0.15s ease;
	}
	.back:hover {
		color: var(--brand);
	}
	.back-arrow {
		font-size: 20px;
		transform: rotate(180deg);
	}
	.brandmark img {
		height: 2.5rem;
		width: auto;
		display: block;
	}

	.head {
		margin: 1.5rem 0 2rem;
	}
	h1 {
		font-size: 2rem;
		font-weight: 700;
		margin-bottom: 0.4rem;
	}
	.sub {
		color: #6e6155;
		max-width: 38rem;
	}

	section {
		margin-bottom: 2.5rem;
	}
	h2 {
		font-size: 1.15rem;
		font-weight: 600;
		margin-bottom: 0.7rem;
	}

	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.92rem;
	}
	th {
		text-align: left;
		font-weight: 600;
		color: #6e6155;
		border-bottom: 2px solid #e4dccb;
		padding: 0.35rem 0.6rem;
	}
	td {
		border-bottom: 1px solid #ece5d6;
		padding: 0.35rem 0.6rem;
	}
	.num {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
	.muted {
		color: #a3988a;
	}
	.time {
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
		color: #6e6155;
	}

	.notice {
		color: #6e6155;
	}
	code {
		font-size: 0.85em;
		background: #ece5d6;
		padding: 0.1em 0.3em;
		border-radius: 4px;
	}
	.legend {
		margin-top: 0.6rem;
		font-size: 0.8rem;
		color: #a3988a;
	}
	.route-link {
		font-size: 0.85em;
		font-weight: 600;
		color: var(--brand);
		text-decoration: none;
		white-space: nowrap;
	}
	.route-link:hover {
		text-decoration: underline;
	}
</style>
