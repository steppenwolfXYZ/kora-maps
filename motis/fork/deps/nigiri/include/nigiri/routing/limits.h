#pragma once

#include <cinttypes>

#include "nigiri/types.h"

namespace nigiri::routing {

// kora fork: with walk-weighted transfer points (kora_walk_points.h,
// fix-long-transfer-walks.md) the round index counts POINTS, not
// boardings — a boarding costs 1..10 points depending on the walk that
// led to it. This cap is therefore a journey points cap, generous
// enough that legitimate multi-long-walk journeys (three >40-min hikes
// plus rides) still fit. Upstream value: 14 (plain transfers).
static constexpr auto const kMaxTransfers = std::uint8_t{45U};
static constexpr auto const kMaxTravelTime = 5_days;
static constexpr auto const kMaxSearchIntervalSize =
    date::days{std::numeric_limits<duration_t::rep>::max() / 1440} -
    (kMaxTravelTime + 2_days);
// kora fork: 3 instead of upstream's 2 (via-stops.md). RAPTOR carries a
// separate Pareto front per "vias visited so far", so every +1 grows the
// per-location state arrays by one slot (raptor_state.cc sizes them by
// kMaxVias + 1) and adds a template instantiation per direction x rt.
// Every site that switches on the via count carries a static_assert on
// this constant: raptor_search.cc, raptor/reconstruct.cc, raptor/pong.cc,
// raptor/raptor_state.cc — all four are fork overlays for that reason.
static constexpr auto const kMaxVias = 3;

}  // namespace nigiri::routing
