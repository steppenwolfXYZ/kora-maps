# Comfort Walk Baseline

## Problem

The client's comfort factor prices walking with a saturating t² curve
whose half-point sits at 30 minutes. Walks of 3–10 minutes, the band
minimize-walking exists for, land on its flattest part: under minimize
walking a 5-minute walk costs +1.4 %, a 10-minute walk +5 %, less than
one extra boarding. A connection that walks 4 minutes less but arrives
8 minutes later can never survive the 20 % overlap test, whichever
slope is applied. Canonical case: Eichmattweg 7 → Mittelweg 6, Ittigen
with minimize walking on — bus 10 → bus 40 via Rosengarten (one
transfer, 4.5 min walking) is pruned by a two-transfer connection with
8.6 min walking that arrives 8 minutes earlier.

A linear curve fixes the band, but a linear curve with a cap creates a
new problem: when both endpoints are addresses far from any stop, every
connection carries the same unavoidable walk, pushing all of them
toward or over the cap where they can no longer be told apart. The
unavoidable share must not count.

## Requirements

**Normal mode: linear walk malus.** The walking malus of the comfort
factor becomes linear in the *reduced* walking time (below):
`reduced / 60 min`, capped at 1. The slope stays 0.1, so the factor
range is unchanged. The malus is **allowed to go negative**: a
connection that walks less than the baseline is genuinely better than
the norm for this query, and its factor may dip below 1.

**Minimize walking: absolute pricing.** A percentage of the journey
cannot serve both ends — 10 % is 2 minutes on a 24-minute trip and 18
on a 3-hour one — while the mode's intuition is absolute: a walked
minute is worth a fixed number of journey minutes, whatever the trip
length. So under minimize walking the effective time is **additive**:

    effective = duration + walkPenalty(reduced) + 5 min × boardings

with `walkPenalty` concave in the reduced walking time (minutes):

- up to 30 minutes: `2.5 × sqrt(5 × reduced)` — 1 min → 5.6 min,
  5 → 12.5, 10 → 17.7, 20 → 25, 30 → 30.6. With the unavoidable share
  removed, the first extra minutes are exactly the walk-to-a-different-
  stop decisions the mode targets and weigh most.
- beyond 30 minutes: **no cap** — one further minute of penalty per
  further minute of walking, continuing from the 30-minute value.
- negative reduced walking: mirrored, `−2.5 × sqrt(5 × |reduced|)`.

Two calibration anchors: on a short trip, saving 4 minutes of walking
must be worth about 8–10 minutes of journey (the canonical case); on a
3-hour trip, saving 5 minutes of walking must not be worth 30 minutes
of journey. The additive rate satisfies both; no multiplicative slope
does.

**Reduced walking time.** `reduced = raw walking sum − baseline`, where
raw walking is the itinerary's total walking as today (access, egress,
transfer walks) and `baseline = minWalkFrom + minWalkTo`:

- `minWalkFrom` is the shortest walking time from the start endpoint
  to any *sufficiently served* quay; `minWalkTo` the same from any such
  quay to the destination. Both come from the walking times the search
  itself uses (the query-time offsets), so they follow the walking-speed
  option and the walking budget exactly like the connections do.
- A station endpoint contributes 0.
- A quay is sufficiently served when its average departures per day
  over the feed period reach `KORA_BASELINE_MIN_DEPARTURES_PER_DAY`
  (server knob, default 2). Quays below it do not define the baseline
  but remain routable; a connection using one may therefore rate below
  the baseline. That is intended.

**Server side.** The plan response carries two new fields,
`koraMinWalkFrom` and `koraMinWalkTo` (seconds, whole minutes since the
offsets are minute-quantised). Both are 0 for station endpoints and
absent when the endpoint has no reachable quay at all. The per-quay
service count is computed once from the loaded timetable on the first
query that needs it, weighted by each trip's active days, and never
touches the search.

**Client side.** The baseline is a property of the query (its two
endpoints and the walking-speed option), not of the result set: every
hop of the cascade, every escalation and every later load yields the
same number, so loading more results never re-rates what is shown. It
resets with the query. In each mode a single effective time —
multiplicative in normal mode, additive under minimize walking — drives
pruning (the Case 1 comfort test, unchanged as a 20 % ratio over that
effective time), badges and auto-select. The Case 2 penalty score is a
separate scale and stays as it is.

## Constraints

- Everything that is not the comfort malus keeps using raw walking:
  long-walk warnings, the 30-minute direct-walk suppression under
  minimize walking, the Case 2 penalty score, the subset-prune saving
  ratio and the walking exemption of Case 1.
- The Case 1 window (9 min / 20 %) and the Case 2 allowance curve are
  not retuned here. Under the additive pricing the Rosengarten case
  rates best outright (42 min effective against 51.5 and 53.7 for its
  two time-dominators), so no window change is needed for it.
- A response without the new fields (older server) yields baseline 0,
  i.e. pricing of raw walking.
- The baseline is a lower bound: when the nearest sufficiently served
  quay is not the one the sensible connections use, some unavoidable
  walking still counts. Accepted.
- The service count ignores real-time data and the query date; a
  seasonal stop counts by its average over the whole feed period.
