# Bicycle Route Options

User-facing options for the cycling tab: bike type, riding pace, a
fast ↔ nice ruler, and avoid-stairs. Companion to
`pedestrian-bicycle-routing.md` (the feature) and
`bicycle-costing-fork.md` (the weighting model these options scale).
Supersedes the "later enhancements" sketch in the latter: the hill
avoidance ruler is dropped for now, the official-route favouring folds
into the fast ↔ nice ruler.

## Problem

The cycling tab has exactly one rider in mind: an everyday cyclist on a
normal bicycle at 18 km/h who accepts today's one balance between
directness and calm streets. Real riders differ on every axis — a
commuter on a fast e-bike wants the road, a leisurely rider wants the
quiet way and no stairs, a racing cyclist wants tarmac only. None of
that is expressible, and the avoid-stairs toggle the feature concept
declared mandatory still has no UI.

## Requirements

### 1. Bike type

Four types, chosen from a dropdown that shows each type's icon, name
and a one-line description (inside the "more options" area, § 6):

| Type | Behaviour |
|---|---|
| **Bicycle** | today's normal bicycle; pace from the pace ruler |
| **Racing bicycle** | as Bicycle, but refuses surfaces rougher than compacted gravel — the engine's built-in road bicycle type; pace from the pace ruler |
| **E-Bike** | motor-assisted up to 25 km/h; no pace ruler |
| **Fast E-Bike** | motor-assisted up to 45 km/h (S-Pedelec); no pace ruler |

Default: Bicycle.

### 2. Pace ruler (Bicycle and Racing bicycle only)

A ruler with four stops, each a flat-ground speed:

| Stop | Flat speed |
|---|---|
| Leisurely | 15 km/h |
| Normal | 20 km/h |
| Fast | 25 km/h |
| Professional | 30 km/h |

Default: Normal. The stop is remembered while an e-bike type is
selected and comes back when switching to a pedal type again.

### 3. Speed model: rider power, not a speed table

The pace stop is a description of the rider, so it must change the
whole grade→speed curve, not only the flat speed:

- The flat speed defines the rider's sustained power (air drag on the
  flat, gravity and rolling resistance on climbs); every grade's speed
  follows from that power. A professional climbs proportionally faster
  than a leisurely rider, not just on the flat.
- **E-bikes** use the same model with the rider at Normal effort plus a
  motor, and the motor cuts out at the type's assist cap (25 / 45
  km/h). Above the cap only rider and gravity act, so downhill both
  types run faster than their cap by themselves.
- **Calibration anchors** (tunable, but the character must hold):
  - Bicycle at Normal pace: speed halves around a 3 % climb and reaches
    walking pace near 10 % — today's everyday-rider curve, kept.
  - E-Bike: 25 km/h on the flat, noticeably slower uphill — a moderate
    motor, e.g. mid-teens km/h on a 6 % climb.
  - Fast E-Bike: holds 45 km/h on gentle climbs (~2 %), only moderately
    slower on steep ones — a strong motor.
- The descent cap (city braking), the steep-discomfort penalty, the
  pushed-bike pace and the stairs model stay as they are; pushing
  speed does not depend on the bike type or pace.
- Durations, ascent/descent and the elevation profile on the result
  cards reflect the chosen type and pace.

### 4. Fast ↔ nice ruler

One ruler with five stops. Each stop is one bundle of engine numbers,
sent as the request option **`route_character`** (`road` / `fast` /
`balanced` / `relaxed` / `quiet`); every per-stop number lives in the
engine's tuning block, so tuning never touches the client. Balanced is
today's model. Default: Balanced.

| Stop | Traffic penalties | Cycle-path bonus | Cycle-route bonus | Quiet boost | Surfaces | Surface relief |
|---|---|---|---|---|---|---|
| Road | off | off | none | none | fast | none |
| Fast | half | half | 0.96 | none | fast | none |
| Balanced | as today | as today (0.90) | 0.92 | 0.95 | balanced | none |
| Relaxed | 1.5× | 1.4× (0.86) | 0.86 | = cycle path (0.86) | leisure | 50 % |
| Quiet | 2.2× | 2× (0.80) | 0.74 | = cycle path (0.80) | leisure | 80 % |

- **Traffic penalties** scale by their excess over 1: bare through roads
  priced by posted speed, painted-lane and sharrow factors, the
  extra-lane step, and all crossing costs (base, per lane, signal, and
  thereby the T-junction share). Never scaled: hills, deviation cost,
  pushed metres, stairs, service roads, ferries and car shuttles,
  alpine guards, and the signed-cycle-path factor (`bicycle=use_sidepath`,
  legally mandatory in Switzerland).
- **Cycle-path bonus**: the great tier for physically separated cycle
  infrastructure, scaled by its discount. Real separated cycle paths
  stay great at every stop.
- **Cycle-route bonus**: the factor for edges on an official cycle
  route (any network level). Strong at the calm stops: on a cycle tour
  the signed route is the point.
- **Quiet boost**: at the calm stops the environment counts, not the
  infrastructure grade. Narrow unclassified roads (no second lane in the
  direction of travel — the graph has no width, and 88 % of Swiss
  unclassified roads carry no lane or width tag; the few tagged with two
  lanes are real roads), tracks, and bike-allowed paths / footways get
  the same factor as a separated cycle path at Relaxed and Quiet, a
  slightly smaller one at Balanced. A cycle path beside a main road is
  thus no better than a gravel lane along the river there; the graph
  cannot tell it from a cycle path through a park.
- **Surfaces** follow the stop for the normal bicycle and both e-bike
  types, as a profile: Road / Fast keep the engine's hybrid tables
  (compacted / dirt / gravel / path at 0.8 / 0.6 / 0.4 / 0.25 speed,
  surcharge from dirt); Balanced keeps those speeds with a milder
  surcharge (1.5 / 2.0 / 4.5 instead of 2.5 / 4.5 / 7.0); Relaxed /
  Quiet ride gravel and dirt as normal ground (0.9 / 0.8 / 0.7 / 0.4,
  surcharge on path only). The racing bicycle keeps the engine's road
  tables at every stop.
- **Surface relief**: at the calm stops a share of the extra riding
  time a rough surface costs is forgiven in the cost only — half at
  Relaxed, most (80 %) at Quiet. Displayed times stay honest.
- The ruler is available for every bike type.

### 4a. Speed-dependent turns and the fast e-bike

- **Turns scale with speed.** The flat per-turn seconds (right 3 s,
  left 4 s, U-turn 8 s; left and right differ little on narrow streets,
  the crossing rule prices big junctions) are sized for a 25 km/h rider
  and scale with the rider's flat speed, in time and cost: braking into
  a tight corner and getting back up to speed costs more the faster one
  rides (the physics: ~1.5 s at 20 km/h, ~3 s at 25, ~6.5 s at 30).
  Leisurely 0.5×, normal 0.75×, fast and e-bike 1×, professional and
  fast e-bike 1.25× (the motor gets it back up to speed quickly). This
  gives the pace ruler depth without a knob of its own.
- **Turns onto a cycle route are (nearly) free at the calm stops.** A
  signed route's own corners must not price it out of following it. A
  turn into an official cycle-route edge pays only a share of the turn
  penalty (junction stop time with its stress, plus the flat seconds):
  25 % at Relaxed, nothing at Quiet, full elsewhere. The deviation cost
  is unaffected.
- **Fast e-bike on roads up to 50 km/h.** At 45 km/h the rider moves
  with the traffic of a 50 zone, so for the fast e-bike a bare posted-50
  road is barely worse than a quiet street (1.1 instead of 1.4) and
  paint on it is the plateau. Faster roads price as for everyone.

### 5. Avoid stairs

A toggle. On: stairs are refused outright (the fork's existing
`exclude_steps` option) — no pricing, no hauling. Off: stairs are
priced as today.

- Selecting E-Bike or Fast E-Bike switches the toggle **on**; the user
  can switch it off again and the choice then sticks for that visit.
  Switching back to a pedal type does not switch it off.
- Default for Bicycle / Racing bicycle: off.

### 6. Where the options live

- On the cycling tab, in the input area where the transit tab shows
  its time controls, one control row: the avoid-stairs toggle, then the
  "More options" button (both always visible), the swap at the tail. Bike
  type dropdown, pace ruler and fast ↔ nice ruler sit behind the
  expander (the transit tab's "more options" pattern: non-default
  indicator, snapping rulers, live description text). The pace ruler
  shows the racing-bike glyph at its Professional stop.
- Every cycling query — the main request, its alternatives, the
  per-crossing land variants, and navigation recalculations
  (`bicycle-navigation.md` § recalculation) — carries the same options.
- The walking tab is untouched by all of this.

### 7. Persistence and deep links

- All four settings persist in the existing routing preferences store
  (`kora_routing_prefs`), the same way the transit options do.
- Non-default settings ride in the routing URL alongside `mode=bike`:
  **`bike`** = `racing` / `ebike` / `sbike`, **`pace`** = `leisurely` /
  `fast` / `pro`, **`roads`** = `road` / `fast` / `relaxed` / `quiet`,
  **`stairs`** = `avoid`. Absent = default. A shared link reproduces
  the query; restoring from a URL applies session-only, never into the
  recipient's store.

### 8. Engine request surface

- Bike type maps onto the engine's `bicycle_type`: `hybrid` for
  Bicycle, `road` for Racing bicycle, and two new fork values,
  **`ebike`** and **`sbike`**, for the assisted types.
- Pace maps onto the existing `cycling_speed` (flat km/h); for the
  e-bike types the option is ignored and the type's model applies.
- New fork option **`route_character`** (string, one of the five stops);
  `exclude_steps` as today. The engine keeps the earlier scalars
  `avoidance_scale` / `bonus_scale` / `surface_profile` as a fallback
  for requests without a route character.
- Everything is query-time: no tile rebuild, no matrix rebuild.

## Constraints

- Pedestrian costing, the transit walk legs and the footpath matrix
  stay byte-identical — the fork still touches bicycle only.
- Bicycle at Normal pace, Balanced, stairs off is the new default. It
  is not byte-identical to today (20 km/h instead of 18, and the power
  model replaces the table), so the full benchmark set is re-run on the
  default before the change ships and must not regress. For the Bern
  pairs, the expected corridor is additionally recorded per ruler stop.
- Road must never route a bike where cycling is not permitted; it only
  stops caring about traffic, not about access.
- The pace ruler never applies to e-bikes; an e-bike's flat speed is
  its cap, not a preference.
- Labels English only; i18n out of scope.
