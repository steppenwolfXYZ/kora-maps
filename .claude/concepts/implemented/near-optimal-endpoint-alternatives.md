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
