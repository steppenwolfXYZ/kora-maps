# Bus mode classification (city bus vs. regional bus)

## Problem

The pfaedle migration replaced the previous numbering- and operator-based rule for splitting `bus` into `bus` (city, blue) vs. `regional_bus` (turquoise) with a single rule: lines whose routed shape is at least 12 km long are regional, everything else is city. The 12 km rule was always a fallback in the old code; the primary signals — line-number digit count and operator — were dropped because they depended on OSM-side tags that no longer exist after the migration to pfaedle-routed GTFS shapes.

The result is that long city-bus routes (which can easily exceed 12 km of road-following geometry) are misclassified as regional, and the classification ignores the operator-specific numbering conventions that determine the real distinction.

## Requirements

For lines in the `bus` bucket (GTFS `route_type` 3 and 11), the mode is `bus` or `regional_bus`. Inputs available: the GTFS `short_name`, the GTFS `agency_id`, and the routed length of the line in kilometres.

### Digit portion of the line number

The "digit portion" of `short_name` is the string obtained by removing every non-digit character. Its length is referred to as `n`. Examples: `"X33"` has `n = 2`, `"200 (Höribus)"` has `n = 3`, `"TEL"` has `n = 0`.

### Two-digit-regional agency set

A closed set of agency_ids called `TWO_DIGIT_REGIONAL_AGENCIES`. For agencies in this set, a 2-digit ref denotes a regional line; a 1-digit ref on the same agency is still a city bus. Membership is feed-specific and must be defined as an explicit list of agency_ids (not a substring match on agency_name).

The set covers operators that follow the "1-digit = city, 2-digit = regional" convention. In the current SBB feed this is:

- STI Bus AG and its sub-agencies (STI, STI Berg, STI-gwb).
- Bus und Service AG (Chur).
- Chur-Dreibündenstein (BCD).
- PostAuto AG (the main PAG agency).
- Trägerverein Historische Postautolinie (THP).

The PostAuto sub-agency for Bus Commune Sion (PAG/BCS) is explicitly **not** in the set: it is a PostAuto-operated city service whose 2-digit refs are city lines.

### transN city-bus agency set

A closed set of agency_ids called `TRANSN_CITY_AGENCIES`. For agencies in this set, a 3-digit ref whose first digit is `1` or `3` denotes a city bus, overriding the default n≥3 → regional rule. transN numbers its urban networks in the 100s and 300s; everything else (200s, 400s, 500s) stays regional.

In the current SBB feed this is:

- TRN-tn (`000153`) — Neuchâtel city network (100s + 120s).
- TRN/tc (`000792`) — La Chaux-de-Fonds + Le Locle urban network (300s).

Other transN-family agencies (TRN/Autr `000796`, TRN/Auto `000156`, etc.) are deliberately **not** in the set: their 100s and 300s are regional village services.

### Classification

Conditions are checked in order; the first one that matches wins.

1. If `n == 3`, `agency_id` is in `TRANSN_CITY_AGENCIES`, and the first digit of the ref is `1` or `3` → **city bus**.
2. If `n >= 3` → **regional bus**. Three or more digits in the ref means regional regardless of operator.
3. If `n == 2` and `agency_id` is in `TWO_DIGIT_REGIONAL_AGENCIES` → **regional bus**.
4. If `n == 0` (pure-letter ref) and the routed length is at least 10 km → **regional bus**. Pure-letter refs (e.g. `A`, `G`, `TEL`, `Rot`) get a length fallback because the digit-based rule cannot apply.
5. Otherwise → **city bus**.

> **Historical note.** A previous iteration of this rule mapped `short_name == "EV"` (Ersatzverkehr / Bahnersatz / rail replacement) to **regional bus**. That rule has been removed: the MVP map shows general year-round connections, so all EV-prefixed lines are dropped upstream in `04_preprocess_gtfs.py` (any route whose `route_short_name` begins with `EV` is excluded before pfaedle runs). A future daily-updating variant of the map will reintroduce them with their own rendering.

## Constraints

- The classification is per emitted feature (i.e. per merged-stop variant of a trip group), not per line. Variants of one line that share `short_name` and `agency_id` will classify the same way under rules 1–3; only the length fallback (rule 4) can produce different outcomes for different variants of a pure-letter line.
- The 10 km threshold in rule 4 uses the pfaedle-routed shape length, not a stop-to-stop straight-line distance.
- Buckets other than `bus` are unaffected. Train, tram, metro, ferry, mountain classification is unchanged.
- `TWO_DIGIT_REGIONAL_AGENCIES` is feed-dependent. If SBB introduces new agencies or splits PostAuto further in a future feed, the list must be re-checked. Other PostAuto-operated city services beyond Sion may exist inside the main PAG agency_id; those cannot be detected from the agency layer alone and would be misclassified by rule 3 until surfaced.
- The previous OSM-era mountain-name keyword override (forcing `mode = mountain` for lines whose OSM name contained Kleine Scheidegg, Jungfraujoch, Schilthorn, Eigergletscher, Jungfrau) is not part of this rule. Mountain promotion is the responsibility of the `mountain_rack_agencies` config and the `route_type`-based bucketing in the pipeline.
- A `regional_bus` outcome from these rules is no longer final: the landuse-based promotion runs directly afterwards and may promote the whole line group to city bus based on its corridor's built-up share — see `citybus-landuse-promotion.md`.
