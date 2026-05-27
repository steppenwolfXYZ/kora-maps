# Freq Score: Relative Malus + Fixed Minimum Threshold

## Requirements

**Relative malus:** Each off-peak dimension (evening, weekend) reduces `core_score` multiplicatively. Exact factors per tier:

- Good service (headway within threshold): factor = 0
- Low service (headway > `LOW_EVE_HEADWAY` / `LOW_WE_HEADWAY`): factor = 0.10
- No service (0 trips): factor = 0.20

```
final_score = core_score × (1 − eve_factor) × (1 − we_factor)
```

Maximum combined reduction is 40%. The existing per-mode `LOW_EVE_HEADWAY`/`LOW_WE_HEADWAY` thresholds are unchanged; only how the malus is applied changes.

**Fixed minimum threshold:** A line is drawn only if `compute_freq_score(...) >= 0.075`. This threshold applies uniformly to all modes. Lines below it are excluded identically to the current `score == 0.0` treatment — removed from `_line_canonical_export` and dropped from drawn output. Mountain mode is exempt.

## Constraints

- `core_trips == 0 → return 0.0` early exit is unchanged.
- `core_trips == 1` special case (`min(0.15, best_hw / CORE_MINUTES)`) is unchanged.
- The zero-service filter on `_line_canonical_export` must use `< 0.075` instead of `== 0.0`.
- `mode_approx = regional_bus` for the bus bucket in the filter is unchanged.
