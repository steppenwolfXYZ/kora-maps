# OTP Migration (Deferred)

## Problem

MOTIS's pedestrian routing is more conservative than the OSM data supports. Even after preprocessing (`preprocess_osm_for_motis.py`) adds `foot=yes` to Swiss `access=agricultural` / `forestry` ways, further gaps keep surfacing — missing crossings at complex urban junctions (canonical case: Eigerplatz, Bern), pedestrian-only shortcuts not selected between adjacent stops, walk detours where the crow-flight route is obviously walkable in reality. The failure mode is systemic: MOTIS's OSR walk profile is strict about which OSM tags mean "walkable" and about how bus/tram stop nodes snap onto the foot network. Every observed case so far has been an OSM-tag or graph-snap conservatism rather than a fundamental algorithm limit, but the class of issues is broad enough that tag-fixing may not close it.

MVP goal is best-available pedestrian navigation. If preprocessor extensions can't close the gap, the backend swap becomes worth doing.

## Requirements

- **When to trigger the migration.** Only when a class of pedestrian failures is reproducibly *not* fixable via `preprocess_osm_for_motis.py` tag rewrites — genuine graph-topology gaps, poor stop-to-walk-network snapping, missing cost model for stairs/elevators, complex station approach failures. Individual OSM tag conservatism (missing `foot=yes`, `access=customers`, service driveways) does NOT trigger the switch; it's fixed in the preprocessor.
- **Target engine.** OpenTripPlanner v2 (OTP). Widely used in production (Portland, Helsinki, various German cities), Java, well-documented, accepts a broader OSM tag set out of the box, has cost models for stairs / elevators, better platform-to-walk-network snapping. Ingests GTFS + OSM PBF in the same shape MOTIS does.
- **Not a hybrid.** No first/last-mile-only pedestrian router next to MOTIS. Coordinating transfer-walk timing across two engines is more integration cost than a full swap and doesn't fix intra-station navigation any better.
- **What survives.** The pipeline's GTFS-side outputs (`gtfs_routed/`, `route_color_index.json`, `line_index.json`, `stop_search_index.json`) and OSM preprocessor stay usable — OTP consumes both. The routing panel UI (`RoutingPanel.svelte`, badges, warnings, cascade) stays; only the client wrapping `client.ts` and the cascade's `plan()` shape re-target OTP's `/otp/routers/default/plan` API.
- **What changes.** The Docker setup under `motis/` swaps for OTP; the walking-budget cascade in `state.svelte.ts` needs re-tuning against OTP's parameter model (its equivalents of `maxPreTransitTime` / `maxPostTransitTime` / `numItineraries` / `searchWindow`); `stripStationWalks` re-checks against OTP's leg shape; direct-walk merging re-checks against OTP's `direct` behaviour.
- **Ceiling.** OTP still won't hit SBB-quality station navigation — SBB layers proprietary indoor plans on top of HAFAS. Any OSM-only engine has the same ceiling. Reaching that quality is a separate initiative (buy HERE / TomTom pedestrian data, or manually annotate top-N Swiss stations).

## Constraints

- Estimated effort: multiple weeks. Not appropriate mid-MVP unless the pedestrian failures become blocking.
- Keep MOTIS running until OTP is fully wired and verified — no cut-over without a side-by-side quality check on the same set of known-problematic queries.
- OTP's OSR replacement handles most tag conservatism natively; that's not a reason to keep tag rewrites in the preprocessor after migration — audit and prune.
- No architectural bets that only make sense under one engine (e.g. MOTIS-specific header hacks in `client.ts`) — keep the client thin so the swap edits one file, not many.
