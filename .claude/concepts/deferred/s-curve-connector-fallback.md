# S-curve Connector Fallback

## Problem

When the symmetric-arc connector construction has no valid solution, the connector falls back to a straight 2-point line. The most common trigger is the **parallel-forward with offset** case (`turn ≈ 0`, chord misaligned with `tA`): the two chosen tangents are parallel but the endpoints are laterally offset. A single arc cannot rejoin two parallel-but-offset tangents; the resulting straight connector has hard kinks at both ends where the casing meets the connector at a non-tangent angle.

An S-shape — two arcs of opposite curvature joined smoothly at an inflection point — would replace these straight fallbacks with a tangent-consistent curve in the cases where a single symmetric arc has no solution but a smooth bend still exists.

## Requirements

### Scope

- Applies to every connector type built via the symmetric-arc construction: pill↔pill, pill↔anchored-disc, and anchored-disc↔anchored-disc. Wherever the picker would otherwise emit a straight fallback for one of those types, the S-curve is tried first.
- Pill↔unanchored-disc and unanchored-disc↔unanchored-disc stay out of scope. The former uses the asymmetric arc-then-straight construction (no symmetric S-curve variant fits), and the latter is unconstrained at both ends by design.
- The S-curve geometry only admits a valid solution when the two chosen tangents are parallel (`turn ≈ 0`); U-turn and sub-floor cases will trial the S-curve but produce no result and fall through to straight.

### Construction

Each S-curve is **point-symmetric** about the midpoint of its endpoints, composed of two arcs of equal radius `R` joined smoothly at an inflection point on the chord. Stubs `sA` and `sB` at the endpoints are allowed; for the point-symmetric variant they are equal (`sA = sB`).

The radius is determined by the geometry: in a local frame with the endpoint A at the origin, `tA` along `+x`, and B at `(L, h)` (where `L` is the along-tangent length and `h` is the lateral offset), `R = (L² + h²) / (4h)`. The two arcs sit on opposite sides of the chord.

### Caps and floors

- The per-mode `CURVE_MAX_RADIUS_M_BY_MODE` cap applies as today. Wider S-curves are clamped to the cap.
- `CURVE_MIN_RADIUS_M` (5 m) floor applies as today. An S-curve whose `R` falls below the floor is dropped and the connector falls back to straight.

### Picker integration

The S-curve is a last resort before straight: it is tried only when every single-arc tangent combination the picker would normally enumerate for the connector type (axial / perp at each pill tip, the 4 cardinals at each anchored disc) either returns None or returns the parallel-forward 2-point chord. When any single-arc combo produces a real curve, that curve wins under the existing per-combo rules (axial-default, `CURVE_PERP_PREF_RATIO` between perp and axial) and the S-curve is never tried.

Within the S-curve trial itself the picker enumerates the same tangent combinations the single-arc enumerated, builds an S-curve for each, and picks the one with the shortest total length. If no combination yields a valid S-curve (every combo violates the floor or dedups below 3 vertices), the connector falls back to straight.

### Sampling and dedup

Each of the two arcs is sampled at the existing `CURVE_TARGET_SAGITTA_M`-based chord pitch. The combined polyline is passed through the existing `CURVE_DEDUP_TOL_M` (0.5 m) dedup. A polyline that deduplicates below 3 vertices falls back to straight.

## Constraints

- Tighter-than-floor corners must still fall back to straight — the S-curve must not bypass the `CURVE_MIN_RADIUS_M` anti-wobble guard.
- U-turn (anti-parallel) fallback remains straight. The S-curve construction is point-symmetric and only admits a solution for parallel-forward tangents; an anti-parallel pair produces no S-curve and the connector falls through to straight, same as today.
- The point-symmetric form (`sA = sB`, equal-radius arcs) is the only S-curve variant in scope. General asymmetric S-curves (unequal stubs, unequal radii, free inflection-point tangent) are not.

## Status

Deferred. The need for S-curve fallbacks has diminished as other parts of the rendering pipeline have evolved; the straight fallback is acceptable for the cases that still trigger it. Kept on file in case future visual changes make smooth bridging of parallel-tangent connectors worth revisiting.
