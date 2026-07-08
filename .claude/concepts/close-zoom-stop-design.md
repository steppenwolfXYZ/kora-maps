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
- The body is **straight**, with a per-pill axis: each pill-arrow derives its angle from the **stop position line at its own segment** (see Placement). A single-point tangent at the stop was tried first and tilted whole stacks against the line near bends; backward extrapolation of the raw line geometry at terminals produced guesswork angles. Curved bodies were also tried and retired — labels cannot follow the curve, so text and shape angles diverge on bent lines.
- Chevron tip points in the direction of travel away from this stop.
- The occupied length (back cap + body + tip) is exact — stacking math accounts for the rounded cap's bulge.
- Geometry must render crisply through z22 (dense vertices, no tile simplification of the shapes).
- **Duo-tone design (bands B/C)**: white body, border in the line color, a disc in the line color at the round end containing the line number, destination text in black on the white body.
- **Solid design (band A)**: whole pill in the line color, white border, line number only.
- Line numbers are always bold, white, and sized to fit their container.

### Text

- Destination text is left-aligned within its text region (anchored at the disc side; at the chevron side when the label is flipped for readability). Labels rotate with the pill axis and flip 180° when they would render upside-down.
- The text region grants slightly more clearance on the disc side and extends slightly into the chevron base on the arrow side.
- Glyph heights are defined in metres and convert to pixels on the map's zoom curve, so text scales exactly with the pill geometry and can never overflow it. Uniform size within a band, with one exception: wide line numbers (e.g. "IR15") shrink per feature just enough to fit the disc; short numbers keep the band's nominal size. *(Pending re-implementation after the git restore.)*
- All text fitting (wrapping, word abbreviation, ellipsis placement) measures real per-character advance widths from the baked Noto Sans metrics table (`glyph_widths.json`, regular for destinations / bold for numbers, kerning ignored) — no flat character counting. The two-line ellipsis is placed after computing the actual line break, trimmed until the last line truly fits.
- Line breaks are computed at build time (baked into the label). Words longer than a line are **abbreviated with a single dot** — no hyphen splitting, since without linguistic hyphenation the break positions would be nonsense. Text exceeding the band's line budget (B: one line, C: two lines) ends with an ellipsis.
- **Destination shortening**: if the destination begins with the current stop's city (comma- or space-separated — "Bern, …" or "Bern …" on a pill in Bern), the city prefix is stripped; the separator requirement keeps names like "Berneck" intact. If a comma remains afterwards, everything from the comma on is dropped ("Wabern, Tram-Endstation" → "Wabern"). A destination that is exactly the city name is never emptied.
- **Loop-line destinations**: for lines whose first and last stop are the same station (loop lines), the terminus is useless as a destination — at the terminus every pill would name the very stop it sits at (canonical case: Bad Zurzach buses 1–4, all loops from Bahnhof, all showing "Bahnhof" at Bahnhof). Instead each loop gets an **apex stop**: pills at stops **before** the apex show the apex as destination; pills at the apex and all later stops show the terminus. The apex is the stop with the **highest station importance score** (the same per-station score that drives far-zoom dot tiers) within the **middle third of the stop sequence**; on a tie or missing scores, the stop closest to the sequence midpoint wins. The chosen apex name goes through the normal destination-shortening rules. Non-loop lines are unaffected.

### Pill-arrow placement

One pill-arrow per (stop, line, direction). Same-direction variants of a line at a stop collapse into a single pill-arrow listing all their (deduplicated, shortened) destinations. **Departures only**: a line's final stop is an arrival and gets no pill-arrow there.

- **Layover-departure dedup**: a line's first stop gets no pill-arrow when that stop has no platform code and the same line calls again at the same station (UIC) at a platform-coded stop later in the run — the platform-coded call is the real departure and keeps the pill. This is the departure-side mirror of the pill/dot arrival-drop rule; canonical case is Bern bus 30 departing the bare layover point and then serving platform A of the same station. The dot-side departure-skip rule (skip whenever a sibling arrives at the same stop_id) must NOT be copied here — close zoom has no arrival pills to fall back on, so it would erase lines from their termini.
- **Stop position line**: the queue's backbone is the stop's position line — **re-used** from the stop/dot placement, not recomputed: the same fitted-to-the-line slice of real geometry that the debug stop lines draw (for rail, the platform extent). That logic automatically finds the line whose geometry actually covers the stop's ground — at a terminal it draws the stop along the **arrival** line, whose geometry approaches along the street and ends at the stop, so the slice covers exactly the ground behind the stop where the departing queue stands. Nothing is ever extrapolated. Each pill-arrow derives its angle from the stop position line **at its own segment** (the average direction of the part its span covers). Where the stack extends beyond the range, it continues **dead straight**: each pill takes the angle of the last pill-arrow that fit and continues in that direction. At the standard bus/tram platform length about 3.5 pill-arrows fit, enough for ~90% of stops — straight continuation is the exception. Angles are never derived from single-point end tangents or from backward extrapolation of the raw line geometry.
- **Offset measurement**: pills sit beside the line, shifted sideways by the fixed clear gap plus half the pill width. Stepping, spans and gaps are measured along that sideways-shifted course, not along the centerline — measured on the centerline, every degree of bend stretches the gaps on the outside of a curve and squeezes them on the inside.
- **Anchor**: derived from the **stop position line alone** — the raw GTFS stop coordinate is never consulted for placement (it only feeds the stop-line computation itself, the direction tangents, and the rail side rule). Road-mode queues anchor the lead pill's chevron tip at the line's **forward end** (the vehicle pulled fully forward — canonical case: the PostAuto station sawtooth bays in Bern); rail stacks center on the line's **middle**. The line's orientation is its own point order (the travel direction of the line it was sliced from), reversed only when that clearly opposes the group's travel direction. Pill-arrows are always parallel to the debug stop line and sit on it — even when the underlying geometry is wrong (broken routing, e.g. Agno), they are wrong together, consistently.
- **Side of the line**: bus and tram always to the right in direction of travel; rail on the side the GTFS stop position snapped from.
- **Direction grouping**: pills at a stop heading the same way (within 45°) form one stack. Direction is derived from the line tangent at the stop, not from GTFS direction fields.
- **Same-curb resolution (non-rail)**: same-direction groups of the same parent station (UIC) can end up on the same ground under different GTFS platform ids — canonical case Bern, Schanzenstrasse, where southbound city bus 20 stops at platform `:10001` and southbound regional 100/101 at `:10000`, a few metres apart on one curb. Whenever two such groups' stop position lines run laterally closer than 2 m, they are resolved by their along-line overlap, measured against the shorter line:
  - **Overlap > 30%** → merge: one stop position line covering the **union** of both ranges, and one stop — the visits pool and the standard collapse / rightmost-line / stacking logic runs on the merged set. Merging is transitive (chains all merge) and runs before the shortening pass.
  - **Overlap ≤ 30%** → both stop position lines are shortened **symmetrically to the middle of the overlapping section**, so they just touch. If a queue no longer fits in its shortened range, it extends **the other way**: the stack may overflow forward past the line's front end — pills in front of the stop are better than pills overlapping the neighbour. This is the one deliberate exception to the tip-at-the-front anchor rule.
  - Trains are excluded for now (rail keeps strictly per-platform queues); rail has more complex overlap situations to be solved later.
- **Shared path for parallel lines**: when lines in one direction group run on parallel but non-overlapping map geometries (e.g. tram + bus on the same street), every pill-arrow in the group follows the group's **rightmost** line, so they line up on one path; the group's stop position line is taken from that line. (A previous rule preferring lines whose geometry covers the stack's span is obsolete — the stop position line is always fitted within real geometry, so terminal lines no longer produce guesswork axes.)
- **Rail (train + mountain rack rail)**: stack centered on the middle of the stop position line (the platform), fastest line furthest forward.
- **All other modes**: the fastest line's chevron tip lands at the forward end of the stop position line; slower lines queue upstream behind it.
- **Stack gap**: about a tenth of the pill width between consecutive pill-arrows.
- **Sorting metric for "fastest"**: the per-line speed value (speed, not frequency).

### Station background polygon

One translucent polygon per parent station, sitting behind the transit lines (not just behind the stop layers).

- **Shape**: a rounded convex envelope ("hull") around everything the station comprises: all pill-arrow outlines, the line sections adjacent to them, and — for rail — the **full platform extent** along the track (the same platform-extent logic used by the platform debug overlay), plus a fixed outward padding. The outline only bulges outward; it never notches inward between platforms. Covering somewhat more than the exact union is fine.
- No overlapping shapes — exactly one polygon per parent station.
- **Color**: the serving line's color. When lines of several colors serve the station, a blend of all their colors stands in for a gradient (a true polygon gradient fill is not possible in the rendering engine).

## Retired approaches (do not retry without new renderer capabilities)

Three alternative rendering architectures were fully implemented and reverted (July 2026):

- **Curved bodies** — polygon following the line geometry. Labels cannot curve with the shape, so text and body angles diverge on bent lines; replaced by straight bodies on one shared axis per stack.
- **Sprite/symbol pills** — pixel-fixed pills as SDF icons with slot-quantized offsets. Blockers: vector tiles cannot carry per-feature arrays (2D offsets must be quantized into match-expression slot tables); MapLibre multiplies icon-offset by icon-size and text-offset by text-size, silently rescaling tuned gaps whenever size curves change; pixel-fixed pills slide across the map while zooming (deep stacks move wildly relative to their stop).
- **Line-rendered bodies** — body as a stroked LineString (metre length × pixel height via line-offset), disc/tip/labels as symbols. The rotation-frame behavior of symbol offsets vs. icons proved unreliable in practice: misaligned discs and text, detached tips.

Renderer facts learned: `line-offset` and rotation values are per-feature numerics and safe to data-drive; array-valued layout properties are not; a real polygon gradient fill does not exist; per-character advance widths for build-time text measuring live in `glyph_widths.json`.

## Constraints

- Mountain is not a special case: rack rail follows the rail rules; aerial and funicular follow the bus/tram-style rules.
- Band A→B→C sizes, fonts, margins, gaps, and the backdrop padding are seed-and-refine — tune after visual review, but keep the band tables in the pipeline and in the style generator in sync.
- The build-time text wrapping replaces renderer-side wrapping entirely — the renderer must not re-wrap.
- Do not reintroduce an `intercity` bucket or any other mode key when extending the renderer.
