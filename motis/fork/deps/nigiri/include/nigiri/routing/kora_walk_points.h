#pragma once

// kora fork: walk-weighted transfer points (fix-long-transfer-walks.md).
//
// RAPTOR's discrete Pareto criterion is no longer "number of boardings"
// but POINTS: each boarding costs 1 point plus extra points for the walk
// that led to it. The round index of the label arrays becomes a point
// level; a label at level p means "this arrival spent p points".
//
// kora_walk_delta(walk_minutes, minwalk) returns the EXTRA levels a
// walk adds on top of the boarding's own 1. Two tables, selected per
// query via transfer_time_settings::kora_minwalk_points_ (the app's
// minimize-walking toggle — routing-options.md § Minimize walking):
//
//                     standard   minwalk
//   walk <=  3 min ->   +0         +0    (plain transfer = 1 point)
//   walk <=  5 min ->   +0         +1
//   walk <= 10 min ->   +1         +2
//   walk <= 20 min ->   +2         +3
//   walk <= 30 min ->   +3         +4
//   walk <= 40 min ->   +4         +5
//   walk <= 50 min ->   +5         +6
//   walk  > 50 min ->   +6         +6
//
// One point per ~10 min of walking, minwalk one class ahead of
// standard (avoiding a 5-10 min walk is worth an extra transfer).
// Class boundaries are free — the cost of a walk is the level it
// lands on, not the number of rows — so the ladder is fine-grained
// up to 50 min and flat above: the top value (+6) bounds the highest
// level a query can reach and therefore the rounds RAPTOR runs.
// There is NO default for the `minwalk` argument on purpose — every
// call site must pass the query's flag, or search and reconstruction
// could disagree on levels.
//
// Where the deltas attach:
//   - transfer footpaths / intermodal egress: in-search (raptor.h,
//     update_footpaths / update_td_offsets / update_intermodal_footpaths
//     write their target at round k + delta instead of k),
//   - access walks: the start seed level (search.h / pong.cc pass the
//     start offset's walk delta to add_start),
//   - reconstruction consumes the same deltas backwards
//     (reconstruct.cc) and the alternates extraction prices its egress
//     candidates with them (kora_alternatives.h).
//
// The class is always derived from the SAME duration value the search
// adds to the clock (adjusted footpath duration resp. raw offset
// duration), so search and reconstruction can never disagree. The
// same-stop transfer buffer (update_transfers) is a change buffer, not
// a walk — it never gets a delta.

namespace nigiri::routing {

constexpr unsigned kora_walk_delta(int const walk_minutes,
                                   bool const minwalk) {
  if (minwalk) {
    return walk_minutes <= 3    ? 0U
           : walk_minutes <= 5  ? 1U
           : walk_minutes <= 10 ? 2U
           : walk_minutes <= 20 ? 3U
           : walk_minutes <= 30 ? 4U
           : walk_minutes <= 40 ? 5U
                                : 6U;
  }
  return walk_minutes <= 5    ? 0U
         : walk_minutes <= 10 ? 1U
         : walk_minutes <= 20 ? 2U
         : walk_minutes <= 30 ? 3U
         : walk_minutes <= 40 ? 4U
         : walk_minutes <= 50 ? 5U
                              : 6U;
}

}  // namespace nigiri::routing
