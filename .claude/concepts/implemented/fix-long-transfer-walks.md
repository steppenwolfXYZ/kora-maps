# Fix Long Transfer Walks

## Problem

RAPTOR judges journeys on two criteria — arrival time and transfer
count. Walking is folded into the clock but is otherwise free, so a
journey that *walks* a stretch a train covers ties on arrival with one
transfer less and dominates the ride variant, which is pruned and never
returned. Canonical case (Neuendorf, Weier → Wasserfallen, wide
cascade): the returned journey walks 56 min from Oensingen to Balsthal
to catch bus 130, while the S22 bridges exactly that stretch and
catches the same bus — same final arrival, one more transfer, ~4 km
less walking. The absurd variant only surfaces since the full 2-h
transfer table (`koraFullTransfers`) made long transfer walks
reachable; default MOTIS hides the problem by capping transfer walks,
it doesn't solve it.

Post-hoc substitution and collection-time tie-breaking were considered
and rejected: the first is a band-aid that can't move boarding/exit
stops the walk-optimal route would change, the second touches the hot
label arrays. The chosen route keeps RAPTOR's two-criteria structure
and redefines the *cost* criterion.

## Requirements

- **Criterion change (fork-wide, always on).** The discrete criterion
  is no longer "number of boardings" but **points**: each boarding
  costs points according to the walking done since the previous
  alighting (for the first boarding: the access walk). The search
  optimizes the Pareto front over (arrival time, points); labels remain
  plain per-(stop, level) time arrays, with levels indexed by
  accumulated points instead of rounds.
- **Point table** (walk duration → points per boarding):
  - 0–5 min: 1 point (a plain transfer costs 1, as today)
  - 5–10 min: 2 points
  - 10–20 min: 3 points
  - 20–40 min: 5 points
  - over 40 min: 10 points
  The table lives as a named constant table in the fork
  (`kora_walk_points`); boundaries and values are compile-time
  constants, not query knobs.
- **Egress counts too.** At journey collection the egress walk's class
  points are added before dominance is decided, so among equal-arrival
  journeys the one with the shorter egress walk wins. Access counts via
  the first boarding (above). A uniform rule: every walk in the journey
  is priced, no walk is free.
- **Points cap.** The transfer cap's role is taken by a journey points
  cap (`kora_max_journey_points`), generous enough that legitimate
  multi-long-walk journeys (Lötschental-style hikes) still exist —
  order of 45 points, tunable in the fork.
- **Collapsing, not variants.** Equal-arrival journeys differing only
  in walking now resolve *inside* the search: the fewer-points journey
  dominates, exactly one survives. No new alternates mechanism; the
  existing endpoint-alternates feature (`near-optimal-endpoint-
  alternatives.md`) keeps operating unchanged on top.
- **Display semantics unchanged.** Points are search-internal. The
  client keeps deriving transfer counts from legs; no response-shape or
  client changes.
- **Both time modes** (leave-at / arrive-by) behave symmetrically.
- **Direct walk untouched.** The walk-only itinerary from
  `directModes=WALK` is produced outside the transit search and is not
  affected.
- **Acceptance case:** the canonical query above, replayed with the
  app's standard cascade — no returned journey may walk
  Oensingen → Balsthal while an S22 run bridges that stretch within the
  same window at equal final arrival; the S22 variant must be the
  returned journey.

## Constraints

- **Performance is the essence of this design.** The hot loop keeps
  its shape (plain time arrays, one label per stop per level); the only
  cost is more levels to iterate (points cap vs today's transfer cap).
  Budget: measured slowdown on the reference queries stays within ~2–3×
  worst case, expected well below with pruning.
- Walk durations that classify a boarding are the ones the search
  already uses (Valhalla matrix transfer times, access/egress offsets)
  — no new walking computation.
- More Pareto points per query means more raw itineraries reaching the
  client; layer 2's pruning is the accepted control, as with the
  alternates feature.
- A transfer with no walk (same platform) must cost exactly 1 point —
  today's behavior is the floor, never cheaper.
- The two-tier transfer table (`koraFullTransfers`) and the cascade
  logic stay as they are; this concept changes only how the search
  judges what the tables make reachable.
- Upstream sync: the criterion change is fork-only; the upstream bump
  procedure in `motis/fork/README.md` applies unchanged.

## Amendments (settled during implementation)

- **Points cap.** Realised through the existing maximum-transfers
  constant (now 45, upstream 14) rather than a new knob — the round
  index simply counts points. Side effect: a client-sent `maxTransfers`
  now caps points, not boardings (the app never sends it), and the
  one-to-all endpoint's transfer dimension counts levels.
- **Dominance pruning of ahead-written labels.** A label written ahead
  of its round (weighted walk / seed) that is equalled or beaten by a
  fewer-points label before its round starts is dropped un-boarded.
  Pure work saving — such a label can only produce journeys the final
  Pareto filter would discard — but essential: without it the level
  smear cost ~7× the route-scan work on sparse rural queries.
- **Measured performance outcome** (search operations vs old code, same
  data): common urban queries ~1×, coordinate queries ~1.5×, wide
  rural cascade ~1.4×, sparse-rural worst case ~3.5× (≈1.2 s locally;
  the remainder is the intrinsic cost of ~3× more point levels than
  ride rounds on walk-heavy queries). Accepted.
- **Reconstruction termination.** With variable per-boarding cost, the
  backward reconstruction can no longer run a fixed ride count; it ends
  when the remaining level matches a start seed's walk class.
- **ε-alternates interaction.** Alternate egress candidates are scanned
  across all levels per stop and each candidate carries its own level.
  Failed candidate probes print `[VERIFY FAIL] intermodal destination
  reconstruction failed` lines to the server log — expected discard
  noise, never lost journeys (zero such lines with alternates off).
- **PONG symmetry.** Every walk's delta attaches to the same
  footpath/offset in both search directions, keeping the ping/pong
  exact-level pairing intact. Rare mismatches abort PONG and fall back
  to rRAPTOR automatically — correct results, roughly doubled latency
  for that query.
- **Visibility note.** Whether the pre-fix pathology surfaced in the
  app was a data lottery: the client's unconditional prune hides a
  walk-heavy connection whenever some same-arrival, later-departing
  alternative lands in the merged result set. The engine emitted the
  bad connections on every data snapshot tested; only the older local
  snapshot let them through to the UI.
