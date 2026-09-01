// Kora fork: HTTP client for the Valhalla pedestrian router.
// See include/motis/kora_valhalla.h for the contract.

#include "motis/kora_valhalla.h"

#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <exception>
#include <future>
#include <iostream>
#include <map>
#include <mutex>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include "boost/asio/co_spawn.hpp"
#include "boost/asio/io_context.hpp"
#include "boost/json.hpp"
#include "boost/url/url.hpp"

#include "fmt/format.h"
#include "fmt/ostream.h"

#include "utl/verify.h"

#include "motis/http_req.h"

namespace json = boost::json;
using namespace std::chrono_literals;

namespace motis::kora_valhalla {

namespace {

std::string const& base_url() {
  static auto const url = []() {
    auto const* env = std::getenv("KORA_VALHALLA_URL");
    auto s = std::string{env != nullptr && *env != '\0'
                             ? env
                             : "http://kora-valhalla:8002"};
    while (!s.empty() && s.back() == '/') {
      s.pop_back();
    }
    return s;
  }();
  return url;
}

// Pedestrian costing shared by every call. MUST stay in sync with
// COSTING_JSON in scripts/build_valhalla_footpath_matrix.py — the
// import-time matrix and these query-time calls must describe the same
// walker. destination_only_penalty / driveway_factor neutralize
// Valhalla's car-oriented driveway defaults (Swiss footway shortcuts
// routinely cross driveways).
json::object costing() {
  return {
      {"costing", "pedestrian"},
      {"costing_options",
       json::object{
           {"pedestrian",
            json::object{
                {"walking_speed", kWalkSpeedKmh},
                {"use_hills", 1.0},
                {"use_lit", 0.0},
                {"destination_only_penalty", 0.0},
                {"driveway_factor", 1.0},
            }},
       }},
  };
}

struct http_result {
  int status_;
  std::string body_;
};

// Synchronous bridge over MOTIS's coroutine http helpers: dedicated
// io_context per call, exception_ptr captured explicitly (a detached
// completion token would swallow transport errors — and swallowing is
// exactly what the no-silent-fallback rule forbids).
http_result post_sync(std::string const& path,
                      std::string const& body,
                      std::chrono::seconds const timeout) {
  auto result = http_result{};
  auto eptr = std::exception_ptr{};
  auto ioc = boost::asio::io_context{};
  boost::asio::co_spawn(
      ioc,
      [&]() -> boost::asio::awaitable<void> {
        auto const res = co_await http_POST(
            boost::urls::url{base_url() + path},
            {{"Content-Type", "application/json"}}, body, timeout);
        result.status_ = static_cast<int>(res.base().result_int());
        result.body_ = get_http_body(res);
      },
      [&](std::exception_ptr e) { eptr = e; });
  ioc.run();
  if (eptr) {
    try {
      std::rethrow_exception(eptr);
    } catch (std::exception const& e) {
      throw utl::fail(
          "kora fork: Valhalla unreachable at {} ({}) — walking is "
          "unavailable, no OSR fallback (valhalla-pedestrian-router.md)",
          base_url(), e.what());
    }
  }
  return result;
}

http_result get_sync(std::string const& path,
                     std::chrono::seconds const timeout) {
  auto result = http_result{};
  auto eptr = std::exception_ptr{};
  auto ioc = boost::asio::io_context{};
  boost::asio::co_spawn(
      ioc,
      [&]() -> boost::asio::awaitable<void> {
        auto const res = co_await http_GET(
            boost::urls::url{base_url() + path}, {}, timeout);
        result.status_ = static_cast<int>(res.base().result_int());
        result.body_ = get_http_body(res);
      },
      [&](std::exception_ptr e) { eptr = e; });
  ioc.run();
  if (eptr) {
    std::rethrow_exception(eptr);
  }
  return result;
}

json::array locations_json(std::initializer_list<geo::latlng> const coords) {
  auto arr = json::array{};
  for (auto const& c : coords) {
    arr.push_back(json::object{{"lat", c.lat_}, {"lon", c.lng_}});
  }
  return arr;
}

double num(json::value const& v) {
  return v.is_int64() ? static_cast<double>(v.as_int64()) : v.as_double();
}

// Bounded FIFO cache. Both caches exist because the app's query cascade
// (hop re-queries, escalations) and the per-itinerary leg fattening
// re-request identical coordinates constantly — a hit turns a 0.2-2 s
// Valhalla round trip into a map lookup. FIFO instead of LRU keeps the
// implementation trivial; with these capacities the working set of one
// user session fits comfortably either way.
template <typename V>
struct fifo_cache {
  explicit fifo_cache(std::size_t const cap) : cap_{cap} {}

  std::optional<V> get(std::string const& key) {
    auto const lock = std::lock_guard{m_};
    auto const it = map_.find(key);
    if (it == end(map_)) {
      return std::nullopt;
    }
    // Explicit optional<V>{...}: with V itself an optional (route
    // cache), a ternary `nullopt : std::optional{...}` collapses to the
    // INNER optional as common type and the miss re-wraps as an engaged
    // outer holding a disengaged inner — every miss then reads as a
    // cached "no path" and no HTTP ever fires.
    return std::optional<V>{it->second};
  }

  void put(std::string const& key, V value) {
    auto const lock = std::lock_guard{m_};
    if (map_.contains(key)) {
      return;
    }
    if (order_.size() >= cap_) {
      map_.erase(order_.front());
      order_.pop_front();
    }
    order_.push_back(key);
    map_.emplace(key, std::move(value));
  }

  std::mutex m_;
  std::unordered_map<std::string, V> map_;
  std::deque<std::string> order_;
  std::size_t cap_;
};

std::string coord_key(geo::latlng const& c) {
  return fmt::format("{:.7f},{:.7f}", c.lat_, c.lng_);
}

// FNV-1a over the raw coordinate bits — the target list of a one-to-many
// call is deterministic for a given (rtree state, radius), so hashing it
// is enough to key the cache without storing the whole list.
std::uint64_t stops_fingerprint(std::vector<geo::latlng> const& stops) {
  auto h = std::uint64_t{14695981039346656037ULL};
  auto const mix = [&](double const d) {
    auto bits = std::uint64_t{};
    std::memcpy(&bits, &d, sizeof(bits));
    for (auto i = 0U; i != 8U; ++i) {
      h ^= (bits >> (i * 8U)) & 0xFFU;
      h *= 1099511628211ULL;
    }
  };
  for (auto const& s : stops) {
    mix(s.lat_);
    mix(s.lng_);
  }
  return h;
}

// route() results are cached WITHOUT the max cutoff (applied by the
// caller after retrieval), so one entry serves every budget.
// nullopt = Valhalla found no path (cacheable — deterministic).
fifo_cache<std::optional<walk_route>>& route_cache() {
  static auto c = fifo_cache<std::optional<walk_route>>{2048};
  return c;
}

fifo_cache<std::vector<std::optional<std::chrono::seconds>>>&
matrix_cache() {
  static auto c =
      fifo_cache<std::vector<std::optional<std::chrono::seconds>>>{256};
  return c;
}

// Valhalla shapes are Google polyline, precision 6.
geo::polyline decode_polyline6(std::string_view const encoded) {
  auto out = geo::polyline{};
  auto lat = std::int64_t{0};
  auto lng = std::int64_t{0};
  auto i = std::size_t{0U};
  auto const next = [&]() {
    auto shift = 0U;
    auto result = std::int64_t{0};
    while (i < encoded.size()) {
      auto const b = static_cast<std::int64_t>(encoded[i++]) - 63;
      result |= (b & 0x1F) << shift;
      shift += 5U;
      if (b < 0x20) {
        break;
      }
    }
    return (result & 1) != 0 ? ~(result >> 1) : (result >> 1);
  };
  while (i < encoded.size()) {
    lat += next();
    lng += next();
    out.emplace_back(static_cast<double>(lat) / 1e6,
                     static_cast<double>(lng) / 1e6);
  }
  return out;
}

// Accumulate ascent / descent over a sampled elevation profile with a
// reversal threshold (kElevationNoiseM): a run in one direction is
// committed only once the profile turns back by more than the
// threshold, so DEM jitter between neighbouring samples never becomes
// climb. Returns nullopt for a profile too short to say anything.
std::optional<std::pair<double, double>> elevation_gain(
    std::vector<double> const& profile) {
  if (profile.size() < 2U) {
    return std::nullopt;
  }
  auto up = 0.0;
  auto down = 0.0;
  auto anchor = profile.front();  // last committed point
  auto ext = profile.front();  // running extreme since the anchor
  auto dir = 0;  // 0 = undecided, 1 = climbing, -1 = descending
  for (auto i = std::size_t{1U}; i != profile.size(); ++i) {
    auto const v = profile[i];
    if (dir == 0) {
      if (v - anchor > kElevationNoiseM) {
        dir = 1;
        ext = v;
      } else if (anchor - v > kElevationNoiseM) {
        dir = -1;
        ext = v;
      }
    } else if (dir == 1) {
      if (v > ext) {
        ext = v;
      } else if (ext - v > kElevationNoiseM) {
        up += ext - anchor;
        anchor = ext;
        ext = v;
        dir = -1;
      }
    } else {
      if (v < ext) {
        ext = v;
      } else if (v - ext > kElevationNoiseM) {
        down += anchor - ext;
        anchor = ext;
        ext = v;
        dir = 1;
      }
    }
  }
  if (dir == 1) {
    up += ext - anchor;
  } else if (dir == -1) {
    down += anchor - ext;
  }
  return std::pair{up, down};
}

}  // namespace

std::optional<walk_route> route(geo::latlng const& from,
                                geo::latlng const& to,
                                std::chrono::seconds const max) {
  auto const key = coord_key(from) + "|" + coord_key(to);
  auto result = route_cache().get(key);

  if (!result.has_value()) {
    auto body = costing();
    body["locations"] = locations_json({from, to});
    body["directions_options"] = json::object{{"units", "kilometers"}};
    // Ask for the elevation profile along the shape — it feeds the
    // leg's ascent / descent (transit-routing.md § Walk elevation).
    // Valhalla omits the array when no elevation data is configured;
    // that stays a soft absence, never an error.
    body["elevation_interval"] = kElevationIntervalM;

    auto const res = post_sync("/route", json::serialize(body), 10s);
    if (res.status_ == 400) {
      // "No path" and friends are a 400 with a JSON error body — a
      // normal outcome (island platform, isolated coord), not a
      // transport error. Deterministic, so cacheable.
      route_cache().put(key, std::nullopt);
      return std::nullopt;
    }
    utl::verify(res.status_ == 200, "kora fork: Valhalla /route HTTP {}: {}",
                res.status_, res.body_);

    auto const o = json::parse(res.body_).as_object();
    auto const& legs = o.at("trip").as_object().at("legs").as_array();

    auto duration = 0.0;
    auto distance_km = 0.0;
    auto shape = geo::polyline{};
    auto profile = std::vector<double>{};
    auto has_profile = false;
    for (auto const& leg : legs) {
      auto const& lo = leg.as_object();
      auto const& summary = lo.at("summary").as_object();
      duration += num(summary.at("time"));
      distance_km += num(summary.at("length"));
      auto decoded = decode_polyline6(lo.at("shape").as_string());
      shape.insert(end(shape), begin(decoded), end(decoded));
      if (auto const* e = lo.if_contains("elevation");
          e != nullptr && e->is_array()) {
        has_profile = true;
        for (auto const& v : e->as_array()) {
          // Valhalla writes a null-ish sample where the elevation tile
          // has no data; skipping keeps the neighbouring samples
          // adjacent, which is the least-wrong reading of a gap.
          if (v.is_double() || v.is_int64()) {
            profile.push_back(num(v));
          }
        }
      }
    }

    // Explicit optional type on the else branch: a bare std::nullopt
    // has no common type with optional<pair> and would not compile.
    auto const gain = has_profile
                          ? elevation_gain(profile)
                          : std::optional<std::pair<double, double>>{};

    result = legs.empty()
                 ? std::optional<walk_route>{}
                 : std::optional{walk_route{
                       std::chrono::seconds{static_cast<std::int64_t>(
                           std::ceil(duration))},
                       distance_km * 1000.0, std::move(shape),
                       gain ? std::optional<double>{gain->first}
                            : std::optional<double>{},
                       gain ? std::optional<double>{gain->second}
                            : std::optional<double>{}}};
    route_cache().put(key, *result);
  }

  auto const& walk = *result;
  if (!walk.has_value() || walk->duration_ > max) {
    // Mirror OSR's cost cutoff: a walk past the budget is "no path".
    // The cutoff applies AFTER the cache so one entry serves every
    // budget.
    return std::nullopt;
  }
  return walk;
}

namespace {

// One /sources_to_targets request for up to kMaxMatrixTargets stops.
std::vector<std::optional<std::chrono::seconds>> matrix_chunk(
    geo::latlng const& pos,
    std::vector<geo::latlng> const& stops,
    std::size_t const start,
    std::size_t const n,
    bool const forward) {
  auto many = json::array{};
  for (auto i = start; i != start + n; ++i) {
    many.push_back(
        json::object{{"lat", stops[i].lat_}, {"lon", stops[i].lng_}});
  }
  auto const one = locations_json({pos});

  auto body = costing();
  body["sources"] = forward ? one : many;
  body["targets"] = forward ? std::move(many) : one;

  auto const res =
      post_sync("/sources_to_targets", json::serialize(body), 30s);
  utl::verify(res.status_ == 200,
              "kora fork: Valhalla /sources_to_targets HTTP {}: {}",
              res.status_, res.body_);

  auto const o = json::parse(res.body_).as_object();
  auto const& rows = o.at("sources_to_targets").as_array();

  auto const cell_duration =
      [](json::value const& cell) -> std::optional<std::chrono::seconds> {
    if (!cell.is_object()) {
      return std::nullopt;
    }
    auto const* t = cell.as_object().if_contains("time");
    if (t == nullptr || t->is_null()) {
      return std::nullopt;
    }
    return std::chrono::seconds{static_cast<std::int64_t>(std::ceil(num(*t)))};
  };

  auto out = std::vector<std::optional<std::chrono::seconds>>{};
  out.reserve(n);
  if (forward) {
    // One source row, n target cells.
    auto const& row = rows.empty() ? json::array{} : rows.at(0).as_array();
    for (auto i = std::size_t{0U}; i != n; ++i) {
      out.push_back(i < row.size() ? cell_duration(row.at(i)) : std::nullopt);
    }
  } else {
    // n source rows, one target cell each.
    for (auto i = std::size_t{0U}; i != n; ++i) {
      if (i < rows.size() && !rows.at(i).as_array().empty()) {
        out.push_back(cell_duration(rows.at(i).as_array().at(0)));
      } else {
        out.push_back(std::nullopt);
      }
    }
  }
  return out;
}

}  // namespace

std::vector<std::optional<std::chrono::seconds>> one_to_many(
    geo::latlng const& pos,
    std::vector<geo::latlng> const& stops,
    bool const forward) {
  auto const key = fmt::format("{}|{}|{}|{}", coord_key(pos),
                               forward ? 'f' : 'b', stops.size(),
                               stops_fingerprint(stops));
  if (auto const hit = matrix_cache().get(key); hit.has_value()) {
    return *hit;
  }

  // Smaller chunks fired concurrently: Valhalla's matrix latency scales
  // with target count, so 4 parallel 600-target requests finish in
  // roughly a quarter of one 2400-target request (server_threads
  // permitting).
  constexpr auto const kChunk = std::size_t{600};

  auto futures =
      std::vector<std::future<std::vector<std::optional<std::chrono::seconds>>>>{};
  for (auto start = std::size_t{0U}; start < stops.size(); start += kChunk) {
    auto const n = std::min(kChunk, stops.size() - start);
    futures.push_back(std::async(std::launch::async, [&, start, n]() {
      return matrix_chunk(pos, stops, start, n, forward);
    }));
  }

  auto out = std::vector<std::optional<std::chrono::seconds>>{};
  out.reserve(stops.size());
  for (auto& f : futures) {
    auto chunk = f.get();
    out.insert(end(out), begin(chunk), end(chunk));
  }

  matrix_cache().put(key, out);
  return out;
}

void ensure_reachable_or_abort() {
  try {
    auto const res = get_sync("/status", 5s);
    utl::verify(res.status_ == 200, "HTTP {}", res.status_);
  } catch (std::exception const& e) {
    fmt::println(
        std::clog,
        "kora fork: Valhalla is not reachable at {} ({}). Walking is "
        "Valhalla-only in this build — start the valhalla service (see "
        "valhalla/docker-compose.yml) or set KORA_VALHALLA_URL. Exiting; "
        "docker's restart policy will retry.",
        base_url(), e.what());
    std::exit(1);
  }
}

}  // namespace motis::kora_valhalla
