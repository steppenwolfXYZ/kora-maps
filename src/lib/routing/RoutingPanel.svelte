<script lang="ts">
	import { tick, untrack } from 'svelte';
	import EndpointInput from './EndpointInput.svelte';
	import TimeSelector from './TimeSelector.svelte';
	import ResultCard from './ResultCard.svelte';
	import ConnectGrid from './ConnectGrid.svelte';
	import { computeCardStates } from './ranking';
	import { routingState } from './state.svelte';
	import { recentRoutes, endpointLabel, type RecentRoute } from './recents.svelte';
	import { itineraryFingerprint } from './fingerprint';
	import type { Endpoint, Itinerary, Leg } from './types';

	let { onFocusLeg, onEnterMapMode, onFrameRoute, getMapCenter = () => null }: {
		onFocusLeg?: (leg: Leg) => void;
		onEnterMapMode?: (it: Itinerary) => void;
		onFrameRoute?: (it: Itinerary) => void;
		getMapCenter?: () => [number, number] | null;
	} = $props();

	// Shared-only mode (connection-sharing.md § Shared view) renders just the
	// verified shared connection; ranking badges are suppressed there — a
	// single card comparing against itself would always wear the crown.
	let displayed = $derived(routingState.displayedResults);
	let cardStates = $derived(computeCardStates(displayed));
	// Loading-edge suppression: the card at the time-advancing edge (last
	// for leave-at, first for arrive-by) is hidden while it carries a
	// very-slow warning — that is exactly the card retroactive pruning
	// may remove once the next batch loads its dominators, which would
	// make it visibly vanish mid-scroll (every observed vanish case wore
	// the very-slow warning). Hidden, the next batch either prunes it
	// (nothing changes on screen) or keeps it, at which point it is no
	// longer the edge card and appears. Never applied to a sole result,
	// the shared view, or the currently selected connection.
	let cards = $derived.by(() => {
		const items = displayed.map((it, i) => ({ it, state: cardStates[i] }));
		if (routingState.sharedOnly || items.length <= 1) return items;
		const arrive = routingState.mode === 'arrive';
		const edge = arrive ? 0 : items.length - 1;
		const e = items[edge];
		if (
			e?.state?.warnings.some((w) => w.kind === 'very-slow') &&
			itineraryFingerprint(e.it) !== routingState.selectedFingerprint
		) {
			return arrive ? items.slice(1) : items.slice(0, -1);
		}
		return items;
	});
	// "Route set" (routing-persistence.md § Definitions): both endpoints
	// finally set. Drives the clear button and the when/recents swap.
	let routeSet = $derived(!!routingState.from && !!routingState.to);

	// Recents list: 10 rows collapsed, the full stored list (30) after
	// "Show more". Collapses again with the panel remount.
	const RECENTS_COLLAPSED = 10;
	let recentsExpanded = $state(false);
	let visibleRecents = $derived(
		recentsExpanded ? recentRoutes.list : recentRoutes.list.slice(0, RECENTS_COLLAPSED)
	);

	// No-route-set tabs (routing-persistence.md § Connect): Connect default.
	let noRouteTab = $state<'connect' | 'recent'>('connect');

	// Connect board drag result: a full pair loads the route in one shot
	// (current mode/time kept); a half connection through an empty cell
	// clears that side and puts the cursor there.
	function connectRoute(from: Endpoint | null, to: Endpoint | null) {
		if (from && to) {
			routingState.loadRoute({
				from, to, mode: routingState.mode, time: routingState.time
			});
		} else if (from) {
			routingState.setTo(null);
			routingState.setFrom(from);
			void tick().then(() => toInput?.focusSearch());
		} else if (to) {
			routingState.setFrom(null);
			routingState.setTo(to);
			void tick().then(() => fromInput?.focusSearch());
		}
	}

	let resultsEl: HTMLDivElement | null = $state(null);
	let fromInput: EndpointInput | undefined = $state();
	let toInput: EndpointInput | undefined = $state();

	// Open-time cursor placement: openPanel records which endpoint field
	// should receive focus; consume it once when the panel mounts. Remounts
	// (e.g. exiting mobile map mode) find the request already cleared.
	$effect(() => {
		const side = routingState.consumeFocusRequest();
		if (side === 'from') fromInput?.focusSearch();
		else if (side === 'to') toInput?.focusSearch();
	});
	// After a query finishes (loading false→true→false), scroll the
	// selected card into view — arrive-by auto-selects the last result,
	// which sits at the bottom and would otherwise be off-screen. No-op
	// for leave-at (first card is already at the top) and for user-clicked
	// selections (the card is already visible, block:'nearest' won't
	// scroll). loadMore doesn't toggle `loading`, so it never fires here.
	let wasLoading = false;
	$effect(() => {
		const isLoading = routingState.loading;
		if (wasLoading && !isLoading && resultsEl) {
			// Fresh result set: reset the autoscroll edge state before any
			// programmatic scrolling below fires scroll events.
			hasScrolledDown = false;
			armedEarlier = true;
			armedLater = true;
			// The scrollIntoView below can land the list at the bottom edge
			// (arrive-by selects the last card) — briefly suppress the edge
			// sentinels so that placement doesn't auto-load later results.
			suppressAutoUntil = performance.now() + 400;
			const fp = untrack(() => routingState.selectedFingerprint);
			if (fp) {
				const results = untrack(() => routingState.displayedResults);
				const idx = results.findIndex((it) => itineraryFingerprint(it) === fp);
				if (idx >= 0) {
					const card = resultsEl.querySelectorAll('.card')[idx] as HTMLElement | undefined;
					card?.scrollIntoView({ block: 'nearest' });
				}
			}
		}
		wasLoading = isLoading;
	});

	// ── Autoscroll (replaces the earlier/later load-more buttons) ──────
	// Bottom edge: reaching it by scrolling loads later connections.
	// Top edge: starts gesture-gated (an upward wheel/touch gesture that
	// BEGINS while resting at the top loads earlier connections); once
	// the user has scrolled down, arriving back at the top behaves like
	// the bottom edge and triggers on its own. A direction that returned
	// nothing new is disarmed for position-based triggers and only a
	// fresh gesture retries it. Wheel/touch gestures also work when the
	// content is too short to scroll (e.g. the shared-connection view).
	const EDGE_EPS = 2;
	const GESTURE_GAP_MS = 300;
	const TOUCH_THRESHOLD = 12;

	let hasScrolledDown = false;
	let armedEarlier = true;
	let armedLater = true;
	let suppressAutoUntil = 0;
	let lastWheelT = 0;
	let wheelFromTop = false;
	let wheelFromBottom = false;
	let touchStartY = 0;
	let touchFromTop = false;
	let touchFromBottom = false;
	let touchFired = false;

	function atTop(el: HTMLElement): boolean {
		return el.scrollTop <= EDGE_EPS;
	}
	function atBottom(el: HTMLElement): boolean {
		return el.scrollTop + el.clientHeight >= el.scrollHeight - EDGE_EPS;
	}
	function inFlight(): boolean {
		return routingState.loading || !!routingState.loadingMore;
	}
	function canExtend(): boolean {
		return routingState.hasQueried && displayed.length > 0;
	}

	function onScroll() {
		const el = resultsEl;
		if (!el || !canExtend()) return;
		if (el.scrollTop > EDGE_EPS) hasScrolledDown = true;
		if (inFlight() || performance.now() < suppressAutoUntil) return;
		if (armedLater && atBottom(el) && el.scrollHeight > el.clientHeight + EDGE_EPS) {
			void triggerLater();
		} else if (armedEarlier && hasScrolledDown && atTop(el)) {
			void triggerEarlier();
		}
	}

	function onWheel(e: WheelEvent) {
		const el = resultsEl;
		if (!el || !canExtend()) return;
		const now = performance.now();
		// A pause between wheel events starts a new gesture; momentum
		// events of a fling arrive faster and stay in the old one, so a
		// fling that merely arrives at an edge cannot trigger.
		if (now - lastWheelT > GESTURE_GAP_MS) {
			wheelFromTop = atTop(el);
			wheelFromBottom = atBottom(el);
		}
		lastWheelT = now;
		if (inFlight()) return;
		if (e.deltaY < 0 && wheelFromTop && atTop(el)) {
			wheelFromTop = false; // one trigger per gesture
			void triggerEarlier();
		} else if (e.deltaY > 0 && wheelFromBottom && atBottom(el)) {
			wheelFromBottom = false;
			void triggerLater();
		}
	}

	function onTouchStart(e: TouchEvent) {
		const el = resultsEl;
		if (!el) return;
		touchStartY = e.touches[0].clientY;
		touchFromTop = atTop(el);
		touchFromBottom = atBottom(el);
		touchFired = false;
	}

	function onTouchMove(e: TouchEvent) {
		const el = resultsEl;
		if (!el || touchFired || !canExtend() || inFlight()) return;
		const dy = e.touches[0].clientY - touchStartY;
		// Finger moving down = scrolling up (earlier); up = later.
		if (dy > TOUCH_THRESHOLD && touchFromTop && atTop(el)) {
			touchFired = true;
			void triggerEarlier();
		} else if (dy < -TOUCH_THRESHOLD && touchFromBottom && atBottom(el)) {
			touchFired = true;
			void triggerLater();
		}
	}

	async function triggerLater() {
		if (inFlight() || !canExtend()) return;
		armedLater = false;
		routingState.exitSharedOnly();
		const prevCount = displayed.length;
		await routingState.loadMoreLater();
		if (displayed.length > prevCount) armedLater = true;
	}

	async function triggerEarlier() {
		if (inFlight() || !canExtend()) return;
		armedEarlier = false;
		routingState.exitSharedOnly();
		const prevCount = displayed.length;
		const p = routingState.loadMoreEarlier();
		// Let the top loader row enter the DOM uncompensated (it nudges
		// the cards down as visible feedback), then compensate every
		// further top-side height change — the streamed prepends and the
		// loader's removal — so the card the user is looking at never
		// moves once results start arriving.
		await tick();
		compensateTop = true;
		try {
			await p;
			await tick();
		} finally {
			compensateTop = false;
		}
		if (displayed.length > prevCount) armedEarlier = true;
	}

	// Scroll-position preservation while earlier results stream in: the
	// pre-effect snapshots the scroll metrics before each DOM update
	// caused by result/loader changes, the post-effect restores the
	// visual position by the measured growth. Manual on purpose — native
	// scroll anchoring is not reliable across browsers for this.
	let compensateTop = false;
	let topPrePending = false;
	let topPreHeight = 0;
	let topPreTop = 0;
	$effect.pre(() => {
		void displayed.length;
		void routingState.loadingMore;
		if (!compensateTop || !resultsEl) return;
		topPreHeight = resultsEl.scrollHeight;
		topPreTop = resultsEl.scrollTop;
		topPrePending = true;
	});
	$effect(() => {
		void displayed.length;
		void routingState.loadingMore;
		if (!topPrePending || !resultsEl) return;
		topPrePending = false;
		const delta = resultsEl.scrollHeight - topPreHeight;
		if (delta !== 0) resultsEl.scrollTop = topPreTop + delta;
	});

	// Main routing shell. Replaces the map menu / stop search top-controls
	// while open (Map.svelte decides visibility). Runs a query whenever
	// both endpoints are set and any input changes. Dedup lives in the
	// store (see `lastQueryKey` in state.svelte.ts) so a bare remount —
	// e.g. exiting mobile map mode — doesn't refetch.
	$effect(() => {
		const from = routingState.from;
		const to = routingState.to;
		void routingState.mode;
		void routingState.time;
		void routingState.timeVersion;
		if (!from || !to) return;
		void routingState.runQuery();
	});

	function clearRoute() {
		routingState.clearRoute();
		// The From input remounts in its empty-search form — focus lands
		// there so the user can start the next route immediately.
		void tick().then(() => fromInput?.focusSearch());
	}

	function pickRecent(r: RecentRoute) {
		// Past date/time falls back to "depart now" — only here, on recents
		// selection; URL loads and in-session restores keep past times
		// (routing-persistence.md § Constraints).
		const past = r.time !== null && Date.parse(r.time) < Date.now();
		routingState.loadRoute({
			from: r.from,
			to: r.to,
			mode: past ? 'leave' : r.mode,
			time: past ? null : r.time
		});
	}

	// Local-calendar day key so day-boundary markers respect the viewer's TZ.
	function dayKey(iso: string): string {
		const d = new Date(iso);
		return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
	}
	const dayFmt = new Intl.DateTimeFormat(undefined, {
		weekday: 'short', day: 'numeric', month: 'short'
	});
	function fmtDay(iso: string): string {
		return dayFmt.format(new Date(iso));
	}
	// Reference day for the first result: the requested query time (arrive-by
	// or leave-at), or now if none set. If the first itinerary already sits
	// on a later day, it gets a marker too.
	function baselineIso(): string {
		return routingState.time ?? new Date().toISOString();
	}
</script>

<div class="routing-panel" role="dialog" aria-label="Route planning">
	<div class="rp-head">
		<span class="rp-title">
			<span class="material-symbols-outlined rp-title-icon" aria-hidden="true">directions</span>
			Route
		</span>
		{#if routeSet}
			<button class="rp-clear" onclick={clearRoute}>
				<span class="material-symbols-outlined" aria-hidden="true">delete</span>
				Clear route
			</button>
		{/if}
		<button
			class="rp-close icon-btn"
			onclick={() => routingState.closePanel()}
			aria-label="Close route planning"
		>×</button>
	</div>

	<div class="rp-endpoints">
		<div class="rp-inputs">
			<EndpointInput
				bind:this={fromInput}
				label="From"
				endpoint={routingState.from}
				placeholder="Start"
				onChange={(ep) => routingState.setFrom(ep)}
				otherIsCurrent={routingState.to?.type === 'current'}
			/>
			<EndpointInput
				bind:this={toInput}
				label="To"
				endpoint={routingState.to}
				placeholder="Destination"
				onChange={(ep) => routingState.setTo(ep)}
				otherIsCurrent={routingState.from?.type === 'current'}
			/>
		</div>
		<button
			class="rp-swap icon-btn"
			onclick={() => routingState.swap()}
			aria-label="Swap start and destination"
		>
			<span class="material-symbols-outlined">swap_vert</span>
		</button>
	</div>

	<div class="rp-when">
		<TimeSelector
			mode={routingState.mode}
			time={routingState.time}
			onMode={(m) => routingState.setMode(m)}
			onTime={(t) => routingState.setTime(t)}
		/>
	</div>

	{#if !routeSet}
		<!-- No-route-set view (routing-persistence.md § Connect): tabbed
		     Connect grid / Recent list below the when-controls, until both
		     endpoints are set. -->
		<div class="rp-suggest">
			<div class="rp-tabs" role="group" aria-label="Suggestions">
				<button
					class:active={noRouteTab === 'connect'}
					onclick={() => (noRouteTab = 'connect')}
				>Connect</button>
				<button
					class:active={noRouteTab === 'recent'}
					onclick={() => (noRouteTab = 'recent')}
				>Recent</button>
			</div>
			{#if noRouteTab === 'connect'}
				<ConnectGrid {getMapCenter} onConnect={connectRoute} />
			{:else if recentRoutes.list.length > 0}
				<div class="rp-recents">
					{#each visibleRecents as r (r.at)}
						<button class="rp-recent" onclick={() => pickRecent(r)}>
							<span class="rp-recent-ep">{endpointLabel(r.from)}</span>
							<span class="material-symbols-outlined rp-recent-arrow" aria-hidden="true">chevron_right</span>
							<span class="rp-recent-ep">{endpointLabel(r.to)}</span>
						</button>
					{/each}
					{#if !recentsExpanded && recentRoutes.list.length > RECENTS_COLLAPSED}
						<button class="rp-recents-more" onclick={() => (recentsExpanded = true)}>
							Show more
						</button>
					{/if}
				</div>
			{:else}
				<div class="rp-status">No recent routes yet</div>
			{/if}
		</div>
	{/if}

	{#if routingState.hasQueried || routingState.sharedExpired}
	<div class="rp-results-sep" aria-hidden="true"></div>
	<!-- Touch handlers only extend the scroll gesture (autoscroll edge
	     triggers), they add no interactive semantics. -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="rp-results"
		bind:this={resultsEl}
		onscroll={onScroll}
		onwheel={onWheel}
		ontouchstart={onTouchStart}
		ontouchmove={onTouchMove}
	>
			{#if routingState.sharedExpired}
				<div class="rp-status rp-error">
					This shared connection is no longer available — the timetable
					has likely changed since it was shared.
				</div>
			{/if}
			{#if routingState.loading}
				<div class="rp-loading" role="status" aria-label="Searching for connections">
					<div class="loading-track"><div class="loading-ball"></div></div>
				</div>
			{:else if routingState.error}
				<div class="rp-status rp-error">{routingState.error}</div>
			{:else if displayed.length === 0}
				{#if routingState.hasQueried}
					<div class="rp-status">No connections found</div>
				{/if}
			{:else}
				{#if routingState.selectionInvalid}
					<div class="rp-status rp-error">
						The saved route is no longer valid. Pick one below.
					</div>
				{/if}
				{#if routingState.loadingMore === 'earlier'}
					<div class="rp-inline-loader" role="status" aria-label="Loading earlier connections">
						<div class="loading-track loading-track-inline"><div class="loading-ball"></div></div>
					</div>
				{/if}
				{#each cards as { it, state }, i (i)}
					{@const prevIso = i === 0 ? baselineIso() : cards[i - 1].it.startTime}
					{#if i === 0 || dayKey(it.startTime) !== dayKey(prevIso)}
						<div class="rp-day-marker">{fmtDay(it.startTime)}</div>
					{/if}
					<ResultCard
						itinerary={it}
						badge={routingState.sharedOnly ? null : state?.badge ?? null}
						warnings={state?.warnings ?? []}
						{onFocusLeg}
						{onEnterMapMode}
						{onFrameRoute}
					/>
				{/each}
				{#if routingState.loadingMore === 'later'}
					<div class="rp-inline-loader" role="status" aria-label="Loading later connections">
						<div class="loading-track loading-track-inline"><div class="loading-ball"></div></div>
					</div>
				{/if}
			{/if}
		</div>
	{/if}
</div>

<style>
	.routing-panel {
		/* Width of the swap / × trailing column — keeps the head row's
		   right-alignment in sync with the endpoints row. */
		--rp-tail-col: 1.85rem;
		width: 22rem;
		max-height: calc(100vh - 2rem);
		max-height: calc(100dvh - 2rem);
		/* Brand-gradient hairline along the top edge, white below. The
		   layered background (not border-top) follows the top corner
		   radius and stays put over the scrolling results. */
		background: var(--gradient-brand) top / 100% 3px no-repeat, var(--white);
		border-radius: 0.9rem;
		box-shadow: var(--shadow-control);
		padding: 0.7rem 0.85rem 0.85rem;
		font-family: var(--font-ui);
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		overflow: hidden;
	}
	/* Narrow breakpoint: keep in sync with NARROW_BREAKPOINT in
	   ./layout.ts — the routing panel becomes a full-bleed page. */
	@media (max-width: 699px) {
		.routing-panel {
			width: 100%;
			flex: 1 1 auto;
			min-width: 0;
			max-height: 100vh;
			max-height: 100dvh;
			border-radius: 0;
		}
	}

	.rp-head {
		display: flex;
		align-items: center;
		/* Same gap as .rp-endpoints so the clear button's right edge
		   aligns with the From input's right edge (the × column below
		   mirrors the swap column's width). */
		gap: 0.35rem;
	}
	/* Visible pill button. margin-left:auto right-aligns it; the ×
	   column right of it matches the swap column (see .rp-close), so
	   its right edge lines up with the From input's right edge. */
	.rp-clear {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		margin-left: auto;
		border: none;
		background: var(--gray-100);
		font-family: inherit;
		font-size: 0.72rem;
		line-height: 1.2;
		color: var(--gray-800);
		padding: 0.25rem 0.6rem 0.25rem 0.45rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
	}
	.rp-clear :global(.material-symbols-outlined) {
		font-size: 0.9rem;
		line-height: 1;
	}
	.rp-clear:hover {
		background: var(--gray-200);
		color: var(--brand);
	}
	.rp-title {
		display: inline-flex;
		align-items: center;
		gap: 0.4rem;
		font-size: 0.7rem;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--anthracite);
	}
	.rp-title-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 1.5rem;
		height: 1.5rem;
		border-radius: var(--radius-pill);
		background: var(--gradient-brand);
		font-size: 1rem;
		line-height: 1;
		color: var(--white);
	}
	/* Base look + hover from .icon-btn (app.css); sizing only here.
	   Fixed width mirrors the swap column (--rp-tail-col) so whatever
	   sits left of the × right-aligns with the From input. Without a
	   clear button the × pins itself right via margin-left:auto; with
	   one, the clear button's auto margin takes over (two auto margins
	   would split the free space and float the clear button mid-row). */
	.rp-close {
		font-size: 1.25rem;
		line-height: 1;
		padding: 0.15rem 0;
		width: var(--rp-tail-col);
		margin-left: auto;
	}
	.rp-clear + .rp-close {
		margin-left: 0;
	}

	.rp-endpoints {
		display: flex;
		flex-direction: row;
		align-items: center;
		gap: 0.35rem;
		position: relative;
	}
	.rp-inputs {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		flex: 1 1 auto;
		min-width: 0;
	}
	/* Base look + hover from .icon-btn (app.css); sizing only here. */
	.rp-swap {
		flex: 0 0 auto;
		padding: 0.25rem 0;
		width: var(--rp-tail-col);
	}
	.rp-swap :global(.material-symbols-outlined) { font-size: 1.15rem; line-height: 1; }

	/* Hairline + extra air separates the suggestions block from the
	   search criteria above (same line style as .rp-results-sep). */
	.rp-suggest {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		border-top: 1px solid var(--gray-100);
		margin-top: 0.15rem;
		padding-top: 0.55rem;
		overflow-y: auto;
	}
	/* Segmented toggle per ux-guidelines.md: no container border,
	   inactive segments gray with dark text, active segment gradient
	   with white text. */
	.rp-tabs {
		display: flex;
		width: fit-content;
		border-radius: var(--radius-pill);
		overflow: hidden;
	}
	.rp-tabs button {
		border: none;
		background: var(--gray-100);
		font-family: inherit;
		font-size: 0.78rem;
		line-height: 1.2;
		color: var(--gray-800);
		padding: 0.3rem 0.8rem;
		cursor: pointer;
	}
	.rp-tabs button.active {
		background: var(--gradient-brand);
		color: var(--white);
	}
	.rp-recents {
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
	}
	.rp-recent {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		border: none;
		background: transparent;
		font-family: inherit;
		font-size: 0.85rem;
		line-height: 1.25;
		color: var(--gray-800);
		padding: 0.3rem 0.4rem;
		border-radius: 0.5rem;
		cursor: pointer;
		text-align: left;
		min-width: 0;
	}
	.rp-recent:hover {
		background: var(--gray-100);
	}
	.rp-recent-ep {
		flex: 0 1 auto;
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.rp-recent-arrow {
		flex: 0 0 auto;
		font-size: 1rem;
		color: var(--gray-400);
	}
	.rp-recents-more {
		align-self: flex-start;
		border: none;
		background: transparent;
		font-family: inherit;
		font-size: 0.78rem;
		color: var(--gray-500);
		padding: 0.3rem 0.4rem;
		border-radius: var(--radius-pill);
		cursor: pointer;
	}
	.rp-recents-more:hover {
		background: var(--gray-100);
		color: var(--gray-800);
	}

	.rp-results-sep {
		/* Sits outside the scroll container so it never scrolls — the line
		   stays pinned between the search criteria and the results. As a
		   panel flex child it spans the panel content box, so its edges
		   align with the cards (the scrollbar gutter is carved out only
		   on .rp-results via its negative margin). */
		border-top: 1px solid var(--gray-100);
		height: 0;
		/* Tighten the panel gap below so the first card sits where it did
		   when the line was a border-top on .rp-results with padding-top. */
		margin-bottom: -0.25rem;
	}
	.rp-results {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		overflow-y: auto;
		/* Pull the scroll container into the panel's right padding so the
		   overlay scrollbar paints there instead of over the cards, then
		   inset the cards by the same amount so their right edge stays
		   aligned with the panel content box (symmetric with the left).
		   Negative margin + matching padding keeps the card width
		   unchanged. */
		margin-right: -0.75rem;
		padding-right: 0.75rem;
	}
	.rp-status {
		font-size: 0.85rem;
		color: var(--gray-500);
		padding: 0.35rem 0.15rem;
	}
	.rp-error { color: #a11; }

	.rp-loading {
		padding: 0.5rem 0.15rem;
	}
	/* Bouncing-ball loader (Ogoy-style): a full-width anthracite pill
	   with a gradient border; the ball swings side-to-side and fades
	   kora-green ↔ kora-brown over each half-cycle. The border gradient
	   is deliberately HORIZONTAL (not the diagonal brand angle): the
	   ball's fade tracks its x-position, so at any moment the border
	   above/below the ball has the ball's own color. */
	.loading-track {
		position: relative;
		width: 100%;
		height: var(--loader-h, 2.2rem);
		border: 2px solid transparent;
		border-radius: var(--radius-pill);
		background: linear-gradient(var(--anthracite), var(--anthracite)) padding-box,
			linear-gradient(90deg, var(--kora-green), var(--kora-brown)) border-box;
	}
	/* Compact variant for the in-list earlier/later loaders. */
	.loading-track-inline { --loader-h: 1.5rem; }
	.rp-inline-loader { padding: 0.1rem 0; }
	.loading-ball {
		position: absolute;
		top: 2px;
		bottom: 2px;
		aspect-ratio: 1;
		border-radius: 50%;
		animation: loader-bounce 1s infinite alternate ease-in-out;
	}
	/* Ball diameter = track height 2.2rem − 2×2px border − 2×2px gap;
	   the right endpoint offsets by exactly that plus the gap. Position
	   and color share the keyframe timeline, so the ball's color always
	   matches the border at its x. */
	@keyframes loader-bounce {
		from {
			left: 2px;
			background-color: var(--kora-green);
		}
		to {
			left: calc(100% - var(--loader-h, 2.2rem) + 6px);
			background-color: var(--kora-brown);
		}
	}

	.rp-day-marker {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.72rem;
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		/* Uppercase micro-title — anthracite per ux-guidelines.md. */
		color: var(--anthracite);
		padding: 0.25rem 0.1rem 0.1rem;
	}
	.rp-day-marker::before,
	.rp-day-marker::after {
		content: '';
		flex: 1 1 auto;
		height: 1px;
		background: #e5e5e5;
	}

</style>
