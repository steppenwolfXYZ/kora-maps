#pragma once

// kora fork: ε-alternates (near-optimal-endpoint-alternatives.md).
//
// RAPTOR keeps a per-stop optimum, but journey collection only ever reads
// out the single best (stop arrival + egress offset) combination per
// Pareto point — an equal-or-slightly-worse journey over a different
// egress/access stop is fully computed and silently discarded. The
// helpers here extract those near-optimal endpoint variants out of a
// finished raptor state. Shared by both search drivers: the classic
// rRAPTOR loop (search.h) and the PONG driver's forward ping pass
// (pong.cc). The search itself is untouched — the only added cost is a
// scan over the destination offsets plus a handful of validated
// reconstructions per Pareto point.
//
// Candidate arrival via stop s: round_times[k][s] has two possible
// writers — the vehicle's arrival plus the stop's change-time buffer
// (update_transfers), or a plain transfer footpath from a nearby stop
// (update_footpaths). Which one won is not recorded, and their values
// sit within the change time of each other, so no subtraction guess is
// safe. Instead the candidate is anchored at the upper bound
// round_times[k][s] + walk (the true endpoint time can only be earlier,
// whichever writer won), reconstruction finds the actual vehicle
// (get_transport accepts earlier arrivals and validates boardability
// against the previous round), and the endpoint walk leg is then
// snapped back to the vehicle's real arrival, which also fixes
// dest_time_. Candidates reconstruction cannot validate are dropped —
// a wrong guess costs one failed reconstruction, never a wrong journey.

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <optional>
#include <string>
#include <string_view>
#include <tuple>
#include <utility>
#include <variant>
#include <vector>

#include "nigiri/common/delta_t.h"
#include "nigiri/routing/journey.h"
#include "nigiri/routing/kora_walk_points.h"
#include "nigiri/routing/query.h"
#include "nigiri/routing/transfer_time_settings.h"
#include "nigiri/rt/frun.h"
#include "nigiri/timetable.h"
#include "nigiri/types.h"

namespace nigiri::routing {

inline location_idx_t kora_parent_of(timetable const& tt,
                                     location_idx_t const l) {
  auto const p = tt.locations_.parents_[l];
  return p == location_idx_t::invalid() ? l : p;
}

using kora_fingerprint_t = std::vector<std::tuple<std::uint32_t,
                                                  std::int32_t,
                                                  std::uint32_t,
                                                  std::uint32_t,
                                                  std::uint32_t>>;

// Quay-blind transit fingerprint: one entry per transit leg with the run
// identity and the board/alight parent stations. Journeys with equal
// fingerprints ride the same vehicles between the same stations and
// differ only in quay choice or walking detail — duplicates for the
// user.
inline kora_fingerprint_t kora_transit_fingerprint(timetable const& tt,
                                                   journey const& j) {
  auto out = kora_fingerprint_t{};
  for (auto const& leg : j.legs_) {
    if (auto const* r = std::get_if<journey::run_enter_exit>(&leg.uses_);
        r != nullptr) {
      out.emplace_back(static_cast<std::uint32_t>(to_idx(r->r_.t_.t_idx_)),
                       static_cast<std::int32_t>(to_idx(r->r_.t_.day_)),
                       static_cast<std::uint32_t>(to_idx(r->r_.rt_)),
                       static_cast<std::uint32_t>(
                           to_idx(kora_parent_of(tt, leg.from_))),
                       static_cast<std::uint32_t>(
                           to_idx(kora_parent_of(tt, leg.to_))));
    }
  }
  return out;
}

// True when two consecutive transit legs ride the same line — a
// same-line re-board ("take the 28 one stop, wait for the next 28" or
// "ride the 28 backwards one stop and return on the through run") can
// never beat riding through or taking the direct run, both of which the
// response already carries; such alternates are fabrication noise from
// forcing a transfer count onto an egress stop whose true best is fewer
// transfers. Compared by line NAME, not nigiri route: opposite
// directions and short-turn variants of one line are distinct routes
// but the same fabrication.
inline bool kora_same_route_reboard(timetable const& tt, journey const& j) {
  auto prev = std::string_view{};
  for (auto const& leg : j.legs_) {
    auto const* r = std::get_if<journey::run_enter_exit>(&leg.uses_);
    if (r == nullptr) {
      continue;
    }
    auto const name = r->r_.is_scheduled() ? tt.transport_name(r->r_.t_.t_idx_)
                                           : std::string_view{};
    if (!name.empty() && name == prev) {
      return true;
    }
    prev = name;
  }
  return false;
}

// True when the journey returns to a parent station it already passed —
// riding back to (or through) an earlier station is never sensible; a
// primary can never do this (optimality forbids it), so any alternate
// that does is reconstruction fabrication. A station shared between two
// legs purely as their transfer point (an endpoint of both) is fine;
// the violation is a station appearing in two legs with at least one
// occurrence in a leg's INTERIOR (ridden through).
inline bool kora_revisits_station(timetable const& tt,
                                  rt_timetable const* rtt,
                                  journey const& j) {
  struct entry {
    location_idx_t parent_;
    unsigned first_leg_;
    bool multi_leg_;
    bool interior_;
  };
  auto seen = std::vector<entry>{};
  auto leg_i = 0U;
  for (auto const& leg : j.legs_) {
    auto const* r = std::get_if<journey::run_enter_exit>(&leg.uses_);
    if (r == nullptr) {
      continue;
    }
    ++leg_i;
    auto const fr = rt::frun{tt, rtt, r->r_};
    auto const from = r->stop_range_.from_;
    auto const to = r->stop_range_.to_;  // exclusive
    for (auto i = from; i < to; ++i) {
      auto const p = kora_parent_of(
          tt, fr[static_cast<stop_idx_t>(i)].get_location_idx());
      auto const interior = i != from && i != to - 1U;
      auto const it =
          std::find_if(begin(seen), end(seen),
                       [&](entry const& e) { return e.parent_ == p; });
      if (it == end(seen)) {
        seen.push_back({p, leg_i, false, interior});
      } else {
        it->multi_leg_ = it->multi_leg_ || it->first_leg_ != leg_i;
        it->interior_ = it->interior_ || interior;
        if (it->multi_leg_ && it->interior_) {
          return true;
        }
      }
    }
  }
  return false;
}

// Extracts the near-optimal endpoint alternates for ONE Pareto journey
// out of the live raptor state (forward search: egress side, backward
// search: access side) and appends the successfully reconstructed ones
// to `out`. `reconstruct` must run the owning algo's reconstruction on
// the synthesized journey; it throws on infeasible candidates, which
// drops the candidate. Every quay within the slack is tried (best
// first) — duplicates are controlled ONLY by the quay-blind fingerprint
// set `seen`, so two different lines arriving at different platforms of
// one station both survive, while the same vehicle via a sibling quay
// collapses. Accepted fingerprints are added to `seen`. When the
// primary journey's own fingerprint is not in `seen`
// (`primary_in_seen = false`), one extra acceptance slot compensates
// for the primary's duplicate, which the driver's final
// kora_dedupe_alternatives removes.
template <direction SearchDir, typename RoundTimes, typename Reconstruct>
void kora_collect_endpoint_alternatives(timetable const& tt,
                                        rt_timetable const* rtt,
                                        query const& q,
                                        journey const& j,
                                        RoundTimes const& round_times,
                                        date::sys_days const base,
                                        std::vector<kora_fingerprint_t>& seen,
                                        bool const primary_in_seen,
                                        Reconstruct&& reconstruct,
                                        std::vector<journey>& out) {
  constexpr auto const kFwd = SearchDir == direction::kForward;
  if (q.kora_alt_epsilon_ == duration_t{0} || q.kora_alt_max_ == 0U ||
      q.dest_match_mode_ != location_match_mode::kIntermodal ||
      !q.via_stops_.empty()) {
    return;
  }
  // The primary's own level — diagnostics only; the level scan below
  // deliberately does not cap at it (see the comment on the scan).
  auto const k = static_cast<unsigned>(j.transfers_) + 1U;
  auto const dir = [](int const x) { return kFwd ? x : -x; };
  auto const better = [](delta_t const a, delta_t const b) {
    return kFwd ? a < b : a > b;
  };
  auto const best = unix_to_delta(base, j.dest_time_);

  // kora fork walk-weighted points (kora_walk_points.h): with weighted
  // walks the round index is a point level, and a path reaching an
  // egress stop can sit at ANY level — scan every row (cheap int16
  // reads, ≤ kMaxTransfers+2 per stop) and keep the best candidate per
  // stop: earliest anchored endpoint time, level as tiebreak. The scan
  // must NOT cap at the primary's own level: walk deltas only raise a
  // candidate's level AFTER its row is read (cand_level = lvl + kd), so
  // a capped scan reached high levels via long walks but never via
  // ridden rows — a low-level ride + long walk was extractable while
  // the same corridor with one more boarding and less walking, sitting
  // one row higher, was structurally invisible (canonical: S1 + 15-min
  // walk extractable from a level-2 primary, S44+S3+bus 28 tying its
  // arrival with a third of the walking never read from row 3). The ε
  // gap gate below still bounds which candidates qualify. The
  // candidate's journey level is the label's level plus the egress
  // walk's own class delta; reconstruction is anchored at exactly that
  // level.
  struct kora_cand {
    delta_t time_;
    offset const* o_;
    unsigned level_;  // label level + egress walk delta
  };
  auto cands = std::vector<kora_cand>{};
  for (auto const& o : q.destination_) {
    auto const s = o.target_;
    auto const kd = kora_walk_delta(
        static_cast<int>(o.duration_.count()),
        q.transfer_time_settings_.kora_minwalk_points_);
    auto const change = adjusted_transfer_time(
        q.transfer_time_settings_, tt.locations_.transfer_time_[s].count());
    auto best_cand = std::optional<kora_cand>{};
    for (auto lvl = 1U; lvl + kd < round_times.n_rows_; ++lvl) {
      auto const rt = round_times[lvl][to_idx(s)][0];
      if (rt == kInvalidDelta<SearchDir>) {
        continue;
      }
      auto const cand = clamp(static_cast<int>(rt) +
                              dir(static_cast<int>(o.duration_.count())));
      auto const gap = kFwd ? static_cast<int>(cand) - static_cast<int>(best)
                            : static_cast<int>(best) - static_cast<int>(cand);
      // The anchor overshoots the true endpoint time by up to the change
      // buffer when the entry was vehicle-written — widen the gate by it;
      // the exact slack is re-checked after reconstruction tightened the
      // times.
      if (gap > q.kora_alt_epsilon_.count() + change) {
        continue;
      }
      if (!best_cand.has_value() || better(cand, best_cand->time_)) {
        best_cand = kora_cand{cand, &o, lvl + kd};
      }
    }
    if (best_cand.has_value()) {
      cands.emplace_back(*best_cand);
    }
  }
  std::sort(begin(cands), end(cands), [&](auto const& a, auto const& b) {
    return better(a.time_, b.time_);
  });

  // Stderr diagnostics, off unless the container runs with
  // KORA_ALT_DEBUG set — one line per candidate with its outcome.
  static bool const kDebug = std::getenv("KORA_ALT_DEBUG") != nullptr;
  auto const dbg = [&](location_idx_t const s, delta_t const cand,
                       char const* outcome) {
    if (kDebug) {
      auto const name =
          tt.get_default_translation(tt.locations_.names_[s]);
      std::fprintf(stderr,
                   "[kora-alt] k=%u best=%d cand=%d stop=%.*s -> %s\n",
                   k, static_cast<int>(best), static_cast<int>(cand),
                   static_cast<int>(name.size()), name.data(), outcome);
    }
  };

  auto const limit = static_cast<unsigned>(q.kora_alt_max_) +
                     (primary_in_seen ? 0U : 1U);
  auto n_added = 0U;
  for (auto const& [cand, o, cand_level] : cands) {
    if (n_added == limit) {
      dbg(o->target_, cand, "limit-full");
      continue;
    }
    auto a = journey{.legs_ = {},
                     .start_time_ = j.start_time_,
                     .dest_time_ = delta_to_unix(base, cand),
                     .dest_ = j.dest_,
                     // kora fork walk-weighted points: the candidate's
                     // own level, not the primary's — reconstruction
                     // anchors at transfers_ + 1.
                     .transfers_ = static_cast<std::uint8_t>(cand_level - 1U)};
    a.kora_alt_egress_ = o->target_;
    try {
      reconstruct(a);
    } catch (std::exception const& e) {
      if (kDebug) {
        auto const msg = std::string{"reconstruct-fail: "} + e.what();
        dbg(o->target_, cand, msg.c_str());
      }
      continue;
    }
    if (!a.is_reconstructed_) {
      dbg(o->target_, cand, "not-reconstructed");
      continue;
    }
    // Snap the endpoint walk leg to the adjacent transit leg: the anchor
    // was an upper bound, so reconstruction may have found the vehicle
    // arriving earlier, leaving artificial waiting inside the walk leg.
    // Then re-check the slack against the true endpoint time.
    if (a.legs_.size() >= 2U) {
      if constexpr (kFwd) {
        auto& walk_leg = a.legs_.back();
        auto const& transit_leg = a.legs_[a.legs_.size() - 2U];
        if (std::holds_alternative<offset>(walk_leg.uses_) &&
            walk_leg.dep_time_ > transit_leg.arr_time_) {
          auto const d = walk_leg.dep_time_ - transit_leg.arr_time_;
          walk_leg.dep_time_ -= d;
          walk_leg.arr_time_ -= d;
          a.dest_time_ = walk_leg.arr_time_;
        }
      } else {
        auto& walk_leg = a.legs_.front();
        auto const& transit_leg = a.legs_[1U];
        if (std::holds_alternative<offset>(walk_leg.uses_) &&
            walk_leg.arr_time_ < transit_leg.dep_time_) {
          auto const d = transit_leg.dep_time_ - walk_leg.arr_time_;
          walk_leg.dep_time_ += d;
          walk_leg.arr_time_ += d;
          a.dest_time_ = walk_leg.dep_time_;
        }
      }
    }
    auto const true_time = unix_to_delta(base, a.dest_time_);
    auto const true_gap =
        kFwd ? static_cast<int>(true_time) - static_cast<int>(best)
             : static_cast<int>(best) - static_cast<int>(true_time);
    if (true_gap > q.kora_alt_epsilon_.count()) {
      dbg(o->target_, cand, "outside-slack");
      continue;
    }
    if (kora_same_route_reboard(tt, a)) {
      dbg(o->target_, cand, "same-line-reboard");
      continue;
    }
    if (kora_revisits_station(tt, rtt, a)) {
      dbg(o->target_, cand, "revisits-station");
      continue;
    }
    auto fp = kora_transit_fingerprint(tt, a);
    if (fp.empty() || std::find(begin(seen), end(seen), fp) != end(seen)) {
      dbg(o->target_, cand, "duplicate");
      continue;
    }
    dbg(o->target_, cand, "accepted");
    seen.emplace_back(std::move(fp));
    out.emplace_back(std::move(a));
    ++n_added;
  }
}

// Final duplicate/clutter control for the collected alternates.
//
// 1. Exact duplicates: an alternate whose quay-blind fingerprint matches
//    a primary or an earlier alternate rides exactly the same vehicles
//    between the same stations — dropped. Walk-only alternates (empty
//    fingerprint) are dropped too.
// 2. Endpoint-station dominance: journeys identical except WHERE the
//    endpoint station is (same runs, same remainder — only the exit
//    station for leave-at, the boarding station for arrive-by, and its
//    walk differ) are comparable on exactly two numbers: endpoint time
//    and endpoint walk. An alternate equal-or-worse than a same-
//    remainder primary or alternate on BOTH axes offers nothing — the
//    destination is not "between the stations" — and is dropped. When
//    each station wins one axis, both stay.
// 3. Ride-through redundancy: an alternate whose exit station is served
//    no later by a kept journey's endpoint vehicle — ridden past that
//    journey's own exit, without requiring an earlier departure from
//    home — is the same corridor journey in disguise (canonical:
//    28→6→Egghölzli vs 28→8→Weltpostverein, where the 8 itself reaches
//    Egghölzli one stop later). Every station it serves, the kept
//    journey's vehicle serves at least as well. Mirrored to the
//    boarding side for arrive-by.
//    "Same corridor" is verified, not assumed: the two journeys' full
//    stop sets (every parent station ridden through, interior stops
//    included, minus the query's shared anchor — the origin-side
//    boarding station for leave-at, the destination-side alighting
//    station for arrive-by, shared by every journey of the query by
//    construction) must overlap by ≥ kKoraCorridorCoverage of the
//    SMALLER set. The min-side denominator keeps the express-vs-local
//    pair matching in both directions (the express's stops are a
//    subset of the local's, never the reverse). Without this gate the
//    rule conflated entirely different routes that merely end near
//    each other — canonical: Thun→Belp→S3→bus 28 killed by
//    Thun→Bern→S1→bus 10 reaching Eigerplatz inside the slack, two
//    routes sharing no stop but their origin, with the strictly worse
//    sibling (same trains, more walking, later arrival) surviving.
template <direction SearchDir, typename Journeys>
void kora_dedupe_alternatives(timetable const& tt,
                              rt_timetable const* rtt,
                              Journeys const& primaries,
                              std::vector<journey>& alts) {
  constexpr auto const kFwd = SearchDir == direction::kForward;
  constexpr auto const kSlack = duration_t{1};

  // Endpoint-side transit leg (the vehicle adjacent to the varied
  // endpoint): last transit leg in presented order for forward queries,
  // first for backward.
  auto const endpoint_leg = [&](journey const& j) -> journey::leg const* {
    if constexpr (kFwd) {
      for (auto it = j.legs_.rbegin(); it != j.legs_.rend(); ++it) {
        if (std::holds_alternative<journey::run_enter_exit>(it->uses_)) {
          return &*it;
        }
      }
    } else {
      for (auto const& l : j.legs_) {
        if (std::holds_alternative<journey::run_enter_exit>(l.uses_)) {
          return &l;
        }
      }
    }
    return nullptr;
  };

  // Rule 3's corridor test: the query's shared anchor station — every
  // journey boards there for leave-at (alights there for arrive-by), so
  // it carries no corridor information and is excluded from the sets.
  auto const anchor_station = [&](journey const& j) -> location_idx_t {
    if constexpr (kFwd) {
      for (auto const& l : j.legs_) {
        if (std::holds_alternative<journey::run_enter_exit>(l.uses_)) {
          return kora_parent_of(tt, l.from_);
        }
      }
    } else {
      for (auto it = j.legs_.rbegin(); it != j.legs_.rend(); ++it) {
        if (std::holds_alternative<journey::run_enter_exit>(it->uses_)) {
          return kora_parent_of(tt, it->to_);
        }
      }
    }
    return location_idx_t::invalid();
  };

  // Sorted, deduped set of every parent station the journey's transit
  // legs touch — interior ride-through stops included — minus the
  // shared query anchor.
  auto const corridor_stops =
      [&](journey const& j) -> std::vector<location_idx_t> {
    auto out = std::vector<location_idx_t>{};
    auto const anchor = anchor_station(j);
    for (auto const& leg : j.legs_) {
      auto const* r = std::get_if<journey::run_enter_exit>(&leg.uses_);
      if (r == nullptr) {
        continue;
      }
      auto const fr = rt::frun{tt, rtt, r->r_};
      for (auto i = r->stop_range_.from_; i < r->stop_range_.to_; ++i) {
        auto const p = kora_parent_of(
            tt, fr[static_cast<stop_idx_t>(i)].get_location_idx());
        if (p != anchor) {
          out.push_back(p);
        }
      }
    }
    std::sort(begin(out), end(out));
    out.erase(std::unique(begin(out), end(out)), end(out));
    return out;
  };

  // Two journeys ride the same corridor when their stop sets overlap by
  // at least this share of the SMALLER set (min-side so express-vs-
  // local matches regardless of which side is the express).
  constexpr auto const kKoraCorridorCoverage = 0.75;
  auto const same_corridor = [](std::vector<location_idx_t> const& a,
                                std::vector<location_idx_t> const& b) {
    if (a.empty() || b.empty()) {
      return false;
    }
    auto shared = std::size_t{0U};
    for (auto const& s : a) {
      if (std::binary_search(begin(b), end(b), s)) {
        ++shared;
      }
    }
    return static_cast<double>(shared) >=
           kKoraCorridorCoverage *
               static_cast<double>(std::min(a.size(), b.size()));
  };

  // Coverage descriptor of a kept journey for rule 3: its endpoint
  // vehicle and the stop index bounding what a rider of that journey
  // could still reach (after boarding for fwd, before alighting for
  // bwd), plus the journey's own door times for the no-earlier-
  // commitment gate and its corridor stop set for the same-corridor
  // gate.
  struct cover {
    rt::run run_;
    stop_idx_t bound_;
    unixtime_t dep_, arr_;
    std::vector<location_idx_t> stops_;
  };
  auto covers = std::vector<cover>{};
  auto const add_cover = [&](journey const& j) {
    auto const* l = endpoint_leg(j);
    if (l == nullptr) {
      return;
    }
    auto const& r = std::get<journey::run_enter_exit>(l->uses_);
    covers.push_back(
        {r.r_,
         kFwd ? r.stop_range_.from_
              : static_cast<stop_idx_t>(r.stop_range_.to_ - 1U),
         j.departure_time(), j.arrival_time(), corridor_stops(j)});
  };
  auto const ride_through_redundant = [&](journey const& a) {
    auto const* l = endpoint_leg(a);
    if (l == nullptr) {
      return false;
    }
    auto const s = kora_parent_of(tt, kFwd ? l->to_ : l->from_);
    auto const t_a = kFwd ? l->arr_time_ : l->dep_time_;
    auto const a_stops = corridor_stops(a);
    for (auto const& c : covers) {
      if (kFwd ? c.dep_ + kSlack < a.departure_time()
               : c.arr_ - kSlack > a.arrival_time()) {
        continue;  // would require an earlier commitment than a
      }
      if (!same_corridor(a_stops, c.stops_)) {
        continue;  // different route end to end, not a disguise
      }
      auto const fr = rt::frun{tt, rtt, c.run_};
      if constexpr (kFwd) {
        for (auto i = c.bound_ + 1U; i < fr.size(); ++i) {
          auto const stp = fr[static_cast<stop_idx_t>(i)];
          if (kora_parent_of(tt, stp.get_location_idx()) == s &&
              stp.time(event_type::kArr) <= t_a + kSlack) {
            return true;
          }
        }
      } else {
        for (auto i = stop_idx_t{0U}; i < c.bound_; ++i) {
          auto const stp = fr[i];
          if (kora_parent_of(tt, stp.get_location_idx()) == s &&
              stp.time(event_type::kDep) >= t_a - kSlack) {
            return true;
          }
        }
      }
    }
    return false;
  };
  struct info {
    kora_fingerprint_t prefix_;
    unixtime_t time_;
    unixtime_t::duration walk_;
  };
  // Endpoint-side leg in presented leg order: forward journeys carry the
  // search-dest offset leg last, backward (arrive-by / un-flipped ping)
  // journeys carry it first.
  auto const get_info = [&](journey const& j) -> info {
    auto prefix = kora_transit_fingerprint(tt, j);
    if (prefix.empty() || j.legs_.empty()) {
      return {std::move(prefix), unixtime_t{}, unixtime_t::duration{0}};
    }
    if constexpr (kFwd) {
      std::get<4>(prefix.back()) = 0U;
    } else {
      std::get<3>(prefix.front()) = 0U;
    }
    auto const& el = kFwd ? j.legs_.back() : j.legs_.front();
    auto const walk = std::holds_alternative<offset>(el.uses_)
                          ? el.arr_time_ - el.dep_time_
                          : unixtime_t::duration{0};
    return {std::move(prefix), kFwd ? el.arr_time_ : el.dep_time_, walk};
  };
  auto const dominated_by = [&](info const& a, info const& b) {
    if (a.prefix_.empty() || a.prefix_ != b.prefix_) {
      return false;
    }
    auto const worse_time = kFwd ? a.time_ >= b.time_ : a.time_ <= b.time_;
    return worse_time && a.walk_ >= b.walk_;
  };

  // Better endpoint first, so a dominating alternate is always processed
  // before the ones it dominates and one pass suffices.
  std::sort(begin(alts), end(alts), [&](journey const& a, journey const& b) {
    auto const ia = get_info(a);
    auto const ib = get_info(b);
    auto const earlier = kFwd ? ia.time_ < ib.time_ : ia.time_ > ib.time_;
    return earlier || (ia.time_ == ib.time_ && ia.walk_ < ib.walk_);
  });

  auto seen_fps = std::vector<kora_fingerprint_t>{};
  auto refs = std::vector<info>{};
  for (auto const& j : primaries) {
    if (j.is_reconstructed_) {
      seen_fps.emplace_back(kora_transit_fingerprint(tt, j));
      refs.emplace_back(get_info(j));
      add_cover(j);
    }
  }
  alts.erase(
      std::remove_if(
          begin(alts), end(alts),
          [&](journey const& a) {
            auto fp = kora_transit_fingerprint(tt, a);
            if (fp.empty() || std::find(begin(seen_fps), end(seen_fps), fp) !=
                                  end(seen_fps)) {
              return true;
            }
            auto in = get_info(a);
            if (std::any_of(begin(refs), end(refs), [&](info const& r) {
                  return dominated_by(in, r);
                })) {
              return true;
            }
            if (ride_through_redundant(a)) {
              return true;
            }
            seen_fps.emplace_back(std::move(fp));
            refs.emplace_back(std::move(in));
            add_cover(a);
            return false;
          }),
      end(alts));
}

}  // namespace nigiri::routing
