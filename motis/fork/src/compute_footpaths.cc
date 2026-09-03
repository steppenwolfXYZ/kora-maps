// Kora Maps fork of motis/src/compute_footpaths.cc.
//
// Replaces MOTIS's OSR-based stop-to-stop transfer-table generator with
// a loader that reads a precomputed CSV matrix (rows:
// from_stop_id,to_stop_id,duration_sec). The matrix is built by
// scripts/build_valhalla_footpath_matrix.py querying the local Valhalla
// pedestrian router (see .claude/concepts/valhalla-pedestrian-router.md).
//
// Every loaded pair also passes the minimum-transfer-time floor (see
// transfer-point-optimization.md § Minimum transfer time): the matrix
// says how long the walk takes, the feed says how long a change is
// allowed to take, and the transfer table gets the larger of the two.
// The per-pair minima come from GTFS transfers.txt (KORA_GTFS_TRANSFERS
// _PATH), NOT from nigiri's profile-0 footpaths — those mix transfers
// .txt rows with the loader's geometric link_stop_distance links (100 m
// default), and a geometric link between two quays of one island
// platform carries a 0-minute duration that would defeat the floor.
//
// The env var KORA_FOOTPATH_MATRIX_PATH selects Valhalla as the
// footpath source. When set, this function ignores every OSR argument
// and populates TWO transfer tables from the matrix (two-tier split,
// see transfer-point-optimization.md § Two-tier transfer table):
//   - kFootProfile: rows ≤ KORA_TRANSFER_CAP_MINUTES (default 30) —
//     the table default queries search on.
//   - kora_valhalla::kFullTransferProfile: all rows up to
//     max_footpath_length (2 h) — fallback queries (cascade
//     escalation) and station-endpoint offsets.
// When unset, the function throws — the concept forbids a
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
#include "motis/kora_valhalla.h"

#include <algorithm>
#include <charconv>
#include <cstdint>
#include <cstdlib>
#include <cstring>
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

// GTFS transfers.txt of the feed being imported — source of the
// operator's own per-pair minimum transfer times. Default matches the
// bind mount in motis/docker-compose.yml (`../data/gtfs_motis` ->
// `/data/gtfs`).
constexpr auto kTransfersEnvVar = "KORA_GTFS_TRANSFERS_PATH";
constexpr auto kDefaultTransfersPath = "/data/gtfs/transfers.txt";

// Floor applied to a quay-to-quay transfer the feed says nothing about.
// Two minutes is MOTIS's own `default_transfer_time` for same-stop
// re-boarding, so a change between two quays is now never cheaper than
// staying on one.
constexpr auto kDefaultFloorMinutes = 2U;

// Absolute floor, below which no transfer may ever fall — including one
// the feed marks as a timed connection. Alighting and boarding at the
// same instant is the reckless tier (routing-options.md § Connection
// safety), which the search must never hand out on its own.
constexpr auto kAbsoluteFloorMinutes = 1U;

// Cap (minutes) of the DEFAULT transfer table (foot profile). The full
// 2-h table goes into kora_valhalla::kFullTransferProfile for fallback
// queries and station-endpoint offsets. See transfer-point-optimization.md
// § Two-tier transfer table.
constexpr auto kCapEnvVar = "KORA_TRANSFER_CAP_MINUTES";
constexpr auto kDefaultCapMinutes = 30L;

std::chrono::minutes read_transfer_cap() {
  auto const* env = std::getenv(kCapEnvVar);
  if (env == nullptr || *env == '\0') {
    return std::chrono::minutes{kDefaultCapMinutes};
  }
  auto mins = 0L;
  auto const [ptr, ec] =
      std::from_chars(env, env + std::strlen(env), mins);
  utl::verify(ec == std::errc{} && *ptr == '\0' && mins > 0,
              "kora fork: {} must be a positive integer, got '{}'",
              kCapEnvVar, env);
  return std::chrono::minutes{mins};
}

// Trim a single trailing CR (Windows line endings) — everything else is
// tolerated by std::string_view comparisons directly.
std::string_view rtrim_cr(std::string_view s) {
  if (!s.empty() && s.back() == '\r') {
    s.remove_suffix(1);
  }
  return s;
}

// Split one CSV line on commas outside double quotes and strip the
// quotes. transfers.txt in the Swiss feed quotes every field.
std::vector<std::string_view> split_csv(std::string_view line) {
  auto out = std::vector<std::string_view>{};
  auto in_quotes = false;
  auto field_start = std::size_t{0};
  auto const push = [&](std::size_t const end) {
    auto f = line.substr(field_start, end - field_start);
    if (f.size() >= 2U && f.front() == '"' && f.back() == '"') {
      f = f.substr(1, f.size() - 2);
    }
    out.push_back(f);
  };
  for (auto i = std::size_t{0}; i != line.size(); ++i) {
    if (line[i] == '"') {
      in_quotes = !in_quotes;
    } else if (line[i] == ',' && !in_quotes) {
      push(i);
      field_start = i + 1U;
    }
  }
  push(line.size());
  return out;
}

// Key for a directed pair of locations. `to_idx` lives in cista (the
// strong-type header), reached by ADL on location_idx_t — nigiri does
// not re-export it under its own namespace.
constexpr std::uint64_t pair_key(n::location_idx_t const from,
                                 n::location_idx_t const to) {
  return (static_cast<std::uint64_t>(to_idx(from)) << 32U) |
         static_cast<std::uint64_t>(to_idx(to));
}

// The operator's own minimum transfer time per directed quay pair, in
// SECONDS, read from GTFS transfers.txt. Only cross-stop rows are read:
// same-stop rows land in nigiri's `locations_.transfer_time_` at load
// time and never pass through the footpath table.
//
//   transfer_type=2 -> min_transfer_time is the minimum.
//   transfer_type=1 -> a timed (guaranteed) connection: the vehicles are
//                      scheduled to meet, so the pair is exempt from the
//                      default floor (0 here; kAbsoluteFloorMinutes still
//                      applies). These rows are keyed by TRIP pair, which
//                      a stop-keyed table cannot express — the exemption
//                      therefore widens to every trip over that quay pair.
//                      Accepted: the feed carries 281 such rows.
//   everything else -> ignored (0/3 carry no time, 4/5 are stay-seated).
//
// Duplicates keep the SMALLEST value, so a pair listed as both timed and
// timed-with-a-minimum ends up on the permissive side.
std::unordered_map<std::uint64_t, unsigned> read_official_minimums(
    std::unordered_map<std::string, n::location_idx_t> const& id_idx) {
  auto const* env = std::getenv(kTransfersEnvVar);
  auto const path =
      std::string{env != nullptr && *env != '\0' ? env : kDefaultTransfersPath};
  auto in = std::ifstream{path};
  utl::verify(in.good(),
              "kora fork: cannot open GTFS transfers.txt at {} — set {} or "
              "mount the feed; refusing to import without the operator's "
              "minimum transfer times (transfer-point-optimization.md "
              "§ Minimum transfer time)",
              path, kTransfersEnvVar);

  auto header = std::string{};
  utl::verify(static_cast<bool>(std::getline(in, header)),
              "kora fork: {} is empty", path);
  auto header_view = rtrim_cr(header);
  // Strip a UTF-8 BOM: the official feed ships one.
  if (header_view.rfind("\xEF\xBB\xBF", 0) == 0) {
    header_view.remove_prefix(3);
  }
  auto col = std::unordered_map<std::string, std::size_t>{};
  {
    auto const fields = split_csv(header_view);
    for (auto i = std::size_t{0}; i != fields.size(); ++i) {
      col.emplace(std::string{fields[i]}, i);
    }
  }
  auto const idx = [&](char const* name) {
    auto const it = col.find(name);
    utl::verify(it != end(col), "kora fork: {} has no '{}' column", path,
                name);
    return it->second;
  };
  auto const c_from = idx("from_stop_id");
  auto const c_to = idx("to_stop_id");
  auto const c_type = idx("transfer_type");
  auto const c_min = idx("min_transfer_time");

  auto out = std::unordered_map<std::uint64_t, unsigned>{};
  auto n_timed = 0UL;
  auto n_unknown = 0UL;
  auto line = std::string{};
  while (std::getline(in, line)) {
    auto const trimmed = rtrim_cr(line);
    if (trimmed.empty()) {
      continue;
    }
    auto const f = split_csv(trimmed);
    if (f.size() <= std::max({c_from, c_to, c_type, c_min})) {
      continue;
    }
    if (f[c_type] != "1" && f[c_type] != "2") {
      continue;
    }
    if (f[c_from] == f[c_to]) {
      continue;
    }
    auto const from_it = id_idx.find(std::string{f[c_from]});
    auto const to_it = id_idx.find(std::string{f[c_to]});
    if (from_it == end(id_idx) || to_it == end(id_idx)) {
      ++n_unknown;
      continue;
    }
    auto secs = 0U;
    if (f[c_type] == "1") {
      ++n_timed;  // timed connection: no minimum beyond the absolute floor
    } else {
      auto const v = f[c_min];
      auto const [ptr, ec] =
          std::from_chars(v.data(), v.data() + v.size(), secs);
      if (ec != std::errc{} || ptr != v.data() + v.size()) {
        continue;  // type 2 without a usable time carries no information
      }
    }
    auto const key = pair_key(from_it->second, to_it->second);
    auto const it = out.find(key);
    if (it == end(out)) {
      out.emplace(key, secs);
    } else {
      it->second = std::min(it->second, secs);
    }
  }

  fmt::println(std::clog,
               "kora fork: {} per-pair minimum transfer times from {} "
               "({} timed connections, {} rows with unknown stop ids)",
               out.size(), path, n_timed, n_unknown);
  return out;
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
    std::unordered_map<std::string, n::location_idx_t> const& id_idx,
    std::unordered_map<std::uint64_t, unsigned> const& official_minimums,
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

  auto line = std::string{};
  auto n_rows = 0UL;
  auto n_kept = 0UL;
  auto n_unknown_id = 0UL;
  auto n_over_cap = 0UL;
  auto n_raised = 0UL;
  auto const max_secs = static_cast<unsigned>(max_duration.count());

  // Minimum-transfer-time floor (transfer-point-optimization.md
  // § Minimum transfer time). The walk is what Valhalla measured; the
  // floor is what the operator allows. The table gets the larger, so a
  // change is never scheduled faster than either permits.
  auto const floored = [&](n::location_idx_t const from,
                           n::location_idx_t const to,
                           unsigned const secs) {
    auto const walk_min = (secs + 59U) / 60U;
    auto const it = official_minimums.find(pair_key(from, to));
    auto const floor_min = it != end(official_minimums)
                               ? (it->second + 59U) / 60U
                               : kDefaultFloorMinutes;
    auto const dur =
        std::max({walk_min, floor_min, kAbsoluteFloorMinutes});
    if (dur > walk_min) {
      ++n_raised;
    }
    return n::duration_t{dur};
  };

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
          transfers[from_it->second].emplace_back(n::footpath{
              to_it->second,
              floored(from_it->second, to_it->second, secs)});
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
        n::footpath{to_it->second, floored(from_it->second, to_it->second, secs)});
    ++n_kept;
  }

  fmt::println(std::clog,
               "kora fork: loaded {} footpath rows ({} kept, {} unknown id, "
               "{} over max_footpath_length, {} raised to the minimum "
               "transfer time) from {}",
               n_rows, n_kept, n_unknown_id, n_over_cap, n_raised, path);
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

  // Built once and shared by both matrix loads below.
  auto const id_idx = build_id_index(tt);
  auto const official_minimums = read_official_minimums(id_idx);

  // Sort, filter, mirror, and write `transfers` into the given profile
  // slot, then build its lower-bound graphs.
  auto const publish = [&](n::profile_idx_t const profile_idx,
                           std::chrono::minutes const max_duration) {
    // Sort each source's list by (target, duration) to satisfy nigiri's
    // build_lb_graph assumptions; drop over-cap entries as a safety
    // net (load_matrix_into already filters, but sort/erase is cheap).
    for (auto& fps : transfers) {
      std::erase_if(fps, [&](n::footpath fp) {
        return fp.duration() > max_duration;
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
      tt.locations_.footpaths_out_[profile_idx].emplace_back(x);
    }
    for (auto const& x : transfers_in) {
      tt.locations_.footpaths_in_[profile_idx].emplace_back(x);
    }

    n::loader::build_lb_graph<n::direction::kForward>(tt, profile_idx);
    n::loader::build_lb_graph<n::direction::kBackward>(tt, profile_idx);
  };

  for (auto const& mode : settings) {
    for (auto& fps : transfers) {
      fps.clear();
    }

    if (mode.profile_idx_ == n::kFootProfile) {
      // Two-tier split (transfer-point-optimization.md § Two-tier
      // transfer table): the foot profile gets only the capped subset
      // default queries search on; the full table goes into the spare
      // slot below.
      auto const cap = std::min(read_transfer_cap(),
                                std::chrono::duration_cast<std::chrono::minutes>(
                                    mode.max_duration_));
      load_matrix_into(id_idx, official_minimums, transfers, cap);
      publish(n::kFootProfile, cap);

      for (auto& fps : transfers) {
        fps.clear();
      }
      load_matrix_into(id_idx, official_minimums, transfers,
                       mode.max_duration_);
      publish(kora_valhalla::kFullTransferProfile,
              std::chrono::duration_cast<std::chrono::minutes>(
                  mode.max_duration_));
    } else {
      // Non-foot profiles: keep the transfer table empty. The map's UI
      // does not surface wheelchair or car routing; leaving these empty
      // costs nothing at query time and preserves the concept's "no
      // silent OSR fallback" invariant.
      fmt::println(std::clog,
                   "kora fork: profile_idx {} left empty (Valhalla covers "
                   "foot only)",
                   static_cast<unsigned>(mode.profile_idx_));
      publish(mode.profile_idx_,
              std::chrono::duration_cast<std::chrono::minutes>(
                  mode.max_duration_));
    }
  }

  return elevator_footpath_map_t{};
}

}  // namespace motis
