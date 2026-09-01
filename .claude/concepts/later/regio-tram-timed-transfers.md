# Regio-Tram Timed Transfers

Refinement of the timed-transfer warning exception
(`routing-options.md` § Connection warnings). Planned, not scheduled —
the tuning effort is not worth it right now. Interim behavior shipped
instead: tram → bus is treated as timed always.

## Problem

The tight-transfer warnings exempt timed feeder transfers (the road
vehicle waits for the arriving train). Some trams are effectively
light rail (Bern 6 to Worb, BLT 19 to Waldenburg, Forchbahn 18, BLT
10, Limmattalbahn 20) and rural buses wait for them too — but city
buses do not wait for city trams. And even for a genuine regio tram,
the waiting only happens at its rural stops, not at its urban end.
The interim "tram → bus is always timed" rule over-suppresses warnings
for city tram → city bus transfers.

## Requirements

Two-level refinement, the second building on the first:

1. **Per-line `regio_trams` flag** — which trams qualify at all.
   Baked alongside `hf_gondolas` in `route_color_index.json`; the
   client then applies the tram → bus exemption only to flagged
   routes.
2. **Per-station rural gate** — where the waiting actually happens.
   A station-level urban/rural flag baked into
   `stop_search_index.json` (from the pipeline's existing per-stop
   urbanness computation); the exemption applies only when the
   transfer station is rural. Could also refine the train → bus
   exemption (restore warnings at big-city stations, currently an
   accepted blind spot).

### Options for the per-line classification

Measured values (2026-08 feed): regio trams — BLT 19: 31.1 km/h /
1126 m mean stop spacing, Forchbahn 18: 30.8 / 853, BLT 10: 23.3 /
626, VBZ 12: 23.2 / 592, BLT 17: 19.0 / 914, Limmattalbahn 20:
20.3 / 482, Bern 6: 19.7 / 530. City tram cluster: speed ≤ 19.3,
spacing ≤ 500 (closest: TPG 14 at 19.3 / 498).

- **Speed threshold alone** (~21 km/h): clean for the fast lines,
  misses Bern 6 and Limmattalbahn 20; a cut at ~19.5 would catch
  Bern 6 but sits 0.4 km/h above the largest city tram — too fragile
  across feed updates.
- **Spacing threshold alone** (~600 m): catches BLT 17 (slow but
  express-spaced), same fragility at Bern 6 (530) vs TPG 14 (498).
- **Combined rule + config whitelist** (preferred): flag when
  `speed ≥ 21 OR spacing ≥ 600`, plus a config whitelist for the
  borderline residue (Bern 6, Limmattalbahn 20) — same precedent as
  `mountain_agency_ids`. Deterministic and robust; the whitelist
  needs occasional curation.

### Options for the per-station gate

- Bucket the existing urbanness score into urban / rural at a tunable
  threshold. Main open question is the cut: Worb must count rural,
  Muri bei Bern probably urban — needs a pass over real values along
  the candidate lines.
- Possibly combine with `stop_tier` (large hubs never wait,
  regardless of surroundings).

## Constraints

- The client maps a transfer leg's quay stop id to the station entry
  by stripping the quay suffix off the SLOID and matching the search
  index's parent-id field — no new index needed.
- Whitelists always by `agency_id` + ref, never name substrings
  (same rule as `mountain_agency_ids`).
- Until implemented, the interim rule stands: tram → bus exempt
  everywhere (tram → tram is NOT exempt).
