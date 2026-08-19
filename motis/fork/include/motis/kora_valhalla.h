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

namespace motis::kora_valhalla {

// Base walking speed baked into every Valhalla call. MUST stay equal to
// WALK_SPEED_KMH in scripts/build_valhalla_footpath_matrix.py — the
// matrix (transfer table) and the live query-time walks describe the
// same physical walking and must agree. Changing it requires a matrix
// rebuild.
constexpr auto const kWalkSpeedKmh = 5.1;

struct walk_route {
  std::chrono::seconds duration_;
  double distance_m_;
  geo::polyline shape_;
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
