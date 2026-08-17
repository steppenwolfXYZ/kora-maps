# Valhalla pedestrian router

## Problem

MOTIS's built-in OSM-based walker produces poor walking times on three surfaces the user sees: direct walk-only itineraries, pre/post-transit walks (query coord ↔ first/last stop), and inter-transit transfer walks (stop ↔ stop between two transit legs). Measured symptoms: a ~4–5 min fixed overhead per off-graph endpoint (so any direct coord-to-coord walk pays ~9 min of pure connector cost on top of real walking time), no elevation awareness, and per-route walking speeds that vary without a physical reason. Downstream effects: direct walks look far slower than they are; transit itineraries with one-minute bus rides beat the walk they replace purely because they pay the endpoint penalty once instead of twice; and mid-journey walking shortcuts that a fast walker would actually take never enter MOTIS's transfer table, so RAPTOR cannot surface them.

## Requirements

- **Valhalla is the sole walking authority.** Every walking duration and geometry surfaced to the user comes from Valhalla, not from MOTIS's OSM walker. Direct walks, pre/post-transit walks, and inter-transit transfer walks are all Valhalla-computed.
- **Valhalla-computed transfer table drives RAPTOR.** MOTIS's stop-to-stop transfer table is populated from Valhalla, so RAPTOR sees real walking times when deciding whether a mid-journey walk is worth taking. A "fast walker shortcut" that Pareto-dominates the transit-only alternative on arrival time appears in results automatically — this is not a new surfacing rule, it is the existing Pareto-pruning behaviour operating on correct data.
- **Elevation-aware walking cost.** Slope, stairs, and surface influence walking duration on all three surfaces per Valhalla's own pedestrian cost model.
- **Configurable walking speed profile.** The base walking speed is a project-level configuration with a single default reflecting a normal-brisk pace. Per-user adaptation and speed learned from history are explicitly future work, not part of the MVP.
- **Direct-walk timing matches transit-leg walk timing.** The same physical walk of X metres returns the same duration whether it appears as a direct itinerary or as a pre/post-transit leg. No fixed endpoint penalties, no per-leg constants introduced anywhere in the app or the fork.
- **Transfer table coverage.** Valhalla is queried for every stop pair within a configurable walking-time radius sufficient to let mid-journey walking shortcuts of at least 15–20 min appear in RAPTOR results. The radius is a configurable value; it must be at least the walking-time equivalent of MOTIS's `max_footpath_length`.
- **New identifiers introduced:**
  - `valhalla_footpath_matrix` — the precomputed stop-to-stop walking-time matrix produced at import time by calling Valhalla, consumed by MOTIS's transfer-table builder in place of its own OSM-walker output.
  - A MOTIS config key that selects Valhalla as the footpath source (name to be pinned during implementation).

## Constraints

- **MOTIS's RAPTOR is untouched.** The fork replaces only the source of the stop-to-stop footpath matrix. Search algorithm, dominance rules, `transfers.txt` handling for stay-seated / forbidden transfers, and result shape stay as-is.
- **MOTIS's OSM ingest stays in place** for uses other than pedestrian footpath generation. The fork narrows what MOTIS's own walker is called for; it does not remove OSM from MOTIS.
- **No silent fallback to MOTIS's OSM walker.** If Valhalla is unreachable at query time or a matrix call fails at import time, the failure surfaces explicitly. Mixing Valhalla-quality and MOTIS-quality times in the same result would be worse than either alone.
- **Elevation data lives inside Valhalla.** No separate elevation pipeline is maintained for pedestrian routing; Valhalla's own elevation source is authoritative.
- **Architecture must not preclude bike and car-sharing legs on the same Valhalla instance.** Not a MVP feature, but the integration shape (client, matrix pipeline, config surface) is chosen so a later addition of bike or car-share routing against the same Valhalla is a straightforward extension, not a rewrite.
- **App-layer per-user speed adaptation and learned speeds are out of scope.** Router-side infrastructure that would block these later must not be introduced, but the features themselves are deferred.
