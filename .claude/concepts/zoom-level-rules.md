# Per-Zoom-Level Visibility Rules

## Problem

At low zoom levels, showing every transit line and stop produces an illegible map. We need explicit, curated rules describing what each integer zoom level should show — which lines, which stops, and how the geographic context (how alone a line is, how it connects to the main network, how important each stop is in its surroundings) factors into the decisions.

## Requirements

### Output

Each line and each stop carries an integer `min_zoom` (4–12 inclusive). The MapLibre style renders a feature when `tippecanoe.minzoom <= current_zoom`. No runtime filter, no opacity gate — visibility is binary at integer zoom levels only.

### Algorithm overview

`min_zoom` is computed in this order:

1. **Per-mode line rules** — each line gets a candidate `min_zoom` from its mode's line table.
2. **Connectivity** — line `min_zoom` values are adjusted by the stop-weighted average rule (see "Connectivity (isolation avoidance)" below) so isolated branches drag their connectors forward or get held back as appropriate.
3. **Per-mode stop rules** — each stop gets a candidate `min_zoom` from its mode's stop table, using the connectivity-adjusted line `min_zoom` values.
4. **Stops follow lines** — each stop's final `min_zoom` is raised to at least the smallest `min_zoom` of any line serving it.

### Per-mode rules

Each line / stop receives the lowest `min_zoom` of any rule it matches. Per-mode tables are evaluated top-to-bottom; a rule at level N adds any features that match its condition.

Metrics referenced below:

- **length** — polyline length of the line (`line_km`).
- **spread** — geodesic distance between the two stops furthest apart on the line.
- **salience** — score from the salience pipeline. `salience: top X %` means the X % highest-salience lines **within the same mode**.
- **is_intersection** — stop served by ≥ 2 distinct line_keys of the same mode at the level being evaluated.
- **is_terminus** — first or last stop of at least one visible line of the same mode at the level being evaluated.
- **importance-greedy ≤ 1 / X km** — over the line's stops not already accepted, sort by stop importance score desc, accept a stop iff no already-accepted stop on the same line is within X km along the polyline.

#### Train

A train line is **intercity** iff its `route_short_name` matches one of `intercity_route_prefixes` (config; default `[IC, ICE, EC]`).

Lines

| Level | Rule |
|---|---|
| 4 | intercity |
| 5 | length ≥ 30 km AND salience: top 50 % |
| 6 | all remaining |

Stops

| Level | Rule |
|---|---|
| 7 | is_intersection OR is_terminus |
| 8 | served by an intercity line |
| 9 | importance-greedy ≤ 1 / 5 km |
| 10 | importance-greedy ≤ 1 / 3 km |
| 11 | all remaining |

#### Metro

Lines

| Level | Rule |
|---|---|
| 8 | spread ≥ 20 km |
| 9 | all remaining |

Stops

| Level | Rule |
|---|---|
| 10 | is_intersection OR is_terminus |
| 11 | importance-greedy ≤ 1 / 1 km |
| 12 | all remaining |

#### Ferry

Lines

| Level | Rule |
|---|---|
| 6 | spread ≥ 20 km |
| 7 | spread ≥ 10 km |
| 8 | spread ≥ 5 km |
| 9 | all remaining |

Stops

| Level | Rule |
|---|---|
| 10 | all stops on visible ferry lines |

#### Mountain

Lines

| Level | Rule |
|---|---|
| 6 | length ≥ 20 km |
| 7 | length ≥ 10 km |
| 8 | length ≥ 5 km |
| 9 | length ≥ 2 km |
| 10 | length ≥ 0.5 km |
| 11 | all remaining |

Stops

| Level | Rule |
|---|---|
| 10 | all stops on every visible mountain line, from this level onwards. Mountain lines first becoming visible at z11 bring their stops with them at z11. |

#### Regional bus

Lines

| Level | Rule |
|---|---|
| 7 | spread ≥ 25 km AND salience: top 30 % |
| 8 | spread ≥ 15 km AND salience: top 50 % |
| 9 | spread ≥ 5 km |
| 10 | all remaining |

Stops

| Level | Rule |
|---|---|
| 10 | is_intersection OR is_terminus |
| 11 | importance-greedy ≤ 1 / 1 km |
| 12 | all remaining |

#### Tram

Lines

| Level | Rule |
|---|---|
| 9 | spread ≥ 8 km |
| 10 | all remaining |

Stops

| Level | Rule |
|---|---|
| 10 | is_intersection OR is_terminus |
| 11 | importance-greedy ≤ 1 / 1 km |
| 12 | all remaining |

#### Bus

Lines

| Level | Rule |
|---|---|
| 10 | spread ≥ 5 km |
| 11 | all remaining |

Stops

| Level | Rule |
|---|---|
| 10 | is_intersection OR is_terminus |
| 11 | importance-greedy ≤ 1 / 1 km |
| 12 | all remaining |

### Salience score (per line)

A line's `salience` is a value in `[0, 1]` indicating how alone the line is geographically. Higher = more isolated; lower = more crowded by competing lines. Used by the per-mode "salience: top X %" rules above.

Comparator modes (what counts as competition for each mode):

| Mode | Comparator modes |
|---|---|
| train | {train} |
| metro | {metro, train} |
| tram | {tram, metro, train} |
| bus | {bus, tram, metro, train} |
| regional_bus | {regional_bus, bus, tram, metro, train} |
| ferry | {ferry} |
| mountain | {mountain} |

**Competition density** (per line L):

1. Sample points along L's polyline every `salience_sample_step_m` (default 1000 m).
2. For each sample, find every other line whose mode is in `comparators(L)` whose polyline passes within `salience_radius_m[mode_of_L]` of that sample.
3. Each match contributes `1 − distance / radius` to the sample's score (linear falloff; `distance` is the nearest-point distance from the sample to the other line; matches farther than the radius contribute 0).
4. `competition_count(L)` = mean of per-sample scores across all samples on L.

The per-sample average keeps long and short lines comparable — a long line with consistently quiet territory averages to the same value as a short line with the same per-stretch density.

**Per-mode radius**:

| Mode | Radius (m) |
|---|---|
| train | 30 000 |
| regional_bus | 10 000 |
| metro | 5 000 |
| tram | 5 000 |
| bus | 5 000 |
| ferry | 10 000 |
| mountain | 5 000 |

**Normalisation (within mode)**:

Lines of the same mode are ranked by `competition_count` ascending. The lowest count → `salience = 1.0`; the highest → `0.0`; intermediate values linear.

`top X %` in the per-mode rules means the X % of lines with the highest `salience` within the same mode.

### Line graph and base set

Stops are clustered into "super-UICs" by 250 m proximity. Two lines share an edge in the line graph if they share at least one super-UIC. Edge weight = `max(travel_duration(u), travel_duration(v))`, where `travel_duration(line) = line_km / speed_kmh`.

The **MBST** (minimum bottleneck spanning tree) of this graph is the tree connecting every line where the worst edge weight is as small as possible — picking the most direct passenger connectors between every pair of lines. Every non-base line ends up with a unique chain of ancestor connectors back to the base set, and that chain is what the connectivity rule operates on.

The **base set** is the **largest connected component of intercity train lines** in the line graph (intercity per the train z4 rule). If multiple intercity components exist, only the largest counts as base; the others follow normal connectivity rules. The MBST is rooted at a virtual super-source connected to every base line.

### Stop importance score

Per stop (collapsed to canonical UIC, 250 m cluster). Each category independently awards points; the score is the **sum of points across all categories**.

| Category | Definition | 3 pts | 2 pts | 1 pt | 0 pts |
|---|---|---|---|---|---|
| **Dwell time** | average `departure − arrival` across trips visiting the stop | > 3 min | > 0 min | — | else (0 min / no data) |
| **Urbanness** | bracket from "Urbanness bracket" below | city | town | village | rural |
| **Nearby transit** *(train stops only — 0 pts for all other modes)* | distinct bus / tram line_keys with ≥ 1 stop within 1 km of this train stop, EXCLUDING lines that also serve this train stop | > 3 lines | > 0 lines | — | else |
| **Interchange** | the stop has ≥ 2 distinct line_keys serving it | interchange, ≥ 1 is a train line | interchange, no train line | — | not an interchange |

Score = sum of the four category points. Max for a train stop is 12; max for a non-train stop is 9 (nearby-transit contributes 0).

The score is per-stop, computed once per pipeline run, written to `data/transit/stop_attributes_sources.json`. `nearby_transit_radius_m` (the 1 km radius) lives in `config.yaml > zoom_level_rules.stop_importance`; the other thresholds (3 min, line counts) are hard-coded in the implementation.

### Urbanness bracket

Derived from OSM building density around each stop. Two `building=*` counts are taken per canonical UIC: within 200 m (`c200`) and within 500 m (`c500`). The bracket is assigned by evaluating the rules top-to-bottom; the first matching row wins (`elseif` semantics):

| Condition | Bracket |
|---|---|
| `c500 > 600` | city |
| `c500 > 300` | town |
| `c200 > 30` | village |
| (else) | rural |

Counts and the resulting bracket are baked into a new diagnostic `data/transit/urbanness.json`, keyed by UIC. The two radii and the threshold numbers live in `config.yaml > zoom_level_rules.urbanness`.

### Connectivity (isolation avoidance)

After the per-mode rules have assigned a candidate `min_zoom` to each line, the MBST (defined above) is consulted to make sure each visible line is connected to the main network at the zoom it shows.

The rule, per non-base line **L** with path `L → C1 → C2 → … → base` through the MBST:

- **L's `min_zoom`** is the floor of the stop-weighted average over the lines on L's path (L itself plus its ancestor connectors): `floor( Σ stops_i · z_i / Σ stops_i )`. Z_i is each line's candidate `min_zoom` from the per-mode rules.
- **Each connector C's `min_zoom`** is the **minimum** of the values produced by all branches whose path runs through C. A connector with many branches takes the earliest zoom that any of them dragged it to.

A large branch with small connectors pulls them strongly forward; a small branch with large connectors gets held back. Lines never connected to base in the line graph follow their per-mode rules unchanged — no special fallback (for mountain this means landing at z11 via the "all remaining" row, which is intentional).

### Stops follow lines

A stop's `min_zoom` is at least the smallest `min_zoom` among the lines serving it. A stop is never visible before at least one of its lines.

### New identifiers and outputs

- `zoom_level_rules` block in `scripts/transit/config.yaml`. Sub-keys:
  - `salience.sample_step_m` — default `1000`.
  - `salience.radius_m` — per-mode table; defaults `train: 30000`, `regional_bus: 10000`, `metro: 5000`, `tram: 5000`, `bus: 5000`, `ferry: 10000`, `mountain: 5000`.
  - `line_graph.cluster_threshold_m` — default `250` (used to merge nearby stops into super-UICs for the line graph / MBST).
  - `stop_importance.nearby_transit_radius_m` — default `1000`. The only point-system threshold lifted into config; the per-category point thresholds (3 min, > 0 lines, etc.) stay hard-coded for now, alongside the logic they belong to.
  - `urbanness` — `radius_inner_m` (default 200), `radius_outer_m` (default 500); bracket thresholds for `c500` and `c200` as described in "Urbanness bracket" above.
  - `intercity_route_prefixes` — list of `route_short_name` prefixes that mark a train as intercity. Default: `[IC, ICE, EC]`.
- Per-feature properties (line / stop):
  - `min_zoom` (integer).
  - `salience` retained for diagnostics.
  - For stops: `importance_score`, `urbanness_bracket`, `is_intersection`, `is_terminus`.
- Per-stop diagnostic `data/transit/urbanness.json` and a new section in `salience.json` recording which rule placed each line / stop at its final zoom level.

## Constraints

- All zoom values are integers 4–12 inclusive. No fractional `min_zoom`. The MapLibre style uses plain `tippecanoe.minzoom`-driven visibility with constant per-layer opacity.
- Mountain visual style (light yellow, fixed width) is preserved.
- The salience score and its components are written to every feature for diagnostic use.
- `tippecanoe.minzoom` is what tile inclusion responds to. The style does NOT carry any runtime `min_zoom` filter — MapLibre's filter context evaluates `["zoom"]` against the tile's integer zoom, so a per-feature fractional zoom gate is not feasible there.
- Rules apply per pipeline run; no per-zoom recomputation at render time.

## Possible future extensions

- **Stop-importance from external data** — population density (BFS open data for CH), POI density, parking capacity. The OSM-building-density heuristic is a stand-in.
- **Per-region rule overrides** — e.g. urban centres show more transit at lower zooms. Out of scope until the base rules are validated.
- **Time-of-day variants** — different rule sets for weekday peak vs night. Requires the daily-frequency variant of the pipeline that's also out of scope.
