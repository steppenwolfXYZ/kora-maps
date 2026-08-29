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
static constexpr auto const kMaxVias = 2;

}  // namespace nigiri::routing
