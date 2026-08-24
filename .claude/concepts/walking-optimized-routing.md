# Walking Optimized Routing

## Problem

MOTIS optimizes arrival time and transfer count only. Journeys that trade a
few minutes of travel time for substantially less walking are Pareto-dominated
during the search and never generated; on top of that, the direct-walk
dominance mechanisms (fastest-direct bound, direct filter, short-walk gate)
remove everything slower than walking straight to the destination. Real case:
a coordinate query near Eigerplatz → Konsumstrasse 14c returns only the
9-minute direct walk, although a two-tram connection via Eigerplatz → Brunnhof
exists that walks ~2 minutes less. For users for whom walking is a burden,
exactly these connections matter — today they are structurally invisible, in
every mode.

## Requirements

**Fork: minimal-walking candidate generation**

- For every plan query where at least one endpoint is a coordinate (address /
  map point / current location), the forked MOTIS additionally produces
  *minimal-walking candidates*: journeys whose access and/or egress on the
  coordinate side(s) is restricted to the nearest stop of that side.
- "Nearest stop" means nearest by walking duration (per the walking data the
  query already computes), not by beeline.
- Candidate generation is exempt from every direct-walk-based suppression:
  the fastest-direct bound, the direct filter, and the short-walk gate must
  not remove candidates.
- Candidates are returned in the same response as the normal results — the
  client keeps making exactly one request per query.
- Each candidate itinerary carries a new response field `minimalWalking: true`
  so the client can distinguish candidates from normal results. Journeys found
  by both searches are deduplicated and count as normal results (no flag).
- Station-to-station queries are unaffected: no coordinate endpoint, no
  candidate pass.
- Both leave-at and arrive-by queries produce candidates symmetrically.

**Client: standard routing**

- When the pruned normal result set contains no transit option (i.e. only
  direct walks), minimal-walking candidates surface in the result list, so the
  user is never shown "just walk" while a transit option with meaningfully
  less walking exists.
- A candidate only surfaces if it is actually worth showing:
  - it reduces total walking by at least 2 minutes compared to the direct
    walk, and
  - its total duration is at most 3× the direct walk duration.
- When the normal result set already contains transit options, candidates are
  merged into the normal list and ranked by the existing pruning — they may
  appear when they survive it, but must never displace a better option.

**Performance**

- No additional client→server request in any mode.
- The candidate pass must not meaningfully increase query latency; the
  expensive per-query work (walking offsets for coordinate endpoints) is
  already being computed and must be reused, not recomputed.

## Constraints

- The future "minimal walking mode" (walking-first ranking, walking measured
  in meters with an uphill malus, mode UI) is **not** part of this concept.
  This fork work is its prerequisite: candidates must always be delivered and
  flagged, regardless of whether the current mode displays them, so the future
  mode can be built purely client-side on top of this.
- Client-side pruning semantics (the two-case dominance rules) stay unchanged;
  the `minimalWalking` flag gates when candidates surface, it does not exempt
  them from pruning in standard routing.
- The nearest stop can be useless in edge cases (no service, wrong direction).
  Candidate generation does not need to second-guess this — a candidate that
  yields no acceptable journey simply produces nothing.
- Walking-time data quality is out of scope: transfer walks come from the
  Valhalla matrix / live calls and inherit OSM gaps (e.g. missing track
  crossings between platforms). Such fixes happen in OSM, not here.
