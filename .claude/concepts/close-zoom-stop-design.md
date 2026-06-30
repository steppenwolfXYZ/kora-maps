# Close-zoom stop design

## Problem

We have three zoom levels of stop representation. Pill-zoom is finished, far-zoom dots are near done. Close-zoom (high zoom levels) currently has no dedicated design — it just inherits the pill-zoom rendering, which wastes the available screen space at z17+. At close zoom we have room to show platform-level information: which line uses which platform, in which direction, and what the actual extent of the station is.

## Requirements

### Activation

- Replaces pill-zoom rendering from z17 upward. Hard cut at the z16 → z17 boundary; no fade overlap.

### Station background polygon

A single transparent yellow polygon per station, sitting behind everything else at this zoom.

- **Shape derivation** — union of the following, then buffered with equal padding and rounded outer corners:
  - **Rail stations (train + mountain rack rail)**: the actual platform shapes plus the existing debug station/platform geometries.
  - **All other modes (bus, tram, aerial, funicular, ferry)**: the pill-arrows themselves plus the line segments adjacent to each pill-arrow.
- **Padding**: equal distance around every pill-arrow and every line section that sits next to a pill-arrow. Initial seed value picked to look right at z17; refine after first render. Scales with zoom on the same curve as the rest of the close-zoom layer.
- **Outer border**: very round corners.
- **Color**: fixed yellow regardless of which modes the station serves. The yellow is not mode-specific. Mode color appears only on the pill-arrows.

### Pill-arrow shape

The visual element placed on each platform for each line+direction.

- Rounded rectangle ("pill") where the going-to end is replaced by a chevron tip — only the tip is pointed (a `>` shape), the body's long sides remain straight and parallel. The opposite end stays rounded as a normal pill end.
- Chevron tip points in the direction of travel away from this stop.
- Pill background uses the line's mode color (follows existing per-mode color logic; no special case for mountain).
- Casing: white, per existing transit casing rule.
- Contents: the line's short name (or equivalent) and the trip destination.
- Destination text is truncated with ellipsis when it exceeds the pill's max width.
- Pill width is fixed but scales slightly with zoom — from "just readable" at the lowest active zoom to "comfortably readable" at the highest.

### Pill-arrow placement

One pill-arrow per (line, direction, platform).

- **Rail (train + mountain rack rail)**: positioned along the actual platform, centered on the platform's middle in the simple case. Multiple lines on the same platform stack along the platform axis.
  - When a platform serves both directions, the stacks fan outward from the platform center — fastest line at each outer end, slower lines toward the middle. Arrows from opposite directions never point at each other (sort from outside in).
  - When a platform serves only one direction, the stack sits with the fastest line forward (in direction of travel) and slower lines queued behind.
- **All other modes (bus, tram, aerial, funicular, ferry)**: the leading pill-arrow starts at the platform point and the stack extends upstream, parallel to the local line tangent.
  - Fastest line is in front (closest to the platform point in direction of travel); slower lines queue behind it.
- **Sorting metric for "fastest"**: the same speed value that drives line thickness (`width_base`). Speed, not frequency.
- **Atlas-missing fallback (rail)**: when a platform has no atlas data (`no_atlas_match`), fall back to the pfaedle-snapped stop position with the on-track offset rule below.
- **On-track offset**: when the pill-arrow's anchor sits exactly on the pfaedle track geometry (no atlas offset placing it off to the side), shift the pill-arrow to the right of the line in direction of travel. Offset distance is whatever leaves a clean visible gap between the pill-arrow and the line — no overlap, enough air to read as "next to" not "on top of".

### Identity per direction

Pill-arrows are emitted per `direction_key`. The same line through the same platform in opposite directions produces two distinct pill-arrows.

## Constraints

- No mode-specific special-casing on the yellow background — it is one fixed color across all stations.
- Mountain mode is not a special case in placement: mountain rack rail follows the rail platform rule; aerial and funicular mountain follow the bus/tram-style upstream-stack rule.
- Casing remains white for every mode, including mountain.
- Padding, on-track offset distance, and pill width zoom curve are seed-and-refine — exact values to be tuned after first render.
- Do not reintroduce an `intercity` bucket or any other mode key when extending the renderer.
