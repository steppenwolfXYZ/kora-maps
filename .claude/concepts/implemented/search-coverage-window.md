# Search coverage window

## Problem

The transit cascade has no notion of *what time span it has actually
searched*. It infers the search frontier from the results MOTIS returns:
the next hop starts one minute past the latest known departure (earliest
known arrival for backward hops). A MOTIS response is a Pareto set, not a
time slice — under minimize walking it routinely contains a walk-lighter
connection many hours out (a bus that only runs in the morning), which
then declares the whole night "searched". Viable connections in between
are never fetched, and the list a user sees depends on the path taken to
reach it (query at 22:30 + "later" ≠ query at 23:30).

The backward direction has the same disease and one more: MOTIS ignores
the `searchWindow` on arrive-by queries (a 900 s and a 7200 s window
return the identical set), so a backward hop cannot even ask for a
bounded span.

## Requirements

### Coverage as explicit state

- The cascade state carries the searched span explicitly, as two
  half-ranges anchored at the query time:
  - `departureCoverage` — every journey **departing** inside this span
    is known. Grows forward (leave-at hops, "later").
  - `arrivalCoverage` — every journey **arriving** inside this span is
    known. Grows backward (arrive-by hops, "earlier").
- A leave-at query starts with `departureCoverage = [t, t + initial
  window]` and an empty `arrivalCoverage`; an arrive-by query the mirror.
- **The shown list is a pure function of the query and its coverage**:
  it is the pruned, sorted, capped set of all journeys whose departure
  lies in `departureCoverage` or whose arrival lies in
  `arrivalCoverage`. Two paths that reach the same coverage show the
  same journeys for it. This is the property the whole concept exists
  for and is the acceptance criterion.

### Membership rule

- A journey returned by a query enters the candidate set (and the
  dedupe set) **only if its anchor lies inside the span that query
  searched** — its departure inside the forward window, its arrival
  inside the backward window. Anything else is discarded, not pooled:
  the hop that covers its time will return it again.
- The span a query searched is the interval MOTIS **reports** having
  searched (see § Fork prerequisites), not the window that was
  requested. MOTIS extends its own interval contiguously until it has
  enough journeys; that extension is exact coverage and is kept. A
  journey whose anchor lies outside the reported interval (the Pareto
  outlier) is dropped.
- Walk-only direct journeys are anchored at the query time and are
  always inside.

### Frontier advance

- A hop searches exactly `[frontier, frontier + W]` (forward) or
  `[frontier − W, frontier]` (backward), where `W` is the hop window.
- After a hop, coverage advances to the end of the **reported
  interval** — at least the requested window, further when MOTIS
  extended. An empty hop (MOTIS found nothing even after extending, or
  hit its own extension limit) still advances coverage by the reported
  interval, which is fully known. The separate "2 h step on empty" rule
  is subsumed.
- The empty-hop streak limit no longer bounds a night: MOTIS's own
  extension carries a hop across a service gap in one call, so the
  cascade is bounded by the maximum span and the request deadline only.
- There is no merge cap: a hop merges every journey inside the reported
  span. The cap existed to keep a batch from replacing the visible list;
  the settled display (below) makes that impossible, and merging whole
  spans means fewer hops.
- Coverage never advances from a result's anchor beyond the searched
  window. This is the single rule that fixes the reported bug.

### Extensions

- "Later" extends `departureCoverage` from its end; "earlier" extends
  `arrivalCoverage` from its start. In arrive-by mode the first forward
  hop seeds at the later of the departure-coverage end and the latest
  known departure + 1 min (mirror for the first backward hop in leave-at
  mode); afterwards seeds are coverage ends only.
- The sparse-service escalation reads its frontier from coverage, which
  is now exact rather than inferred.
- The stateless replay + cache model is unchanged: coverage is part of
  the cached state and is reproduced by the replay.

### Settled display

- Loading more connections never changes what is already on screen and
  never depends on the path that built the list. Both follow from one
  rule: a journey is **shown only once it is settled** — coverage
  reaches past the span in which any dominator of it could lie, so no
  later load can bring one in.
- A dominator departs before the dominated journey arrives (Pareto and
  comfort rules alike), give or take the ranking's time slack. So on
  the departure axis a journey is settled when coverage reaches its
  arrival + slack; on the arrival axis, when coverage reaches back to
  its departure − slack. Under minimize walking the reach grows by the
  reverse-displacement window (3 h), since a walk-lighter journey that
  far out on the primary axis may still displace.
- The cascade keeps hopping until the required number of *settled*
  survivors exists, so the initial query typically covers about one
  hop beyond the shown arrivals. That is the overfetch, and it is
  bounded by the reach, not by guesswork.
- **Side rule**: a journey on the far side of the query time — before a
  leave-at time, after an arrive-by time — never prunes one on the near
  side. Leaving earlier than asked is not an alternative to the
  connection asked for. Near-side journeys are pruned among themselves;
  far-side ones against everything. Without it, an "earlier" load
  could still retire a shown connection.
- Display quotas are per side: `forwardTarget` for the departure-
  covered side, `backwardTarget` for the arrival-covered side. The
  query's own side starts at five, the other at zero; every "later"
  adds five to the forward quota, every "earlier" five to the backward
  one. Each side shows its settled survivors nearest the query time
  (head of the forward side, tail of the backward side). Growth on one
  side therefore only appends at that side's far end.
- No pinning. The list is the same for the same coverage and quotas
  whichever clicks produced them.
- Residual: the comfort rule's reach is unbounded in principle (a
  drastically more comfortable journey far earlier can retire a much
  worse later one). That is accepted as the rare exception rather than
  papered over with pinning.

### Fork prerequisites

**Reported search interval**

- The plan response carries the interval the search actually covered,
  as two fork-only fields `koraSearchedFrom` / `koraSearchedTo` (ISO
  timestamps). For leave-at it is the departure span, for arrive-by the
  arrival span; it starts at the requested window and includes every
  contiguous extension the search made on its own.
- It is the sole source of coverage on the app side. The requested
  window is never used as a substitute, so a server without the fields
  cannot silently produce wrong coverage: the cascade treats a missing
  interval as a hard error.

**Arrive-by window must be honoured**

- MOTIS must bound arrive-by queries to `[t − searchWindow, t]` on the
  arrival axis, the mirror of what leave-at already does on the
  departure axis. The fork's plan endpoint currently builds an inverted
  interval for arrive-by (window sign flipped and then subtracted
  again), which is the presumed cause.
- Acceptance probe, against the local server, same endpoints as the
  reported case, `time=12:00Z`: `arriveBy=true&searchWindow=7200` must
  return arrivals reaching back toward 10:00; today it returns the same
  set as `searchWindow=900` (earliest arrival 11:35). Leave-at with
  `7200` already spans to 14:00 and must keep doing so.
- This is a prerequisite of the coverage model, not an optional
  improvement: without a bounded backward window, `arrivalCoverage`
  cannot be stated.

### Regression case

- Eichmattweg 7, Bern → Geburtshaus Luna, Ostermundigen, leave-at
  2026-09-21 22:30 local, minimize walking on, then "later": the bus 10
  departures at 23:30, 23:47 and 00:07 local must appear before the
  next-morning bus 28 departures. The same list section must appear when
  querying 23:30 local directly.

## Constraints

- Pruning rules, the display cap, sorting, badges and warnings do not
  change. Only *which journeys are candidates* changes.
- The Pareto outlier itself (the morning bus 28) is not lost: it is
  discarded from the 22:30 query and returns once coverage reaches the
  morning, exactly where the timeline puts it.
- Performance: the settled rule costs about one extra hop on the
  initial query (coverage must reach one trip length past the shown
  arrivals; one or two more under minimize walking), while dropping the
  merge cap saves hops on every extension. A service gap is crossed by
  MOTIS's own extension inside one call rather than by empty hops. The request deadline and truncation behaviour remain the
  safety net; the wide-budget escalation is unchanged.
- Coverage is per query and per walking budget. A budget escalation
  that replaces the candidate set resets coverage to what the wide
  search has actually covered.
- Both fork changes are confined to the plan endpoint (interval
  construction, and surfacing the interval the search already returns);
  the search core is not touched.
