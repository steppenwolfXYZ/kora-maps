// Kora Maps fork of motis/src/compute_footpaths.cc.
//
// Replaces MOTIS's OSR-based stop-to-stop transfer-table generator with
// a loader that reads a precomputed CSV matrix (rows:
// from_stop_id,to_stop_id,duration_sec). The matrix is built by
// scripts/build_valhalla_footpath_matrix.py querying the local Valhalla
// pedestrian router (see .claude/concepts/valhalla-pedestrian-router.md).
//
// The env var KORA_FOOTPATH_MATRIX_PATH selects Valhalla as the
// footpath source. When set, this function ignores every OSR argument
// and populates tt.locations_.footpaths_out_/_in_[kFootProfile] from
// the matrix. When unset, the function throws — the concept forbids a
// silent fallback to the OSM walker, since mixing Valhalla-quality and
// OSR-quality times in one result set is worse than either alone.
//
// Non-foot profiles (wheelchair, car) are left empty. The map has no
// wheelchair / car routing surface, and the concept scopes Valhalla to
// pedestrians. If a future profile needs the matrix path, extend
// routed_transfers_settings rather than adding another env var.
//
// Everything else in the MOTIS binary is unmodified. The transfer table
// on disk (tt_ext.bin) is written by the same import.cc call site; only
// the values inside it change.

#include "motis/compute_footpaths.h"

#include <algorithm>
#include <charconv>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

#include "fmt/core.h"
#include "fmt/ostream.h"

#include "nigiri/loader/build_lb_graph.h"
#include "nigiri/timetable.h"

#include "utl/verify.h"

namespace n = nigiri;

namespace motis {

namespace {

constexpr auto kEnvVar = "KORA_FOOTPATH_MATRIX_PATH";

// Trim a single trailing CR (Windows line endings) — everything else is
// tolerated by std::string_view comparisons directly.
std::string_view rtrim_cr(std::string_view s) {
  if (!s.empty() && s.back() == '\r') {
    s.remove_suffix(1);
  }
  return s;
}

// Build id → location_idx_t lookup over every location in the
// timetable. Uses the bare id (as it appears in stops.txt), not the
// tag-prefixed form MOTIS uses in its public API, because the matrix is
// produced from the same stops.txt the importer read.
std::unordered_map<std::string, n::location_idx_t> build_id_index(
    n::timetable const& tt) {
  auto out = std::unordered_map<std::string, n::location_idx_t>{};
  out.reserve(tt.n_locations());
  for (auto i = n::location_idx_t{0U}; i != tt.n_locations(); ++i) {
    out.emplace(std::string{tt.locations_.ids_.at(i).view()}, i);
  }
  return out;
}

// Parse one CSV row: from_stop_id,to_stop_id,duration_sec. Returns
// false on malformed rows (skipped by the caller with a running count).
bool parse_row(std::string_view line, std::string_view& from,
               std::string_view& to, unsigned& secs) {
  auto const c1 = line.find(',');
  if (c1 == std::string_view::npos) return false;
  auto const c2 = line.find(',', c1 + 1);
  if (c2 == std::string_view::npos) return false;
  from = line.substr(0, c1);
  to = line.substr(c1 + 1, c2 - c1 - 1);
  auto const dur = line.substr(c2 + 1);
  auto const* first = dur.data();
  auto const* last = dur.data() + dur.size();
  auto const [ptr, ec] = std::from_chars(first, last, secs);
  return ec == std::errc{} && ptr == last;
}

void load_matrix_into(
    n::timetable& tt,
    n::vector_map<n::location_idx_t, std::vector<n::footpath>>& transfers,
    std::chrono::seconds max_duration) {
  auto const* env = std::getenv(kEnvVar);
  utl::verify(env != nullptr && *env != '\0',
              "kora fork: {} not set — refusing to fall back to MOTIS's OSM "
              "walker (see valhalla-pedestrian-router.md, No silent fallback)",
              kEnvVar);

  auto const path = std::string{env};
  auto in = std::ifstream{path};
  utl::verify(in.good(), "kora fork: cannot open footpath matrix at {}", path);

  auto const id_idx = build_id_index(tt);

  auto line = std::string{};
  auto n_rows = 0UL;
  auto n_kept = 0UL;
  auto n_unknown_id = 0UL;
  auto n_over_cap = 0UL;
  auto const max_secs = static_cast<unsigned>(max_duration.count());

  // Header row first — accept either the documented header
  // (`from_stop_id,to_stop_id,duration_sec`) or a data row.
  if (std::getline(in, line)) {
    auto const first = rtrim_cr(line);
    if (first.rfind("from_stop_id", 0) != 0) {
      auto from = std::string_view{};
      auto to = std::string_view{};
      auto secs = 0U;
      if (parse_row(first, from, to, secs)) {
        ++n_rows;
        // Fall through: process the row before the loop below.
        auto const from_it = id_idx.find(std::string{from});
        auto const to_it = id_idx.find(std::string{to});
        if (from_it == id_idx.end() || to_it == id_idx.end()) {
          ++n_unknown_id;
        } else if (secs > max_secs) {
          ++n_over_cap;
        } else {
          transfers[from_it->second].emplace_back(
              n::footpath{to_it->second, n::duration_t{(secs + 59U) / 60U}});
          ++n_kept;
        }
      }
    }
  }

  while (std::getline(in, line)) {
    auto const trimmed = rtrim_cr(line);
    if (trimmed.empty()) continue;
    auto from = std::string_view{};
    auto to = std::string_view{};
    auto secs = 0U;
    if (!parse_row(trimmed, from, to, secs)) continue;
    ++n_rows;
    auto const from_it = id_idx.find(std::string{from});
    auto const to_it = id_idx.find(std::string{to});
    if (from_it == id_idx.end() || to_it == id_idx.end()) {
      ++n_unknown_id;
      continue;
    }
    if (secs > max_secs) {
      ++n_over_cap;
      continue;
    }
    transfers[from_it->second].emplace_back(
        n::footpath{to_it->second, n::duration_t{(secs + 59U) / 60U}});
    ++n_kept;
  }

  fmt::println(std::clog,
               "kora fork: loaded {} footpath rows ({} kept, {} unknown id, "
               "{} over max_footpath_length) from {}",
               n_rows, n_kept, n_unknown_id, n_over_cap, path);
}

}  // namespace

elevator_footpath_map_t compute_footpaths(
    osr::ways const& /*w*/,
    osr::lookup const& /*lookup*/,
    osr::platforms const& /*pl*/,
    nigiri::timetable& tt,
    platform_matches_t const& /*matches*/,
    way_matches_storage const* /*way_matches*/,
    osr::elevation_storage const* /*elevations*/,
    std::vector<routed_transfers_settings> const& settings) {
  auto transfers = n::vector_map<n::location_idx_t, std::vector<n::footpath>>(
      tt.n_locations());
  auto transfers_in =
      n::vector_map<n::location_idx_t, std::vector<n::footpath>>{};

  for (auto const& mode : settings) {
    for (auto& fps : transfers) {
      fps.clear();
    }

    if (mode.profile_idx_ == n::kFootProfile) {
      load_matrix_into(tt, transfers, mode.max_duration_);
    } else {
      // Non-foot profiles: keep the transfer table empty. The map's UI
      // does not surface wheelchair or car routing; leaving these empty
      // costs nothing at query time and preserves the concept's "no
      // silent OSR fallback" invariant.
      fmt::println(std::clog,
                   "kora fork: profile_idx {} left empty (Valhalla covers "
                   "foot only)",
                   static_cast<unsigned>(mode.profile_idx_));
    }

    // Sort each source's list by (target, duration) to satisfy nigiri's
    // build_lb_graph assumptions; drop over-cap entries as a safety
    // net (load_matrix_into already filters, but sort/erase is cheap).
    for (auto& fps : transfers) {
      std::erase_if(fps, [&](n::footpath fp) {
        return fp.duration() > mode.max_duration_;
      });
      std::sort(fps.begin(), fps.end());
    }

    // Mirror into the reverse-direction table.
    transfers_in.clear();
    transfers_in.resize(tt.n_locations());
    for (auto i = n::location_idx_t{0U}; i != tt.n_locations(); ++i) {
      auto const& out = transfers[i];
      for (auto const fp : out) {
        transfers_in[fp.target()].push_back(n::footpath{i, fp.duration()});
      }
    }
    for (auto& v : transfers_in) {
      std::sort(v.begin(), v.end());
    }

    for (auto const& x : transfers) {
      tt.locations_.footpaths_out_[mode.profile_idx_].emplace_back(x);
    }
    for (auto const& x : transfers_in) {
      tt.locations_.footpaths_in_[mode.profile_idx_].emplace_back(x);
    }

    n::loader::build_lb_graph<n::direction::kForward>(tt, mode.profile_idx_);
    n::loader::build_lb_graph<n::direction::kBackward>(tt, mode.profile_idx_);
  }

  return elevator_footpath_map_t{};
}

}  // namespace motis
