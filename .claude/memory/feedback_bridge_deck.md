---
name: Bridge deck rendering lessons
description: Hard-won lessons about bridge deck rendering in MapLibre GL — what works and what doesn't
type: feedback
---

One unified `bridge-deck` line layer is the only workable approach. Do NOT split into per-class deck layers.

**Why:** Per-class deck layers (one per road type) render before each road fill, leaving the road fill covering the center — producing a hollow "donut" effect. Multiple features on the same bridge (e.g. rail + footpath) create overlapping or gapped shapes.

**How to apply:** Keep exactly one `bridge-deck` layer covering all `brunnel=bridge` transportation features. Width must be wide enough to be visible (1.5px min at far zoom, ~15m at close zoom). No `maxzoom` — the deck must be visible at all zoom levels.

**MapLibre constraint:** `["zoom"]` expression must be the direct input to a top-level `interpolate` or `step`. It cannot be nested inside `["max", ...]` or other math expressions. Use multiple stops in the interpolate instead.

**Unavoidable limitation:** In 2D vector tile rendering there is no elevation data, so it's impossible to place the deck only between "roads above the bridge" and "roads below the bridge". The layer order workaround (normal roads before deck, bridge roads after) means roads approaching the bridge at grade will render on top of the deck and show a small flange. This is acceptable and cannot be fixed without 3D/elevation data.

**Per-class deck layers were tried and reverted multiple times** — do not attempt again.
