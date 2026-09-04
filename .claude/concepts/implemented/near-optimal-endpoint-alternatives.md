# Near-Optimal Endpoint Alternatives

## Problem

RAPTOR judges journeys on exactly three door-to-door numbers — departure
time, arrival time, transfer count — and keeps one journey per optimal
point. Walking is folded into the clock (access/egress offsets) but is
never a criterion of its own, so alternatives that tie or near-tie on the
three numbers while walking meaningfully less are silently collapsed
away and never reach the client. Canonical case (Eichmattweg 7 →
Scharnachtalstrasse 10, 09:02): bus 31 → bus 19 with a same-platform
transfer at Thunplatz and a short egress walk from Manuelstrasse ties the
returned 31 → tram 8 → Weltpostverein journey to the minute, but only
the tram variant survives.

The collapse happens at journey *collection*, not during the search: the
search state holds an optimal arrival per stop, and the destination's
arrival is the minimum over (stop arrival + egress walk) across all
offset stops — reconstruction follows only that single winner back. The
losing endpoint variants are fully computed and simply never read out.

The client's own pruning layer (comfort rating, dominance rules) already
encodes which of these alternatives are worth showing — it just never
gets to see them.

## Requirements

- **Alternates are read out of the finished search, not searched for.**
  The fork returns, in addition to each Pareto-optimal journey, alternate
  journeys that differ in the final alighting stop (leave-at queries) or
  the first boarding stop (arrive-by queries) and whose door-to-door
  arrival (departure for arrive-by) lands within a slack of that
  journey's optimum. The RAPTOR search itself is unchanged; only journey
  collection/reconstruction reads more entries out of the existing state.
- **Two query-time knobs**, sent by the app on every plan request:
  - `alternativesEpsilon` — the slack in seconds. `0` (the default)
    disables the feature entirely and preserves upstream behavior.
  - `alternativesMax` — cap on alternates per Pareto point.
  The app's values are tuned to layer 2's own marginality windows so the
  server returns a superset of what layer 2 would ever keep, but not
  more than slightly so.
- **Alternates are ordinary itineraries.** They appear in the normal
  itinerary list of the response, indistinguishable in shape from the
  primaries — no new response fields, no client parsing changes. Existing
  server-side post-processing (transfer placement, leg rendering, walking
  legs via Valhalla) applies to them like to any journey.
- **Primaries are never displaced.** Every journey the query returns
  today is still returned; alternates are additive. Duplicate alternates
  (same sequence of trips, boarded and left at the same stops as a
  primary or another alternate) are dropped server-side.
- **Layer 2 stays the arbiter.** No comfort or walking judgment moves
  into the server. The client's existing pruning decides which
  alternates survive; the server's only job is to stop withholding them.
- **Acceptance case:** the canonical query above, replayed with the
  app's standard cascade — the 31 → 19 journey (Thunplatz same-platform
  transfer, Manuelstrasse egress) must be present in the raw merged
  result set that reaches layer 2.

## Constraints

- Performance is of the essence: the search phase must not slow down at
  all; the added cost is bounded by reconstruction of at most
  `alternativesMax` extra journeys per Pareto point plus response size.
- Alternates are by definition dominated under the three-number rule —
  the final journey collection must not let its dominance filter reject
  them.
- Mid-route collapses (same first/last stops, different path or
  transfer structure in between) are out of scope. Recovering those
  would require multi-criteria search (bag-based RAPTOR) — deliberately
  rejected as too slow.
- Both time modes are covered symmetrically (egress side for leave-at,
  access side for arrive-by), but only the query's offset side — not
  both ends at once.
- The cascade's hop queries inherit the knobs unchanged; the resulting
  growth of the merged pre-pruning set is accepted (layer 2 already
  scales to it).

## Amendments (settled during implementation)

Requirements that emerged once the feature ran against real queries —
all consequences of deliberately reading out non-optimal journeys,
which forfeits the sensibility guarantees the search's optimality used
to provide implicitly:

- **Sensibility filters.** An alternate must never re-board the same
  line (compared by line name — opposite directions and variants of a
  line count as the same line), and must never return to or ride
  through a parent station the journey already visited (a station shared
  between two legs purely as their transfer point is fine). Both shapes
  are reconstruction fabrications, feasible but pointless.
- **Duplicate control by ridden vehicles.** Alternates are deduplicated
  by quay-blind vehicle fingerprint (the runs ridden plus board/alight
  parent stations) against the primaries and each other — never by
  station exclusivity, so two different lines arriving at different
  platforms of one station both survive.
- **Endpoint-station dominance.** Among journeys identical except the
  varied endpoint station, an alternate equal-or-worse in both endpoint
  time and endpoint walk is dropped; two stations coexist only when the
  destination genuinely lies between them (each wins one axis).
- **Ride-through redundancy.** An alternate whose endpoint station is
  served no later by a kept journey's endpoint vehicle — ridden past
  that journey's own exit, without requiring an earlier departure from
  home — is the same corridor journey in disguise and is dropped.
  "Same corridor" must be verified, never assumed from the endpoint
  alone: the two journeys' full stop sets (every parent station ridden
  through, interior stops included, minus the query's shared anchor —
  the origin-side boarding station for leave-at, the destination-side
  alighting station for arrive-by, which every journey of the query
  shares by construction and which therefore carries no corridor
  information) must overlap by at least 75% of the smaller set. The
  smaller-set denominator keeps express-vs-local pairs matching in
  both directions (the express's stops are a subset of the local's,
  never the reverse). Journeys below the overlap are genuinely
  different routes that merely end near each other, and the rule must
  leave them for the client's ranking to judge — canonical failure
  before the gate: Thun→Belp→S3→bus 28 (Gürbetal line) dropped because
  Thun→Bern→S1→bus 10 (mainline) reached Eigerplatz inside the slack,
  two routes sharing no stop but their origin, with the strictly worse
  sibling surviving.
- **Once per Pareto point.** Extraction runs once per (arrival,
  transfers) point, not once per search-cursor rediscovery. The
  accepted performance budget is ~2.5× the alternates-off search time
  (measured ≈ 35 → 80 ms locally on the reference query).
- **No exact-time anchor guessing.** Candidate times are upper bounds;
  reconstruction accepts a vehicle arriving at or before the anchor and
  the endpoint leg is snapped to the vehicle actually found. Deriving
  an exact anchor from search-internal values is forbidden — which
  internal writer set a stop's entry is unobservable, and guessing made
  results flip between otherwise-identical queries.
- **Diagnostics.** With the server env `KORA_ALT_DEBUG=1`, extraction
  logs one line per candidate with its outcome (accepted / the reason
  dropped) to stderr.
- **Client counterpart.** The cascade's hop merge must never split a
  same-minute departure group across its merge cap (the follow-up hop
  anchors past that minute, permanently losing the unmerged sibling) —
  alternates made same-minute siblings common enough to expose this.
