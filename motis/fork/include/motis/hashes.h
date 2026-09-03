#pragma once

#include <filesystem>
#include <map>
#include <string>
#include <utility>

namespace motis {

using meta_entry_t = std::pair<std::string, std::uint64_t>;
using meta_t = std::map<std::string, std::uint64_t>;

constexpr auto const osr_version = []() {
  return meta_entry_t{"osr_bin_ver", 37U};
};
constexpr auto const adr_version = []() {
  return meta_entry_t{"adr_bin_ver", 15U};
};
constexpr auto const adr_ext_version = []() {
  return meta_entry_t{"adr_ext_bin_ver", 6U};
};
constexpr auto const n_version = []() {
  // kora fork: upstream 37 + 1000. The types.h overlay bumps kNProfiles
  // to 6, which changes the timetable binary layout — this bump makes
  // every import task fingerprinted on the nigiri version (tt,
  // osr_footpath) rebuild instead of silently serving pre-bump indexes
  // (the "import-skip trap", README § Runtime environment). The +1000
  // offset keeps future upstream bumps (38, 39, …) distinct so a
  // MOTIS_REF bump still re-imports.
  return meta_entry_t{"nigiri_bin_ver", 1037U};
};
constexpr auto const tbd_version = []() {
  return meta_entry_t{"tbd_bin_ver", 1U};
};
constexpr auto const matches_version = []() {
  return meta_entry_t{"matches_bin_ver", 6U};
};
constexpr auto const tiles_version = []() {
  return meta_entry_t{"tiles_bin_ver", 2U};
};
constexpr auto const osr_footpath_version = []() {
  return meta_entry_t{"osr_footpath_bin_ver", 5U};
};
constexpr auto const routed_shapes_version = []() {
  return meta_entry_t{"routed_shapes_ver", 11U};
};

std::string to_str(meta_t const&);

meta_t read_hashes(std::filesystem::path const& data_path,
                   std::string const& name);

void write_hashes(std::filesystem::path const& data_path,
                  std::string const& name,
                  meta_t const& h);

}  // namespace motis
