# Usable time

## Problem

The client quality filter's Case 1 (overlapping pairs) drops a
Pareto-time-dominated connection unconditionally once the time gap
exceeds 9 minutes — comfort is never consulted. This deletes slower
direct connections that many travellers would deliberately choose:
canonical case Bern → Chur, where the direct IR35 (3 h 10, 0 transfers)
is dropped by IC + IC3 combinations (2 h 20, 1 transfer) although a
long uninterrupted train ride is more pleasant and less risky than a
tight change in Zürich.

A flat "minutes per saved transfer" allowance was considered and
rejected: a saved transfer next to a short bus or S-Bahn hop is worth
just as much as the first one, and the value of an uninterrupted ride
grows with ride length. Both are captured by pricing the *ride quality*
itself instead of counting transfers.

## Requirements

### Usable time (per itinerary)

**Usable time** is the portion of an itinerary the traveller can
actually use (work, read, play). It is the sum over all transit legs
of a per-leg usable duration; walking, waiting, and transfer buffers
contribute nothing.

Per-leg calculation, from the leg's duration:

- The first and the last 5 minutes count 0 — settling in and packing
  up. Any leg of 10 minutes or less therefore contributes 0.
- The following 10 minutes on each side (minutes 5–15 from either end)
  count at half the mode rate.
- Everything between counts at the full mode rate.

Mode rates:

- **Train** (all rail): 1.0 — full.
- **Ferry, mountain** (aerial, funicular, rack): 1.0 — not always true
  in practice, but the coolness factor makes up for it.
- **Tram, metro**: 0.5 throughout (so the ramp minutes count a
  quarter).
- **Bus** (city, regional, coach): 0 — too much shaking.
- Unknown transit modes count as train.

**Hassle time** is the itinerary's judged duration (duration minus any
planned via dwell) minus its usable time. It is the derived quantity
both features below consume: everything that is not a usable transit
minute — walks, waits, boarding ramps, bus rides — counts in full.

### Feature 1 — display

The expanded connection details (leg list) end with a visible
usable-time group of three rows:

- **Total travel time** — the connection's full duration.
- **Active travel time** — total minus usable time.
- **Usable time** — as calculated above, with a clickable (i) icon
  that toggles a short inline explainer of what usable time means.

Shown only when usable time is positive — an all-bus connection shows
nothing rather than "0 min". The collapsed card is unchanged.

### Feature 2 — rescue in the quality filter

A connection that Case 1 would drop (any Case 1 verdict — time test or
comfort test) is **kept** when, against the dominating connection B:

- its hassle time is at least **10 minutes** lower than B's, and
- its judged duration is at most **1.5×** B's.

Both conditions must hold against every connection that dominates it —
one standing Case 1 verdict still drops it.

Calibration (Bern → Chur, the case that must survive): direct IR35
hassle ≈ 20 min vs IC8 + IC3 hassle ≈ 49 min → 29 min advantage;
duration ratio 190/140 = 1.36. Survives both gates with headroom.

## Constraints

- The rescue applies **only to Case 1**. The unconditional prunes
  (Rules 0, 0b, 0c, 0d) and Case 2 are untouched — those remove noise
  or comfort-inferior options, which a hassle advantage cannot redeem.
- The rescue only ever *adds* survivors: a rescued connection is the
  slower one of its pair, so it cannot newly dominate anything, and it
  does not time-beat its dominators on Case 2's primary axis. No new
  drops, no mutual-drop risk.
- Badges and warnings are unchanged. A rescued connection may carry a
  very-slow warning when it crosses those thresholds (at the 1.5×
  ratio cap this only happens at the boundary).
- Chronological sorting is unchanged; rescued connections slot in by
  time like any other survivor.
- Minimize-walking mode uses the same rescue rule — hassle time counts
  walking in full, so the mode needs no special casing.
- Known cost (accepted): repeating slow directs (e.g. the hourly IR35)
  each survive their own dominators and occupy result slots, so the
  cascade reaches its 5-survivor target with fewer distinct fast
  options shown.
