// Kora fork: HTTP client for the Valhalla pedestrian router.
//
// Valhalla is the sole walking authority (see
// .claude/concepts/valhalla-pedestrian-router.md). This client backs the
// two query-time surfaces the fork rewires away from OSR:
//   - one_to_many(): pre/post-transit offsets (get_offsets WALK branch)
//   - route():       WALK leg geometry + direct walk itineraries
//                    (street_routing kFoot intercept)
// The import-time transfer table comes from the precomputed matrix
// (compute_footpaths.cc) and never goes through this client.
//
// Error policy — "no silent fallback": transport-level failures
// (Valhalla down, timeout) THROW; the query fails with an error instead
// of degrading to OSR walking. A pair Valhalla cannot connect ("no
// path") is a normal result: route() returns nullopt, one_to_many()
// yields nullopt cells.

#pragma once

#include <chrono>
#include <optional>
#include <vector>

#include "geo/latlng.h"
#include "geo/polyline.h"

#include "nigiri/types.h"

namespace motis::kora_valhalla {

// Profile slot holding the FULL (2 h) Valhalla transfer table. The foot
// profile carries only the capped subset (KORA_TRANSFER_CAP_MINUTES,
// default 30) that default queries search on; the full table lives in
// its own dedicated slot (added by the fork's nigiri types.h overlay)
// and is selected per query via the `koraFullTransfers=true` flag the
// app sends on cascade escalation. NEVER park it in an existing named
// slot: the bike / car slots lose lower-bound-graph transit edges for
// routes without the matching allowed-flag (walk-scale lower bounds →
// RAPTOR overprunes), and profile 2 is hardwired as the wheelchair flag
// in the raptor drivers. Station-endpoint WALK offsets always read the
// full table. See transfer-point-optimization.md § Two-tier transfer
// table.
constexpr auto const kFullTransferProfile = nigiri::kKoraFullTransferProfile;

// Base walking speed baked into every Valhalla call. MUST stay equal to
// WALK_SPEED_KMH in scripts/build_valhalla_footpath_matrix.py — the
// matrix (transfer table) and the live query-time walks describe the
// same physical walking and must agree. Changing it requires a matrix
// rebuild.
constexpr auto const kWalkSpeedKmh = 5.1;

// Spacing (metres) at which Valhalla samples the elevation profile along
// a walk shape. Matches the ~30 m native resolution of the SRTM-derived
// elevation tiles — finer sampling would only add interpolation noise.
constexpr auto const kElevationIntervalM = 30.0;

// Seconds charged for each lift ride, roughly wait plus travel. Valhalla
// defaults to 0, which makes a lift a free level change and beats the ramp
// beside it. MUST stay equal to `elevator_penalty` in COSTING_JSON in
// scripts/build_valhalla_footpath_matrix.py — the transfer matrix and the
// query-time walks have to describe the same walker. Changing it requires
// a matrix rebuild.
constexpr auto const kElevatorPenaltySec = 60.0;

// Reversal threshold (metres) of the ascent / descent accumulator. A
// direction change smaller than this is DEM noise, not a hill: summing
// raw sample deltas over a multi-kilometre walk otherwise invents tens
// of metres of climb.
constexpr auto const kElevationNoiseM = 3.0;

// Speed (km/h) charged for the residual gap between a walk shape's
// endpoint and the coordinate that was actually asked for. Valhalla
// snaps a request onto the nearest routable edge, so a stop whose
// platform has no walkable geometry is "reached" from wherever that
// edge happens to be. Charging the leftover distance at normal walking
// pace would understate it — an unmodelled gap is more likely to hide
// stairs or a detour than a clear straight run — so it is charged
// slower. Deliberately not punitive: the platform walk network
// (station-walk-network.md) removes the gap at the stations where it
// mattered, and this only covers what it could not reach.
constexpr auto const kGapWalkSpeedKmh = 2.6;

// Gaps below this are snapping noise, not walking.
constexpr auto const kGapIgnoreM = 1.0;

// Distance (metres) over which the slow gap speed applies. Past it the
// remainder is charged at normal walking pace: a short gap is plausibly
// unmodelled stairs or a kink, but a 200 m one means the requested point
// simply sits off the network — a free-form map click in a field — and
// penalising all of it would let the gap dominate the leg.
constexpr auto const kGapPenaltyMaxM = 60.0;

struct walk_route {
  std::chrono::seconds duration_;
  double distance_m_;
  geo::polyline shape_;
  // Noise-filtered ascent / descent along the walk, in metres, derived
  // from the elevation profile Valhalla samples every
  // kElevationIntervalM along the shape. nullopt when the response
  // carried no elevation array (no elevation data built) — the API's
  // leg.elevationUp / leg.elevationDown then stay absent.
  std::optional<double> ascent_m_;
  std::optional<double> descent_m_;
};

// Point-to-point pedestrian route. Returns nullopt when Valhalla finds
// no path or the walk exceeds `max`. Throws on transport failure.
std::optional<walk_route> route(geo::latlng const& from,
                                geo::latlng const& to,
                                std::chrono::seconds max);

// One query coordinate against many stop coordinates.
// forward=true: walking pos -> stop (pre-transit).
// forward=false: walking stop -> pos (post-transit).
// Result[i] is the walking time to/from stops[i]; nullopt = unreachable.
// Throws on transport failure.
std::vector<std::optional<std::chrono::seconds>> one_to_many(
    geo::latlng const& pos,
    std::vector<geo::latlng> const& stops,
    bool forward);

// Startup probe: GET /status once, abort the process with a clear
// message when Valhalla is unreachable. Called from server() — never
// from import (import consumes the CSV matrix, not live Valhalla).
// Under docker `restart: unless-stopped` the resulting exit acts as a
// natural wait-for-valhalla retry loop.
void ensure_reachable_or_abort();

}  // namespace motis::kora_valhalla
