# Transfer Stop Selection

## Problem

Where a journey changes vehicles is chosen arbitrarily. A bus or tram
that calls at several stops near a station gives several ways to reach
the same platform on the same trips at the same times, and the engine
returns whichever the footpath table happens to list first.

At Bern this alights at Kocherpark and walks 9 minutes to the platform
when staying aboard two more stops walks 5 — same tram, same train, same
arrival, one transfer either way. The same cause produced alightings at
Bahnhof instead of Hirschengraben, and a bus 20 detour that ends where a
short walk from Hirschengraben would have.

This is not the problem `fix-long-transfer-walks.md` addresses. That
concept keeps absurdly long walks out of the search, at deliberately
coarse resolution, and is working as intended. Nor is it
`near-optimal-endpoint-alternatives.md`, which varies a journey's first
boarding or last alighting stop — here the stop that varies is an
intermediate transfer.

## Requirements

- Among reconstructions of one journey that are equal on departure time,
  arrival time and transfer count, and differ only in where the traveller
  changes, the engine must return the best rather than the first found.
- **Ranking is walking first, connection safety second.** Less walking is
  preferred; where walking is equal, the variant leaving more slack for
  the vehicle being caught is preferred. Ties beyond that resolve
  deterministically — the same variant for the same query every time.
- **The result set does not grow.** One journey in, one journey out; this
  replaces a representative, it does not add alternatives. Paging and the
  client's pruning see the same number of journeys as before.
- **The search is unchanged.** No additional rounds, no new Pareto
  criterion, no finer quantisation of any search dimension. Whatever
  work this costs is confined to reading out a journey that has already
  been found.
- **No comfort policy moves to the server.** Choosing between journeys
  that genuinely differ stays with the client's ranking. The server only
  stops describing one journey worse than it could.

## Constraints

- Resolution is the transfer table's: footpath durations and event times
  are whole minutes, so two candidates within the same minute cannot be
  distinguished. Sub-minute selection would require changing the
  timetable's time type and is out of scope.
- A same-stop transfer needs no walk and is already optimal; it must keep
  precedence over any footpath candidate.
- Reconstruction must still succeed wherever it succeeds today. Widening
  the candidate search must never turn a reconstructible journey into a
  failure.
- The walk-weighted point classes stay as they are. This concept must not
  be implemented by refining them — that would cost search rounds for
  resolution the point dimension cannot afford to carry.
