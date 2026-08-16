// Autocomplete request scheduler (geocoding-search.md § Rate limiting and
// request coalescing).
//
//   - At most one request in flight at a time.
//   - Minimum `minIntervalMs` between two fires (measured start-to-start).
//   - Exactly one pending slot: new input during flight or cooldown REPLACES
//     the pending entry rather than queueing behind it.
//   - Results are delivered via `onResult` only when they still represent
//     the user's latest intent — stale in-flight results (superseded by a
//     newer pending input) are dropped.
//   - A request identical to the last completed one short-circuits.

export interface SchedulerOptions<TResult> {
	minIntervalMs: number;
	fetcher: (query: string, signal: AbortSignal) => Promise<TResult>;
	onResult: (result: TResult, query: string) => void;
}

export class AutocompleteScheduler<TResult> {
	private minIntervalMs: number;
	private fetcher: (query: string, signal: AbortSignal) => Promise<TResult>;
	private onResult: (result: TResult, query: string) => void;

	private inflightQuery: string | null = null;
	private inflightAbort: AbortController | null = null;
	private lastFireTime = 0;
	private pending: string | null = null;
	private pendingTimer: ReturnType<typeof setTimeout> | null = null;
	private lastCompletedQuery: string | null = null;

	constructor(opts: SchedulerOptions<TResult>) {
		this.minIntervalMs = opts.minIntervalMs;
		this.fetcher = opts.fetcher;
		this.onResult = opts.onResult;
	}

	/** Ask the scheduler to fetch results for `query`. May fire now, later, or
	 * not at all (if superseded before the cooldown ends). */
	request(query: string): void {
		if (query === this.lastCompletedQuery && this.inflightQuery !== query) {
			// Already delivered exactly this — nothing to do.
			this.pending = null;
			this.clearPendingTimer();
			return;
		}
		if (this.inflightQuery === query) {
			// Same query already flying — don't double-fire; keep no pending.
			this.pending = null;
			this.clearPendingTimer();
			return;
		}
		this.pending = query;
		this.tryFire();
	}

	/** Cancel any pending / in-flight work (component teardown). Does not
	 * touch `lastCompletedQuery` — the cache survives so a re-mount with the
	 * same query doesn't re-fetch. */
	dispose(): void {
		this.pending = null;
		this.clearPendingTimer();
		if (this.inflightAbort) this.inflightAbort.abort();
		this.inflightAbort = null;
		this.inflightQuery = null;
	}

	private clearPendingTimer() {
		if (this.pendingTimer !== null) {
			clearTimeout(this.pendingTimer);
			this.pendingTimer = null;
		}
	}

	private tryFire() {
		if (this.pending === null) return;
		if (this.inflightQuery !== null) return; // wait for in-flight to finish
		const wait = this.minIntervalMs - (Date.now() - this.lastFireTime);
		if (wait > 0) {
			if (this.pendingTimer === null) {
				this.pendingTimer = setTimeout(() => {
					this.pendingTimer = null;
					this.tryFire();
				}, wait);
			}
			return;
		}
		this.fire();
	}

	private fire() {
		const query = this.pending;
		if (query === null) return;
		this.pending = null;
		this.clearPendingTimer();
		this.lastFireTime = Date.now();
		const ac = new AbortController();
		this.inflightQuery = query;
		this.inflightAbort = ac;
		this.fetcher(query, ac.signal).then(
			(result) => {
				// Deliver only if the just-fired query still represents user
				// intent: either nothing newer is pending, or the pending
				// slot happens to hold the same string (user backspaced to it).
				if (this.pending === null || this.pending === query) {
					this.lastCompletedQuery = query;
					this.onResult(result, query);
					if (this.pending === query) this.pending = null;
				}
			},
			() => {
				// Abort or fetcher error — drop silently.
			}
		).finally(() => {
			this.inflightQuery = null;
			this.inflightAbort = null;
			this.tryFire();
		});
	}
}
