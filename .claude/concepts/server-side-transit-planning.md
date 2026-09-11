# Server-side transit planning

## Problem

The transit search is a client-driven cascade: a narrow query, an
optional wide retry, then a time-advance hop loop, with dominance
pruning re-run on the accumulated set after every hop. A single user
query therefore fans out into many MOTIS requests from the browser —
a dozen or more is common, and the minimize-walking toggle drove one
Bern–Ittigen query to 24 hops and 16 s.

Three downsides:

- Every hop is a client round trip. On mobile with poor reception the
  latency multiplies, and each hop carries a full 2-h window (~150
  itineraries) of which one or two are kept.
- Many requests per user action load the server and the nginx proxy
  for no user-visible gain.
- The cascade, the escalation heuristics and the ranking are the app's
  routing intelligence, and they ship as readable client code against a
  publicly reachable MOTIS proxy.

Separately, the hop loop's merge cap advances the search frontier by the
few results it merges, not by the window it fetched. When pruning retires
nearly everything (minimize walking), the frontier crawls a minute per
hop.

## Requirements

### 1. One endpoint, one request per user action

- The app exposes its own transit planning endpoint, `/api/plan`, served
  by the SvelteKit server. The client makes exactly **one** request to it
  per user action (initial query, "earlier connections", "later
  connections") and receives the **final, pruned, sorted result list** as
  the panel shows it today.
- The endpoint runs the complete cascade internally against MOTIS over
  the server-side MOTIS address (the private `MOTIS_INTERNAL_URL` already
  used by share verification): stage 1 narrow query, stage 2 walking-
  budget escalation on its triggers, stage 3 hop cascade, mid-cascade
  sparse-gap escalation, dominance pruning, minimize-walking suppression,
  sorting and the result cap. Behaviour and results must match the current
  client pipeline exactly for the same inputs — this is a relocation, not
  a redesign.
- The client keeps: endpoint inputs, options, URL round-trip, selection,
  card states, map rendering, warnings, and the ranking knobs the cards
  need for display. Everything that decides *which* itineraries come back
  moves to the server.
- Response is one-shot: the loader shows a single generic searching state
  until the response arrives. The per-stage progress lines ("no options
  yet, looking further ahead…") and the "results were pruned" indicator
  are dropped for now. Streaming intermediate results is a possible later
  extension and must not be designed against.

### 2. Request contract

- The request carries what the client's query key carries today: from,
  to, vias with waits, leave-at / arrive-by, time (a null time is pinned
  to "now" by the client before sending, as today), and the routing
  options (walk speed, safety, minimize walking).
- "Earlier" / "later" are the same endpoint with three extra inputs:
  direction, the walking budget the shown list was built with (narrow or
  wide), and the fingerprints of the itineraries currently shown, so the
  server extends the list without repeating entries. The endpoint is
  stateless — no server-side session for a running search.
- Share verification on opening a shared connection (the wide-from-the-
  start search that looks for one fingerprint) is a mode of the same
  endpoint: the request names the wanted fingerprint, the response says
  whether it was found and returns it if so.

### 3. Response contract

- The result list, plus the metadata the client needs to continue: the
  walking budget the cascade settled on (narrow / wide), and the
  fingerprints the server merged, so the next "earlier / later" request
  can pass them back.
- Errors map to the same user-facing messages as today (unreachable,
  server error, no route), decided by the server so the client never
  interprets MOTIS responses.

### 4. Hop merge cap follows the pruning ratio

The hop loop's merge cap is derived from how selective pruning proved to
be in this search, so the frontier moves in proportion to what survives:

- After stage 1 (and again after a wide escalation), record
  `pruneRatio = kept / returned`.
- Per hop, the number of itineraries merged is
  `needed / pruneRatio`, multiplied by a safety margin of 1.5, rounded
  up, never below `needed`, never above the batch.
- A ratio of zero (nothing survived) uses the whole batch.
- The same-minute anchor rule stays: a merge never splits itineraries
  that share the last merged departure (arrival) minute.

### 5. Public MOTIS proxy closes

- With the client off MOTIS, the nginx `/routing/` location is removed.
  MOTIS stays loopback-bound and reachable only from the app server and
  the internal docker network.
- The client-side `PUBLIC_MOTIS_URL` and the MOTIS client module in the
  browser bundle go away.

### 6. Stats count user queries

- The stats page's routing counter switches from MOTIS proxy log lines to
  requests against `/api/plan`. One user action = one count. This is what
  the counter was always meant to measure; historic hop-based counts are
  not converted.
- The most-requested route pairs keep working: the place names and
  endpoint tokens the log-based aggregation reads must remain visible in
  the access log for the new endpoint.

## Constraints

- Ranking and pruning semantics do not change. Every existing rule
  (Case-1 overlap, walk dominance, via-wait handling, minimize-walking
  suppression, the cap keeping the end nearest the query time) applies
  unchanged on the server.
- The direct cycling / walking tabs are out of scope: they keep calling
  Valhalla from the client, one request each.
- The Valhalla route proxy in nginx stays as it is.
- The server must guard the cascade's own bounds (5-day span, empty-hop
  streak) and add a wall-clock ceiling per request so a pathological
  search cannot hold a server worker indefinitely.
- Aborting: a newer client request supersedes an older one on the client
  as today; the server does not need to cancel MOTIS work in flight.
- The share-expiry re-verification the server already performs stays a
  server-internal MOTIS call.
