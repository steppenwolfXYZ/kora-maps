# Far-zoom stop dot redesign

## Problem

At zooms 7–13.99 the map shows stops as plain dots (pills take over from z14). Today every dot is the same size — a busy interchange and a once-an-hour halt look identical. The dot should encode how important the stop is, so that hubs read as hubs at a glance.

## Requirements

### Stop score

The score is still computed and exposed on every dot feature, but its role is no longer to drive a per-stop size directly — it now feeds into the **tier assignment** (and gates whether a stop is "big enough" to qualify for a higher tier). Each drawn stop gets a single numeric `stop_score`, aggregated at the **parent UIC** level — platforms of the same physical station combine, and the dot rendered for that station carries the combined score. Stops whose feature has no resolvable parent UIC (e.g. mountain straight-line embedded `gtfs_stops` without a `stop_id`) carry no score.

Per emitted feature × per stop on its sequence, the base contribution is:

```
contribution = effective_weight × terminal_multiplier × (1 + freq_score)
```

where `freq_score ∈ [0, 1]` is the existing per-line score used for line width. `(1 + freq_score)` ranges from 1 (at the bucket's `worst_freq`) to 2 (at `best_freq`) — a low-frequency line still counts but a high-frequency line counts twice as much.

Inputs:

- **`effective_weight`** — for non-train modes this is just `mode_weight` (bus/regional_bus 1.0, tram 1.5, metro 2.0, mountain/ferry 3.0; the mountain weight applies uniformly across every `mountain_origin`). For `mode == train`, the class-specific entry in `train_class_weights` **replaces** the train mode weight when the line's `ref` matches a known class:

  | Class | Match | Default weight |
  |---|---|---|
  | IC | `ref` matches `zoom_level_rules.intercity_route_prefixes` (IC / ICE / EC) | 6 |
  | IR | `ref` starts with `IR` (and didn't match an IC prefix) | 5 |
  | RE | `ref` starts with `RE` | 4 |
  | R (bare) | `ref` starts with `R` followed by a digit / space / end (so `RJ` and `RB` are *not* in this bucket) | 4 |
  | default | none of the above (S-Bahn, S, etc.) | 3 (= `mode_weights.train`) |

- **`terminal_multiplier`** — applied when the stop is the feature's **first or last** stop in its sequence (the feature terminates at this stop in this direction). Default 1.5; mid-line passthroughs use 1.0. This also re-admits the arrival-terminus case, which would otherwise be silently dropped — a line that ends at a stop still represents a real service relation to that stop.

**Counting rules:**

- **Per direction.** Emitted features are per-direction. Both directions of a bidirectional line still produce separate features upstream.
- **Dedup per `ref`.** At a single parent UIC, only one variant per `ref` contributes — when multiple variants of the same line serve the stop (across either direction and any sub-route), the highest-contribution one wins. Per-direction dedup was the original aim but proved impossible to do robustly: a line like S5 fans out from a hub to several endpoints, so its variants carry many distinct `(first_uic, last_uic)` pairs and a direction-aware key collapses nothing. Per-`ref` dedup collapses the whole family into one entry; the terminal multiplier compensates with its smaller `1.5` value (vs `3.0` under directional dedup) so a terminal stop still scores roughly proportionally.
- **One contribution per feature per UIC.** A loop feature that re-visits the same parent UIC within one direction contributes once, not per pass-through.

`freq_score` is reused as-is (no recomputation). All boosts and the per-`ref` line dedup live inside the per-feature loop in step 06. The visual dedup pass (overlapping-dots merge) is separate and runs later in step 07 — see the Dedup section.

### Tier assignment

The continuous score no longer drives the dot's size directly. Instead the score, combined with line-count and per-line-type counts, decides which **tier** a stop falls into; the tier in turn carries its own size, color, casing, and label rules. Each tier defines an explicit visual design — sizes are deliberate, not interpolated.

**Counting input:** tier is computed from the stop's **own** base line composition and its base `stop_score`. Absorbed stops' lines do **not** roll into the absorber for tier evaluation — dedup is a visual cleanup, not a rescoring, so a hub's tier reflects the hub itself, not the noise it swallowed. Tier is assigned **once per stop** in step 06 (alongside the score) and stays fixed at every far-zoom zoom level. "IC lines" mean lines whose `ref` starts with `IC` / `ICE` / `EC` (per `zoom_level_rules.intercity_route_prefixes`). "Train lines" means the `train` mode bucket (not mountain, not metro).

**Assignment:** the table below is evaluated top-down; the first rule that matches wins. The order is not strictly by size — the natural mode order (train → mountain → ferry → tram/bus) is what makes the if/else queue clean, since mountain and ferry rules already exclude trains and each other.

| Tier | Rule |
|---|---|
| **Major train station** | Has ≥ 1 IC line AND `stop_score ≥ 100` (very top: Zürich HB / Bern HB / Basel SBB level) |
| **Main train station** | Has ≥ 1 IC line AND `stop_score ≥ 40` |
| **Important train station** | Has any train line AND `stop_score ≥ 20` |
| **Train station** | Has any train line AND `stop_score ≥ 10` |
| **Small train station** | Has any train line |
| *(Metro tiers — deferred until non-CH countries are added)* | |
| **Major mountain stop** | Has mountain lines AND no train lines AND `mountain_line_count ≥ 2` (see below) |
| **Mountain stop** | Has mountain lines AND no train lines |
| **Ferry stop** | Has a ferry line AND no train lines AND no mountain lines |
| **Major tram/bus hub** | `stop_score ≥ 15` OR has ≥ 1 metro line (metro-OR is a placeholder until metro tiers are defined) |
| **Big tram/bus station** | `stop_score ≥ 6` |
| **Normal tram/bus stop** | `stop_score ≥ 1.5` |
| **Small bus stop** | Has any bus line |

**Mountain line counting** (only used for the Major-vs-regular mountain check):

- **Mountain line**: `1.0` if it does not terminate at the stop, `0.9` if it does terminate. The 0.9 dampens transfer-only stations where two funicular / aerial sections meet at their termini — two terminating mountain lines at the same stop sum to `1.8`, below the `≥ 2` threshold, so they stay "Mountain stop" rather than "Major mountain stop".
- **Ferry line** at the same stop: always `1.0`.
- **Tram / bus line** at the same stop: `0.5` regardless of terminal status.
- Train lines don't participate — a stop with any train line is already in a train tier.

Example outcomes: 2 terminating mountain lines → 1.8 → **Mountain**. 3 terminating mountain lines → 2.7 → **Major**. 1 through-mountain + 1 tram → 1.5 → **Mountain**. 1 through-mountain + 1 ferry → 2.0 → **Major**.

**Per-stop exceptions:** an optional config list lets a UIC be forced into a specific tier regardless of rule matches. Reserved for edge cases that defy the rules; not expected to be heavily used.

### Size per tier

Each tier has a fixed diameter (in CSS px) at z7 and z13; linear-in-zoom interpolation between them, and linear extrapolation past z13 through z13.99 (the pill takes over at z14 — see `stops-pill-zoom.md` § "Visual style", where `max_d(z14)` is matched to the largest tier's z13.99 extrapolated diameter for a seamless handoff). Only the lower edge z7 is clamped. Within a tier there is **no per-stop size variation** — the continuous score decides which tier a stop lands in; once the tier is fixed the size follows the tier table.

| Tier | z7 diameter | z13 diameter |
|---|---|---|
| Major train station | 7 | 18 |
| Main train station | 6.5 | 15 |
| Important train station | 6 | 13 |
| Train station | 5 | 11 |
| Small train station | 4 | 9 |
| Major mountain stop | 3 | 6 |
| Mountain stop | 2.5 | 5 |
| Ferry stop | 3 | 6 |
| Major tram/bus hub | 4.5 | 10 |
| Big tram/bus station | 3.5 | 7.5 |
| Normal tram/bus stop | 2.5 | 5.5 |
| Small bus stop | 2 | 4 |

Anchors that fix the rest of the table:

- **Major train station** at `7 / 18` = the current cap. Country-top hubs (Zürich HB / Bern HB / Basel SBB) hit this.
- **Small train station** at `4 / 9` = the lowest train tier, deliberately sized just above **Major tram/bus hub** so any train stop is visually heavier than any pure bus interchange.
- **Small bus stop** at `2 / 4` = the current lower floor.
- **Major tram/bus hub** at `4.5 / 10` = sized just below **Small train station**; a busy multi-line bus interchange is prominent but never outranks a real train stop.
- **Major mountain stop** and **Ferry stop** both at `3 / 6` — deliberately identical size; differentiation is by mode symbol, not diameter.

### Implementation split

- **Pipeline (step 06)** computes each UIC's tier from `stop_score` + base line composition (per the tier assignment rules) and writes it into `stop_size_scores.json` (`{uic: {"score": …, "tier": …}}`). Step 07 reads that file and stamps `stop_tier` onto every dot feature alongside `stop_score`. The property is named `stop_tier` (not `tier`) to avoid collision with the pre-existing `tier` from `stop_salience`. Line composition inputs (`has_ic`, `has_train`, `mountain_line_count`, …) are consumed at tier-computation time; they do not need to be exposed as feature properties. Tier is fixed here — the step 07 dedup pass does not re-assign it.
- **Pipeline (step 07 — dedup)** merges absorbed dots into their absorber for popup listing only (`lines_json_zN`) and hides absorbed features via `tippecanoe.minzoom`. It does not modify tier or score.
- **Style (`generate_style.py`)** holds the tier → diameter mapping and the per-zoom interpolation. A single `match ["get", "stop_tier"] …` expression on `circle-radius` looks up the right z7 / z13 corners; the outer `interpolate zoom` blends between them.

Consequences:

- **Diameter tweaks are style-only** — edit the tier size in config (or the mapping in `generate_style.py`), re-run `python3 scripts/generate_style.py`, reload. No pipeline rebuild.
- **Threshold or rule changes** (e.g. Main train `≥ 40` → `≥ 50`, or adjusting mountain terminal factor) do require a pipeline re-run (`--start 6`) because tier assignment is baked into the feature.

### Dedup of overlapping dots

Stops that visually touch at a given far-zoom level merge into one. The classic case is a bus stop physically next to a train station — different parent UICs, so the parent-UIC aggregation above does not combine them, but visually they should read as one stop and the lines served by the bus stop should add to the train station's apparent importance.

**Touch criterion.** Two dots touch at zoom z when the pixel distance between their centers is ≤ `radius_A(z) + radius_B(z) + min_spacing_px`. `min_spacing_px` is a single config value applied at every zoom — the radii already scale with zoom, so the effective spacing scales naturally.

**Direction.** Mode hierarchy gates the absorber side first: `train` outranks `mountain` / `ferry`, which outrank everything else. A dot can absorb a neighbour only if its mode rank is greater than or equal to the neighbour's. Within the same rank, the higher-score dot absorbs the lower; tiebreak on equal scores by `stop_id`. Across ranks, a strictly higher-ranked dot absorbs a lower-ranked neighbour even when the lower-ranked one has the higher raw score — so a busy bus interchange next to a small train station gets absorbed into the train station, never the other way around. The absorbed dot disappears at the zoom level where it was touched, and at every lower zoom. Its lines merge into the absorber's per-zoom popup listing (`lines_json_zN`) so a click on the absorber shows what is currently rolled up at that zoom. The absorber's **score, tier, and size are not affected** — dedup is a visual cleanup, not a rescoring.

**Absorber identity is preserved.** The absorber keeps its own mode, color, position, and `stop_id`. Absorbed dots are removed from the far-zoom output entirely — they do not influence the absorber's appearance beyond the score. At z14+ they still render via the pill-zoom layer (dedup does not touch z14+).

**Per-zoom scope.** Dedup runs per integer zoom level z ∈ {7, 8, …, 13}, descending from z13 → z7. Disc radii grow with zoom but real-world distance per pixel grows faster, so absorption is **monotonic downward**: once a dot is eaten at zoom z, it stays eaten at every z′ < z.

At each zoom z (in descending order):
1. Compute the current disc radius for every surviving stop at z from its current tier's z-diameter (linear interpolate between the tier's z7 and z13 anchors).
2. Apply mode hierarchy first (train > mountain/ferry > everything else). Within the same rank, process survivors in descending score order (tiebreak by `stop_id`). Each absorber finds surviving neighbours within touch range and eats them; across ranks a higher-ranked dot absorbs a lower-ranked neighbour even at a lower score.
3. Iterate within zoom z until stable — an absorber that grew by eating one neighbour may now reach another.
4. Carry the surviving set forward to z−1.

**Per-zoom lines on the feature.** A stop that absorbs neighbours at low zoom but not at high zoom accumulates a different line set at each zoom. The feature stores per-zoom line lists (`lines_json_z7`..`lines_json_z13`) so the popup shows exactly what is currently rolled up at the rendered zoom. A stop with no absorption carries the same list at every zoom.

**Per-zoom score (debug-only).** The pipeline continues to write `score_z7`..`score_z13` per surviving stop for now, but nothing renders from them — tier and size are fixed at the base state and do not respond to per-zoom score changes. These properties are kept temporarily for debugging (they make it easy to inspect how much a hub absorbed at each zoom) and should be dropped once the tier system is stable. TODO: remove `score_zN` from the feature output.

**Visibility encoding.** Absorbed-everywhere stops get `tippecanoe.minzoom: 14` — they disappear from the far-zoom layer entirely and only render via the pill-zoom layer. Partially-absorbed stops (absorbed at z ≤ k, surviving at z > k) get `tippecanoe.minzoom: k + 1`.

**New config block:**

```yaml
stop_dot_dedup:
  min_spacing_px: 2.0
```

## Constraints

- **Visibility logic is unchanged.** Which stops appear at which zoom (e.g. train-only between z7 and z9, all stops from z10) is governed by the existing thresholds and stays as is. The redesign affects only the size of dots that are already drawn, and the dedup pass which removes overlapping ones.
- **Stop eligibility is unchanged.** Dots are drawn only for stops served by at least one emitted (drawn) line. The score is computed only over those stops, and percentile defaults are taken from that same set.
- **Dedup is far-zoom only.** Absorption operates at z7–z13. At z14+ all stops render via the pill-zoom layer, regardless of whether they were absorbed at far-zoom.
- **z14+ rendering belongs to the pill design concept and is not touched here.** The far-zoom redesign owns z7–z13.99 exclusively. The two ranges are drawn as two separate style layers reading the same `transit_stops` PMTiles source: the far-zoom layer (`-far` suffix, tier-driven sizes, `maxzoom: 14`) and the pill-zoom layer (`minzoom: 14`) so the two layers do not overlap.
