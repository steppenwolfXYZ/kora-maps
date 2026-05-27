# Night Line Exclusion

**Status:** Planned

## Problem

Night bus lines (OSM `ref` prefixed with `N`, e.g. N1, N18, N46) are appearing on the map despite having no service within any defined daytime time window. These lines should be invisible — they are not useful to a map focused on daytime walkable transit.

The runtime filter correctly excludes night GTFS lines: trips that depart outside all defined time windows (core, evening, weekend) never increment `line_freq`, so those lines are absent from both `_line_canonical_export` and `gtfs_index`. The filter works as intended for the GTFS data.

The failure is on the OSM side. When OSM route N18 attempts to match, it finds no GTFS entry for "N18" (correctly excluded). The matching cascade then applies an alpha-prefix fallback — stripping the digits from "N18" to try the bare prefix "N". There exist unrelated daytime GTFS lines with `short_name="N"` that do have service. The fallback finds one of these, assigns its freq_score to OSM N18, and the night line enters the drawn output.

## Current workaround

None. 181 night lines are currently drawn.

## Requirements

The alpha-prefix fallback in the main loop matching cascade must be suppressed for OSM routes whose `ref` starts with `N` followed by one or more digits (i.e. matches `^N\d`). When the exact and normalised ref lookups find no GTFS entry, the line must be treated as unmatched (`freq_score = None`) and dropped immediately — no alpha-prefix fallback, no geo-fallback, no further cascade steps.

The suppression must apply to the `bus` and `regional_bus` buckets only, since ferry lines with N-prefixed refs (Navibus N1–N4 on Lake Geneva) are in the `ferry` bucket and have legitimate direct GTFS matches.

## Constraints

- **Ferry N-lines unaffected:** Navibus lines (N1–N4, ferry bucket) have direct GTFS matches and must continue to be drawn. The suppression must not touch the ferry bucket.
- **Non-night N-lines:** Any non-night bus line whose ref happens to start with N and lacks a direct GTFS match will also be suppressed. This is an acceptable false-positive rate given the near-universal Swiss convention of N-prefixes for night lines.
- **Stop assignment:** Once the main loop drops the line (freq_score = None), the stop assignment loop never runs for it. No change is needed in the stop assignment section.
- **Runtime filter intact:** The existing time-window filter on GTFS data must not be changed. The suppression is a complementary gate on the OSM side, not a replacement.
