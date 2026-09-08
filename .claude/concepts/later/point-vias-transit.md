# Point Vias on the Transit Tab

## Problem

Vias on the transit tab are stations only (`via-stops.md`). The errand
that motivated vias in the first place — pick something up, drop something
off, meet someone — almost always happens at an address or a POI, not on a
platform. Today such a journey has to be approximated by the nearest
station, which mis-measures the walk and makes the wait meaningless. The
cycling and walking tabs already accept points as vias; the transit tab
is the odd one out.

The cause is the routing engine: its plan API takes vias as stop IDs and
resolves each to a timetable location. Coordinates are refused.

## Requirements

### Via endpoints

- The transit tab accepts a **point** (address, POI, map click) as a via,
  in any position and in any mix with station vias. Current location
  stays excluded as a via on every tab.
- The via search on the transit tab offers stations and geocoder results
  together, the same mixed ranking the direct tabs use for vias today.
- The wait control applies to point vias exactly as to station vias.

### Journey semantics

- A point via is reached and left **on foot**: the journey arrives at
  some stop, walks to the point, stays the requested wait, walks to a
  stop (the same one or a different one) and continues. Both walks use
  the same walking authority and the same walk ceiling as From / To
  points.
- The router chooses the arrival and departure stops around the point
  freely; the user never picks them.
- The walks count as travel time; the stay counts as via stay, never as a
  transfer. Every duration-based judgement in the panel (badges,
  warnings, ranking) treats a point via like a station via plus its
  walks.
- Arrive-by mode works with point vias, and the via order shown is the
  direction of travel, as for station vias.
- A point via nobody can walk to within the ceiling yields no results,
  exactly like any other unsatisfiable query — never a silently skipped
  via.

### Result display

- The leg list shows the walk to the point, the stay at the point (named
  by the point's display name, falling back to coordinates), and the walk
  away, as distinct rows.
- The map marks the point via distinctly, with the same marker family
  used for station vias, and draws both walk legs.

### Persistence and sharing

- The existing `via` URL parameter carries point vias in the `lat,lng`
  token form the direct tabs already write; `viaWait` pairs with it as
  today. A cold load of such a URL reproduces the chain and issues the
  query.
- Recents, the query fingerprint and the shared-connection identity
  include point vias with their coordinates and waits.

## Constraints

- The engine's stop-ID-only via API is the blocker. Two ways to satisfy
  the requirements are known; the choice is an architecture decision to
  be taken before implementation:
  - **Engine extension** in the Kora fork: a coordinate via becomes a set
    of candidate stops around the point with walk offsets on the arrival
    and the departure side, searched as one query. Keeps joint
    optimisation and arrive-by semantics intact; deeper engine work.
  - **Client-side chaining**: the journey is split at every point via into
    consecutive plan calls stitched into one itinerary. No engine change,
    but no joint optimisation across the split, arrive-by has to chain
    backwards, and result cards, map, URL and recents all have to
    understand a composite journey.
- Three vias maximum in total, stations and points combined.
- A via-less query must stay byte-identical to today's request.
- Station vias keep their current behaviour unchanged.
- Nothing in the transit pipeline, the tile artefacts or the stop search
  index changes.
