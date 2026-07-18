# Popups

Click-popups on the map give a short human-readable summary of what was clicked. Three popups exist: **station popup** (a stop dot or pill), **line popup** (a transit line), and **close-zoom departures popup** (at z17+, showing scheduled departures for a stop). This document defines what each contains.

## Shared conventions

- Font: Saira across every popup.
- Opened on click, closed by clicking elsewhere or clicking a different feature.
- Click priority when features overlap: station > line. The close-zoom departures popup is triggered only in its own zoom band.
- Stop names appear in full — no shortening, even where the base map style abbreviates them for space.

## Station popup

### Layout

Three sections stacked vertically:

1. **Station name** — title, bold.
2. **Line badges** — colored badges, one per unique line (see dedup rule), each with a hover tooltip showing where the line runs. Label text is Saira ExtraBold (800). The list is collapsible / expandable — see below.
3. **Average departures per hour** — one metric line at the bottom of the popup.

### Expandable line list

A small chevron sits to the left of the badge row. Clicking the chevron (or the badge row itself) toggles between two states of the section:

- **Collapsed (default).** The badges flow horizontally, wrapping to fit the popup width. Hover tooltip on each badge carries the `A ↔ B` route.
- **Expanded.** The same list flips to vertical: one line per badge, with the route (same `A ↔ B` string) shown as plain text to the right. All badges take the same width (that of the widest label) so the route text aligns in a left-flush column.

The chevron rotates 90° when open. Popup width may grow to fit the widest route line. Hover tooltip stays enabled in both states.

### Average departures per hour

Sum of `f_weighted` across every line serving the station, where `f_weighted` is already the 60/20/20 core / evening / weekend weighted trips/hour per direction (see `frequency-weighted-line-scoring.md` and `multi-date-frequency-sampling.md`). Both directions of a line count separately — each direction produces its own departures.

Rendered as `Departures: **N**/h` — the literal "Departures: " prefix, the number bolded, then "/h". Number is a rounded integer, or one decimal below 10. Baked onto every stop feature at build time; no client-side computation.

### Line badge dedup

One badge per `(ref, mode)`. Both directions of one line merge into one badge. Same ref in different modes (e.g. tram 3 vs. bus 3) stay as separate badges even though the collision is rare in practice.

Badge label, color, and mode use the merged group's representative choice.

### Line tooltip

Format: `A ↔ B`, where A and B are the two termini of the line as seen from this station — one per direction downstream from the station.

- **Subsumption.** A line can have multiple variants that terminate in the same direction at different points. Drop any variant whose terminus is on the stop sequence of a longer-running variant in the same downstream direction. The subsumed variant contributes nothing to the tooltip. Example: bus terminates most trips at Schliern but some at Köniz Schloss (an intermediate stop on the way to Schliern) — the tooltip lists only Schliern.
- **Diverging termini.** If a downstream direction still has multiple distinct termini after subsumption (neither on the other's sequence), list them joined by ` · ` (middle dot). Example: `A · C ↔ B`.
- **Direction assignment.** "Downstream" is defined per station: for each variant serving the station, its downstream terminus is the last stop of the variant's stop sequence relative to this station's position in that sequence. Variants group into two sides by which downstream terminus they head toward; the tooltip's two ends of `↔` are those two groups.
- **Identity for matching.** "Same station" throughout the tooltip logic (locating this station in a variant's sequence, deciding whether a terminus is on another variant's sequence) is matched at the parent-station / merged-UIC level, not by full stop_id with platform suffix. Different platforms of one station are treated as the same location.
- **Loops and one-direction lines.** If only one side has termini (loop, aerial, funicular's single downstream), the tooltip drops the `↔` and shows just the terminus list.

### Far-zoom dot behaviour

Same layout applies. When a dot absorbs other stops (see `stops-far-zoom-dot-redesign.md`), the departures-per-hour and line badges reflect what is absorbed at the current zoom — the same principle as today's `lines_json_zN` per-zoom popup content. A parallel per-zoom departures-per-hour value accompanies the per-zoom lines list.

## Line popup

TBD — spec to follow. Shares the Saira font and click-open behaviour above.

## Close-zoom departures popup

TBD — spec to follow. Triggered at z17+ only. Shares the Saira font and click-open behaviour above.

## Constraints

- Per-line terminus info is not currently baked onto stop features. It must flow from step 06 (which knows the stop sequences from `line_stops.json`) through `line_lookup` and onto every stop feature so the tooltip is buildable client-side without extra fetches.
