# Close-zoom stop design

## Problem

We have three zoom levels of stop representation. Pill-zoom is finished, far-zoom dots are near done. Close-zoom (high zoom levels) previously had no dedicated design — it just inherited the pill-zoom rendering, which wastes the available screen space at z17+. At close zoom we have room to show platform-level information: which line stops where, in which direction, toward which destination, and what the actual extent of the station is.

## Requirements

### Activation and zoom bands

- Replaces pill-zoom rendering from z17 upward. Hard cut at the z16 → z17 boundary; no fade overlap.
- Three zoom bands with band-specific content. The arrow does **not** grow across bands (all bands are equally long in metres) — zooming in itself provides the extra pixels, which the higher bands spend on destination text while glyph heights (in metres) shrink:
  - **Band A (z17)**: very small pill-arrow, solid in the line color with a white border, containing only the centered line number. No disc, no destination.
  - **Band B (z18)**: slim single-line pill-arrow — white body, border in the line color, colored disc at the round end with the line number, one line of destination text.
  - **Band C (z19+)**: same footprint as band B, destination may wrap to two lines; smaller glyph height (in metres) so the zoom gain is absorbed.
- Band switches are hard cuts at integer zooms (A→B at z18, B→C at z19).

### Pill-arrow shape

The visual element placed beside the line for each line+direction at a stop.

- Rounded rectangle ("pill") where the going-to end is replaced by a chevron tip — only the tip is pointed (a `>` shape); the opposite end stays rounded.
- The body is **curved**: it follows the line geometry (offset parallel to it), bending with the line where necessary. Never a rigid straight box on a curved line.
- Chevron tip points in the direction of travel away from this stop.
- The occupied length (back cap + body + tip) is exact — stacking math accounts for the rounded cap's bulge.
- Geometry must render crisply through z22 (dense vertices, no tile simplification of the shapes).
- **Duo-tone design (bands B/C)**: white body, border in the line color, a disc in the line color at the round end containing the line number, destination text in black on the white body.
- **Solid design (band A)**: whole pill in the line color, white border, line number only.
- Line numbers are always bold, white, and sized to fit their container.

### Text

- Destination text is left-aligned within its text region (anchored at the disc side; at the chevron side when the label is flipped for readability). Labels rotate with the pill axis and flip 180° when they would render upside-down.
- The text region grants slightly more clearance on the disc side and extends slightly into the chevron base on the arrow side.
- Glyph heights are defined in metres and convert to pixels on the map's zoom curve, so text scales exactly with the pill geometry and can never overflow it. Uniform size within a band — no per-label shrinking.
- Line breaks are computed at build time (baked into the label). Words longer than a line are **abbreviated with a single dot** — no hyphen splitting, since without linguistic hyphenation the break positions would be nonsense. Text exceeding the band's line budget (B: one line, C: two lines) ends with an ellipsis.
- **Destination shortening**: if the destination begins with the current stop's city (comma- or space-separated — "Bern, …" or "Bern …" on a pill in Bern), the city prefix is stripped; the separator requirement keeps names like "Berneck" intact. If a comma remains afterwards, everything from the comma on is dropped ("Wabern, Tram-Endstation" → "Wabern"). A destination that is exactly the city name is never emptied.

### Pill-arrow placement

One pill-arrow per (stop, line, direction). Same-direction variants of a line at a stop collapse into a single pill-arrow listing all their (deduplicated, shortened) destinations. **Departures only**: a line's final stop is an arrival and gets no pill-arrow there.

- **Anchor**: always the snapped on-line stop position, shifted sideways by a fixed clear gap between the line and the pill's inner edge — a consistent visual gap everywhere.
- **Side of the line**: bus and tram always to the right in direction of travel; rail on the side the GTFS stop position snapped from.
- **Direction grouping**: pills at a stop heading the same way (within 45°) form one stack. Direction is derived from the line tangent at the stop, not from GTFS direction fields.
- **Shared path for parallel lines**: when lines in one direction group run on parallel but non-overlapping map geometries (e.g. tram + bus on the same street), every pill-arrow in the group follows the group's **rightmost** line, so they line up on one path.
- **Rail (train + mountain rack rail)**: stack centered on the platform middle along the track, fastest line furthest forward.
- **All other modes**: the fastest line's chevron tip lands exactly on the stop point; slower lines queue upstream behind it.
- **Stack gap**: about a tenth of the pill width between consecutive pill-arrows.
- **Sorting metric for "fastest"**: the per-line speed value (speed, not frequency).

### Station background polygon

One translucent polygon per parent station, sitting behind the transit lines (not just behind the stop layers).

- **Shape**: a rounded convex envelope ("hull") around everything the station comprises: all pill-arrow outlines, the line sections adjacent to them, and — for rail — the **full platform extent** along the track (the same platform-extent logic used by the platform debug overlay), plus a fixed outward padding. The outline only bulges outward; it never notches inward between platforms. Covering somewhat more than the exact union is fine.
- No overlapping shapes — exactly one polygon per parent station.
- **Color**: the serving line's color. When lines of several colors serve the station, a blend of all their colors stands in for a gradient (a true polygon gradient fill is not possible in the rendering engine).

## Constraints

- Mountain is not a special case: rack rail follows the rail rules; aerial and funicular follow the bus/tram-style rules.
- Band A→B→C sizes, fonts, margins, gaps, and the backdrop padding are seed-and-refine — tune after visual review, but keep the band tables in the pipeline and in the style generator in sync.
- The build-time text wrapping replaces renderer-side wrapping entirely — the renderer must not re-wrap.
- Do not reintroduce an `intercity` bucket or any other mode key when extending the renderer.
