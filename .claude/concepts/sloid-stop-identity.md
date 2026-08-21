# SLOID stop identity

## Problem

The official GTFS feed switched its Swiss stop-ID scheme on 2026-06-04
(SLOID migration, announced and permanent). Stop IDs changed from
UIC-based (`8500010:0:19`, `Parent8500010`) to SLOID-based
(`ch:1:sloid:10:0:19`, `Parentch:1:sloid:10`); the UIC number moved into
a new `didok` column. Content is otherwise unchanged (verified against
the last pre-switch release: UIC mapping loss-free, coords and platform
codes identical up to routine upkeep) and granularity increased.

The pipeline's identity logic still assumes UIC-prefixed stop IDs: every
`stop_id.split(":")[0]` fallback now yields `"ch"`, the routing
sidecar's platform snap extracts garbage UICs from parent IDs and
matches nothing, and post-pfaedle steps have no UIC at all because
pfaedle strips the non-standard `didok` column. We adopt SLOID as the
canonical scheme rather than remapping back — SLOIDs are the platform's
official stable identifiers; link stability with the old scheme is
explicitly not required.

## Terminology (new feed structure)

- **Station** — StopPlace SLOID (`ch:1:sloid:10`), carried by the
  parent stop (`Parentch:1:sloid:10`). Every Swiss stop row carries the
  station's UIC number in `didok`.
- **Track** (quay) — a plain quay stop (`ch:1:sloid:10:0:19`) whose
  `platform_code` is the public track/stop designation (`19`, `A`).
- **Sector variant** — a `_gen:` stop
  (`ch:1:sloid:10_gen:ch:1:sloid:10:0:19_pf:19A-D`) representing a
  sector range on a track. Its `_gen:` middle part names the quay SLOID
  when known, or `missingSLOID` otherwise; its `platform_code` is the
  sector-range code (`19A-D`, `2A`). Special characters in codes are
  replaced with periods (`21/22` → `21.22`).

Trips reference tracks and sector variants alike; both are real
boarding stops.

## Requirements

### Canonical identity

1. The feed's `stop_id` (SLOID-based) is the canonical stop identity
   end to end: pipeline artifacts, `line_stops.json`, MOTIS sidecar,
   footpath matrix, and every diagnostic keyed by stop.
2. The station-level merge key (the "merged UIC" of the identity model)
   is the numeric UIC taken from the `didok` column — never parsed out
   of `stop_id`. Trip grouping, `content_tg_id` hashes, direction keys,
   dwell aggregation, stop scores/tiers, far-zoom dedup, and search
   index dedup all key on it. Because the UIC set is unchanged across
   the migration, line keys and trip-group IDs come out identical to
   the pre-switch pipeline.
3. Every `split(":")[0]`-style UIC fallback is removed. Where a stop
   has no usable `didok`, the stop merges only under its parent (or
   stands alone) — no digit-guessing from IDs.

### Identity sidecar (new artifact: `stop_identity.json`)

4. Step 04 emits a per-stop identity table covering every stop in the
   filtered feed: canonical `stop_id`, station SLOID, `didok` UIC,
   track code, sector code (when the stop is a sector variant), the
   referenced quay `stop_id` (when derivable), and parent `stop_id`.
   This is the single bridge across pfaedle: all post-pfaedle consumers
   (steps 06/07, the MOTIS GTFS sidecar, diagnostics) take UIC / SLOID /
   track / sector from it, never from `stops.txt` columns that pfaedle
   drops.

### Track vs sector

5. Map rendering (all zoom levels — dots, pills, pill-arrows,
   connectors, labels) operates at **track** granularity. Sector
   variants collapse onto their track: via the quay SLOID in their
   `_gen:` ID when present, else by station + the track prefix of their
   sector code (`19A-D` → `19`). A collapsed sector variant contributes
   its trips/frequency to the track and never yields a separate drawn
   stop or a sector-range label.
6. Routing keeps full stop granularity: sector variants remain distinct
   stops in the MOTIS sidecar and the footpath matrix, with their own
   coords, so platform-sector walking is routed faithfully.

### Routing sidecar

7. The platform snap keys on (UIC from the identity sidecar,
   `platform_code`) against OSM `(uic_ref, local_ref)`. Sector variants
   snap via their track's code when no sector-specific OSM platform
   exists. Stops that don't match keep their feed coords (which are the
   official atlas coords since the migration).

### Joins to external sources

8. The atlas traffic-point join keys on SLOID directly (`stop_id` for
   plain stops, the quay SLOID for sector variants) — no UIC round-trip.
9. OSM matching (`uic_ref`) and any station-level external join use the
   `didok` UIC.

### Config and client

10. `gtfs_stop_overrides` / `gtfs_trip_overrides` entries that name
    stop IDs are re-keyed to the SLOID scheme (canonical case:
    Grindelwald Terminal).
11. The client-facing station key in `stop_search_index.json` and stop
    deep links is the `didok` UIC (numeric, stable, matches OSM/atlas
    vocabulary). Line deep links keep the existing
    `ref~agency_id~mode~trip_group_id` keys, which requirement 2 keeps
    stable.

## Constraints

- Foreign stops (DE/FR/IT/AT, `8002301…`-style IDs, including their
  `_gen:missingSLOID` variants) must keep working; they also carry
  `didok`. `missingSLOID` sector variants have no quay reference —
  track collapse for them uses station + code prefix only.
- pfaedle's input/output contract is untouched; the identity sidecar is
  the only bridge.
- No behavior change for anything not keyed on stop identity: bucket
  classification, frequency gates, mountain rules, EV drop, casing and
  color rules, bridge deck.
- The matrix and MOTIS import already run on canonical SLOIDs; their
  formats don't change — only the sidecar's snapping becomes functional
  again.
- Old-scheme artifacts (pre-migration matrix CSVs, cached indexes) are
  not migrated; they are rebuilt.
