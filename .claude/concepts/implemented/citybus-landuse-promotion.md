# City-Bus Promotion via Landuse Evaluation

## Problem

The city_bus / regional_bus split is number-based and deliberately conservative: almost no true regional bus is misclassified as city bus, but many genuinely urban lines are stuck in regional_bus. Manual reclassification lists are unmaintainable across feed updates. The landuse-based corridor metric developed in the `find_citybus_candidates_v2.py` diagnostic separates the two classes well and should run as a dynamic evaluation inside the pipeline.

## Requirements

### Evaluation metric (as validated in the v2 diagnostic)

- **Built-up share, side-aware:** the line's shaped geometry is sampled every 50 m (samples deduplicated on a 100 m grid, keeping the local travel heading). At each sample, the 500 m disc of raster cells is split into the left and the right half-disc relative to the heading; each half's built-up fraction is computed and the better side wins the sample. The line's share is the mean of the per-sample winning-side fractions.
- **Spread:** the straight-line distance between the line's two furthest-apart sample points (not the polyline length).
- **Pass rule:** the line passes when `share >= threshold(spread)` with
  `threshold(spread) = 1 − (1 − pass_anchor_share) · 0.5^((spread − pass_anchor_km) / pass_halving_km) − pass_soften`.
  Calibrated values: 50% anchor at 1 km, halving distance 4 km, soften 0.049 (≈ 45% at 1 km, ≈ 70% at 5 km, asymptote 95.1%).

### Built-up landuse raster (new step-03 artifact)

- Step 03 additionally produces a built-up landuse raster: OSM `landuse` polygons of the built-up classes (residential, commercial, industrial, retail, construction, garages, railway, brownfield, education, institutional) rasterized onto a 100 m grid, stored as a cached artifact in `data/osm/` (same per-country extraction pattern and per-artifact idempotency as the existing extracts).
- The class list stays in code; it defines the metric and is not a tuning knob.

### Promotion in the pipeline

- Every bus trip group that the number-based rule classifies as **regional_bus** is evaluated against the metric using its pfaedle-shaped geometry; groups that pass are **promoted to city_bus**. Promotion only — no demotion of number-classified city buses, and trolleybus (route_type 800) stays fixed city_bus without evaluation.
- **Placement:** the evaluation runs in the emission stage of the scoring step, immediately after the number-based rule answers "regional_bus" for a variant — the point where the shaped geometry is already loaded. The trip-grouping partition uses the coarse `bus` bucket (the city/regional split is not part of group identity), so promoting at this point cannot change grouping; `line_key` and all stop-side artifacts are derived downstream from the emitted mode and stay consistent automatically.
- The decision is made **once per line group** over the union of all its variants' geometry (both directions, all branches), so all variants of a line agree on the mode.
- A promoted group behaves as city_bus **everywhere downstream**: bucket-dependent frequency gates and score endpoints, line color and width curves, `mode` on all emitted features, the mode component of `line_key`, stop membership strings, pill rendering, and the search index. No artifact may carry a mixed or stale mode for a promoted line.
- Lines whose geometry pfaedle could not shape are not evaluated and keep their number-based class.
- Groups containing seasonal-rescue variants (see `seasonal-regional-bus-rescue.md`) are **not evaluated**: the rescue exists only for regional buses, and the existing rule drops rescued variants that classify as city bus — promoting such a group would silently delete it from the map instead of recoloring it.

### Config

- New keys in `scripts/transit/config.yaml` (section `citybus_promotion`): `pass_anchor_km`, `pass_anchor_share`, `pass_halving_km`, `pass_soften`. The `pass_soften` key is the primary tuning knob.
- The v2 diagnostic script reads the same config keys so tuning in the diagnostic and pipeline behavior cannot drift apart.

### Diagnostics

- The pipeline writes a per-evaluated-group record (share, spread, threshold, promoted y/n) to a diagnostic JSON in `data/transit/` so every promotion decision is auditable after a rebuild.

## Constraints

- Trip grouping partitions by bucket, and `line_key` embeds the mode — the promotion must be applied at a point where the group identity stays consistent across all downstream artifacts (exact placement is an implementation choice, but no downstream consumer may see the pre-promotion mode).
- The evaluation depends only on geometry and the landuse raster — it must not depend on frequency data, so gating and promotion stay independent.
- The raster extraction requires Docker/osmium, consistent with the rest of step 03; a missing raster artifact must fail the run loudly rather than silently skipping promotion.
- The v2 diagnostic script and its output file remain available as the tuning tool; the v1 diagnostic stays untouched.
