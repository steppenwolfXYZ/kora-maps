<script lang="ts">
	import { tick, untrack } from 'svelte';
	import { browser } from '$app/environment';
	import EndpointInput from './EndpointInput.svelte';
	import TimeSelector from './TimeSelector.svelte';
	import ResultCard from './ResultCard.svelte';
	import DirectRouteCard from './DirectRouteCard.svelte';
	import ConnectGrid from './ConnectGrid.svelte';
	import RoutingOptions from './RoutingOptions.svelte';
	import { computeCardStates } from './ranking';
	import { fmtDuration } from './itineraryFormat';
	import { routingOptions } from './options.svelte';
	import { rankOptionsFor, routingState } from './state.svelte';
	import { recentRoutes, endpointLabel, type RecentRoute } from './recents.svelte';
	import { loadStationIndex, type StationEntry } from './stationIndex';
	import { loadHfGondolaRoutes, modeMidColor } from './legColor';
	import { itineraryFingerprint } from './fingerprint';
	import type { Endpoint, Itinerary, Leg } from './types';

	let { onFocusLeg, onEnterMapMode, onFrameRoute, onFrameDirectRoutes, getMapCenter = () => null }: {
		onFocusLeg?: (leg: Leg) => void;
		onEnterMapMode?: (it: Itinerary) => void;
		onFrameRoute?: (it: Itinerary) => void;
		/** Frame all shown direct cycling / walking alternatives
		 * (pedestrian-bicycle-routing.md). */
		onFrameDirectRoutes?: () => void;
		getMapCenter?: () => [number, number] | null;
	} = $props();

	// Direct cycling / walking tab active (pedestrian-bicycle-routing.md
	// § Mode tabs): no time controls, no options, no vias; results are
	// DirectRouteCards instead of the transit connection cards.
	let direct = $derived(routingState.travelMode !== 'transit');

	// Shared-only mode (connection-sharing.md § Shared view) renders just the
	// verified shared connection; ranking badges are suppressed there — a
	// single card comparing against itself would always wear the crown.
	let displayed = $derived(routingState.displayedResults);
	// Continuous-gondola routes (routing-options.md § Connection
	// warnings) — loaded once, warnings recompute when it arrives.
	let hfGondolas = $state<Set<string> | null>(null);
	$effect(() => {
		void loadHfGondolaRoutes().then((s) => { hfGondolas = s; });
	});
	let cardStates = $derived(computeCardStates(displayed, {
		...rankOptionsFor(),
		hfGondolaRoutes: hfGondolas
	}));
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

	// Station colors for the recent-route stop boxes — same source and
	// fallback chain as the Connect tiles (ConnectGrid.tileGrad): baked
	// average → dominant color from the search index, else a tint→tone
	// of the mode mid-color, else null (CSS anthracite fallback).
	let stationIdx = $state<Map<string, StationEntry> | null>(null);
	$effect(() => {
		void loadStationIndex().then((idx) => { if (idx) stationIdx = idx; });
	});
	function epGrad(ep: Endpoint): { a: string; b: string } | null {
		if (ep.type !== 'station') return null;
		const e = stationIdx?.get(ep.uic);
		if (e?.ca && e?.cd) return { a: e.ca, b: e.cd };
		const mid = modeMidColor(ep.mode);
		return mid ? { a: `color-mix(in srgb, ${mid} 72%, #fff)`, b: mid } : null;
	}

	// Kept in sync with EndpointInput's STATION_MODE_ICON / endpointIcon.
	const RECENT_MODE_ICON: Record<string, string> = {
		train:        'train',
		metro:        'subway',
		tram:         'tram',
		bus:          'directions_bus',
		regional_bus: 'directions_bus',
		ferry:        'directions_boat',
		mountain:     'gondola_lift'
	};
	// No 'current' branch: recents never contain current-location
	// endpoints (materialized at record time, legacy entries filtered
	// out on read — see recents.svelte.ts).
	function epIcon(ep: Endpoint): string {
		if (ep.type === 'point') return ep.kind === 'poi' ? 'place' : 'home_work';
		if (ep.type !== 'station') return 'directions_transit_filled';
		return (ep.mode && RECENT_MODE_ICON[ep.mode]) || 'directions_transit_filled';
	}

	// No-route-set tabs (routing-persistence.md § Connect): Connect default;
	// the last choice persists in localStorage across panel opens/reloads.
	const TAB_KEY = 'kora.routing.suggestTab';
	function readStoredTab(): 'connect' | 'recent' {
		if (!browser) return 'connect';
		try {
			return localStorage.getItem(TAB_KEY) === 'recent' ? 'recent' : 'connect';
		} catch {
			return 'connect';
		}
	}
	let noRouteTab = $state<'connect' | 'recent'>(readStoredTab());
	function pickTab(tab: 'connect' | 'recent') {
		noRouteTab = tab;
		try {
			localStorage.setItem(TAB_KEY, tab);
		} catch {
			// Storage unavailable — the choice still holds this session.
		}
	}

	// Connect board drag result: a full pair loads the route in one shot
	// (current mode/time kept); a half connection through an empty cell
	// clears that side and puts the cursor there. Either way the filled
	// side's input must leave search mode — the panel's open-time cursor
	// placement may have left it there, and a lingering (empty) search
	// form would hide the endpoint the drag just set.
	function connectRoute(from: Endpoint | null, to: Endpoint | null) {
		if (from && to) {
			fromInput?.stopEdit();
			toInput?.stopEdit();
			routingState.loadRoute({
				from, to, mode: routingState.mode, time: routingState.time
			});
		} else if (from) {
			fromInput?.stopEdit();
			routingState.setTo(null);
			routingState.setFrom(from);
			void tick().then(() => toInput?.focusSearch());
		} else if (to) {
			toInput?.stopEdit();
			routingState.setFrom(null);
			routingState.setTo(to);
			void tick().then(() => fromInput?.focusSearch());
		}
	}

	let resultsEl: HTMLDivElement | null = $state(null);
	let fromInput: EndpointInput | undefined = $state();
	let toInput: EndpointInput | undefined = $state();
	// One entry per via row, in row order (via-stops.md § Panel UI).
	let viaInputs: (EndpointInput | undefined)[] = $state([]);

	/** Insert an empty via row and put the cursor in it — adding a stop is
	 * always followed by naming it. */
	function addViaAt(index: number) {
		routingState.insertViaAt(index);
		void tick().then(() => viaInputs[index]?.focusSearch());
	}

	// The To row's `+` demotes the destination to a via and opens a fresh
	// one below. Only a station can make that move — vias are stations.
	let canSplitDestination = $derived(
		routingState.to?.type === 'station' && routingState.canAddVia
	);

	function splitDestination() {
		if (!routingState.promoteToToVia()) return;
		void tick().then(() => toInput?.focusSearch());
	}

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
			// Fresh result set: drop any half-open pull band.
			resetPull();
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

	// ── Pull-to-load at the list edges (touch only) ────────────────────
	// Overscrolling past an edge opens a rubber-band band carrying an
	// arrow that turns around once the pull passes the threshold; the
	// load fires only on RELEASE while past it. Pulling back before
	// releasing cancels, so merely arriving at an edge — or flinging into
	// one — can never load anything. Pointer devices have no release
	// event and get the .rp-more-btn buttons instead (which are also the
	// only path a keyboard or screen reader has to extend the list), so
	// there is deliberately no wheel handling here at all.
	const PULL_THRESHOLD = 52; // damped px that arm the load
	const PULL_MAX = 84; // asymptotic rubber-band limit
	const RELEASE_MS = 260; // spring-back duration (matches the CSS)
	const EDGE_EPS = 2;

	let pullDir = $state<'earlier' | 'later' | null>(null);
	let pull = $state(0);
	let releasing = $state(false);
	let pullArmed = $derived(pull >= PULL_THRESHOLD);
	let pullOffset = $derived(pullDir === 'later' ? -pull : pull);

	let rawPull = 0;
	let releaseTimer: ReturnType<typeof setTimeout> | null = null;
	let touchLastY = 0;
	let touchFromTop = false;
	let touchFromBottom = false;

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
		// Earlier / later only exist on the transit tab — a direct query
		// has no time axis to extend along.
		return routingState.travelMode === 'transit'
			&& routingState.hasQueried && displayed.length > 0;
	}
	// Diminishing returns: unbounded raw travel maps onto 0..PULL_MAX, so
	// the band always feels like it is resisting.
	function damp(x: number): number {
		return PULL_MAX * (1 - Math.exp(-x / PULL_MAX));
	}

	function addPull(dir: 'earlier' | 'later', delta: number) {
		if (releaseTimer) {
			clearTimeout(releaseTimer);
			releaseTimer = null;
			releasing = false;
		}
		if (pullDir !== dir) rawPull = 0;
		rawPull = Math.max(0, rawPull + delta);
		pull = damp(rawPull);
		pullDir = rawPull > 0 ? dir : null;
	}

	/** End the open pull. Loads only when released past the threshold. */
	function endPull(fire: boolean) {
		if (!pullDir) return;
		const dir = pullDir;
		const go = fire && pullArmed && !inFlight() && canExtend();
		rawPull = 0;
		pull = 0;
		releasing = true;
		if (releaseTimer) clearTimeout(releaseTimer);
		releaseTimer = setTimeout(() => {
			releaseTimer = null;
			releasing = false;
			pullDir = null;
		}, RELEASE_MS);
		if (go) void (dir === 'earlier' ? triggerEarlier() : triggerLater());
	}

	function resetPull() {
		if (releaseTimer) clearTimeout(releaseTimer);
		releaseTimer = null;
		rawPull = 0;
		pull = 0;
		releasing = false;
		pullDir = null;
	}

	function onTouchStart(e: TouchEvent) {
		const el = resultsEl;
		if (!el) return;
		touchLastY = e.touches[0].clientY;
		const ok = canExtend() && !inFlight();
		touchFromTop = ok && atTop(el);
		touchFromBottom = ok && atBottom(el);
	}

	function onTouchMove(e: TouchEvent) {
		const el = resultsEl;
		if (!el) return;
		const y = e.touches[0].clientY;
		const dy = y - touchLastY;
		touchLastY = y;
		// Finger moving down = pulling the top edge (earlier); up = later.
		// The browser can have committed the gesture to a scroll before
		// our first preventDefault, which makes the following moves
		// non-cancelable — harmless at an edge (there is nothing left to
		// scroll), but calling preventDefault anyway logs an
		// intervention warning, so ask first.
		if (pullDir) {
			if (e.cancelable) e.preventDefault();
			addPull(pullDir, pullDir === 'earlier' ? dy : -dy);
		} else if (dy > 0 && touchFromTop && atTop(el)) {
			if (e.cancelable) e.preventDefault();
			addPull('earlier', dy);
		} else if (dy < 0 && touchFromBottom && atBottom(el)) {
			if (e.cancelable) e.preventDefault();
			addPull('later', -dy);
		}
	}

	// Registered by hand rather than as markup handlers: an open band has
	// to swallow the gesture (preventDefault), which needs listeners that
	// are explicitly non-passive.
	$effect(() => {
		const el = resultsEl;
		if (!el) return;
		const end = () => endPull(true);
		const cancel = () => endPull(false);
		el.addEventListener('touchstart', onTouchStart, { passive: true });
		el.addEventListener('touchmove', onTouchMove, { passive: false });
		el.addEventListener('touchend', end, { passive: true });
		el.addEventListener('touchcancel', cancel, { passive: true });
		return () => {
			el.removeEventListener('touchstart', onTouchStart);
			el.removeEventListener('touchmove', onTouchMove);
			el.removeEventListener('touchend', end);
			el.removeEventListener('touchcancel', cancel);
		};
	});

	async function triggerLater() {
		if (inFlight() || !canExtend()) return;
		routingState.exitSharedOnly();
		await routingState.loadMoreLater();
	}

	async function triggerEarlier() {
		if (inFlight() || !canExtend()) return;
		routingState.exitSharedOnly();
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

	// More-options expander (routing-options.md § UI). Session-local —
	// the persisted values are in options.svelte.ts, only the open/closed
	// state resets with the panel.
	let optionsOpen = $state(false);

	// Main routing shell. Replaces the map menu / stop search top-controls
	// while open (Map.svelte decides visibility). Runs a query whenever
	// both endpoints are set and any input changes. Dedup lives in the
	// store (see `lastQueryKey` in state.svelte.ts) so a bare remount —
	// e.g. exiting mobile map mode — doesn't refetch. All three routing
	// options are query params (minimize walking drives the fork's
	// walk-point table — routing-options.md § Minimize walking), so
	// they re-trigger too.
	$effect(() => {
		const from = routingState.from;
		const to = routingState.to;
		void routingState.mode;
		void routingState.travelMode;
		void routingState.time;
		void routingState.timeVersion;
		// Via stops and their waits are query params too (via-stops.md).
		// Reading the rows themselves (not just the array) makes a wait
		// change re-trigger.
		for (const v of routingState.vias) { void v.station; void v.wait; }
		void routingOptions.walkSpeed;
		void routingOptions.safety;
		void routingOptions.minimizeWalking;
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
			vias: r.vias,
			mode: past ? 'leave' : r.mode,
			time: past ? null : r.time
		});
	}

	/** A recent entry's stops in travel order — From, its vias, To
	 * (via-stops.md § Persistence and sharing). `wait` is null on the two
	 * endpoints and the requested minutes on a via, which is also what the
	 * box's tooltip spells out. */
	function recentChain(r: RecentRoute): {
		ep: Endpoint; wait: number | null; title: string;
	}[] {
		const vias = (r.vias ?? []).map((v) => ({
			ep: v.station as Endpoint,
			wait: v.wait,
			title: v.wait > 0
				? `Via ${endpointLabel(v.station)} — wait ${fmtDuration(v.wait * 60)}`
				: `Via ${endpointLabel(v.station)}`
		}));
		return [
			{ ep: r.from, wait: null, title: endpointLabel(r.from) },
			...vias,
			{ ep: r.to, wait: null, title: endpointLabel(r.to) }
		];
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

{#snippet swapButton()}
	<!-- Shared between the transit tab (inside TimeSelector's timing row)
	     and the direct tabs' slim control row — the endpoints and their
	     swap are common to all three modes. -->
	<button
		class="rp-swap icon-btn"
		onclick={() => routingState.swap()}
		aria-label="Reverse the route"
		title="Reverse the route"
	>
		<span class="material-symbols-outlined">swap_vert</span>
	</button>
{/snippet}

{#snippet addStop(enabled: boolean, onClick: () => void, title: string)}
	<!-- The column is always reserved, so every field keeps the same width
	     whether or not another stop may be added. -->
	{#if enabled}
		<button class="rp-add icon-btn" onclick={onClick} aria-label={title} {title}>+</button>
	{:else}
		<span class="rp-add-spacer" aria-hidden="true"></span>
	{/if}
{/snippet}

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

	<!-- Travel-mode tabs (pedestrian-bicycle-routing.md § Mode tabs):
	     public transit, cycling, walking. Real tabs in the house tab style
	     (same language as the Connect / Recent tabs below), not a
	     segmented toggle. -->
	<div class="rp-travel" role="tablist" aria-label="Travel mode">
		<button
			role="tab"
			class:active={routingState.travelMode === 'transit'}
			aria-selected={routingState.travelMode === 'transit'}
			onclick={() => routingState.setTravelMode('transit')}
		>
			<span class="material-symbols-outlined" aria-hidden="true">directions_transit</span>
			Transit
		</button>
		<button
			role="tab"
			class:active={routingState.travelMode === 'bike'}
			aria-selected={routingState.travelMode === 'bike'}
			onclick={() => routingState.setTravelMode('bike')}
		>
			<span class="material-symbols-outlined" aria-hidden="true">directions_bike</span>
			Cycling
		</button>
		<button
			role="tab"
			class:active={routingState.travelMode === 'walk'}
			aria-selected={routingState.travelMode === 'walk'}
			onclick={() => routingState.setTravelMode('walk')}
		>
			<span class="material-symbols-outlined" aria-hidden="true">directions_walk</span>
			Walking
		</button>
	</div>

	<!-- Endpoint rows (via-stops.md § Panel UI). The "+" of each row sits
	     in its own column right of the field, not inside it: it acts on the
	     row rather than on the field's value, and the column is the one the
	     swap button used to occupy before moving down to the timing row. -->
	<div class="rp-endpoints">
		<div class="rp-row">
			<EndpointInput
				bind:this={fromInput}
				label="From"
				endpoint={routingState.from}
				placeholder="Start"
				onChange={(ep) => {
					routingState.setFrom(ep);
					// Picking a From with no To yet: move the cursor on so the
					// destination can be typed right away.
					if (ep && !routingState.to) void tick().then(() => toInput?.focusSearch());
				}}
				otherIsCurrent={routingState.to?.type === 'current'}
				onRefreshCurrent={() => routingState.refreshCurrentLocation()}
				mixedRanking={direct}
			/>
			{@render addStop(
				!direct && routingState.from !== null && routingState.canAddVia,
				() => addViaAt(0),
				'Add a stop after the start'
			)}
		</div>
		{#each direct ? [] : routingState.vias as v, i}
			<div class="rp-row">
				<EndpointInput
					bind:this={viaInputs[i]}
					via
					label="Via"
					endpoint={v.station}
					placeholder="Stop on the way"
					wait={v.wait}
					onChange={(ep) => {
						if (ep) routingState.setVia(i, ep);
						else routingState.removeVia(i);
					}}
					onWait={(m) => routingState.setViaWait(i, m)}
				/>
				{@render addStop(
					v.station !== null && routingState.canAddVia,
					() => addViaAt(i + 1),
					'Add a stop after this one'
				)}
			</div>
		{/each}
		<div class="rp-row">
			<EndpointInput
				bind:this={toInput}
				label="To"
				endpoint={routingState.to}
				placeholder="Destination"
				onChange={(ep) => routingState.setTo(ep)}
				otherIsCurrent={routingState.from?.type === 'current'}
				onRefreshCurrent={() => routingState.refreshCurrentLocation()}
				mixedRanking={direct}
			/>
			{@render addStop(
				!direct && canSplitDestination,
				splitDestination,
				'Continue past the destination — it becomes a stop on the way'
			)}
		</div>
	</div>

	{#if !direct}
		<!-- Date/time controls and the more-options area belong to the
		     transit tab only (pedestrian-bicycle-routing.md § Mode tabs). -->
		<div class="rp-when">
			<TimeSelector
				mode={routingState.mode}
				time={routingState.time}
				onMode={(m) => routingState.setMode(m)}
				onTime={(t) => routingState.setTime(t)}
				{optionsOpen}
				optionsModified={!routingOptions.isDefault}
				onToggleOptions={() => (optionsOpen = !optionsOpen)}
			>
				{#snippet options()}<RoutingOptions />{/snippet}
				{#snippet swap()}{@render swapButton()}{/snippet}
			</TimeSelector>
		</div>
	{:else}
		<!-- Direct tabs keep only the swap control. -->
		<div class="rp-direct-row">
			{@render swapButton()}
		</div>
	{/if}

	{#if !routeSet}
		<!-- No-route-set view (routing-persistence.md § Connect): tabbed
		     Connect grid / Recent list below the when-controls, until both
		     endpoints are set. -->
		<div class="rp-suggest">
			<div class="rp-tabs" role="tablist" aria-label="Suggestions">
				<button
					role="tab"
					aria-selected={noRouteTab === 'connect'}
					class:active={noRouteTab === 'connect'}
					onclick={() => pickTab('connect')}
				>Connect</button>
				<button
					role="tab"
					aria-selected={noRouteTab === 'recent'}
					class:active={noRouteTab === 'recent'}
					onclick={() => pickTab('recent')}
				>Recent</button>
			</div>
			<div class="rp-suggest-scroll">
			{#if noRouteTab === 'connect'}
				<ConnectGrid {getMapCenter} onConnect={connectRoute} />
			{:else if recentRoutes.list.length > 0}
				<!-- One row per route (click runs it): the two stops as
				     boxes, chevron between. Truncated names surface in
				     full via the native title tooltip. -->
				<div class="rp-recents">
					{#each visibleRecents as r (r.at)}
						<button class="rr-row" onclick={() => pickRecent(r)}>
							{#each recentChain(r) as step, i (i)}
								{#if i > 0}
									<span class="material-symbols-outlined rr-arrow" aria-hidden="true">chevron_right</span>
								{/if}
								{@const g = epGrad(step.ep)}
								<span
									class="rr-ep"
									class:ep-point={step.ep.type === 'point'}
									class:rr-via={step.wait !== null}
									style:--tile-a={g?.a}
									style:--tile-b={g?.b}
									title={step.title}
								>
									<span class="material-symbols-outlined rr-ep-icon" aria-hidden="true">{epIcon(step.ep)}</span>
									<span class="rr-ep-text">{endpointLabel(step.ep)}</span>
								</span>
							{/each}
						</button>
					{/each}
					{#if !recentsExpanded && recentRoutes.list.length > RECENTS_COLLAPSED}
						<button class="rr-more" onclick={() => (recentsExpanded = true)}>
							Show more
						</button>
					{/if}
				</div>
			{:else}
				<div class="rp-status">No recent routes yet</div>
			{/if}
			</div>
		</div>
	{/if}

	{#if routingState.hasQueried || routingState.sharedExpired}
	<div class="rp-results-sep" aria-hidden="true"></div>
	<!-- The wrapper clips the rubber-band travel of .rp-results and hosts
	     the pull bands the travel uncovers. Touch handlers are attached
	     to .rp-results in script (non-passive). -->
	<!-- The transform is applied only while a band is open: a permanent
	     one would turn .rp-results into the containing block of every
	     position:fixed popup rendered from a card. -->
	<div class="rp-results-wrap">
	{#if pullDir}
		<div
			class="rp-pull rp-pull-{pullDir}"
			class:armed={pullArmed}
			class:releasing
			style="height: {pull}px"
			aria-hidden="true"
		>
			<div class="rp-pull-row">
				<span class="material-symbols-outlined rp-pull-arrow">
					{pullDir === 'earlier' ? 'arrow_upward' : 'arrow_downward'}
				</span>
				<span>{pullDir === 'earlier' ? 'Earlier' : 'Later'} connections</span>
			</div>
		</div>
	{/if}
	<div
		class="rp-results"
		class:releasing
		bind:this={resultsEl}
		style={pullDir ? `transform: translateY(${pullOffset}px)` : ''}
	>
			{#if routingState.sharedExpired}
				<div class="rp-status rp-error">
					This shared connection is no longer available — the timetable
					has likely changed since it was shared.
				</div>
			{/if}
			{#if routingState.loading}
				<div class="rp-loading" role="status">
					<div class="rp-loading-head">
						<img class="rp-loading-mark" src="/icon.svg" alt="" draggable="false" />
						{#if routingState.loadingPruned}
							<span class="rp-loading-note">Bad route options were removed</span>
						{/if}
						<span class="rp-loading-text">
							{routingState.loadingStatus ?? 'Route options are loading...'}
						</span>
					</div>
					<div class="loading-track"><div class="loading-ball"></div></div>
				</div>
			{:else if routingState.error}
				<div class="rp-status rp-error">{routingState.error}</div>
			{:else if direct}
				<!-- Direct cycling / walking results: one card per returned
				     alternative (pedestrian-bicycle-routing.md § Result
				     cards). No earlier/later — alternatives all come from
				     the single query. -->
				{#if routingState.directRoutes.length === 0}
					{#if routingState.hasQueried}
						<div class="rp-status">No route found</div>
					{/if}
				{:else}
					{#each routingState.directRoutes as r, i (i)}
						<DirectRouteCard route={r} index={i} onFrameRoutes={onFrameDirectRoutes} />
					{/each}
				{/if}
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
				{:else}
					<button class="rp-more-btn" onclick={() => void triggerEarlier()}>
						<span class="material-symbols-outlined" aria-hidden="true">arrow_upward</span>
						Earlier connections
					</button>
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
				{:else}
					<button class="rp-more-btn" onclick={() => void triggerLater()}>
						<span class="material-symbols-outlined" aria-hidden="true">arrow_downward</span>
						Later connections
					</button>
				{/if}
			{/if}
		</div>
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

	/* Travel-mode tabs (pedestrian-bicycle-routing.md § Mode tabs). Same
	   house tab language as .rp-tabs below (baseline rule, active tab on
	   a gradient underline), plus the mode icon per tab. */
	.rp-travel {
		display: flex;
		gap: 1.1rem;
		border-bottom: 1px solid var(--gray-100);
	}
	.rp-travel button {
		position: relative;
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		border: none;
		background: transparent;
		font-family: inherit;
		font-size: 0.78rem;
		font-weight: 600;
		line-height: 1.2;
		letter-spacing: 0.03em;
		color: var(--gray-500);
		padding: 0.25rem 0.1rem 0.4rem;
		cursor: pointer;
	}
	.rp-travel button :global(.material-symbols-outlined) {
		font-size: 1.05rem;
		line-height: 1;
	}
	.rp-travel button:hover {
		color: var(--brand);
	}
	.rp-travel button.active {
		color: var(--anthracite);
	}
	/* Sits on the container's baseline rule (bottom: -1px covers it). */
	.rp-travel button.active::after {
		content: '';
		position: absolute;
		left: 0;
		right: 0;
		bottom: -1px;
		height: 2px;
		background: var(--gradient-brand-input);
	}

	/* Slim control row of the direct tabs: the shared swap button pinned
	   to the tail column. */
	.rp-direct-row {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		min-height: 2rem;
	}

	.rp-endpoints {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		position: relative;
	}
	/* One endpoint field plus its trailing "+" column. */
	.rp-row {
		display: flex;
		flex-direction: row;
		align-items: center;
		gap: 0.35rem;
		min-width: 0;
	}
	.rp-row > :global(.ep-row) {
		flex: 1 1 auto;
		min-width: 0;
	}
	/* Base look + hover from .icon-btn (app.css); sizing only here. The
	   spacer holds the same width when no "+" is offered, so all fields
	   stay flush regardless of how many stops the chain already has. */
	.rp-add {
		flex: 0 0 auto;
		width: var(--rp-tail-col);
		font-size: 1.15rem;
		line-height: 1;
		padding: 0.15rem 0;
	}
	.rp-add-spacer {
		flex: 0 0 auto;
		width: var(--rp-tail-col);
	}
	/* Base look + hover from .icon-btn (app.css); sizing only here. Lives
	   in the timing row now (TimeSelector's `swap` snippet). */
	.rp-swap {
		flex: 0 0 auto;
		padding: 0 0.35rem;
	}
	.rp-swap :global(.material-symbols-outlined) { font-size: 1.15rem; line-height: 1; display: block; }

	/* Hairline + extra air separates the suggestions block from the
	   search criteria above (same line style as .rp-results-sep). */
	.rp-suggest {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		border-top: 1px solid var(--gray-100);
		margin-top: 0.15rem;
		padding-top: 0.55rem;
		/* Flex children don't shrink below content height by default —
		   without this the block overflows the panel (whose overflow is
		   hidden) and the scroll area inside never engages. */
		min-height: 0;
	}
	/* Scroll area below the (pinned) tab bar. Same scrollbar-gutter
	   trick as .rp-results: pull into the panel's right padding so the
	   overlay scrollbar paints there, inset the content back by the
	   same amount. */
	.rp-suggest-scroll {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		overflow-y: auto;
		min-height: 0;
		/* Left side mirrors the right-side gutter trick so full-bleed
		   children (the recent-row hover bands) can reach the panel
		   edges via negative margins without tripping overflow-x. */
		margin: 0 -0.75rem 0 -0.85rem;
		padding: 0 0.75rem 0 0.85rem;
	}
	/* Real tabs, not a segmented toggle (that shape is reserved for the
	   leave-at/arrive-by control): text labels on a shared baseline rule,
	   the active tab marked by a gradient underline. Steep gradient
	   variant per ux-guidelines.md — the underline is a thin wide element. */
	.rp-tabs {
		display: flex;
		gap: 1.1rem;
		border-bottom: 1px solid var(--gray-100);
	}
	.rp-tabs button {
		position: relative;
		border: none;
		background: transparent;
		font-family: inherit;
		font-size: 0.78rem;
		font-weight: 600;
		line-height: 1.2;
		letter-spacing: 0.03em;
		color: var(--gray-500);
		padding: 0.25rem 0.1rem 0.4rem;
		cursor: pointer;
	}
	.rp-tabs button:hover {
		color: var(--brand);
	}
	.rp-tabs button.active {
		color: var(--anthracite);
	}
	/* Sits on the container's baseline rule (bottom: -1px covers it). */
	.rp-tabs button.active::after {
		content: '';
		position: absolute;
		left: 0;
		right: 0;
		bottom: -1px;
		height: 2px;
		background: var(--gradient-brand-input);
	}
	.rp-recents {
		display: flex;
		flex-direction: column;
		align-items: stretch;
		gap: 0.35rem;
		flex-shrink: 0;
	}
	/* Whole row loads the route; hovering paints a soft blue band across
	   the full panel width (square corners). The negative margins bleed
	   into the scroll container's padding (see .rp-suggest-scroll); the
	   compensating padding keeps the boxes aligned with the panel
	   content. */
	.rr-row {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		min-width: 0;
		border: none;
		background: transparent;
		font-family: inherit;
		margin: 0 -0.75rem 0 -0.85rem;
		padding: 0.3rem 1.05rem 0.3rem 1.15rem;
		cursor: pointer;
	}
	.rr-row:hover {
		background: var(--gray-200);
	}
	/* Station-colored stop boxes, same recipe as the Connect tiles:
	   average → dominant line color at 135°, white text/icons.
	   --tile-a/--tile-b come inline from epGrad; anthracite is the
	   no-color fallback. Point endpoints get a flat utility fill
	   instead (recents never contain current-location endpoints). */
	.rr-ep {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		flex: 0 1 auto;
		min-width: 0;
		--tile-a: color-mix(in srgb, var(--anthracite) 72%, #fff);
		--tile-b: var(--anthracite);
		background: linear-gradient(135deg, var(--tile-a) 0%, var(--tile-b) 100%);
		border-radius: 0.45rem;
		font-size: 0.85rem;
		line-height: 1.25;
		color: var(--white);
		padding: 0.28rem 0.55rem;
		text-align: left;
	}

	/* A via box in a recent row is a stop on the way, not an endpoint —
	   same tile, one shade quieter, and it yields row space to the two
	   endpoints when names are long. */
	.rr-via {
		opacity: 0.82;
		max-width: 32%;
	}
	.rr-ep.ep-point { background: #7b7b7b; }
	.rr-ep-icon {
		flex: 0 0 auto;
		font-size: 1rem;
		color: var(--white);
	}
	.rr-ep-text {
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.rr-arrow {
		flex: 0 0 auto;
		font-size: 1rem;
		color: var(--gray-400);
	}
	.rr-more {
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
	.rr-more:hover {
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
	.rp-results-wrap {
		position: relative;
		display: flex;
		flex-direction: column;
		min-height: 0;
		/* Clips the rubber-band travel of .rp-results (which is moved by
		   a transform, so it never changes the scroll metrics) and the
		   part of a pull band that is not uncovered yet. */
		overflow: hidden;
		/* Pull the scroll container into the panel's right padding so the
		   overlay scrollbar paints there instead of over the cards; the
		   matching padding on .rp-results insets the cards again so their
		   right edge stays aligned with the panel content box (symmetric
		   with the left) and the card width is unchanged. The negative
		   margin lives here, on the clipping box, so the scrollbar is not
		   clipped away. */
		margin-right: -0.75rem;
	}
	.rp-results {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		overflow-y: auto;
		/* No native overscroll bounce or scroll chaining — the pull band
		   below is the app's own overscroll affordance. */
		overscroll-behavior-y: none;
		flex: 1 1 auto;
		min-height: 0;
		padding-right: 0.75rem;
	}
	.rp-results.releasing {
		transition: transform 0.26s ease-out;
	}

	/* Pull-to-load bands. They sit at the very top / bottom of the
	   wrapper, under the scroll container, and are uncovered as it is
	   pushed away by the pull. The arrow points along the finger's pull
	   direction until the threshold is passed, then turns around — an up
	   arrow at the top edge, a down arrow at the bottom edge — and the
	   row lights up as a gradient pill: release now and the connections
	   load. Deliberately not brand red, which reads as an error. */
	.rp-pull {
		position: absolute;
		left: 0;
		right: 0.75rem;
		display: flex;
		justify-content: center;
		overflow: hidden;
		color: var(--gray-500);
		pointer-events: none;
	}
	.rp-pull.releasing {
		transition: height 0.26s ease-out;
	}
	.rp-pull-earlier {
		top: 0;
		align-items: flex-end;
	}
	.rp-pull-later {
		bottom: 0;
		align-items: flex-start;
	}
	.rp-pull-row {
		display: flex;
		align-items: center;
		gap: 0.3rem;
		height: 1.8rem;
		padding: 0 0.7rem;
		border-radius: var(--radius-pill);
		font-size: 0.78rem;
		font-weight: 600;
		white-space: nowrap;
	}
	/* Armed: the house treatment for an active state — gradient fill,
	   white glyph and label (ux-guidelines.md § Usage rules). */
	.armed .rp-pull-row {
		background: var(--gradient-brand);
		color: var(--white);
	}
	.rp-pull-arrow {
		font-size: 1.15rem;
		/* Turned around while the pull is still short of the threshold. */
		transform: rotate(180deg);
		transition: transform 0.22s ease;
	}
	.armed .rp-pull-arrow {
		transform: rotate(0deg);
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
	/* Only the initial search is announced with the mark + wording; the
	   in-list earlier/later loaders stay bare pills. */
	.rp-loading-head {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.5rem;
		margin-bottom: 0.7rem;
	}
	.rp-loading-mark {
		height: 4.5rem;
		width: auto;
	}
	/* Explains a backwards-ticking option count: a later hop found
	   connections that dominate ones already listed. */
	.rp-loading-note {
		font-size: 0.75rem;
		color: var(--gray-500);
		text-align: center;
	}
	.rp-loading-text {
		font-size: 0.85rem;
		color: var(--gray-600);
		letter-spacing: 0.02em;
		text-align: center;
		text-wrap: balance;
		padding: 0 0.5rem;
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

	/* Earlier / later load buttons. Pointer devices have no release
	   event, so they get an explicit click target instead of the pull
	   band; the same button is the only way a keyboard or screen reader
	   can extend the list, so it is always in the DOM. On coarse
	   pointers the pull band covers the job and a permanent button would
	   read as an end-of-list wall, so it is visually hidden there —
	   except while focused, since an invisible focus target is worse
	   than a visible button. Brand red per ux-guidelines.md § Usage
	   rules: red glyph at rest, red fill on hover. */
	.rp-more-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 0.3rem;
		width: 100%;
		padding: 0.3rem 0.5rem;
		border: none;
		border-radius: var(--radius-pill);
		background: none;
		color: var(--brand);
		font-family: inherit;
		font-size: 0.78rem;
		font-weight: 600;
		cursor: pointer;
	}
	.rp-more-btn .material-symbols-outlined {
		font-size: 1.15rem;
	}
	.rp-more-btn:hover,
	.rp-more-btn:focus-visible {
		background: var(--brand);
		color: var(--white);
	}
	@media (hover: none), (pointer: coarse) {
		.rp-more-btn:not(:focus-visible) {
			position: absolute;
			width: 1px;
			height: 1px;
			padding: 0;
			margin: -1px;
			overflow: hidden;
			clip-path: inset(50%);
			white-space: nowrap;
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
