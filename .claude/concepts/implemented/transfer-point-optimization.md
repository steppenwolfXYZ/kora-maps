# Transfer Point Optimization

## Problem

Two shared connections surfaced transfers at awkward stops even though
Pareto-equal alternatives (same arrival, same transfer count) with a much
shorter — or zero — transfer walk existed:

- Eigerplatz → Elfenaupark: bus 28 → 19 transferred via an
  Aegertenstrasse → Tillierstrasse street walk, although both lines stop
  at Thunplatz on the **same platform** (5-minute buffer, zero walk).
- Eigerplatz → Egghölzli: bus 28 → tram 8 transferred at
  Brunnadernstrasse (94 s walk) instead of Thunplatz (23 s walk).

Investigation found two independent causes:

1. The app's plan queries never sent `useRoutedTransfers=true`, so RAPTOR
   ran its transfers on nigiri's default footpath set (derived from GTFS
   `transfers.txt` — sparse, direction-incomplete trip-pair entries)
   instead of the fork's imported Valhalla matrix. This silently violated
   `valhalla-pedestrian-router.md` ("Valhalla-computed transfer table
   drives RAPTOR") — the matrix was imported but unused at query time.
2. nigiri already ships a transfer-placement optimizer that slides each
   transfer to the lowest-penalty stop pair (walk time, same-station
   bonus, buffer reward) — but it only considers *footpath pairs*. A
   same-location transfer (alight and re-board at the very same stop) is
   invisible to it, because neither the Valhalla matrix nor GTFS carries
   self-entries; feasibility of that case lives in the per-stop change
   time, which the optimizer never consults.

## Requirements

- **Routed transfers on every query.** Every plan request the app issues
  runs with `useRoutedTransfers=true`, making the imported Valhalla
  matrix the transfer table RAPTOR and the transfer optimizer operate on.
- **Same-location transfers become candidates.** The fork extends
  nigiri's transfer optimizer so that alighting and re-boarding at the
  same stop competes as a transfer placement: feasibility per the stop's
  change time, scored with the same penalty formula as footpath
  candidates (duration, same-station bonus, buffer reward). A feasible
  zero-walk same-platform transfer must win against a longer walk.
- **Nothing else about the journey changes.** Departure/arrival times,
  transfer count, and the trips ridden stay identical — only where the
  transfer happens may move.
- **Acceptance cases** (the two shares above, replayed):
  - Eigerplatz → Elfenaupark transfers 28 → 19 at Thunplatz, same
    platform, no walk leg longer than the change time.
  - Eigerplatz → Egghölzli transfers 28 → tram 8 at Thunplatz
    (23 s cross-platform walk).

## Constraints

- Via stops must never be skipped by a slid transfer (the existing
  optimizer already guards this; the same guard applies to the new
  candidate type).
- ~~Latency cost of routed transfers is accepted: long-distance queries
  roughly double (~0.2 s → ~0.55 s locally, Bern → Zermatt); short
  queries stay ≤ ~80 ms.~~ *Superseded by the two-tier transfer table
  below — the cost was accepted only until measured; the tiering
  recovers it.*

---

# Two-tier transfer table

## Problem

Routed transfers made the RAPTOR search relax the full Valhalla matrix
(~37M pairs, 2-h walking reach, ~600 neighbors per stop): search time
147M footpath visits vs 0.7M before, 1.5–3× query latency. But the 2-h
reach only matters for rare long-transfer-walk "hack" connections in
sparsely served areas; well-served times and areas never need a
transfer walk beyond 30 minutes.

## Requirements

- **Two transfer tables from one matrix, split at import.** The existing
  matrix CSV is loaded twice (no matrix rebuild): rows ≤ the cap become
  the default transfer table; the full set (up to `max_footpath_length`,
  2 h) becomes a second table. The cap is configurable via the import
  env var `KORA_TRANSFER_CAP_MINUTES`, default 30. At 30 min the default
  table holds ~12% of the pairs.
- **Default queries use the capped table.** Expected to recover most of
  the routed-transfers latency while keeping every normal transfer
  findable and the transfer-point optimization fully working.
- **Fallback queries use the full table.** A query flag
  (`koraFullTransfers=true`, understood by the fork) selects the full
  table. The app sends it exactly when the result cascade escalates to
  the wide walking budget (empty narrow result, >1 h waits, or a ≥4 h
  daytime service gap) — the sparse-service situations where long
  transfer-walk hacks matter. Share verification already queries wide
  from the start and therefore also gets the full table, so shared
  hack connections re-verify correctly.
- **Station-endpoint offsets keep full 2-h reach.** The fork serves
  start/end walking offsets for station endpoints from the imported
  matrix; these must read the full table, never the capped one.

## Constraints

- Requires a MOTIS import re-run (consumes the existing CSV) plus the
  image rebuild — the Valhalla matrix itself is NOT recomputed.
- Known accepted gap: a long-walk hack that would beat a plentiful set
  of mediocre connections in a well-served corridor will not surface,
  because the cascade never escalates there.
- Memory overhead of the second table is ~12% on top of the full set
  (both tables live in the served timetable).
- Wheelchair behavior unchanged (profile stays empty, falls back as
  today).
- The fork overlay grows into nigiri's source tree for the first time
  (until now only MOTIS files were overlaid). The overlay stays a
  patched full-file copy pinned to the nigiri commit MOTIS pins.
- Wheelchair profile behavior unchanged.

---

# Minimum transfer time

## Problem

The transfer table said only how long the walk takes, never how long a
change is allowed to take. Two consequences met in the results:

- The Valhalla matrix carries **11,093 zero-second pairs** (quays whose
  anchors coincide, e.g. the two faces of one island platform). Rounded
  up to whole minutes that is still zero, so the router offered
  connections where alighting and boarding happen at the same instant —
  the Reckless tier (`routing-options.md` § Connection safety), handed
  out at Balanced.
- Even a non-zero walk ignores the operator's own rule. Swiss stations
  publish a minimum change time per quay pair; a 40-second walk across a
  concourse is not a 40-second transfer.

## Requirements

- **Every quay-to-quay transfer is floored at import**, where the matrix
  is loaded into the transfer table. The stored duration becomes
  `max(walking time, minimum transfer time)` — the matrix keeps saying
  how far it is, the feed says how soon you may go.
- **The minimum comes from the feed, per pair.** GTFS `transfers.txt`
  `transfer_type=2` rows carry the operator's own minimum for a directed
  quay pair (94,035 pairs in the current feed, from 60 s to 600 s).
  Those values win over any blanket rule.
- **A flat two minutes applies where the feed is silent.** Two minutes is
  MOTIS's own `default_transfer_time` for staying at one stop, so a
  change between two quays is never cheaper than not changing quay at
  all.
- **Timed connections are exempt from the flat floor.**
  `transfer_type=1` marks a guaranteed connection: the vehicles are
  scheduled to meet, and a one-minute change there is real. Such pairs
  keep their walking time.
- **No transfer ever falls below one minute**, timed connections
  included. Arriving at the instant of departure is Reckless by
  definition and the search must never produce it unasked.
- **Displayed and ranked walking is unaffected.** The floor is a
  scheduling rule, not a walk. Walking times in the leg rows, the walked
  total and the ranking come from the pedestrian router's own seconds
  (see `routing-options.md` § Connection safety), so a floored transfer
  still reports the walk it actually is.

## Constraints

- The minima must be read from `transfers.txt` directly, **not** from
  nigiri's profile-0 footpaths. Profile 0 mixes `transfers.txt` rows with
  the loader's geometric `link_stop_distance` links (100 m by default),
  and a geometric link across an island platform carries a zero-minute
  duration that would cancel the floor it is supposed to supply.
- `transfer_type=1` rows are keyed by **trip** pair, which a stop-keyed
  transfer table cannot express. The exemption therefore widens to every
  trip over that quay pair. Accepted: the feed carries 281 such rows.
- Same-stop transfers never pass through the footpath table — no
  footpath source carries self-entries. They keep using nigiri's
  `locations_.transfer_time_`, which the feed's same-stop
  `transfer_type=2` rows already populate.
- Requires a MOTIS re-import; the Valhalla matrix is NOT recomputed and
  the CSV stays a pure record of real walking times.
- The feed's own ids must resolve against the imported `stops.txt`.
  6,622 rows reference station-level ids the platform-anchored sidecar
  does not carry as quays; they are counted and skipped.
