# pfaedle Stop-Side Awareness

## Problem

pfaedle models a stop as a directionless point on a street centerline. Which side of the street the platform sits on dictates the only physically possible direction of travel for serving it (right-hand traffic), but pfaedle's cost model cannot see sides — so when two route alternatives are near-tied, it may thread a trip through a stop in the impossible direction and then "repair" the path with U-turns or back-street loops. Canonical case: bus 17's 2026 construction diversion at Bern Brunnhof platform A — served northbound in reality, drawn southbound with a nonsense loop via Mattenhofstrasse/Lilienweg. Temporary diversions are extra-exposed because they have no OSM route relation, so no line prior corrects the choice. This failure class has produced issues before and will recur.

## Requirements

### Phase 1 — direction-violation diagnostic (impact assessment, then benchmark)

> Amended after the first implementation round: detection via the geometric *side* of the GTFS coordinate (left of travel) proved unusable — the signal is a few metres, the same magnitude as quay-coordinate and OSM-centerline imprecision, so the side's sign is noise. Detection is now **pure directionality** against the Atlas per-quay `compassDirection` attribute (the direction the quay is served in), which the pipeline already downloads. Validated network-wide: the bearing-difference distribution is strongly bimodal (84% of passages within 15°, a clean second cluster at 180°, sparse in between), and it detects the canonical Brunnhof shape (travel 177° vs quay 357°) while staying quiet on correct lines.

- A diagnostic that detects, for every drawn road-mode line feature (bus, regional_bus, tram), each stop passage where the shape's travel bearing at the stop **opposes the quay's Atlas `compassDirection`** — angular difference at or above a configurable threshold, knob `stop_side_diagnostic.min_bearing_diff_deg` in the pipeline config (sits below the 180° wrong-direction cluster with headroom for angled terminal bays).
- Stops without a defined `compassDirection` are ignored (no directionality to test against; roughly two thirds of passages).
- Works purely from existing pipeline outputs (line geometries, per-feature stop sequences, Atlas stop attributes) — no pfaedle re-run required.
- Additionally excludes, with per-class counts reported: stops outside Switzerland (UIC prefix ≠ 85 — lines partly outside the OSM bbox, out of scope for now) and quays that appear in both directions of the same line (the feed frequently registers a single quay — and its compassDirection — for both directions, so the opposite direction of a correctly drawn line always reads ~180°).
- Output `data/transit/stop_side_violations.json`: one record per violation with line ref, agency, line_key, stop id + name, travel bearing, quay compassDirection, and angular difference; plus a per-line summary sorted by violation count. A companion link list (map deep links per violation) for manual review, following the established offender-list pattern.
- Known limit: only the emitted representative shape per variant is checked — wrong shapes on non-representative trips (e.g. construction diversions that lose the rep-trip vote, the live Brunnhof case) surface in routing results but not in this count.
- Role: first run quantifies the real-world impact and surfaces unknown cases before any pfaedle work; after Phase 2 it is the regression benchmark — the violation count must drop, and no new violations may appear on previously clean lines.

### Phase 2 — wrong-side penalty in pfaedle

- Patch to the locally built pfaedle image (same fork-carrying pattern as the MOTIS fork) adding a penalty when a candidate path passes a stop with the stop's original coordinate on the wrong side of the direction of travel.
- Sign of the side is derived from the stop's original GTFS coordinate relative to the traversal direction at the matched position — data pfaedle already holds; no new inputs.
- New per-mode profile key `routing_wrong_side_penalty` (seconds), default 0 (= off, upstream behaviour unchanged). Enabled for bus and tram profiles only; rail unaffected.
- The penalty is **soft**: sized to break near-ties (the Brunnhof case) but to lose against structurally cheaper evidence — it must never force long detours to satisfy a side, because GTFS coordinates are occasionally on the wrong side themselves.
- Below the same small lateral-offset floor as the diagnostic, no penalty applies.
- Success criteria: the Brunnhof bus-17 diversion variant draws the correct shape (northbound through platform A, right turn onto Schwarztorstrasse); the Phase-1 violation count drops substantially across the network; spot-checked previously-correct lines are unchanged.

## Constraints

- Right-hand traffic is assumed everywhere (the coverage area is entirely right-driving).
- Shapes that are currently correct must not change; the penalty operates at tie-breaker magnitude.
- Stops legitimately passed in both directions by the same line (single-platform termini, loop stops, island platforms) must not acquire distorted shapes — the softness requirement covers this; the diagnostic will report them and they are acceptable known noise, not fix targets.
- The fix must not depend on OSM route relations (diversions have none).
- Upstreaming the patch to pfaedle is desirable but not a requirement; the local image build must not depend on upstream acceptance.
