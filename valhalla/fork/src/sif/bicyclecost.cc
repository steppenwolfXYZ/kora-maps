// kora fork: Kora-owned bicycle costing (bicycle-costing-fork.md).
//
// Full-file overlay of upstream src/sif/bicyclecost.cc at the VALHALLA_REF
// pinned in valhalla/fork/Dockerfile. The upstream file's request parsing,
// access checks, surface / speed / grade tables and test block are kept;
// the weighting model (EdgeCost + the two TransitionCost variants) is
// replaced by the three-tier quality model below. Every tunable lives in
// the `kora` namespace right under this header — nothing else in the file
// carries a magic number of its own. Everything kora-specific is marked
// with a "kora fork:" comment so a VALHALLA_REF bump can re-apply it onto
// the new upstream copy.
//
// Model in one paragraph: an edge's cost is its riding time multiplied by
// a tier factor — great (separated cycle infrastructure, slight bonus),
// fine (painted lanes, quiet streets, living streets: the plateau, all
// ≈ 1.0, so among fine options the shorter/faster one wins) or bad
// (through-traffic roads without infrastructure, multi-lane roads:
// significant penalty, calibrated so a real alternative wins but absurd
// detours do not) — plus the stock hill-avoidance term and the stock
// surface term. Official cycle routes (OSM relation membership, the
// graph's bike_network bit) earn a small multiplicative bonus. Stairs are
// priced steeply, uphill more than downhill, and `exclude_steps` removes
// them entirely. Edges that are walkable but not ridable in the travel
// direction — sidewalks, crossings, pedestrian zones, oneways against us —
// are traversable by pushing the bike at walking pace, priced above riding
// (kora::kPushSpeedKph / kPushFactor); the fork's triplegbuilder overlay
// reports those sections as pedestrian-mode maneuvers so the client can
// draw them dotted. Transitions keep upstream's turn-time model and add the
// crossing rule: moving from one through-traffic road onto another costs
// extra unless it is a right turn; straight ahead it costs only across a
// traffic signal (the proxy for "a real crossing of two big roads").
//
// Request options: everything upstream accepts still parses. `use_roads`
// is accepted for compatibility but inert — the tier model replaces what
// it used to scale. New: `exclude_steps` (bool, default false).

#include "sif/bicyclecost.h"
#include "baldr/directededge.h"
#include "baldr/graphconstants.h"
#include "baldr/nodeinfo.h"
#include "baldr/rapidjson_utils.h"
#include "baldr/turn.h"
#include "proto_conversions.h"
#include "sif/costconstants.h"
#include "sif/hierarchylimits.h"

#include <cassert>

#ifdef INLINE_TEST
#include "test.h"
#include "worker.h"

#include <random>
#endif

using namespace valhalla::midgard;
using namespace valhalla::baldr;

namespace valhalla {
namespace sif {

// ════════════════════════════════════════════════════════════════════════
// kora fork: the tuning surface. Change numbers here, rebuild the router
// image, restart — never a tile rebuild. Each block says what it does and
// how it composes with the others. Future user-facing preferences
// (fast ↔ calm, hill avoidance, official-route favouring) are meant to
// scale these per request, so keep them as plain multipliers / seconds.
// ════════════════════════════════════════════════════════════════════════
namespace kora {

// ── Quality tiers (multiply the edge's riding time) ─────────────────────
// The plateau principle: every "fine" surface sits within a few percent of
// 1.0 so none of them can buy a detour against another; only the tier
// boundaries move a route.
constexpr float kGreatFactor = 0.90f;        // separated lanes, dedicated cycleways
constexpr float kFineFactor = 1.00f;         // painted lanes, quiet streets, living streets
constexpr float kSharedPathFactor = 1.10f;   // paths shared with pedestrians (still fine)
constexpr float kTertiaryBareFactor = 1.50f; // tertiary through road, no infrastructure
constexpr float kBadFactor = 2.00f;          // primary / secondary / trunk without
                                             // infrastructure; any multi-lane road
// Extra on top of kBadFactor when the bad road is also fast (posted or
// assumed speed above the threshold — rural main roads).
constexpr uint32_t kFastRoadKph = 50;
constexpr float kFastRoadExtra = 0.50f;
// A road tagged bicycle=use_sidepath has a parallel cycleway; riding the
// carriageway anyway is treated like a bad road.
constexpr float kUseSidepathFactor = kBadFactor;
// Road classes at or above this one carry through traffic. Everything
// below (unclassified, residential, service) is a quiet street by default.
constexpr baldr::RoadClass kThroughClassLimit = baldr::RoadClass::kTertiary;

// ── Official bicycle routes ─────────────────────────────────────────────
// Membership in an OSM cycle-route relation (any network level — the graph
// stores one bit). Small, in the spirit of the great tier: tips the balance
// between comparable options, never wins a meaningful detour.
constexpr float kBikeNetworkFactor = 0.92f;

// ── Hills ───────────────────────────────────────────────────────────────
// Upstream's avoid-hills table (kAvoidHillsStrength below) scaled by the
// request's (1 - use_hills) is ADDED to the tier factor, so a flat bad road
// against a hilly fine road stays a trade-off: at ~5 % the fine road still
// wins, at ~8 % the flat bad one does. kHillStrength rescales the whole
// table (1.0 = upstream's values).
constexpr float kHillStrength = 1.0f;

// ── Stairs ──────────────────────────────────────────────────────────────
// Time: pushing / carrying speeds. Cost: those seconds times a factor that
// makes ten metres of stairs uphill worth roughly ten minutes of riding.
// Direction comes from the edge's weighted grade (index 6 = flat); a
// staircase the elevation model cannot resolve counts as the mean of both.
constexpr float kStepsSpeedUpKph = 1.5f;
constexpr float kStepsSpeedDownKph = 2.5f;
constexpr float kStepsFactorUp = 25.0f;
constexpr float kStepsFactorDown = 12.0f;
constexpr uint32_t kFlatGradeIndex = 6;

// ── Pushed bike ─────────────────────────────────────────────────────────
// Walkable-but-not-ridable edges (foot-only ways; streets oneway against
// the travel direction) are used at pushing pace. The factor prices the
// pushed second above a ridden one so a push wins only where the riding
// alternatives are clearly worse — the Bern benchmark's Zieglerstrasse
// crossing (a 30 m push saving a 400 m detour) is the calibration case.
constexpr float kPushSpeedKph = 4.5f;
constexpr float kPushFactor = 1.5f;

// ── Crossings (cost seconds added at the transition) ────────────────────
// Applied when BOTH the road being left and the road being entered are
// through-traffic class. Right turns (with-traffic side) are exempt.
constexpr float kCrossingTurnPenalty = 45.0f;           // left / sharp left / reverse
constexpr float kCrossingStraightSignalPenalty = 30.0f; // straight on, across a signal
// Entering a bad-tier road from a quiet one: a small nudge on top of the
// tier factor, which does the real work.
constexpr float kEnterBadPenalty = 15.0f;

} // namespace kora

// Default options/values
namespace {

// Base transition costs
constexpr float kDefaultAlleyPenalty = 60.0f; // Seconds
constexpr float kDefaultGatePenalty = 300.0f; // Seconds
constexpr float kDefaultBssCost = 120.0f;     // Seconds
constexpr float kDefaultBssPenalty = 0.0f;    // Seconds

// Other options
constexpr float kDefaultUseRoad = 0.25f;          // Factor between 0 and 1 (kora fork: inert)
constexpr float kDefaultAvoidBadSurfaces = 0.25f; // Factor between 0 and 1
constexpr float kDefaultUseLivingStreets = 0.5f;  // Factor between 0 and 1
const std::string kDefaultBicycleType = "hybrid"; // Bicycle type

// Default turn costs - modified by the stop impact.
constexpr float kTCStraight = 0.15f;
constexpr float kTCFavorableSlight = 0.2f;
constexpr float kTCFavorable = 0.3f;
constexpr float kTCFavorableSharp = 0.5f;
constexpr float kTCCrossing = 0.75f;
constexpr float kTCUnfavorableSlight = 0.4f;
constexpr float kTCUnfavorable = 1.0f;
constexpr float kTCUnfavorableSharp = 1.5f;
constexpr float kTCReverse = 5.0f;

// Turn costs based on side of street driving
constexpr float kRightSideTurnCosts[] = {kTCStraight,       kTCFavorableSlight,  kTCFavorable,
                                         kTCFavorableSharp, kTCReverse,          kTCUnfavorableSharp,
                                         kTCUnfavorable,    kTCUnfavorableSlight};
constexpr float kLeftSideTurnCosts[] = {kTCStraight,         kTCUnfavorableSlight, kTCUnfavorable,
                                        kTCUnfavorableSharp, kTCReverse,           kTCFavorableSharp,
                                        kTCFavorable,        kTCFavorableSlight};

// Turn stress penalties for low-stress bike.
constexpr float kTPStraight = 0.0f;
constexpr float kTPFavorableSlight = 0.25f;
constexpr float kTPFavorable = 0.75f;
constexpr float kTPFavorableSharp = 1.0f;
constexpr float kTPUnfavorableSlight = 0.75f;
constexpr float kTPUnfavorable = 1.75f;
constexpr float kTPUnfavorableSharp = 2.25f;
constexpr float kTPReverse = 4.0f;

constexpr float kRightSideTurnPenalties[] = {kTPStraight,    kTPFavorableSlight,
                                             kTPFavorable,   kTPFavorableSharp,
                                             kTPReverse,     kTPUnfavorableSharp,
                                             kTPUnfavorable, kTPUnfavorableSlight};
constexpr float kLeftSideTurnPenalties[] = {kTPStraight,    kTPUnfavorableSlight,
                                            kTPUnfavorable, kTPUnfavorableSharp,
                                            kTPReverse,     kTPFavorableSharp,
                                            kTPFavorable,   kTPFavorableSlight};

// Default cycling speed on smooth, flat roads - based on bicycle type (KPH)
constexpr float kDefaultCyclingSpeed[] = {
    25.0f, // Road bicycle: ~15.5 MPH
    20.0f, // Cross bicycle: ~13 MPH
    18.0f, // Hybrid or "city" bicycle: ~11.5 MPH
    16.0f  // Mountain bicycle: ~10 MPH
};

// Minimum and maximum average bicycling speed (to validate input).
// Maximum is just above the fastest average speed in Tour de France time trial
constexpr float kMinCyclingSpeed = 5.0f;  // KPH
constexpr float kMaxCyclingSpeed = 60.0f; // KPH

// Speed factors based on surface types (defined for each bicycle type).
// These values determine the percentage by which speed us reduced for
// each surface type. (0 values indicate unusable surface types).
constexpr float kRoadSurfaceSpeedFactors[] = {1.0f, 1.0f, 0.9f, 0.6f, 0.5f, 0.3f, 0.2f, 0.0f};
constexpr float kHybridSurfaceSpeedFactors[] = {1.0f, 1.0f, 1.0f, 0.8f, 0.6f, 0.4f, 0.25f, 0.0f};
constexpr float kCrossSurfaceSpeedFactors[] = {1.0f, 1.0f, 1.0f, 0.8f, 0.7f, 0.5f, 0.4f, 0.0f};
constexpr float kMountainSurfaceSpeedFactors[] = {1.0f, 1.0f, 1.0f, 1.0f, 0.9f, 0.75f, 0.55f, 0.0f};

// Worst allowed surface based on bicycle type
constexpr Surface kWorstAllowedSurface[] = {Surface::kCompacted, // Road bicycle
                                            Surface::kGravel,    // Cross
                                            Surface::kDirt,      // Hybrid
                                            Surface::kPath};     // Mountain

constexpr float kSurfaceFactors[] = {1.0f, 2.5f, 4.5f, 7.0f};

// Speed adjustment factors based on weighted grade. Comments here show an
// example of speed changes based on "grade", using a base speed of 18 MPH
// on flat roads
constexpr float kGradeBasedSpeedFactor[] = {
    2.2f,  // -10%  - 39.6
    2.0f,  // -8%   - 36
    1.9f,  // -6.5% - 34.2
    1.7f,  // -5%   - 30.6
    1.4f,  // -3%   - 25
    1.2f,  // -1.5% - 21.6
    1.0f,  // 0%    - 18
    0.95f, // 1.5%  - 17
    0.85f, // 3%    - 15
    0.75f, // 5%    - 13.5
    0.65f, // 6.5%  - 12
    0.55f, // 8%    - 10
    0.5f,  // 10%   - 9
    0.45f, // 11.5% - 8
    0.4f,  // 13%   - 7
    0.3f   // 15%   - 5.5
};

// User propensity to use "hilly" roads. Ranges from a value of 0 (avoid
// hills) to 1 (take hills when they offer a more direct, less time, path).
constexpr float kDefaultUseHills = 0.25f;

// Avoid hills "strength". How much do we want to avoid a hill. Combines
// with the usehills factor (1.0 - usehills = avoidhills factor) to create
// a weighting penalty per weighted grade factor. This indicates how strongly
// edges with the specified grade are weighted. Note that speed also is
// influenced by grade, so these weights help further avoid hills.
constexpr float kAvoidHillsStrength[] = {
    2.0f,  // -10%  - Treacherous descent possible
    1.0f,  // -8%   - Steep downhill
    0.5f,  // -6.5% - Good downhill - where is the bottom?
    0.2f,  // -5%   - Picking up speed!
    0.1f,  // -3%   - Modest downhill
    0.0f,  // -1.5% - Smooth slight downhill, ride this all day!
    0.05f, // 0%    - Flat, no avoidance
    0.1f,  // 1.5%  - These are called "false flat"
    0.3f,  // 3%    - Slight rise
    0.8f,  // 5%    - Small hill
    2.0f,  // 6.5%  - Starting to feel this...
    3.0f,  // 8%    - Moderately steep
    4.5f,  // 10%   - Getting tough
    6.5f,  // 11.5% - Tiring!
    10.0f, // 13%   - Ooof - this hurts
    12.0f  // 15%   - Only for the strongest!
};

// Valid ranges and defaults
constexpr ranged_default_t<float> kUseRoadRange{0.0f, kDefaultUseRoad, 1.0f};
constexpr ranged_default_t<float> kUseHillsRange{0.0f, kDefaultUseHills, 1.0f};
constexpr ranged_default_t<float> kAvoidBadSurfacesRange{0.0f, kDefaultAvoidBadSurfaces, 1.0f};

constexpr ranged_default_t<float> kBSSCostRange{0, kDefaultBssCost, kMaxPenalty};
constexpr ranged_default_t<float> kBSSPenaltyRange{0, kDefaultBssPenalty, kMaxPenalty};

BaseCostingOptionsConfig GetBaseCostOptsConfig() {
  BaseCostingOptionsConfig cfg{};
  // override defaults
  cfg.alley_penalty_.def = kDefaultAlleyPenalty;
  cfg.gate_penalty_.def = kDefaultGatePenalty;
  cfg.disable_toll_booth_ = true;
  cfg.disable_rail_ferry_ = true;
  cfg.use_living_streets_.def = kDefaultUseLivingStreets;
  return cfg;
}

const BaseCostingOptionsConfig kBaseCostOptsConfig = GetBaseCostOptsConfig();

// ── kora fork: tier classification ──────────────────────────────────────

enum class Tier : uint8_t { kGreat, kFine, kSharedPath, kTertiaryBare, kBad };

// Pedestrian-first uses that a bicycle may nevertheless be allowed on.
inline bool is_path_like(Use use) {
  return use == Use::kFootway || use == Use::kPath || use == Use::kPedestrian ||
         use == Use::kSidewalk || use == Use::kMountainBike;
}

// kora fork: pushed-bike — walkable but not ridable in the traversal
// direction. forwardaccess is the traversal direction's mask, so the
// reverse edge of a oneway street (bike stripped, foot kept) lands here
// alongside sidewalks, crossings and pedestrian zones.
inline bool is_pushed(const DirectedEdge* edge) {
  return !(edge->forwardaccess() & kBicycleAccess) &&
         (edge->forwardaccess() & kPedestrianAccess);
}

// Does this edge carry through traffic? Road class decides; cycle
// infrastructure and paths never do, whatever class the graph gave them.
inline bool is_through(baldr::RoadClass rc, Use use) {
  return rc <= kora::kThroughClassLimit && use != Use::kCycleway && !is_path_like(use) &&
         use != Use::kLivingStreet;
}

inline Tier classify(const DirectedEdge* edge) {
  const Use use = edge->use();
  const CycleLane lane = edge->cyclelane();
  if (use == Use::kCycleway) {
    return Tier::kGreat;
  }
  if (is_path_like(use)) {
    // Segregated from pedestrians → as good as a cycleway; shared → fine-ish.
    return (lane == CycleLane::kDedicated || lane == CycleLane::kSeparated) ? Tier::kGreat
                                                                             : Tier::kSharedPath;
  }
  if (use == Use::kLivingStreet || use == Use::kTrack) {
    return Tier::kFine; // tracks: the surface term prices the gravel
  }
  if (edge->use_sidepath()) {
    return Tier::kBad;
  }
  if (lane == CycleLane::kSeparated) {
    return Tier::kGreat;
  }
  // Multi-lane in the direction of travel without physical separation is
  // bad whatever the paint says.
  if (edge->lanecount() > 1) {
    return Tier::kBad;
  }
  const baldr::RoadClass rc = edge->classification();
  if (!is_through(rc, use)) {
    return Tier::kFine; // residential, unclassified, service: the quiet streets
  }
  if (lane == CycleLane::kDedicated || lane == CycleLane::kShared) {
    return Tier::kFine; // painted lane on a through road: on the plateau, not above it
  }
  return rc == baldr::RoadClass::kTertiary ? Tier::kTertiaryBare : Tier::kBad;
}

inline float tier_factor(Tier tier, const DirectedEdge* edge) {
  switch (tier) {
    case Tier::kGreat:
      return kora::kGreatFactor;
    case Tier::kFine:
      return kora::kFineFactor;
    case Tier::kSharedPath:
      return kora::kSharedPathFactor;
    case Tier::kTertiaryBare:
      return kora::kTertiaryBareFactor;
    case Tier::kBad:
    default: {
      float f = edge->use_sidepath() ? kora::kUseSidepathFactor : kora::kBadFactor;
      if (edge->speed() > kora::kFastRoadKph) {
        f += kora::kFastRoadExtra;
      }
      return f;
    }
  }
}

// Turn families relative to the driving side. "Exempt" is the turn that
// stays on the with-traffic kerb (right in right-hand traffic).
inline bool is_exempt_turn(Turn::Type t, bool drive_on_right) {
  if (drive_on_right) {
    return t == Turn::Type::kSlightRight || t == Turn::Type::kRight || t == Turn::Type::kSharpRight;
  }
  return t == Turn::Type::kSlightLeft || t == Turn::Type::kLeft || t == Turn::Type::kSharpLeft;
}
inline bool is_straight_on(Turn::Type t, bool drive_on_right) {
  // A slight deviation towards the exempt side already counts as exempt;
  // towards the other side it is still "straight on" for the signal proxy.
  return t == Turn::Type::kStraight ||
         (drive_on_right ? t == Turn::Type::kSlightLeft : t == Turn::Type::kSlightRight);
}

// The crossing rule, shared by both transition directions.
// from_rc / from_use describe the edge being left, `to` the edge entered.
inline float crossing_penalty(baldr::RoadClass from_rc,
                              Use from_use,
                              const DirectedEdge* to,
                              const NodeInfo* node,
                              Turn::Type turn) {
  float penalty = 0.0f;
  const bool right = node->drive_on_right();
  const bool from_through = is_through(from_rc, from_use);
  const bool to_through = is_through(to->classification(), to->use());
  if (from_through && to_through && !is_exempt_turn(turn, right)) {
    if (is_straight_on(turn, right)) {
      if (node->traffic_signal()) {
        penalty += kora::kCrossingStraightSignalPenalty;
      }
    } else {
      penalty += kora::kCrossingTurnPenalty;
    }
  }
  if (!from_through && classify(to) == Tier::kBad) {
    penalty += kora::kEnterBadPenalty;
  }
  return penalty;
}

} // namespace

/**
 * Derived class providing dynamic edge costing for bicycle routes.
 */
class BicycleCost : public DynamicCost {
public:
  /**
   * Construct bicycle costing. Pass in cost type and costing_options using protocol buffer(pbf).
   * @param  costing specified costing type.
   * @param  costing_options pbf with request costing_options.
   */
  BicycleCost(const Costing& costing_options);

  // virtual destructor
  virtual ~BicycleCost() {
  }

  /**
   * Checks if access is allowed for the provided directed edge.
   * This is generally based on mode of travel and the access modes
   * allowed on the edge. However, it can be extended to exclude access
   * based on other parameters such as conditional restrictions and
   * conditional access that can depend on time and travel mode.
   * @param  edge                        Pointer to a directed edge.
   * @param  is_dest                     Is a directed edge the destination?
   * @param  pred                        Predecessor edge information.
   * @param  tile                        Current tile.
   * @param  edgeid                      GraphId of the directed edge.
   * @param  current_time                Current time (seconds since epoch). A value of 0
   *                                     indicates the route is not time dependent.
   * @param  tz_index                    timezone index for the node
   * @param  destonly_access_restr_mask  Mask containing access restriction types that had a
   * local traffic exemption at the start of the expansion. This mask will be mutated by eliminating
   * flags for locally exempt access restriction types that no longer exist on the passed edge
   *
   * @return Returns true if access is allowed, false if not.
   */
  virtual bool Allowed(const baldr::DirectedEdge* edge,
                       const bool is_dest,
                       const EdgeLabel& pred,
                       const graph_tile_ptr& tile,
                       const baldr::GraphId& edgeid,
                       const uint64_t current_time,
                       const uint32_t tz_index,
                       uint8_t& restriction_idx,
                       uint8_t& destonly_access_restr_mask) const override;

  /**
   * Checks if access is allowed for an edge on the reverse path
   * (from destination towards origin). Both opposing edges (current and
   * predecessor) are provided. The access check is generally based on mode
   * of travel and the access modes allowed on the edge. However, it can be
   * extended to exclude access based on other parameters such as conditional
   * restrictions and conditional access that can depend on time and travel
   * mode.
   * @param  edge                        Pointer to a directed edge.
   * @param  pred                        Predecessor edge information.
   * @param  opp_edge                    Pointer to the opposing directed edge.
   * @param  tile                        Current tile.
   * @param  edgeid                      GraphId of the opposing edge.
   * @param  current_time                Current time (seconds since epoch). A value of 0
   *                                     indicates the route is not time dependent.
   * @param  tz_index                    timezone index for the node
   * @param  destonly_access_restr_mask  Mask containing access restriction types that had a
   * local traffic exemption at the start of the expansion. This mask will be mutated by eliminating
   * flags for locally exempt access restriction types that no longer exist on the passed edge
   * @return  Returns true if access is allowed, false if not.
   */
  virtual bool AllowedReverse(const baldr::DirectedEdge* edge,
                              const EdgeLabel& pred,
                              const baldr::DirectedEdge* opp_edge,
                              const graph_tile_ptr& tile,
                              const baldr::GraphId& opp_edgeid,
                              const uint64_t current_time,
                              const uint32_t tz_index,
                              uint8_t& restriction_idx,
                              uint8_t& destonly_access_restr_mask) const override;

  /**
   * Only transit costings are valid for this method call, hence we throw
   * @param edge
   * @param departure
   * @param curr_time
   * @return
   */
  virtual Cost EdgeCost(const baldr::DirectedEdge*,
                        const baldr::TransitDeparture*,
                        const uint32_t) const override {
    throw std::runtime_error("BicycleCost::EdgeCost does not support transit edges");
  }

  bool IsClosed(const baldr::DirectedEdge*, const graph_tile_ptr&) const override {
    return false;
  }

  /**
   * Get the cost to traverse the specified directed edge. Cost includes
   * the time (seconds) to traverse the edge.
   * @param   edge       Pointer to a directed edge.
   * @param   tile       Current tile.
   * @param   time_info  Time info about edge passing.
   * @return  Returns the cost and time (seconds)
   */
  virtual Cost EdgeCost(const baldr::DirectedEdge* edge,
                        const baldr::GraphId&,
                        const graph_tile_ptr&,
                        const baldr::TimeInfo&,
                        uint8_t&) const override;

  /**
   * Returns the cost to make the transition from the predecessor edge.
   * Defaults to 0. Costing models that wish to include edge transition
   * costs (i.e., intersection/turn costs) must override this method.
   * @param  edge          Directed edge (the to edge)
   * @param  node          Node (intersection) where transition occurs.
   * @param  pred          Predecessor edge information.
   * @param  tile          Pointer to the graph tile containing the to edge.
   * @param  reader_getter Functor that facilitates access to a limited version of the graph reader
   * @return Returns the cost and time (seconds)
   */
  virtual Cost
  TransitionCost(const baldr::DirectedEdge* edge,
                 const baldr::NodeInfo* node,
                 const EdgeLabel& pred,
                 const graph_tile_ptr& tile,
                 const std::function<LimitedGraphReader()>& reader_getter) const override;

  /**
   * Returns the cost to make the transition from the predecessor edge
   * when using a reverse search (from destination towards the origin).
   * @param  idx                Directed edge local index
   * @param  node               Node (intersection) where transition occurs.
   * @param  pred               the opposing current edge in the reverse tree.
   * @param  edge               the opposing predecessor in the reverse tree
   * @param  tile               Graphtile that contains the node and the opp_edge
   * @param  edge_id            Graph ID of opp_pred_edge to get its tile if needed
   * @param  reader_getter      Functor that facilitates access to a limited version of the graph
   * reader
   * @param  has_measured_speed Do we have any of the measured speed types set?
   * @param  internal_turn      Did we make an turn on a short internal edge.
   * @return  Returns the cost and time (seconds)
   */
  virtual Cost TransitionCostReverse(const uint32_t idx,
                                     const baldr::NodeInfo* node,
                                     const baldr::DirectedEdge* pred,
                                     const baldr::DirectedEdge* edge,
                                     const graph_tile_ptr& tile,
                                     const GraphId& pred_id,
                                     const std::function<LimitedGraphReader()>& reader_getter,
                                     const bool /*has_measured_speed*/,
                                     const InternalTurn /*internal_turn*/) const override;

  /**
   * Get the cost factor for A* heuristics. This factor is multiplied
   * with the distance to the destination to produce an estimate of the
   * minimum cost to the destination. The A* heuristic must underestimate the
   * cost to the destination. So a time based estimate based on speed should
   * assume the maximum speed is used to the destination such that the time
   * estimate is less than the least possible time along roads.
   *
   * kora fork: the smallest edge factor the tier model can produce is
   * kGreatFactor * kBikeNetworkFactor (< 1), so the 2x-speed assumption
   * upstream makes (factor 0.5) still underestimates.
   */
  virtual float AStarCostFactor() const override {
    // Assume max speed of 2 * the average speed set for costing
    return kSpeedFactor[static_cast<uint32_t>(2 * speed_)] * min_linear_cost_factor_;
  }

  /**
   * Get the current travel type.
   * @return  Returns the current travel type.
   */
  virtual uint8_t travel_type() const override {
    return static_cast<uint8_t>(type_);
  }

  virtual Cost BSSCost() const override {
    return {kDefaultBssCost, kDefaultBssPenalty};
  };

  // Hidden in source file so we don't need it to be protected
  // We expose it within the source file for testing purposes

  float use_roads_;          // kora fork: parsed for API compatibility, inert
  float avoid_bad_surfaces_; // Preference of avoiding bad surfaces for the bike type
  bool exclude_steps_;       // kora fork: refuse stairs outright (avoid-stairs toggle)

  // Average speed (kph) on smooth, flat roads.
  float speed_;

  // Bicycle type
  BicycleType type_;

  // Minimal surface type that will be penalized for costing
  Surface minimal_surface_penalized_;
  Surface worst_allowed_surface_;

  // Surface speed factors (based on road surface type).
  const float* surface_speed_factor_;

  // Elevation/grade penalty (weighting applied based on the edge's weighted
  // grade (relative value from 0-15)
  float grade_penalty[16];

protected:
  /**
   * Function to be used in location searching which will
   * exclude and allow ranking results from the search by looking at each
   * edges attribution and suitability for use as a location by the travel
   * mode used by the costing method. It's also used to filter
   * edges not usable / inaccessible by bicycle.
   */
  bool Allowed(const baldr::DirectedEdge* edge,
               const graph_tile_ptr& tile,
               uint16_t disallow_mask = kDisallowNone) const override {
    return DynamicCost::Allowed(edge, tile, disallow_mask) && !edge->bss_connection() &&
           edge->use() != Use::kSteps &&
           (avoid_bad_surfaces_ != 1.0f || edge->surface() <= worst_allowed_surface_);
  }
};

// Bicycle route costs are distance based with some favor/avoid based on
// attribution. Speed is derived based on bicycle type or user input and
// is modulated based on surface type and grade factors.

// Constructor
BicycleCost::BicycleCost(const Costing& costing)
    : DynamicCost(costing, TravelMode::kBicycle, kBicycleAccess) {
  const auto& costing_options = costing.options();

  // Set hierarchy to allow unlimited transitions
  for (auto& h : hierarchy_limits_) {
    h.set_max_up_transitions(kUnlimitedTransitions);
  }

  // Get the base costs
  get_base_costs(costing);

  // Get the bicycle type - enter as string and convert to enum
  const std::string& bicycle_type = costing_options.transport_type();
  if (bicycle_type == "cross") {
    type_ = BicycleType::kCross;
  } else if (bicycle_type == "road") {
    type_ = BicycleType::kRoad;
  } else if (bicycle_type == "mountain") {
    type_ = BicycleType::kMountain;
  } else {
    type_ = BicycleType::kHybrid;
  }

  speed_ = costing_options.cycling_speed();
  avoid_bad_surfaces_ = costing_options.avoid_bad_surfaces();
  minimal_surface_penalized_ = kWorstAllowedSurface[static_cast<uint32_t>(type_)];
  worst_allowed_surface_ = avoid_bad_surfaces_ == 1.0f ? minimal_surface_penalized_ : Surface::kPath;

  // Set the surface speed factors for the bicycle type.
  if (type_ == BicycleType::kRoad) {
    surface_speed_factor_ = kRoadSurfaceSpeedFactors;
  } else if (type_ == BicycleType::kHybrid) {
    surface_speed_factor_ = kHybridSurfaceSpeedFactors;
  } else if (type_ == BicycleType::kCross) {
    surface_speed_factor_ = kCrossSurfaceSpeedFactors;
  } else {
    surface_speed_factor_ = kMountainSurfaceSpeedFactors;
  }

  // kora fork: use_roads is kept only so requests that send it stay valid.
  use_roads_ = costing_options.use_roads();
  exclude_steps_ = costing_options.exclude_steps();

  // Populate the grade penalties (based on use_hills factor - value between 0 and 1)
  // kora fork: scaled once more by kHillStrength.
  float use_hills = costing_options.use_hills();
  float avoid_hills = (1.0f - use_hills);
  for (uint32_t i = 0; i <= kMaxGradeFactor; i++) {
    grade_penalty[i] = kora::kHillStrength * avoid_hills * kAvoidHillsStrength[i];
  }

  use_hierarchy_limits = false;
}

// Check if access is allowed on the specified edge.
bool BicycleCost::Allowed(const baldr::DirectedEdge* edge,
                          const bool is_dest,
                          const EdgeLabel& pred,
                          const graph_tile_ptr& tile,
                          const baldr::GraphId& edgeid,
                          const uint64_t current_time,
                          const uint32_t tz_index,
                          uint8_t& restriction_idx,
                          uint8_t& destonly_access_restr_mask) const {
  // Check bicycle access and turn restrictions. Bicycles should obey
  // vehicular turn restrictions. Allow Uturns at dead ends only.
  // Skip impassable edges and shortcut edges.
  // kora fork: an edge that is walkable but not ridable is admitted too —
  // the bike is pushed there (EdgeCost prices it as walking).
  if ((!IsAccessible(edge) && !is_pushed(edge)) || edge->is_shortcut() ||
      (!pred.deadend() && pred.opp_local_idx() == edge->localedgeidx() &&
       pred.mode() == TravelMode::kBicycle) ||
      (!ignore_turn_restrictions_ && (pred.restrictions() & (1 << edge->localedgeidx()))) ||
      IsUserAvoidEdge(edgeid) || CheckExclusions<true>(edge, pred)) {
    return false;
  }

  // Disallow transit connections
  // (except when set for multi-modal routes (FUTURE)
  if (edge->use() == Use::kTransitConnection || edge->use() == Use::kEgressConnection ||
      edge->use() == Use::kPlatformConnection /* && !allow_transit_connections_*/) {
    return false;
  }

  // kora fork: the avoid-stairs toggle.
  if (exclude_steps_ && edge->use() == Use::kSteps) {
    return false;
  }

  // Prohibit certain roads based on surface type and bicycle type.
  // kora fork: not while pushing — on foot any surface is fine.
  if (edge->surface() > worst_allowed_surface_ && !is_pushed(edge)) {
    return false;
  }
  return DynamicCost::EvaluateRestrictions(access_mask_, edge, is_dest, tile, edgeid, current_time,
                                           tz_index, restriction_idx, destonly_access_restr_mask);
}

// Checks if access is allowed for an edge on the reverse path (from
// destination towards origin). Both opposing edges are provided.
bool BicycleCost::AllowedReverse(const baldr::DirectedEdge* edge,
                                 const EdgeLabel& pred,
                                 const baldr::DirectedEdge* opp_edge,
                                 const graph_tile_ptr& tile,
                                 const baldr::GraphId& opp_edgeid,
                                 const uint64_t current_time,
                                 const uint32_t tz_index,
                                 uint8_t& restriction_idx,
                                 uint8_t& destonly_access_restr_mask) const {
  // Check access, U-turn (allow at dead-ends), and simple turn restriction.
  // Do not allow transit connection edges.
  // kora fork: pushed edges admitted, as in Allowed().
  if ((!IsAccessible(opp_edge) && !is_pushed(opp_edge)) || opp_edge->is_shortcut() ||
      opp_edge->use() == Use::kTransitConnection || opp_edge->use() == Use::kEgressConnection ||
      opp_edge->use() == Use::kPlatformConnection ||
      (!pred.deadend() && pred.opp_local_idx() == edge->localedgeidx() &&
       pred.mode() == TravelMode::kBicycle) ||
      (!ignore_turn_restrictions_ && (opp_edge->restrictions() & (1 << pred.opp_local_idx()))) ||
      IsUserAvoidEdge(opp_edgeid) || CheckExclusions<false>(opp_edge, pred)) {
    return false;
  }

  // kora fork: the avoid-stairs toggle.
  if (exclude_steps_ && opp_edge->use() == Use::kSteps) {
    return false;
  }

  // Prohibit certain roads based on surface type and bicycle type.
  // kora fork: not while pushing.
  if (edge->surface() > worst_allowed_surface_ && !is_pushed(opp_edge)) {
    return false;
  }
  return DynamicCost::EvaluateRestrictions(access_mask_, opp_edge, false, tile, opp_edgeid,
                                           current_time, tz_index, restriction_idx,
                                           destonly_access_restr_mask);
}

// Returns the cost to traverse the edge and an estimate of the actual time
// (in seconds) to traverse the edge.
Cost BicycleCost::EdgeCost(const baldr::DirectedEdge* edge,
                           const baldr::GraphId& edgeid,
                           const graph_tile_ptr&,
                           const baldr::TimeInfo&,
                           uint8_t&) const {
  // kora fork: stairs — pushing / carrying time, steep cost, uphill worse.
  if (edge->use() == Use::kSteps) {
    const uint32_t wg = edge->weighted_grade();
    float kph, factor;
    if (wg > kora::kFlatGradeIndex) {
      kph = kora::kStepsSpeedUpKph;
      factor = kora::kStepsFactorUp;
    } else if (wg < kora::kFlatGradeIndex) {
      kph = kora::kStepsSpeedDownKph;
      factor = kora::kStepsFactorDown;
    } else {
      kph = 0.5f * (kora::kStepsSpeedUpKph + kora::kStepsSpeedDownKph);
      factor = 0.5f * (kora::kStepsFactorUp + kora::kStepsFactorDown);
    }
    const float sec = edge->length() * 3.6f / kph;
    return {shortest_ ? edge->length() : sec * factor, sec};
  }

  // Ferries are a special case - they use the ferry speed (stored on the edge)
  if (edge->use() == Use::kFerry) {
    // Compute elapsed time based on speed. Modulate cost with weighting factors.
    assert(edge->speed() < kSpeedFactor.size());
    float sec = (edge->length() * kSpeedFactor[edge->speed()]);
    return {shortest_ ? edge->length() : sec * ferry_factor_, sec};
  }

  // kora fork: pushed bike — walking pace on edges we may not (or, for
  // bicycle=dismount tagging, must not) ride. The tier model does not
  // apply on foot; time at pushing pace times kPushFactor is the whole
  // price.
  if (is_pushed(edge) || edge->dismount()) {
    const float sec = edge->length() * 3.6f / kora::kPushSpeedKph;
    return {shortest_ ? edge->length() : sec * kora::kPushFactor, sec};
  }

  // kora fork: tier factor + official-route bonus + hills + surface.
  float factor = tier_factor(classify(edge), edge);
  if (edge->bike_network()) {
    factor *= kora::kBikeNetworkFactor;
  }
  factor += grade_penalty[edge->weighted_grade()];

  // If surface is worse than the minimum we add a surface factor
  if (edge->surface() >= minimal_surface_penalized_) {
    factor +=
        avoid_bad_surfaces_ * kSurfaceFactors[static_cast<uint32_t>(edge->surface()) -
                                              static_cast<uint32_t>(minimal_surface_penalized_)];
  }

  // Compute bicycle speed based on surface factor and grade (dismount
  // edges returned above via the pushed branch — kora fork). Lower bike
  // speed for rougher surfaces (amount depends on the bicycle type).
  // Weighted grade (relative measure of elevation change along the edge)
  // modulates speed based on elevation changes.
  uint32_t bike_speed = static_cast<uint32_t>(
      (speed_ * surface_speed_factor_[static_cast<uint32_t>(edge->surface())] *
       kGradeBasedSpeedFactor[edge->weighted_grade()]) +
      0.5f);

  factor *= EdgeFactor(edgeid);

  // Compute elapsed time based on speed. Modulate cost with weighting factors.
  float sec = (edge->length() * kSpeedFactor[bike_speed]);
  return {shortest_ ? edge->length() : sec * factor, sec};
}

// Returns the time (in seconds) to make the transition from the predecessor
Cost BicycleCost::TransitionCost(const baldr::DirectedEdge* edge,
                                 const baldr::NodeInfo* node,
                                 const EdgeLabel& pred,
                                 const graph_tile_ptr& /*tile*/,
                                 const std::function<LimitedGraphReader()>& /*reader_getter*/) const {
  // Get the transition cost for country crossing, ferry, gate, toll booth,
  // destination only, alley, maneuver penalty
  uint32_t idx = pred.opp_local_idx();
  Cost c = base_transition_cost(node, edge, &pred, idx);

  // Upstream's turn-time model: stop impact times a turn-type cost gives
  // the seconds, the turn type adds stress on top.
  float seconds = 0.0f;
  float turn_stress = 1.0f;
  const Turn::Type turn = edge->turntype(idx);
  const auto stopimpact = edge->stopimpact(idx);
  if (stopimpact > 0) {
    uint32_t turn_type = static_cast<uint32_t>(turn);
    turn_stress += (node->drive_on_right()) ? kRightSideTurnPenalties[turn_type]
                                            : kLeftSideTurnPenalties[turn_type];

    // Take the higher of the turn degree cost and the crossing cost
    float turn_cost =
        (node->drive_on_right()) ? kRightSideTurnCosts[turn_type] : kLeftSideTurnCosts[turn_type];
    if (turn_cost < kTCCrossing && edge->edge_to_right(idx) && edge->edge_to_left(idx)) {
      turn_cost = kTCCrossing;
    }

    // Transition time = stopimpact * turncost
    seconds += stopimpact * turn_cost;
  }

  // kora fork: the crossing rule.
  const float penalty = crossing_penalty(pred.classification(), pred.use(), edge, node, turn);

  // Return cost (time and penalty)
  c.cost += shortest_ ? 0 : seconds * turn_stress + penalty;
  c.secs += seconds;
  return c;
}

// Returns the cost to make the transition from the predecessor edge
// when using a reverse search (from destination towards the origin).
// pred is the opposing current edge in the reverse tree
// edge is the opposing predecessor in the reverse tree
Cost BicycleCost::TransitionCostReverse(const uint32_t idx,
                                        const baldr::NodeInfo* node,
                                        const baldr::DirectedEdge* pred,
                                        const baldr::DirectedEdge* edge,
                                        const graph_tile_ptr& /*tile*/,
                                        const GraphId& /*pred_id*/,
                                        const std::function<LimitedGraphReader()>& /*reader_getter*/,
                                        const bool /*has_measured_speed*/,
                                        const InternalTurn /*internal_turn*/) const {

  // Bicycles should be able to make uturns on short internal edges; therefore, InternalTurn
  // is ignored for now.

  // Get the transition cost for country crossing, ferry, gate, toll booth,
  // destination only, alley, maneuver penalty
  Cost c = base_transition_cost(node, edge, pred, idx);

  float seconds = 0.0f;
  float turn_stress = 1.0f;
  const Turn::Type turn = edge->turntype(idx);
  const auto stopimpact = edge->stopimpact(idx);
  if (stopimpact > 0) {
    uint32_t turn_type = static_cast<uint32_t>(turn);
    turn_stress += (node->drive_on_right()) ? kRightSideTurnPenalties[turn_type]
                                            : kLeftSideTurnPenalties[turn_type];

    // Take the higher of the turn degree cost and the crossing cost
    float turn_cost =
        (node->drive_on_right()) ? kRightSideTurnCosts[turn_type] : kLeftSideTurnCosts[turn_type];
    if (turn_cost < kTCCrossing && edge->edge_to_right(idx) && edge->edge_to_left(idx)) {
      turn_cost = kTCCrossing;
    }

    // Transition time = stopimpact * turncost
    seconds += stopimpact * turn_cost;
  }

  // kora fork: the crossing rule (pred is the edge being left here too).
  const float penalty = crossing_penalty(pred->classification(), pred->use(), edge, node, turn);

  // Return cost (time and penalty)
  c.cost += shortest_ ? 0.f : seconds * turn_stress + penalty;
  c.secs += seconds;
  return c;
}

void ParseBicycleCostOptions(const rapidjson::Document& doc,
                             const std::string& costing_options_key,
                             Costing* c,
                             google::protobuf::RepeatedPtrField<CodedDescription>& warnings) {
  c->set_type(Costing::bicycle);
  c->set_name(Costing_Enum_Name(c->type()));
  auto* co = c->mutable_options();

  rapidjson::Value dummy;
  const auto& json = rapidjson::get_child(doc, costing_options_key.c_str(), dummy);

  ParseBaseCostOptions(json, c, kBaseCostOptsConfig, warnings);
  JSON_PBF_RANGED_DEFAULT(co, kUseRoadRange, json, "/use_roads", use_roads, warnings);
  JSON_PBF_RANGED_DEFAULT(co, kUseHillsRange, json, "/use_hills", use_hills, warnings);
  JSON_PBF_RANGED_DEFAULT(co, kAvoidBadSurfacesRange, json, "/avoid_bad_surfaces", avoid_bad_surfaces,
                          warnings);
  JSON_PBF_DEFAULT(co, kDefaultBicycleType, json, "/bicycle_type", transport_type);
  // kora fork: avoid-stairs toggle.
  JSON_PBF_DEFAULT_V2(co, false, json, "/exclude_steps", exclude_steps);

  // convert string to enum, set ranges and defaults based on enum
  BicycleType type;
  std::transform(co->mutable_transport_type()->begin(), co->mutable_transport_type()->end(),
                 co->mutable_transport_type()->begin(),
                 [](const unsigned char ch) { return std::tolower(ch); });
  if (co->transport_type() == "cross") {
    type = BicycleType::kCross;
  } else if (co->transport_type() == "road") {
    type = BicycleType::kRoad;
  } else if (co->transport_type() == "mountain") {
    type = BicycleType::kMountain;
  } else {
    type = BicycleType::kHybrid;
  }

  // This is the average speed on smooth, flat roads. If not present or outside the
  // valid range use a default speed based on the bicycle type.
  const auto t = static_cast<uint32_t>(type);
  ranged_default_t<float> kCycleSpeedRange{kMinCyclingSpeed, kDefaultCyclingSpeed[t],
                                           kMaxCyclingSpeed};

  JSON_PBF_RANGED_DEFAULT(co, kCycleSpeedRange, json, "/cycling_speed", cycling_speed, warnings);
  JSON_PBF_RANGED_DEFAULT(co, kBSSCostRange, json, "/bss_return_cost", bike_share_cost, warnings);
  JSON_PBF_RANGED_DEFAULT(co, kBSSPenaltyRange, json, "/bss_return_penalty", bike_share_penalty,
                          warnings);
}

cost_ptr_t CreateBicycleCost(const Costing& costing_options) {
  return std::make_shared<BicycleCost>(costing_options);
}

} // namespace sif
} // namespace valhalla

/**********************************************************************************************/

#ifdef INLINE_TEST

using namespace valhalla;
using namespace sif;

namespace {

class TestBicycleCost : public BicycleCost {
public:
  TestBicycleCost(const Costing& costing_options) : BicycleCost(costing_options){};

  using BicycleCost::alley_penalty_;
  using BicycleCost::country_crossing_cost_;
  using BicycleCost::destination_only_penalty_;
  using BicycleCost::ferry_transition_cost_;
  using BicycleCost::gate_cost_;
  using BicycleCost::maneuver_penalty_;
  using BicycleCost::service_penalty_;
};

TestBicycleCost* make_bicyclecost_from_json(const std::string& property, float testVal) {
  std::stringstream ss;
  ss << R"({"costing": "bicycle", "costing_options":{"bicycle":{")" << property << R"(":)" << testVal
     << "}}}";
  Api request;
  ParseApi(ss.str(), valhalla::Options::route, request);
  return new TestBicycleCost(request.options().costings().find(Costing::bicycle)->second);
}

std::uniform_real_distribution<float>*
make_distributor_from_range(const ranged_default_t<float>& range) {
  float rangeLength = range.max - range.min;
  return new std::uniform_real_distribution<float>(range.min - rangeLength, range.max + rangeLength);
}

TEST(BicycleCost, testBicycleCostParams) {
  constexpr unsigned testIterations = 250;
  constexpr unsigned seed = 0;
  std::mt19937 generator(seed);
  std::shared_ptr<std::uniform_real_distribution<float>> distributor;
  std::shared_ptr<TestBicycleCost> ctorTester;

  const auto& defaults = kBaseCostOptsConfig;

  // maneuver_penalty_
  distributor.reset(make_distributor_from_range(defaults.maneuver_penalty_));
  for (unsigned i = 0; i < testIterations; ++i) {
    ctorTester.reset(make_bicyclecost_from_json("maneuver_penalty", (*distributor)(generator)));
    EXPECT_THAT(ctorTester->maneuver_penalty_,
                test::IsBetween(ctorTester->maneuver_penalty_, defaults.maneuver_penalty_.max));
  }

  // alley_penalty_
  distributor.reset(make_distributor_from_range(defaults.alley_penalty_));
  for (unsigned i = 0; i < testIterations; ++i) {
    ctorTester.reset(make_bicyclecost_from_json("alley_penalty", (*distributor)(generator)));
    EXPECT_THAT(ctorTester->alley_penalty_,
                test::IsBetween(defaults.alley_penalty_.min, defaults.alley_penalty_.max));
  }

  // service_penalty_
  distributor.reset(make_distributor_from_range(defaults.service_penalty_));
  for (unsigned i = 0; i < testIterations; ++i) {
    ctorTester.reset(make_bicyclecost_from_json("service_penalty", (*distributor)(generator)));
    EXPECT_THAT(ctorTester->service_penalty_,
                test::IsBetween(defaults.service_penalty_.min, defaults.service_penalty_.max));
  }

  // destination_only_penalty_
  distributor.reset(make_distributor_from_range(defaults.dest_only_penalty_));
  for (unsigned i = 0; i < testIterations; ++i) {
    ctorTester.reset(
        make_bicyclecost_from_json("destination_only_penalty", (*distributor)(generator)));
    EXPECT_THAT(ctorTester->destination_only_penalty_,
                test::IsBetween(defaults.dest_only_penalty_.min, defaults.dest_only_penalty_.max));
  }

  // gate_cost_ (Cost.secs)
  distributor.reset(make_distributor_from_range(defaults.gate_cost_));
  for (unsigned i = 0; i < testIterations; ++i) {
    ctorTester.reset(make_bicyclecost_from_json("gate_cost", (*distributor)(generator)));
    EXPECT_THAT(ctorTester->gate_cost_.secs,
                test::IsBetween(defaults.gate_cost_.min, defaults.gate_cost_.max));
  }

  // gate_penalty_ (Cost.cost)
  distributor.reset(make_distributor_from_range(defaults.gate_penalty_));
  for (unsigned i = 0; i < testIterations; ++i) {
    ctorTester.reset(make_bicyclecost_from_json("gate_penalty", (*distributor)(generator)));
    EXPECT_THAT(ctorTester->gate_cost_.cost,
                test::IsBetween(defaults.gate_penalty_.min, defaults.gate_penalty_.max));
  }

  // country_crossing_cost_ (Cost.secs)
  distributor.reset(make_distributor_from_range(defaults.country_crossing_cost_));
  for (unsigned i = 0; i < testIterations; ++i) {
    ctorTester.reset(make_bicyclecost_from_json("country_crossing_cost", (*distributor)(generator)));
    EXPECT_THAT(ctorTester->country_crossing_cost_.secs,
                test::IsBetween(defaults.country_crossing_cost_.min,
                                defaults.country_crossing_cost_.max));
  }

  // country_crossing_penalty_ (Cost.cost)
  distributor.reset(make_distributor_from_range(defaults.country_crossing_penalty_));
  for (unsigned i = 0; i < testIterations; ++i) {
    ctorTester.reset(
        make_bicyclecost_from_json("country_crossing_penalty", (*distributor)(generator)));
    EXPECT_THAT(ctorTester->country_crossing_cost_.cost,
                test::IsBetween(defaults.country_crossing_penalty_.min,
                                defaults.country_crossing_penalty_.max +
                                    defaults.country_crossing_cost_.def));
  }

  // ferry_cost_ (Cost.secs)
  distributor.reset(make_distributor_from_range(defaults.ferry_cost_));
  for (unsigned i = 0; i < testIterations; ++i) {
    ctorTester.reset(make_bicyclecost_from_json("ferry_cost", (*distributor)(generator)));
    EXPECT_THAT(ctorTester->ferry_transition_cost_.secs,
                test::IsBetween(defaults.ferry_cost_.min, defaults.ferry_cost_.max));
  }

  // use_roads_
  distributor.reset(make_distributor_from_range(kUseRoadRange));
  for (unsigned i = 0; i < testIterations; ++i) {
    ctorTester.reset(make_bicyclecost_from_json("use_roads", (*distributor)(generator)));
    EXPECT_THAT(ctorTester->use_roads_, test::IsBetween(kUseRoadRange.min, kUseRoadRange.max));
  }

  // speed_
  constexpr ranged_default_t<float> kRoadCyclingSpeedRange{kMinCyclingSpeed, kDefaultCyclingSpeed[0],
                                                           kMaxCyclingSpeed};
  distributor.reset(make_distributor_from_range(kRoadCyclingSpeedRange));
  for (unsigned i = 0; i < testIterations; ++i) {
    ctorTester.reset(make_bicyclecost_from_json("cycling_speed", (*distributor)(generator)));
    EXPECT_THAT(ctorTester->speed_,
                test::IsBetween(kRoadCyclingSpeedRange.min, kRoadCyclingSpeedRange.max));
  }
}
} // namespace

#endif
