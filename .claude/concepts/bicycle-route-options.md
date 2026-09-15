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

Four types, a segmented choice, always visible on the cycling tab
(on one line with the avoid-stairs toggle, § 5):

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

One ruler with five stops. It scales the model's traffic-related
levers around today's tuning, which becomes the middle stop. Two
request-level scalars carry it: **`avoidance_scale`** multiplies the
*excess* of every traffic penalty (bare through roads priced by posted
speed, painted-lane and sharrow factors, the extra-lane step, and all
crossing costs — base, per lane, signal, and thereby the T-junction
share); **`bonus_scale`** multiplies the discount of the two
infrastructure bonuses (separated cycle infrastructure, official
cycle-route membership).

| Stop | `avoidance_scale` | `bonus_scale` | Reads as |
|---|---|---|---|
| Road | 0 | 0 | traffic ignored, cycle paths earn nothing |
| Fast | 0.5 | 0.5 | direct, main roads tolerated |
| Balanced | 1.0 | 1.0 | today's model |
| Relaxed | 1.5 | 1.4 | |
| Quiet | 2.2 | 2.0 | accepts real detours for cycle paths and signed routes |

Default: Balanced.

- **Not scaled** by the ruler: hills, turn and deviation costs, pushed
  metres, stairs, service roads, ferries and car shuttles, surface
  handling, alpine guards. They are not a fast-versus-nice question.
- **Road keeps one penalty:** a carriageway with a signed parallel
  cycle path (`bicycle=use_sidepath`) stays priced as today, because a
  blue-signed cycle path is legally mandatory in Switzerland.
- The ruler is available for every bike type.

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
  its time controls: bike type and the avoid-stairs toggle always
  visible, sharing one line; pace ruler and fast ↔ nice ruler behind
  the transit tab's "more options" pattern (expander with the
  non-default indicator, snapping rulers, live description text).
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
- New fork options **`avoidance_scale`** and **`bonus_scale`** (floats,
  default 1.0); `exclude_steps` as today.
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
