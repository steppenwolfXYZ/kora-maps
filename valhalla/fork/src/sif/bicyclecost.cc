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
// Model in one paragraph: an edge's cost is its honest riding time — a
// rider-power model (the request's flat speed sets the rider's sustained
// watts; every grade's speed follows from that power, an everyday rider
// halves around 3 % climb; e-bike types add a motor with an assist cap)
// prices hills, so altitude avoids itself — multiplied by a quality factor:
// great (separated infrastructure, slight bonus), fine (painted lanes,
// quiet streets: the plateau, ≈ 1.0, shorter/faster wins) or, for bare
// through roads, a speed-limit-driven factor (30 km/h free … 80 km/h
// heavy); grades on through roads are capped against DEM artifacts.
// Official cycle routes earn a small bonus; destination-only carries no
// penalty; ferries and car shuttles ride at their service speed plus a
// boarding wait, with on-board kilometres priced high so crossings win
// only against disproportionate detours. Walkable-but-not-ridable edges
// are pushed: honest grade-aware walking time, a per-section allowance,
// per-metre penalties beyond, sac_scale/surface guards (T2 heavy, T3+
// and impassable rock refused). Stairs cost hauling time plus committing
// fees at 2 m / 4 m; `exclude_steps` removes them. Transitions keep
// upstream's turn-time model and add: a flat per-turn cost, a cost-only
// deviation penalty for leaving the intuitive continuation (same-class
// roughly-straight, else near-class exactly-straight), and the crossing
// rule — turning between through roads at a real multi-lane crossing
// (4+ through arms) costs per lane; T-junctions and roundabouts are
// exempt. The fork's triplegbuilder overlay reports pushed sections as
// pedestrian-mode maneuvers so the client can draw them dotted.
//
// Request options: everything upstream accepts still parses. `use_roads`
// is accepted for compatibility but inert — the tier model replaces what
// it used to scale. New: `exclude_steps` (bool, default false);
// `route_character` (`road` / `fast` / `balanced` / `relaxed` / `quiet`
// — the fast ↔ nice ruler of bicycle-route-options.md, one bundle of
// per-stop numbers in the kora Route character block); `bicycle_type`
// additionally accepts `ebike` / `sbike` (motor-assisted rider models,
// capped at 25 / 45 km/h). Without `route_character` the older scalars
// `avoidance_scale` / `bonus_scale` (floats, default 1) and
// `surface_profile` (`fast` / `balanced` / `leisure`) still apply.

#include "sif/bicyclecost.h"
#include <cmath>
#include "baldr/directededge.h"
#include "baldr/graphconstants.h"
#include "baldr/nodeinfo.h"
#include "baldr/rapidjson_utils.h"
#include "baldr/turn.h"
#include "proto_conversions.h"
#include "sif/costconstants.h"
#include "sif/hierarchylimits.h"

#include <algorithm>
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
// The fast ↔ nice ruler (bicycle-route-options.md § 4) scales these per
// request through the Route character block below: `avoidance`
// multiplies the EXCESS over 1 of every traffic penalty (bare-road speed
// curve, painted-lane / sharrow factors, the extra-lane step, and all
// crossing seconds — base, per lane, signal, T-junction share, the
// enter-bad nudge); `great_scale` the DISCOUNT of the great tier; the
// official-route factor and the quiet boost are set directly. 1 / 1 is
// this file's tuning; 0 / 0 is the Road stop (traffic ignored, cycle
// paths earn nothing). Never scaled: hills, deviation, pushing, stairs,
// service roads, ferries, alpine guards, and the use_sidepath factor (a
// signed cycle path is legally mandatory). The old request scalars
// `avoidance_scale` / `bonus_scale` still drive avoidance / great_scale
// when no route_character is sent.
constexpr float kAvoidanceScaleMax = 5.0f;
constexpr float kBonusScaleMax = 3.0f; // keeps great × route ≥ 0.5 (A* bound)
constexpr float kGreatFactor = 0.90f;      // separated lanes, dedicated cycleways
constexpr float kFineFactor = 1.00f;       // painted lanes, quiet streets, living streets
constexpr float kSharedPathFactor = 1.10f; // paths shared with pedestrians (still fine)
// A through road WITHOUT bike infrastructure is priced by its speed, not
// its road class: Swiss city roads are never extremely dangerous for
// bikes. 30 km/h zones carry no penalty at all whatever the class, 50 a
// noticeable penalty, 60 more, 80 the full bad-road factor. Piecewise
// linear between the points over the POSTED limit (posted_speed below):
// the directed edge's own speed() is NOT the limit in our tiles — the
// tile build's default-speeds table replaces it with a density-inferred
// travel speed (23 km/h on an urban primary), which read every city
// through road as a 30 zone and silently switched this tier off.
constexpr float kBareSpeedPoints[][2] = {
    {30.0f, 1.00f},
    {50.0f, 1.40f},
    {60.0f, 1.60f},
    {80.0f, 2.20f},
};
// A through road WITH paint at kPaintSpeedKph or faster sits slightly
// below the plateau, not on it: a painted lane a touch worse than a quiet
// street, a sharrow (cycleway=shared_lane — a pictogram in the car lane,
// no lane of one's own) clearly worse but still better than nothing.
// Below that speed paint stays on the plateau. Faster roads scale both
// along the bare curve (factor × bare(speed) / bare(kPaintSpeedKph)).
// Previously every painted or shared lane sat on the plateau, which let
// the five-lane Laupenstrasse with its sharrow tie with the Mühlematt
// quiet-street corridor (bern-eichmatt-aarbergergasse).
constexpr uint32_t kPaintSpeedKph = 50;
constexpr float kPaintedLaneFactor = 1.05f;
constexpr float kSharrowFactor = 1.15f;
// Every lane per direction beyond the first adds this to a painted or
// bare through road's factor: a multi-lane street with paint is not a
// quiet street with paint. Bus lanes never count — the OSM preprocessing
// subtracts bus/PSV lanes from the lane tags before the tile build.
constexpr float kExtraLaneStep = 0.20f;
// Service roads (bus-only links, depot and parking aisles, driveways):
// ridable, a small per-metre surcharge so the search does not wander
// through a depot by accident, never enough to cost a route a 25 m
// link. Deliberately no flat entry fee — upstream's 15 s service_penalty
// (zeroed in GetBaseCostOptsConfig) was inherited unnoticed and made a
// short bus-only link decide a Bern city ride.
constexpr float kServiceRoadFactor = 1.20f;
// Posted limit assumed for a through road without a maxspeed tag: the
// Swiss in-town default. Outside towns the tiles' inferred speed is
// higher than this and wins (see posted_speed).
constexpr uint32_t kUnpostedThroughSpeedKph = 50;

// ── Turn restrictions ───────────────────────────────────────────────────
// A bike can always dismount, so an OSM turn restriction never forbids a
// bicycle movement — it can only force a push around the corner, which
// is what a wrongly mapped no_right_turn produced in Heimberg (17 m of
// sidewalk to dodge a sign that applies to nobody). The fork therefore
// obeys a (via-node) turn restriction only where the maneuver actually
// crosses traffic that matters: never for with-traffic turns, and
// otherwise only when a road posted above kQuietStreetMaxKph meets at
// the junction (paths, cycleways and 30-zone streets do not count;
// untagged quiet streets are assumed 30). This covers simple (all-mode)
// restrictions and complex via-node ones alike — a restriction with an
// exception (except=psv, the Heimberg case) is stored as complex. The
// base class evaluates complex restrictions in a non-virtual method, so
// the constructor switches that off and Allowed() re-does the via-node
// case itself; via-way restrictions (multi-edge, in practice U-turn bans
// on dual carriageways) are thereby ignored for bikes altogether.
constexpr uint32_t kQuietStreetMaxKph = 30;
// A road tagged bicycle=use_sidepath has a parallel cycleway; riding the
// carriageway anyway is priced like a fast bare road regardless of speed.
constexpr float kUseSidepathFactor = 2.20f;
// Road classes at or above this one carry through traffic. Everything
// below (unclassified, residential, service) is a quiet street by default.
constexpr baldr::RoadClass kThroughClassLimit = baldr::RoadClass::kTertiary;

// ── Official bicycle routes ─────────────────────────────────────────────
// Membership in an OSM cycle-route relation (any network level — the graph
// stores one bit). Small, in the spirit of the great tier: tips the balance
// between comparable options, never wins a meaningful detour.
constexpr float kBikeNetworkFactor = 0.92f;

// ── Hills: rider power, not a speed table ────────────────────────────────
// The primary hill mechanism is honest time. The request's flat speed
// (`cycling_speed`) describes the RIDER: it fixes the watts they sustain
// on the flat against rolling resistance and air drag, and every grade's
// riding speed then follows from that same power against gravity —
// a professional climbs proportionally faster than a leisurely rider,
// not just on the flat. Calibration (bicycle-route-options.md § Speed
// model): at 20 km/h flat (≈ 90 W) the speed halves around a 3 % climb
// and is at walking pace near 10 % — the everyday-rider curve the old
// hand-written table encoded at 18 km/h. Descents are capped by city
// braking, not physics. The per-grade table is computed once per
// request in the constructor (ride_speed_kph_).
//
// E-bike types (`ebike` / `sbike`) use the same model: the rider at
// Normal effort plus a motor, and the motor cuts out at the type's assist
// cap. Above the cap only rider and gravity act — downhill both types run
// faster than the cap by themselves; on the flat they sit exactly at it
// (the motor fills up to the cap, the rider alone cannot exceed it). The
// basic e-bike's motor is moderate, so climbs slow it noticeably
// (mid-teens km/h on 6 %); the fast e-bike's is strong enough to hold ~45
// on gentle climbs and stay in the 30s on 6 %. Watts are tuning numbers.
constexpr float kRiderBikeMassKg = 95.0f; // rider + bike + bag
constexpr float kRollingResistance = 0.008f;
constexpr float kDragAreaM2 = 0.5f; // CdA, upright posture
constexpr float kAirDensityKgM3 = 1.2f;
constexpr float kGravityMS2 = 9.81f;
// Braking cap on descents: at least this, and a little above the rider's
// own flat speed for faster riders / the fast e-bike.
constexpr float kDescentCapMinKph = 32.0f;
constexpr float kDescentCapFlatFactor = 1.15f;
// Rider effort assumed on an e-bike (the pace ruler does not apply).
constexpr float kEbikeRiderFlatKph = 20.0f;
struct MotorProfile {
  float motor_w;  // motor power at the wheel
  float cap_kph;  // assist cut-off
};
constexpr MotorProfile kEbikeMotor{200.0f, 25.0f};
constexpr MotorProfile kSbikeMotor{600.0f, 45.0f};
// Upstream's 16 grade buckets, in percent.
constexpr float kGradePct[] = {-10.0f, -8.0f, -6.5f, -5.0f, -3.0f, -1.5f, 0.0f,  1.5f,
                               3.0f,   5.0f,  6.5f,  8.0f,  10.0f, 11.5f, 13.0f, 15.0f};
// Extra discomfort ONLY in pushing territory (≥ ~10 % up) and on
// treacherous descents — everything below that is priced by time alone.
// Scaled by (1 - use_hills) like upstream's table; kHillStrength rescales
// the whole thing.
constexpr float kSteepDiscomfort[] = {
    0.30f, // -10%  fast descent needs constant braking
    0.15f, // -8%
    0.0f,  // -6.5%
    0.0f,  // -5%
    0.0f,  // -3%
    0.0f,  // -1.5%
    0.0f,  // 0%
    0.0f,  // 1.5%
    0.0f,  // 3%
    0.0f,  // 5%
    0.0f,  // 6.5%
    0.0f,  // 8%
    0.40f, // 10%   most everyday riders push from here
    0.80f, // 11.5%
    1.50f, // 13%
    2.20f  // 15%
};
constexpr float kHillStrength = 1.0f;
// ── Grade cap on through roads (elevation-artifact fallback) ────────────
// The DEM samples the structures a road passes UNDER (rail overpasses,
// bridges), baking fake 10-15 % spikes into underpasses — the canonical
// case is Schwarzenburgstrasse under the rail line at Weissenstein, where
// a level ride reads as a mountain. Engineered through roads are never
// genuinely that steep in a city, so their grade index is capped at
// 6.5 %; small streets keep their full grades (steep lanes are real).
// This is the interim guard — the correct fix (endpoint-interpolated
// elevation for layer<0 ways at graph build) is queued and needs a tile
// rebuild. Known cost: sustained alpine climbs on primary roads read a
// touch too fast until then.
constexpr uint32_t kThroughGradeCapIndex = 10; // bucket 10 = 6.5 %

// ── Stairs ──────────────────────────────────────────────────────────────
// Two honest components, both mostly TIME so displayed durations stay
// truthful:
//   1. Hauling pace: carrying a bike over steps is slow — a per-metre
//      time rate, uphill far worse than down.
//   2. Committing fees at length checkpoints: below 2 m a stair is
//      trivial (lift the bike over, no fee); from 2 m the carry has to
//      be figured out (fee one), from 4 m it is real hauling (fee two).
//      Each fee counts once as time and once more as cost-only penalty;
//      downward fees are half the upward ones.
// Fees are per edge: back-to-back fragments of a real staircase each
// ≥ 2 m still sum to about the right total, and steps ways are rarely
// fragmented — a sub-2 m fragment of a longer flight dodging its fee is
// the accepted imprecision (stateless costing cannot track sections).
// Direction comes from the edge's weighted grade (index 6 = flat); a
// staircase the elevation model cannot resolve — most stubs — counts as
// the mean of up and down.
constexpr float kStairsSecPerMUp = 13.0f;
constexpr float kStairsSecPerMDown = 7.0f;
constexpr float kStairsFeeThreshold1M = 2.0f;
constexpr float kStairsFeeThreshold2M = 4.0f;
constexpr float kStairsFeeUpSec = 20.0f;   // per checkpoint: as time AND as cost
constexpr float kStairsFeeDownSec = 10.0f; // per checkpoint: as time AND as cost
constexpr uint32_t kFlatGradeIndex = 6;

// ── Pushed bike ─────────────────────────────────────────────────────────
// Walkable-but-not-ridable edges (foot-only ways; streets oneway against
// the travel direction) are used at pushing pace. Short pushes are a
// genuinely worthwhile option and cost nothing beyond their honest time:
// the first kPushFreeMeters are penalty-free. Beyond that, every pushed
// metre accrues kPushPenaltySecPerM of cost, so LONG pushes are what
// gets discouraged — on a climb, riding is barely faster than pushing,
// and without this the router cut corners over any footpath (canonical:
// Spiezwiler's 100 m Sportplatz walkway instead of staying on Stutz,
// where a 20 m link exists). The allowance is per edge, not per push
// section — the forward transition cannot see whether the predecessor
// was pushed — so a section chopped into short edges collects a little
// extra slack; calibrate kPushPenaltySecPerM with that in mind.
// ── Alpine terrain (sac_scale) ──────────────────────────────────────────
// The international OSM hiking scale, baked per edge. T1 is fine —
// ridable, even. T2 carries a very hard cost factor (riding or pushing
// a bike on a mountain-hiking path is a last resort). T3 and above are
// refused outright: no bike belongs there, pushed or not — the Gabi
// Klettersteig (T5, bridge-tagged, grade data flattened by upstream's
// bridge clamp) is the canonical case grade limits provably cannot
// catch. Impassable SURFACE (the graph attribute Valhalla derives from
// OSM surface + smoothness, keeping the worst) additionally refuses
// pushing — the pushed branch waives the surface bar, which is right
// for gravel and wrong for rock.
constexpr float kSacT2CostFactor = 4.0f;

constexpr float kPushSpeedKph = 4.5f;
// Pushing uphill is slower AND deserves extra discouragement — shoving
// a bike up a slope is the worst part of any route. The speed factor
// scales the pushing pace by the edge grade (walking slows less than
// riding, so this is gentler than the everyday curve; downhill pushing
// stays near flat pace — braking a pushed bike is easy). On top, every
// pushed metre at a climbing grade (≥ ~3 %) pays the uphill surcharge.
// Caveat: edges shorter than the ~30 m elevation raster read as flat,
// so tiny stubs escape both — the fixed costs are what prices those.
constexpr float kPushGradeSpeedFactor[] = {
    1.00f, // -10%
    1.00f, // -8%
    1.00f, // -6.5%
    1.00f, // -5%
    1.00f, // -3%
    1.00f, // -1.5%
    1.00f, // 0%
    0.90f, // 1.5%
    0.80f, // 3%
    0.70f, // 5%
    0.62f, // 6.5%
    0.55f, // 8%
    0.50f, // 10%
    0.45f, // 11.5%
    0.42f, // 13%
    0.40f  // 15%
};
constexpr uint32_t kPushUphillGradeIndex = 8; // bucket 8 = 3%
constexpr float kPushUphillExtraSecPerM = 0.6f;
// The allowance is per push SECTION (contiguous pushed edges,
// uninterrupted by riding), not per edge: EdgeCost charges the penalty
// on every pushed metre, and the transition into the section's first
// edge grants the allowance back as a rebate. That closes the
// confetti loophole — Spiezwiler's 100 m walkway is a dozen 2-9 m
// fragments (several of them the synthetic station-walk welds), and a
// per-edge allowance made almost all of it free. The rebate is capped
// at the first edge's own penalty, so a transition+edge relaxation
// never goes below honest time (the search needs non-negative
// relaxations); a section whose first fragment is shorter than the
// allowance loses the remainder — a few seconds, acceptable.
constexpr float kPushFreeMeters = 20.0f;
// Sized so a long push effectively prices near 2.5 km/h on the flat
// (time at 4.5 km/h is 0.8 s/m, the penalty adds 0.6). Together with the
// turn and deviation penalties this outprices the 100 m Sportplatz
// walkway cut at Spiezwiler against the normal road.
constexpr float kPushPenaltySecPerM = 0.6f;

// ── Ferries & car shuttles ──────────────────────────────────────────────
// Water ferries and rail ferries (car-shuttle trains — Lötschberg,
// Furka, Vereina; bicycle=yes in OSM) get the same treatment: on-board
// time from the edge's own speed (derived from the OSM duration tag)
// plus a flat expected wait at boarding, plus a cost factor that keeps a
// ferry from ever beating riding ALONG the shore — it should win only
// where it genuinely crosses. Upstream instead left the rail-ferry
// preference unparsed for bicycles, which decayed to "maximally avoid":
// a 6 h boarding penalty plus pedaling the shuttle's 17 km at the fake
// alpine grade the DEM gives a way through a mountain — the Lötschberg
// shuttle priced worse than climbing Grimsel.
constexpr float kFerryWaitSec = 1800.0f; // expected boarding wait, both kinds
// Per-second cost multiplier on board. Deliberately high: every km on
// board must be bought by saving several km of riding, so a short hop
// across a lake (or the roadless Lötschberg) wins while a long cruise —
// or the through-shuttle to Iselle, with the Simplon pass above it —
// loses unless the land alternative is disproportionately worse. The
// client additionally shows a ferry-free variant whenever a crossing
// wins, so the sporting choice stays with the rider.
constexpr float kFerryFactor = 8.0f;

// ── Turns ───────────────────────────────────────────────────────────────
// Every real direction change costs a few seconds of time AND cost:
// tight turns force braking, and a route with many turns is harder to
// navigate — zigzag mazes through quiet grids must not tie with a
// straight corridor of equal length. Indexed by Turn::Type (straight,
// slight-right, right, sharp-right, reverse, sharp-left, left,
// slight-left); left turns cost more than right — they cross traffic
// (right-hand driving; the map's area is CH). Roundabout circulation is
// exempt, as with the crossing rule. Amplified by upstream's
// turn-stress multiplier like the stop-impact seconds.
constexpr float kTurnSecByType[] = {
    0.0f,  // straight
    1.5f,  // slight right
    3.0f,  // right
    5.0f,  // sharp right
    8.0f,  // reverse
    6.0f,  // sharp left
    4.0f,  // left — on narrow streets left and right differ little; the
           // crossing rule prices the real difference at big junctions
    2.0f   // slight left
};

// ── Deviation from the intuitive continuation ───────────────────────────
// Cost-only (no time): leaving a road that visibly goes on adds
// navigation load even when the turn itself is gentle. The intuitive
// continuation is found by road category and geometry, two stages: any
// edge of the SAME class as the road we are on going roughly straight
// (straight or slight); failing that, any edge within one class going
// exactly straight. Take something else while such a continuation
// exists → the penalty. No candidate (T-junctions, road ends, forks
// resolved by neither stage) → no penalty; roundabouts exempt; a
// continuation we cannot legally use (oneway against us) does not
// count — our turn is then the forced choice, not a deviation.
constexpr float kDeviationPenaltySec = 3.0f;

// ── Route character: the fast ↔ nice ruler's per-stop bundle ────────────
// The client's five ruler stops (bicycle-route-options.md § 4) each
// select one row here; everything a stop changes lives in this table.
//   avoidance     scales the EXCESS over 1 of every traffic penalty
//                 (bare-road speed curve, painted-lane / sharrow
//                 factors, extra-lane step, all crossing seconds)
//   great_scale   scales the DISCOUNT of the great tier (separated
//                 cycle infrastructure)
//   route_bonus   the factor an edge on an official cycle route gets
//                 (any network level — the graph stores one bit)
//   quiet_boost   factor for the "away from traffic" edges: narrow
//                 unclassified roads (no second lane in this direction —
//                 88 % of CH unclassified roads carry no lane or width
//                 tag at all, and the few tagged lanes=2 are real roads),
//                 tracks, and bike-allowed paths / footways. ≥ 1 = off.
//                 At the calm stops it equals the great tier: on a
//                 cycle tour a gravel lane is as good as a cycle path
//                 beside a road, which the graph cannot tell apart from
//                 a cycle path through a park.
//   surface       the surface speed / surcharge tables (next block)
//   surface_relief cost-only compensation of the surface slowdown:
//                 this share of the extra riding time a rough surface
//                 costs is forgiven in the COST (the displayed time
//                 stays honest). 0.5 = half, 0.8 = most of it.
//   route_turn    share of the turn penalty (upstream's stop-impact
//                 seconds with their stress multiplier, plus the flat
//                 per-turn seconds) charged when the turn is INTO an
//                 official cycle route edge — a signed route's own
//                 corners must not price it out of following it (the
//                 zigzag rule is for grids, not for a lane in the
//                 forest). 1 = full, 0 = free; the deviation cost stays.
// Balanced is this file's own tuning; Road ignores traffic and earns
// nothing from infrastructure.
struct CharacterProfile {
  float avoidance;
  float great_scale;
  float route_bonus;
  float quiet_boost;
  const char* surface;
  float surface_relief;
  float route_turn;
};
constexpr CharacterProfile kCharacterRoad{0.0f, 0.0f, 1.00f, 1.00f, "fast", 0.0f, 1.0f};
constexpr CharacterProfile kCharacterFast{0.5f, 0.5f, 0.96f, 1.00f, "fast", 0.0f, 1.0f};
constexpr CharacterProfile kCharacterBalanced{1.0f, 1.0f, 0.92f, 0.95f, "balanced", 0.0f, 1.0f};
constexpr CharacterProfile kCharacterRelaxed{1.5f, 1.4f, 0.86f, 0.86f, "leisure", 0.5f, 0.25f};
constexpr CharacterProfile kCharacterQuiet{2.2f, 2.0f, 0.74f, 0.80f, "leisure", 0.8f, 0.0f};

// ── Turns scale with speed ──────────────────────────────────────────────
// The flat per-turn seconds (kTurnSecByType) are sized for a 25 km/h
// rider. Braking into a tight corner and getting back up to speed costs
// more the faster one rides (the physics: ~1.5 s at 20 km/h, ~3 s at
// 25, ~6.5 s at 30 with a normal effort out of the corner), so they
// scale with the rider's flat speed (time AND cost — the displayed
// duration carries it). Piecewise linear over these points: leisurely
// 0.5, normal 0.75, fast / e-bike 1.0, professional and fast e-bike
// 1.25 — the S-Pedelec has the power to get back up to speed quickly.
// Falls out of the pace ruler and the e-bike types without a knob of
// its own.
constexpr float kTurnScalePoints[][2] = {
    {15.0f, 0.50f},
    {20.0f, 0.75f},
    {25.0f, 1.00f},
    {30.0f, 1.25f},
    {45.0f, 1.25f},
};

// ── Fast e-bike on roads up to 50 km/h ──────────────────────────────────
// At 45 km/h the S-Pedelec rides WITH the traffic of a 50 zone, so a
// bare 50 road is barely worse than a quiet street and paint on it is
// the plateau: its own bare-road curve (posted 50 → 1.1 instead of 1.4)
// and a paint threshold of 60 instead of kPaintSpeedKph. Faster roads
// price as for everyone.
constexpr float kSbikeBareSpeedPoints[][2] = {
    {30.0f, 1.00f},
    {50.0f, 1.10f},
    {60.0f, 1.60f},
    {80.0f, 2.20f},
};
constexpr uint32_t kSbikePaintSpeedKph = 60;

// ── Surfaces: profiles per route character ──────────────────────────────
// Upstream prices surfaces per bicycle type twice: a speed factor per
// surface (real time) and a cost surcharge for surfaces at or worse
// than the type's "penalized from" level (avoid_bad_surfaces × a table
// per step beyond it). Its hybrid numbers treat gravel like a road
// bike would (0.4× speed AND +0.63 cost), which chased routes off a
// gravel national cycle route (Veloland 8's Aareweg between Kiesen and
// Thun). The request's `surface_profile` — the client maps its fast ↔
// nice ruler onto it (bicycle-route-options.md § 4) — picks one of
// three tables for the hybrid-based types (bicycle, ebike, sbike):
//   fast      everyday commuting: upstream's hybrid speeds, surcharge
//             from dirt (upstream's table) — speed matters, rough
//             ground is worth avoiding.
//   balanced  hybrid speeds, a milder surcharge from dirt.
//   leisure   cycle-tour mode: gravel and dirt are normal ground —
//             gentler speeds, surcharge on path only.
// The road type (racing bicycle) keeps upstream's tables at every
// stop — surface avoidance is that type's identity; cross / mountain
// are untouched too (not offered by the client).
// Surface order: paved_smooth, paved, paved_rough, compacted, dirt,
// gravel, path, impassable (index 0..7).
struct SurfaceProfile {
  float speed[8];       // speed factor per surface
  Surface penalize_from; // first surface that carries a surcharge
  float surcharge[4];    // avoid_bad_surfaces × this, per step from penalize_from
};
constexpr SurfaceProfile kSurfaceFast{{1.0f, 1.0f, 1.0f, 0.8f, 0.6f, 0.4f, 0.25f, 0.0f},
                                      Surface::kDirt,
                                      {2.5f, 4.5f, 7.0f, 7.0f}};
constexpr SurfaceProfile kSurfaceBalanced{{1.0f, 1.0f, 1.0f, 0.8f, 0.6f, 0.4f, 0.25f, 0.0f},
                                          Surface::kDirt,
                                          {1.5f, 2.0f, 4.5f, 4.5f}};
constexpr SurfaceProfile kSurfaceLeisure{{1.0f, 1.0f, 1.0f, 0.9f, 0.8f, 0.7f, 0.4f, 0.0f},
                                         Surface::kPath,
                                         {4.5f, 4.5f, 4.5f, 4.5f}};

// ── Crossings (cost seconds added at the transition) ────────────────────
// Applied when the road being ENTERED is through-traffic class and the
// junction is a real crossing. The junction decides, never the road being
// left: a cyclist arriving from a quiet street, a cycle track beside the
// main road or a footway crosses the same carriageway as one arriving
// on the main road (canonical: Tscharnerstrasse → Eigerplatz, and the
// Effingerstrasse cycle track → Seilerstrasse — both free under the
// original both-roads-through rule). The penalty scales with the widest
// through arm: a small base for a single-lane crossing plus a strong
// step per additional lane in one direction — crossing a one-lane road
// is routine, every further lane is what makes a crossing genuinely
// hostile. (Lane counts come from the tiles; the OSM preprocessing
// subtracts bus lanes before the tile build, since a bus lane does not
// make a crossing harder.) The turn direction scales it: turning across
// (left) pays in full, straight on from a side arm half (the graph
// cannot tell whether the carriageway is crossed), turning with traffic
// (right) a quarter — reduced, not exempt: a big junction is unpleasant
// whichever way you turn. Four or more through arms are a crossing; a
// T-junction (three) is free when its widest arm has one lane per
// direction (canonical: Simmentalstrasse → Frutigenstrasse in
// Spiezwiler) and pays kCrossingTeeShare when it has two or more.
// Roundabouts are exempt — a Kreisel is the safe way across a big road,
// not a crossing to avoid. Straight on ALONG a through road is not a
// crossing of it; there a traffic signal at the node is the proxy for
// "two big roads cross here". A junction the costing cannot inspect
// (tile boundary) charges nothing.
constexpr float kCrossingTurnBaseSec = 8.0f;
constexpr float kCrossingTurnPerLaneSec = 12.0f;
constexpr float kCrossingStraightSignalPenalty = 30.0f; // straight on, across a signal
constexpr float kCrossingRightShare = 0.25f;        // turning with traffic
constexpr float kCrossingSideStraightShare = 0.5f;  // straight on from a side arm
constexpr float kCrossingTeeShare = 0.75f;          // multi-lane T-junction
// Entering a genuinely fast bare road (≥ this speed, no bike
// infrastructure) from a quiet street: a small nudge on top of the
// speed factor, which does the real work.
constexpr uint32_t kEnterBadSpeedKph = 60;
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
const std::string kDefaultSurfaceProfile = "fast"; // kora fork: surface profile
const std::string kDefaultRouteCharacter = "";    // kora fork: empty = use the scalars

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

// User propensity to use "hilly" roads. Ranges from a value of 0 (avoid
// hills) to 1 (take hills when they offer a more direct, less time, path).
constexpr float kDefaultUseHills = 0.25f;

// Valid ranges and defaults
constexpr ranged_default_t<float> kUseRoadRange{0.0f, kDefaultUseRoad, 1.0f};
constexpr ranged_default_t<float> kUseHillsRange{0.0f, kDefaultUseHills, 1.0f};
constexpr ranged_default_t<float> kAvoidBadSurfacesRange{0.0f, kDefaultAvoidBadSurfaces, 1.0f};
// kora fork: the fast ↔ nice ruler scales (see the kora block).
constexpr ranged_default_t<float> kAvoidanceScaleRange{0.0f, 1.0f, kora::kAvoidanceScaleMax};
constexpr ranged_default_t<float> kBonusScaleRange{0.0f, 1.0f, kora::kBonusScaleMax};

constexpr ranged_default_t<float> kBSSCostRange{0, kDefaultBssCost, kMaxPenalty};
constexpr ranged_default_t<float> kBSSPenaltyRange{0, kDefaultBssPenalty, kMaxPenalty};

BaseCostingOptionsConfig GetBaseCostOptsConfig() {
  BaseCostingOptionsConfig cfg{};
  // override defaults
  cfg.alley_penalty_.def = kDefaultAlleyPenalty;
  cfg.gate_penalty_.def = kDefaultGatePenalty;
  // kora fork: no destination-only penalty for bicycles. The graph bakes
  // motor_vehicle=destination in as destination_only, and the base
  // costing's 600 s default made every such street cost like a ~3 km
  // detour — bikes fled exactly the quiet quarters the fine tier wants
  // (the Bern benchmark's Mühlematt quarter is the canonical case).
  // motor_vehicle=destination does not restrict bicycles at all. A
  // request can still send destination_only_penalty explicitly.
  cfg.dest_only_penalty_.def = 0.0f;
  // kora fork: no flat service-road entry fee either — service roads are
  // priced per metre by kServiceRoadFactor. A request can still send
  // service_penalty explicitly.
  cfg.service_penalty_.def = 0.0f;
  cfg.disable_toll_booth_ = true;
  cfg.disable_rail_ferry_ = true;
  cfg.use_living_streets_.def = kDefaultUseLivingStreets;
  return cfg;
}

const BaseCostingOptionsConfig kBaseCostOptsConfig = GetBaseCostOptsConfig();

// ── kora fork: tier classification ──────────────────────────────────────

enum class Tier : uint8_t { kGreat, kFine, kSharedPath, kPaintedLane, kSharrow, kService, kBad };

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

// kora fork: uses that continue a push section (the forward search's
// EdgeLabel exposes only the predecessor's Use, not its access mask, so
// section starts are detected by use-type: a foot-type predecessor means
// the push is already running). A RIDDEN bicycle=yes footway before a
// push misreads as continuation and costs the section its allowance —
// a few seconds, accepted; the reverse search detects starts exactly.
inline bool is_foot_use(Use use) {
  return is_path_like(use) || use == Use::kSteps || use == Use::kPedestrianCrossing ||
         use == Use::kPlatform;
}

// Does this edge carry through traffic? Road class decides; cycle
// infrastructure and paths never do, whatever class the graph gave them.
inline bool is_through(baldr::RoadClass rc, Use use) {
  return rc <= kora::kThroughClassLimit && use != Use::kCycleway && !is_path_like(use) &&
         use != Use::kLivingStreet;
}

// kora fork: the road's posted limit, from EdgeInfo (the directed edge's
// speed() is the tile build's inferred travel speed, useless as a danger
// signal — see kBareSpeedPoints). Untagged or unlimited → the in-town
// default, unless the inferred speed says the road is faster (rural).
inline uint32_t posted_speed(const graph_tile_ptr& tile, const DirectedEdge* edge) {
  uint32_t limit = 0;
  if (tile != nullptr) {
    limit = tile->edgeinfo(edge).speed_limit();
  }
  if (limit == 0 || limit == baldr::kUnlimitedSpeedLimit) {
    return std::max(kora::kUnpostedThroughSpeedKph, edge->speed());
  }
  return limit;
}

inline Tier classify(const graph_tile_ptr& tile, const DirectedEdge* edge, uint32_t paint_kph) {
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
  if (use == Use::kServiceRoad || use == Use::kDriveway || use == Use::kParkingAisle) {
    return Tier::kService; // small per-metre surcharge, no entry fee
  }
  if (edge->use_sidepath()) {
    return Tier::kBad;
  }
  if (lane == CycleLane::kSeparated) {
    return Tier::kGreat;
  }
  if (!is_through(edge->classification(), use)) {
    return Tier::kFine; // residential, unclassified: the quiet streets
  }
  // Paint on a through road: on the plateau in a 30 zone, slightly below
  // it from kPaintSpeedKph (tier_factor adds the lane steps).
  if (lane == CycleLane::kDedicated || lane == CycleLane::kShared) {
    if (posted_speed(tile, edge) < paint_kph) {
      return Tier::kFine;
    }
    return lane == CycleLane::kDedicated ? Tier::kPaintedLane : Tier::kSharrow;
  }
  return Tier::kBad; // bare through road — priced by speed in tier_factor
}

// kora fork: a factor's excess over 1, scaled by the avoidance ruler.
inline float scaled_excess(float factor, float scale) {
  return 1.0f + (factor - 1.0f) * scale;
}

// kora fork: rider-power model (see the kora Hills block). Power a rider
// sustains to hold `flat_kph` on level ground.
inline float rider_power_w(float flat_kph) {
  const float v = flat_kph / 3.6f;
  const float rolling = kora::kRollingResistance * kora::kRiderBikeMassKg * kora::kGravityMS2;
  const float drag = 0.5f * kora::kAirDensityKgM3 * kora::kDragAreaM2 * v * v;
  return (rolling + drag) * v;
}

// Steady speed (km/h) at which `power_w` balances rolling resistance,
// gravity at `grade_pct` and air drag. f(v) = a·v + c·v³ with a negative
// on descents, so f is not monotone near zero — but f(0) = 0 < P and
// f(hi) > P, and the crossing above f's minimum is unique: bisection.
inline float speed_at_grade_kph(float power_w, float grade_pct) {
  const float g = grade_pct / 100.0f;
  const float sin_theta = g / std::sqrt(1.0f + g * g);
  const float a = kora::kRiderBikeMassKg * kora::kGravityMS2 * (kora::kRollingResistance + sin_theta);
  const float c = 0.5f * kora::kAirDensityKgM3 * kora::kDragAreaM2;
  float lo = 0.0f, hi = 40.0f; // m/s — beyond any braking cap
  for (int i = 0; i < 50; ++i) {
    const float mid = 0.5f * (lo + hi);
    const float f = a * mid + c * mid * mid * mid;
    if (f < power_w) {
      lo = mid;
    } else {
      hi = mid;
    }
  }
  return 0.5f * (lo + hi) * 3.6f;
}

// kora fork: everything the tier factor needs from the request — the
// route character's numbers plus the bike type's road curve (see the
// kora Route character / Fast e-bike blocks).
struct TierWeights {
  float avoidance = 1.0f;
  float great_scale = 1.0f;
  float quiet_boost = 1.0f; // ≥ 1: off
  const float (*bare)[2] = kora::kBareSpeedPoints;
  size_t bare_n = sizeof(kora::kBareSpeedPoints) / sizeof(kora::kBareSpeedPoints[0]);
  uint32_t paint_kph = kora::kPaintSpeedKph;
};

// The speed curve for through roads without bike infrastructure.
inline float bare_speed_factor(uint32_t speed_kph, const TierWeights& w) {
  const auto* pts = w.bare;
  const size_t n = w.bare_n;
  const float s = static_cast<float>(speed_kph);
  if (s <= pts[0][0]) {
    return pts[0][1];
  }
  for (size_t i = 1; i < n; ++i) {
    if (s <= pts[i][0]) {
      const float f = (s - pts[i - 1][0]) / (pts[i][0] - pts[i - 1][0]);
      return pts[i - 1][1] + f * (pts[i][1] - pts[i - 1][1]);
    }
  }
  return pts[n - 1][1];
}

// kora fork: the grade bucket the costing responds to — through roads are
// capped (see kThroughGradeCapIndex) because their extreme grades are
// DEM artifacts from structures passing overhead, not real climbs.
inline uint32_t effective_grade(const DirectedEdge* edge) {
  const uint32_t wg = edge->weighted_grade();
  if (wg > kora::kThroughGradeCapIndex && is_through(edge->classification(), edge->use())) {
    return kora::kThroughGradeCapIndex;
  }
  return wg;
}

// kora fork: lanes per direction beyond the first, as a factor step.
inline float extra_lane_step(const DirectedEdge* edge) {
  const uint32_t lanes = std::max(1u, edge->lanecount());
  return kora::kExtraLaneStep * static_cast<float>(lanes - 1);
}

// kora fork: the "away from traffic" edges the quiet boost applies to
// (kora Route character block): narrow unclassified roads and tracks.
// Bike-allowed paths qualify through their tier (kSharedPath).
inline bool quiet_road(const DirectedEdge* edge) {
  const Use use = edge->use();
  if (use == Use::kTrack) {
    return true;
  }
  return use == Use::kRoad && edge->classification() == baldr::RoadClass::kUnclassified &&
         edge->lanecount() < 2 && !edge->use_sidepath();
}

// `w` carries the request's ruler numbers (see the kora tiers and Route
// character blocks): traffic penalties scale by their excess over 1, the
// great tier by its discount, quiet roads / tracks / shared paths drop to
// the quiet boost when one is set; use_sidepath is deliberately unscaled.
inline float tier_factor(const graph_tile_ptr& tile,
                         Tier tier,
                         const DirectedEdge* edge,
                         const TierWeights& w) {
  switch (tier) {
    case Tier::kGreat:
      return 1.0f - (1.0f - kora::kGreatFactor) * w.great_scale;
    case Tier::kFine:
      return (w.quiet_boost < 1.0f && quiet_road(edge)) ? std::min(kora::kFineFactor, w.quiet_boost)
                                                        : kora::kFineFactor;
    case Tier::kSharedPath:
      return w.quiet_boost < 1.0f ? std::min(kora::kSharedPathFactor, w.quiet_boost)
                                  : kora::kSharedPathFactor;
    case Tier::kService:
      return kora::kServiceRoadFactor;
    case Tier::kPaintedLane:
    case Tier::kSharrow: {
      const float base = tier == Tier::kPaintedLane ? kora::kPaintedLaneFactor : kora::kSharrowFactor;
      const float speed_scale =
          bare_speed_factor(posted_speed(tile, edge), w) / bare_speed_factor(w.paint_kph, w);
      return scaled_excess(base * speed_scale + extra_lane_step(edge), w.avoidance);
    }
    case Tier::kBad:
    default:
      return edge->use_sidepath()
                 ? kora::kUseSidepathFactor
                 : scaled_excess(bare_speed_factor(posted_speed(tile, edge), w) + extra_lane_step(edge),
                                 w.avoidance);
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

// kora fork: is the taken edge a deviation from the intuitive
// continuation? (See kDeviationPenaltySec.) `idx` is the opposing
// predecessor's local index at the node — the key the per-pair turn
// types are stored under; `pred_class` the class of the road arrived
// on; `taken` the edge entered.
inline bool is_deviation(const graph_tile_ptr& tile,
                         const NodeInfo* node,
                         const uint32_t idx,
                         const baldr::RoadClass pred_class,
                         const DirectedEdge* taken) {
  if (tile == nullptr || taken->roundabout()) {
    return false;
  }
  // The node's edges are only readable when this tile really owns the
  // node — at tile boundaries and hierarchy transitions the tile handed
  // to the costing can be another one, and indexing into it runs out of
  // bounds (found the hard way: 500s at exactly those junctions). Guard
  // by bounds AND by the taken edge lying inside the node's edge range;
  // when either fails, we cannot see the junction — no penalty.
  const uint32_t ei = node->edge_index();
  const uint32_t ec = node->edge_count();
  if (ei + ec > tile->header()->directededgecount()) {
    return false;
  }
  const DirectedEdge* first = tile->directededge(ei);
  if (taken < first || taken >= first + ec) {
    return false;
  }
  bool a_other = false, a_taken = false; // same class, roughly straight
  bool b_other = false, b_taken = false; // class ±1, exactly straight
  const DirectedEdge* e = first;
  for (uint32_t i = 0; i < ec; ++i, ++e) {
    if (e->is_shortcut() ||
        !(e->forwardaccess() & (kBicycleAccess | kPedestrianAccess))) {
      continue;
    }
    const Turn::Type tt = e->turntype(idx);
    const int dc = static_cast<int>(e->classification()) - static_cast<int>(pred_class);
    const bool rough =
        tt == Turn::Type::kStraight || tt == Turn::Type::kSlightRight || tt == Turn::Type::kSlightLeft;
    if (dc == 0 && rough) {
      (e == taken ? a_taken : a_other) = true;
    }
    if (dc >= -1 && dc <= 1 && tt == Turn::Type::kStraight) {
      (e == taken ? b_taken : b_other) = true;
    }
  }
  // Stage A decides when it has any candidate; stage B only otherwise.
  if (a_other || a_taken) {
    return !a_taken;
  }
  if (b_other || b_taken) {
    return !b_taken;
  }
  return false;
}

// kora fork: junction shape for the crossing rule — how many
// through-class arms meet at this node, and whether any of them is
// multi-lane. Same ownership guards as is_deviation: when the node's
// edges are not readable from this tile, `valid` stays false and the
// crossing rule charges nothing.
struct JunctionArms {
  bool valid = false;
  uint32_t through_arms = 0;
  uint32_t max_lanes = 1; // widest through arm, lanes in its direction
};

inline JunctionArms junction_arms(const graph_tile_ptr& tile,
                                  const NodeInfo* node,
                                  const DirectedEdge* taken) {
  JunctionArms j;
  if (tile == nullptr) {
    return j;
  }
  const uint32_t ei = node->edge_index();
  const uint32_t ec = node->edge_count();
  if (ei + ec > tile->header()->directededgecount()) {
    return j;
  }
  const DirectedEdge* first = tile->directededge(ei);
  if (taken < first || taken >= first + ec) {
    return j;
  }
  j.valid = true;
  const DirectedEdge* e = first;
  for (uint32_t i = 0; i < ec; ++i, ++e) {
    if (e->is_shortcut()) {
      continue;
    }
    if (is_through(e->classification(), e->use())) {
      ++j.through_arms;
      j.max_lanes = std::max(j.max_lanes, e->lanecount());
    }
  }
  return j;
}

// kora fork: the posted limit of a junction arm for the turn-restriction
// test — untagged arms count as the in-town default only when they are
// through roads; a quiet street without a sign is a 30 zone.
inline uint32_t crossed_arm_speed(const graph_tile_ptr& tile, const DirectedEdge* arm) {
  const uint32_t limit = tile->edgeinfo(arm).speed_limit();
  if (limit == 0 || limit == baldr::kUnlimitedSpeedLimit) {
    return is_through(arm->classification(), arm->use()) ? kora::kUnpostedThroughSpeedKph
                                                          : kora::kQuietStreetMaxKph;
  }
  return limit;
}

// kora fork: the node an outgoing directed edge of `tile` leaves from,
// found by its position in the tile's edge array (nodes are stored in
// edge-index order). Level-safe where a predecessor label is not: at a
// hierarchy transition the search expands the node's counterpart on
// another level with the same label, so pred.endnode() then names a node
// of a different tile. Null when the edge is not in this tile.
inline const NodeInfo* node_of_edge(const graph_tile_ptr& tile, const DirectedEdge* edge) {
  const uint32_t ecount = tile->header()->directededgecount();
  if (ecount == 0) {
    return nullptr;
  }
  const DirectedEdge* first = tile->directededge(0);
  if (edge < first || edge >= first + ecount) {
    return nullptr;
  }
  const uint32_t idx = static_cast<uint32_t>(edge - first);
  uint32_t lo = 0, hi = tile->header()->nodecount();
  while (lo < hi) {
    const uint32_t mid = lo + (hi - lo) / 2;
    const NodeInfo* n = tile->node(mid);
    if (idx < n->edge_index()) {
      hi = mid;
    } else if (idx >= n->edge_index() + n->edge_count()) {
      lo = mid + 1;
    } else {
      return n;
    }
  }
  return nullptr;
}

// kora fork: does a turn restriction on this maneuver matter for a bike?
// (See kQuietStreetMaxKph.) `from_arm` / `to_arm` are the outgoing edges
// at the node that represent the road left and the road entered; for a
// left or U-turn every road at the node counts (oncoming traffic is
// crossed either way), straight on only the intersecting ones.
inline bool turn_restriction_matters(const graph_tile_ptr& tile,
                                     const NodeInfo* node,
                                     const DirectedEdge* from_arm,
                                     const DirectedEdge* to_arm,
                                     Turn::Type turn) {
  const bool right = node->drive_on_right();
  if (is_exempt_turn(turn, right)) {
    return false; // with traffic: crosses nothing
  }
  const bool straight = is_straight_on(turn, right);
  const uint32_t ei = node->edge_index();
  const uint32_t ec = node->edge_count();
  if (ei + ec > tile->header()->directededgecount()) {
    return true; // cannot inspect: obey
  }
  const DirectedEdge* e = tile->directededge(ei);
  for (uint32_t i = 0; i < ec; ++i, ++e) {
    if (e->is_shortcut() || (straight && (e == from_arm || e == to_arm))) {
      continue;
    }
    const Use use = e->use();
    if (use == Use::kCycleway || is_path_like(use) || use == Use::kSteps ||
        use == Use::kPedestrianCrossing || use == Use::kLivingStreet) {
      continue;
    }
    if (crossed_arm_speed(tile, e) > kora::kQuietStreetMaxKph) {
      return true;
    }
  }
  return false;
}

// kora fork: is a complex VIA-NODE restriction between the predecessor
// and the keyed edge active — by the base class's own rules (forward:
// key = the edge entered, match on `from`; reverse: key = the reverse
// tree's outgoing edge, match on `to`; timed restrictions count only on
// timed queries; probable ones per restriction_probability)? Via-way
// restrictions are skipped (see kQuietStreetMaxKph).
inline bool via_node_restriction_active(const graph_tile_ptr& tile,
                                        const bool forward,
                                        const baldr::GraphId& key,
                                        const baldr::GraphId& pred_edgeid,
                                        const uint32_t access_mask,
                                        const uint8_t restriction_probability,
                                        const uint64_t current_time,
                                        const uint32_t tz_index) {
  for (const auto& cr : tile->GetComplexRestrictions(forward, key, access_mask)) {
    if (cr.via_count() != 0) {
      continue;
    }
    if ((cr.type() == baldr::RestrictionType::kNoProbable ||
         cr.type() == baldr::RestrictionType::kOnlyProbable) &&
        (restriction_probability == 0 || restriction_probability > cr.probability())) {
      continue;
    }
    const baldr::GraphId other = forward ? cr.from_graphid() : cr.to_graphid();
    if (other != pred_edgeid) {
      continue;
    }
    if (cr.has_dt()) {
      if (!current_time ||
          !baldr::DateTime::is_conditional_active(cr.dt_type(), cr.begin_hrs(), cr.begin_mins(),
                                                  cr.end_hrs(), cr.end_mins(), cr.dow(),
                                                  cr.begin_week(), cr.begin_month(),
                                                  cr.begin_day_dow(), cr.end_week(), cr.end_month(),
                                                  cr.end_day_dow(), current_time,
                                                  baldr::DateTime::get_tz_db().from_index(tz_index))) {
        continue;
      }
    }
    return true;
  }
  return false;
}

// The crossing rule, shared by both transition directions.
// from_rc / from_use describe the edge being left, `to` the edge entered.
inline float crossing_penalty(baldr::RoadClass from_rc,
                              Use from_use,
                              const DirectedEdge* to,
                              const NodeInfo* node,
                              Turn::Type turn,
                              const graph_tile_ptr& tile,
                              const TierWeights& w) {
  float penalty = 0.0f;
  // Roundabouts are the safe way across a big road — never a crossing to
  // penalize (entering / circulating; the exit is an exempt right turn).
  if (to->roundabout()) {
    return penalty;
  }
  const bool right = node->drive_on_right();
  const bool from_through = is_through(from_rc, from_use);
  const bool to_through = is_through(to->classification(), to->use());
  if (to_through) {
    // Share of the crossing penalty by turn direction (see the kora
    // block): across in full, with traffic a quarter, straight on from
    // a side arm half. Straight on ALONG a through road is not a
    // crossing of it — there the signal proxy applies instead.
    float share = 1.0f;
    if (is_exempt_turn(turn, right)) {
      share = kora::kCrossingRightShare;
    } else if (is_straight_on(turn, right)) {
      if (from_through) {
        share = 0.0f;
        if (node->traffic_signal()) {
          penalty += kora::kCrossingStraightSignalPenalty;
        }
      } else {
        share = kora::kCrossingSideStraightShare;
      }
    }
    if (share > 0.0f) {
      // The junction decides, whatever we arrived on: 4+ through arms
      // are a crossing; a multi-lane T-junction a reduced one; base
      // rate plus a step per lane beyond the first on the widest arm.
      const JunctionArms j = junction_arms(tile, node, to);
      if (j.valid) {
        float junction_share = 0.0f;
        if (j.through_arms >= 4) {
          junction_share = 1.0f;
        } else if (j.through_arms == 3 && j.max_lanes >= 2) {
          junction_share = kora::kCrossingTeeShare;
        }
        penalty += share * junction_share *
                   (kora::kCrossingTurnBaseSec +
                    kora::kCrossingTurnPerLaneSec * static_cast<float>(j.max_lanes - 1));
      }
    }
  }
  if (!from_through && classify(tile, to, w.paint_kph) == Tier::kBad && !to->use_sidepath() &&
      posted_speed(tile, to) >= kora::kEnterBadSpeedKph) {
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
    // kora fork: the fastest the bike ever rides is the descent cap, and
    // the smallest edge factor is the great tier times the official-route
    // bonus at the request's bonus scale (kBonusScaleMax keeps that
    // ≥ 0.5); every other term only adds. Seconds per metre at the cap
    // times that floor underestimates every ridden, pushed or ferried
    // metre.
    // The quiet boost never goes below the great tier, and the surface
    // relief never forgives more than the surface's own extra time, so
    // cost per metre stays ≥ flat-speed time × great × route bonus.
    const float great = 1.0f - (1.0f - kora::kGreatFactor) * tw_.great_scale;
    const float min_factor = std::min(great, tw_.quiet_boost) * route_bonus_;
    return (3.6f / max_ride_speed_kph_) * min_factor * min_linear_cost_factor_;
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
  bool request_ignores_turns_; // kora fork: the request asked to ignore turn restrictions
  TierWeights tw_;             // kora fork: the ruler's tier numbers + the type's road curve
  float route_bonus_;          // kora fork: factor for official-cycle-route edges
  float surface_relief_;       // kora fork: share of the surface slowdown forgiven in cost
  float turn_scale_;           // kora fork: flat per-turn seconds scaled by flat speed
  float route_turn_;           // kora fork: share of the turn penalty into a cycle-route edge

  // Average speed (kph) on smooth, flat roads.
  float speed_;
  // kora fork: riding speed per grade bucket from the rider-power model
  // (motor and caps applied), and its maximum (the descent cap) for A*.
  float ride_speed_kph_[16];
  float max_ride_speed_kph_;

  // Bicycle type
  BicycleType type_;

  // Minimal surface type that will be penalized for costing
  Surface minimal_surface_penalized_;
  Surface worst_allowed_surface_;

  // Surface speed factors (based on road surface type).
  const float* surface_speed_factor_;
  // kora fork: cost surcharge table per step from minimal_surface_penalized_
  // (upstream's kSurfaceFactors, or the request's surface profile's).
  const float* surface_cost_factor_;

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
           edge->sac_scale() < SacScale::kDemandingMountainHiking &&
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
  // kora fork: `ebike` / `sbike` are hybrid bikes with a motor profile —
  // the upstream enum (and the trip leg's travel type) stays untouched.
  const std::string& bicycle_type = costing_options.transport_type();
  const kora::MotorProfile* motor = nullptr;
  if (bicycle_type == "cross") {
    type_ = BicycleType::kCross;
  } else if (bicycle_type == "road") {
    type_ = BicycleType::kRoad;
  } else if (bicycle_type == "mountain") {
    type_ = BicycleType::kMountain;
  } else if (bicycle_type == "ebike") {
    type_ = BicycleType::kHybrid;
    motor = &kora::kEbikeMotor;
  } else if (bicycle_type == "sbike") {
    type_ = BicycleType::kHybrid;
    motor = &kora::kSbikeMotor;
  } else {
    type_ = BicycleType::kHybrid;
  }

  // kora fork: the rider-power speed table (kora Hills block). For an
  // e-bike the request's cycling_speed is ignored — its flat speed is the
  // assist cap, and the rider pedals at Normal effort.
  const float rider_flat_kph = motor ? kora::kEbikeRiderFlatKph : costing_options.cycling_speed();
  const float rider_w = rider_power_w(rider_flat_kph);
  speed_ = motor ? motor->cap_kph : rider_flat_kph;
  const float descent_cap = std::max(kora::kDescentCapMinKph, speed_ * kora::kDescentCapFlatFactor);
  max_ride_speed_kph_ = 0.0f;
  for (uint32_t i = 0; i <= kMaxGradeFactor; i++) {
    float v = speed_at_grade_kph(rider_w, kora::kGradePct[i]);
    if (motor) {
      const float assisted = speed_at_grade_kph(rider_w + motor->motor_w, kora::kGradePct[i]);
      // Below the cap the motor sets the pace; at the cap it cuts out and
      // the rider alone continues — never slower than the cap it reached.
      v = assisted <= motor->cap_kph ? assisted : std::max(motor->cap_kph, v);
    }
    v = std::min(v, descent_cap);
    // Riding can never be slower than walking the bike at that grade.
    v = std::max(v, kora::kPushSpeedKph * kora::kPushGradeSpeedFactor[i]);
    ride_speed_kph_[i] = v;
    max_ride_speed_kph_ = std::max(max_ride_speed_kph_, v);
  }
  // kora fork: the fast ↔ nice ruler (kora Route character block). A
  // known route_character selects its bundle; otherwise the older
  // scalars apply with no quiet boost and no surface relief.
  const std::string& character = costing_options.route_character();
  const kora::CharacterProfile* cp = character == "road"       ? &kora::kCharacterRoad
                                     : character == "fast"     ? &kora::kCharacterFast
                                     : character == "balanced" ? &kora::kCharacterBalanced
                                     : character == "relaxed"  ? &kora::kCharacterRelaxed
                                     : character == "quiet"    ? &kora::kCharacterQuiet
                                                               : nullptr;
  if (cp) {
    tw_.avoidance = cp->avoidance;
    tw_.great_scale = cp->great_scale;
    tw_.quiet_boost = cp->quiet_boost;
    route_bonus_ = cp->route_bonus;
    surface_relief_ = cp->surface_relief;
    route_turn_ = cp->route_turn;
  } else {
    tw_.avoidance = costing_options.avoidance_scale();
    tw_.great_scale = costing_options.bonus_scale();
    tw_.quiet_boost = 1.0f;
    route_bonus_ = 1.0f - (1.0f - kora::kBikeNetworkFactor) * costing_options.bonus_scale();
    surface_relief_ = 0.0f;
    route_turn_ = 1.0f;
  }
  // kora fork: the fast e-bike's own road curve (kora Fast e-bike block).
  if (motor == &kora::kSbikeMotor) {
    tw_.bare = kora::kSbikeBareSpeedPoints;
    tw_.bare_n = sizeof(kora::kSbikeBareSpeedPoints) / sizeof(kora::kSbikeBareSpeedPoints[0]);
    tw_.paint_kph = kora::kSbikePaintSpeedKph;
  }
  // kora fork: turn seconds scale with the flat speed (kora Turns block).
  {
    const auto& pts = kora::kTurnScalePoints;
    constexpr size_t n = sizeof(kora::kTurnScalePoints) / sizeof(kora::kTurnScalePoints[0]);
    turn_scale_ = pts[n - 1][1];
    if (speed_ <= pts[0][0]) {
      turn_scale_ = pts[0][1];
    } else {
      for (size_t i = 1; i < n; ++i) {
        if (speed_ <= pts[i][0]) {
          const float f = (speed_ - pts[i - 1][0]) / (pts[i][0] - pts[i - 1][0]);
          turn_scale_ = pts[i - 1][1] + f * (pts[i][1] - pts[i - 1][1]);
          break;
        }
      }
    }
  }
  avoid_bad_surfaces_ = costing_options.avoid_bad_surfaces();
  minimal_surface_penalized_ = kWorstAllowedSurface[static_cast<uint32_t>(type_)];
  worst_allowed_surface_ = avoid_bad_surfaces_ == 1.0f ? minimal_surface_penalized_ : Surface::kPath;

  // Set the surface speed factors for the bicycle type.
  surface_cost_factor_ = kSurfaceFactors;
  if (type_ == BicycleType::kRoad) {
    surface_speed_factor_ = kRoadSurfaceSpeedFactors;
  } else if (type_ == BicycleType::kHybrid) {
    // kora fork: the hybrid-based types take the request's surface
    // profile (kora Surfaces block) instead of upstream's tables.
    const std::string profile = cp ? std::string(cp->surface) : costing_options.surface_profile();
    const kora::SurfaceProfile& sp = profile == "leisure"    ? kora::kSurfaceLeisure
                                     : profile == "balanced" ? kora::kSurfaceBalanced
                                                             : kora::kSurfaceFast;
    surface_speed_factor_ = sp.speed;
    surface_cost_factor_ = sp.surcharge;
    minimal_surface_penalized_ = sp.penalize_from;
    worst_allowed_surface_ = avoid_bad_surfaces_ == 1.0f ? minimal_surface_penalized_ : Surface::kPath;
  } else if (type_ == BicycleType::kCross) {
    surface_speed_factor_ = kCrossSurfaceSpeedFactors;
  } else {
    surface_speed_factor_ = kMountainSurfaceSpeedFactors;
  }

  // kora fork: use_roads is kept only so requests that send it stay valid.
  use_roads_ = costing_options.use_roads();
  exclude_steps_ = costing_options.exclude_steps();
  // kora fork: turn restrictions are judged by Allowed() with the
  // crossing test (see kQuietStreetMaxKph); the base class's complex-
  // restriction check is not virtual, so it is switched off here. A
  // request-level ignore keeps its meaning through request_ignores_turns_.
  request_ignores_turns_ = ignore_turn_restrictions_;
  ignore_turn_restrictions_ = true;

  // Populate the grade penalties (based on use_hills factor - value between 0 and 1)
  // kora fork: the steep-discomfort table (pushing territory only) scaled
  // by kHillStrength — honest time from the rider-power speed table is the
  // primary hill mechanism.
  float use_hills = costing_options.use_hills();
  float avoid_hills = (1.0f - use_hills);
  for (uint32_t i = 0; i <= kMaxGradeFactor; i++) {
    grade_penalty[i] = kora::kHillStrength * avoid_hills * kora::kSteepDiscomfort[i];
  }

  // kora fork: boarding is priced by kFerryWaitSec in TransitionCost for
  // both ferry kinds — zero upstream's transition costs so nothing double
  // counts. In particular the rail-ferry one: with its options unparsed
  // (disable_rail_ferry_) it decays to the 6 h maximum penalty.
  ferry_transition_cost_ = {0.0f, 0.0f};
  rail_ferry_transition_cost_ = {0.0f, 0.0f};

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
      IsUserAvoidEdge(edgeid) || CheckExclusions<true>(edge, pred)) {
    return false;
  }
  // kora fork: a via-node turn restriction — simple or complex — binds
  // only where the maneuver crosses traffic that matters
  // (turn_restriction_matters). `tile` holds `edge`, hence the node it
  // leaves from.
  if (!request_ignores_turns_) {
    const bool simple = (pred.restrictions() & (1 << edge->localedgeidx())) != 0;
    const bool complex =
        !simple && (edge->end_restriction() & access_mask_) &&
        via_node_restriction_active(tile, true, edgeid, pred.edgeid(), access_mask_,
                                    restriction_probability_, current_time, tz_index);
    if (simple || complex) {
      const NodeInfo* node = node_of_edge(tile, edge);
      if (node == nullptr) {
        return false; // cannot inspect the junction: obey
      }
      const DirectedEdge* from_arm = tile->directededge(node->edge_index() + pred.opp_local_idx());
      if (turn_restriction_matters(tile, node, from_arm, edge,
                                   edge->turntype(pred.opp_local_idx()))) {
        return false;
      }
    }
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

  // kora fork: no bike on T3+ terrain, ridden or pushed.
  if (edge->sac_scale() >= SacScale::kDemandingMountainHiking) {
    return false;
  }

  // Prohibit certain roads based on surface type and bicycle type.
  // kora fork: not while pushing — on foot any surface is fine, EXCEPT
  // impassable (rock, rungs — not pushable either).
  if (edge->surface() > worst_allowed_surface_ &&
      (!is_pushed(edge) || edge->surface() == Surface::kImpassable)) {
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
      IsUserAvoidEdge(opp_edgeid) || CheckExclusions<false>(opp_edge, pred)) {
    return false;
  }
  // kora fork: same turn-restriction test as Allowed(), seen from the
  // reverse tree. Forward, the move is opp_edge → the edge at
  // pred.opp_local_idx() on the node opp_edge ends at; `edge` is that
  // node's outgoing arm opposing opp_edge and the key of the reverse
  // complex-restriction index. `tile` holds opp_edge; the node is
  // inspected only when `edge` lies in the same tile (otherwise: obey).
  if (!request_ignores_turns_) {
    const bool simple = (opp_edge->restrictions() & (1 << pred.opp_local_idx())) != 0;
    const bool may_be_complex = !simple && (edge->start_restriction() & access_mask_);
    if (simple || may_be_complex) {
      const NodeInfo* node = node_of_edge(tile, edge);
      if (node == nullptr) {
        return false;
      }
      bool restricted = simple;
      if (may_be_complex) {
        const uint32_t idx = static_cast<uint32_t>(edge - tile->directededge(0));
        const baldr::GraphId edge_id(tile->id().tileid(), tile->id().level(), idx);
        restricted = via_node_restriction_active(tile, false, edge_id, pred.edgeid(), access_mask_,
                                                 restriction_probability_, current_time, tz_index);
      }
      if (restricted) {
        const DirectedEdge* to_arm = tile->directededge(node->edge_index() + pred.opp_local_idx());
        const DirectedEdge* from_arm = tile->directededge(node->edge_index() + edge->localedgeidx());
        if (turn_restriction_matters(tile, node, from_arm, to_arm,
                                     to_arm->turntype(edge->localedgeidx()))) {
          return false;
        }
      }
    }
  }

  // kora fork: the avoid-stairs toggle.
  if (exclude_steps_ && opp_edge->use() == Use::kSteps) {
    return false;
  }

  // kora fork: no bike on T3+ terrain, ridden or pushed.
  if (opp_edge->sac_scale() >= SacScale::kDemandingMountainHiking) {
    return false;
  }

  // Prohibit certain roads based on surface type and bicycle type.
  // kora fork: not while pushing — except impassable surface.
  if (edge->surface() > worst_allowed_surface_ &&
      (!is_pushed(opp_edge) || edge->surface() == Surface::kImpassable)) {
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
                           const graph_tile_ptr& tile,
                           const baldr::TimeInfo&,
                           uint8_t&) const {
  // kora fork: stairs — hauling time plus committing fees at the length
  // checkpoints. See the kora block for the model.
  if (edge->use() == Use::kSteps) {
    const uint32_t wg = edge->weighted_grade();
    float per_m, fee;
    if (wg > kora::kFlatGradeIndex) {
      per_m = kora::kStairsSecPerMUp;
      fee = kora::kStairsFeeUpSec;
    } else if (wg < kora::kFlatGradeIndex) {
      per_m = kora::kStairsSecPerMDown;
      fee = kora::kStairsFeeDownSec;
    } else {
      per_m = 0.5f * (kora::kStairsSecPerMUp + kora::kStairsSecPerMDown);
      fee = 0.5f * (kora::kStairsFeeUpSec + kora::kStairsFeeDownSec);
    }
    const float len = edge->length();
    float fees = 0.0f;
    if (len >= kora::kStairsFeeThreshold1M) {
      fees += fee;
    }
    if (len >= kora::kStairsFeeThreshold2M) {
      fees += fee;
    }
    const float sec = len * per_m + fees; // fees count once as time…
    const float cost = sec + fees;        // …and once more as cost
    return {shortest_ ? len : cost, sec};
  }

  // kora fork: ferries AND rail ferries (car shuttles) use the ferry
  // speed stored on the edge — never the bike's grade-driven speed, so a
  // shuttle through a mountain is immune to the DEM's fake grades. The
  // boarding wait lives in TransitionCost.
  if (edge->use() == Use::kFerry || edge->use() == Use::kRailFerry) {
    assert(edge->speed() < kSpeedFactor.size());
    float sec = (edge->length() * kSpeedFactor[edge->speed()]);
    return {shortest_ ? edge->length() : sec * kora::kFerryFactor, sec};
  }

  // kora fork: pushed bike — walking pace on edges we may not (or, for
  // bicycle=dismount tagging, must not) ride. The tier model does not
  // apply on foot: grade-scaled pushing time plus the per-metre penalty
  // on every metre (uphill pays the extra surcharge) — the section's
  // free allowance is granted back at its entry transition
  // (see kPushFreeMeters).
  if (is_pushed(edge) || edge->dismount()) {
    const uint32_t pg = effective_grade(edge);
    const float sec =
        edge->length() * 3.6f / (kora::kPushSpeedKph * kora::kPushGradeSpeedFactor[pg]);
    float per_m = kora::kPushPenaltySecPerM;
    if (pg >= kora::kPushUphillGradeIndex) {
      per_m += kora::kPushUphillExtraSecPerM;
    }
    float cost = sec + edge->length() * per_m;
    // kora fork: T2 terrain — last resort, pushed or not.
    if (edge->sac_scale() == SacScale::kMountainHiking) {
      cost *= kora::kSacT2CostFactor;
    }
    return {shortest_ ? edge->length() : cost, sec};
  }

  // kora fork: tier factor + official-route bonus + hills + surface, the
  // tier and bonus terms at the request's ruler scales.
  const uint32_t grade = effective_grade(edge);
  float factor = tier_factor(tile, classify(tile, edge, tw_.paint_kph), edge, tw_);
  // T2 terrain: ridable in principle, a last resort in practice.
  if (edge->sac_scale() == SacScale::kMountainHiking) {
    factor *= kora::kSacT2CostFactor;
  }
  if (edge->bike_network()) {
    factor *= route_bonus_;
  }
  factor += grade_penalty[grade];

  // If surface is worse than the minimum we add a surface factor
  // (kora fork: from the type's / profile's table, see kora Surfaces).
  if (edge->surface() >= minimal_surface_penalized_) {
    factor += avoid_bad_surfaces_ *
              surface_cost_factor_[std::min<uint32_t>(3, static_cast<uint32_t>(edge->surface()) -
                                                         static_cast<uint32_t>(minimal_surface_penalized_))];
  }

  // Compute bicycle speed from the rider-power table (kora fork — the
  // primary hill mechanism; dismount edges returned above via the pushed
  // branch) and the surface factor (rougher surfaces slow the bike by an
  // amount that depends on the bicycle type). The grade is the capped
  // one so DEM spikes on through roads distort neither cost nor the
  // displayed time. Surface factors of 0 mark refused surfaces, which
  // Allowed() already rejects; guard anyway.
  const float surface_factor =
      std::max(0.1f, surface_speed_factor_[static_cast<uint32_t>(edge->surface())]);
  const float bike_speed = ride_speed_kph_[grade] * surface_factor;
  // kora fork: surface relief (kora Route character block) — forgive a
  // share of the surface's extra riding time in the cost only. The
  // extra time is (1/s - 1) of the smooth-surface time; the cost keeps
  // (1 - relief) of it, i.e. the factor (1 + e(1 - r)) · s on the
  // honest, slowed seconds.
  if (surface_relief_ > 0.0f && surface_factor < 1.0f) {
    const float extra = 1.0f / surface_factor - 1.0f;
    factor *= (1.0f + extra * (1.0f - surface_relief_)) * surface_factor;
  }

  factor *= EdgeFactor(edgeid);

  // Compute elapsed time based on speed. Modulate cost with weighting factors.
  float sec = edge->length() * 3.6f / bike_speed;
  return {shortest_ ? edge->length() : sec * factor, sec};
}

// Returns the time (in seconds) to make the transition from the predecessor
Cost BicycleCost::TransitionCost(const baldr::DirectedEdge* edge,
                                 const baldr::NodeInfo* node,
                                 const EdgeLabel& pred,
                                 const graph_tile_ptr& tile,
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

  // kora fork: flat per-turn cost (braking + navigation load), scaled by
  // the rider's speed (kora Turns block).
  if (!edge->roundabout()) {
    seconds += turn_scale_ * kora::kTurnSecByType[static_cast<uint32_t>(turn)];
  }
  // kora fork: a turn INTO an official cycle route pays only the
  // character's share of the turn penalty (kora Route character block).
  if (edge->bike_network()) {
    seconds *= route_turn_;
  }

  // kora fork: the crossing rule, at the request's avoidance scale.
  float penalty = tw_.avoidance *
                  crossing_penalty(pred.classification(), pred.use(), edge, node, turn, tile, tw_);

  // kora fork: deviation from the intuitive continuation (cost only).
  if (is_deviation(tile, node, idx, pred.classification(), edge)) {
    penalty += kora::kDeviationPenaltySec;
  }

  // kora fork: expected wait when boarding a ferry / car shuttle. Real
  // time, so it reaches the displayed duration too.
  if ((edge->use() == Use::kFerry || edge->use() == Use::kRailFerry) &&
      pred.use() != Use::kFerry && pred.use() != Use::kRailFerry) {
    c.secs += kora::kFerryWaitSec;
    c.cost += kora::kFerryWaitSec;
  }

  // kora fork: push-section allowance — entering a pushed edge from a
  // ridden one starts a section; rebate the free metres, capped at this
  // edge's own penalty so the relaxation stays non-negative. Stairs have
  // their own free length in EdgeCost and are excluded here.
  if ((is_pushed(edge) || edge->dismount()) && edge->use() != Use::kSteps &&
      !is_foot_use(pred.use())) {
    c.cost -= std::min(kora::kPushFreeMeters, static_cast<float>(edge->length())) *
              kora::kPushPenaltySecPerM;
  }

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
                                        const graph_tile_ptr& tile,
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

  // kora fork: flat per-turn cost (braking + navigation load), scaled by
  // the rider's speed (kora Turns block).
  if (!edge->roundabout()) {
    seconds += turn_scale_ * kora::kTurnSecByType[static_cast<uint32_t>(turn)];
  }
  // kora fork: a turn INTO an official cycle route pays only the
  // character's share (the forward move enters `edge` here too).
  if (edge->bike_network()) {
    seconds *= route_turn_;
  }

  // kora fork: the crossing rule (pred is the edge being left here too).
  float penalty = tw_.avoidance *
                  crossing_penalty(pred->classification(), pred->use(), edge, node, turn, tile, tw_);

  // kora fork: deviation from the intuitive continuation (cost only).
  if (is_deviation(tile, node, idx, pred->classification(), edge)) {
    penalty += kora::kDeviationPenaltySec;
  }

  // kora fork: ferry / car-shuttle boarding wait, as in TransitionCost.
  if ((edge->use() == Use::kFerry || edge->use() == Use::kRailFerry) &&
      pred->use() != Use::kFerry && pred->use() != Use::kRailFerry) {
    c.secs += kora::kFerryWaitSec;
    c.cost += kora::kFerryWaitSec;
  }

  // kora fork: push-section allowance, as in TransitionCost — here the
  // predecessor is a real edge, so section starts are detected exactly.
  // Stairs have their own free length in EdgeCost, excluded here.
  if ((is_pushed(edge) || edge->dismount()) && edge->use() != Use::kSteps &&
      !is_pushed(pred) && !pred->dismount()) {
    c.cost -= std::min(kora::kPushFreeMeters, static_cast<float>(edge->length())) *
              kora::kPushPenaltySecPerM;
  }

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
  // kora fork: surface profile (kora Surfaces block), lower-cased below.
  JSON_PBF_DEFAULT(co, kDefaultSurfaceProfile, json, "/surface_profile", surface_profile);
  std::transform(co->mutable_surface_profile()->begin(), co->mutable_surface_profile()->end(),
                 co->mutable_surface_profile()->begin(),
                 [](const unsigned char ch) { return std::tolower(ch); });
  // kora fork: route character (kora Route character block), lower-cased.
  JSON_PBF_DEFAULT(co, kDefaultRouteCharacter, json, "/route_character", route_character);
  std::transform(co->mutable_route_character()->begin(), co->mutable_route_character()->end(),
                 co->mutable_route_character()->begin(),
                 [](const unsigned char ch) { return std::tolower(ch); });
  // kora fork: avoid-stairs toggle and the fast ↔ nice ruler scales.
  JSON_PBF_DEFAULT_V2(co, false, json, "/exclude_steps", exclude_steps);
  JSON_PBF_RANGED_DEFAULT(co, kAvoidanceScaleRange, json, "/avoidance_scale", avoidance_scale,
                          warnings);
  JSON_PBF_RANGED_DEFAULT(co, kBonusScaleRange, json, "/bonus_scale", bonus_scale, warnings);

  // convert string to enum, set ranges and defaults based on enum
  BicycleType type;
  std::transform(co->mutable_transport_type()->begin(), co->mutable_transport_type()->end(),
                 co->mutable_transport_type()->begin(),
                 [](const unsigned char ch) { return std::tolower(ch); });
  // kora fork: `ebike` / `sbike` keep their string (the constructor reads
  // it) and take the hybrid defaults here.
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
