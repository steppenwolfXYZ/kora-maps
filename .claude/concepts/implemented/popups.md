# Popups

Click-popups on the map give a short human-readable summary of what was clicked. Popups exist for the **station** (a stop dot or pill), the **line** (a transit line), the **pill-arrow** (z17+), the **place** (a POI or address reached from the search bar), and the **close-zoom departures popup** (at z17+, showing scheduled departures for a stop). This document defines what each contains.

## Shared conventions

- Font: Saira across every popup.
- Opened on click, closed by clicking elsewhere or clicking a different feature.
- Clicking a line badge (station popup or line popup) closes the popup and enters the line detail view for that line — see `line-detail-view.md`.
- Click priority when features overlap: station > line. The close-zoom departures popup is triggered only in its own zoom band.
- Stop names appear in full — no shortening, even where the base map style abbreviates them for space.
- Only one popup is open at a time. Popups are opened by clicking the map and — for the station and place popups — by picking a result in the main search bar (`stop-search.md` § Selection); either path replaces whatever was open.
- **Route from / to buttons.** The station, pill-arrow and place popups all end in the same row, which sets the routing panel's From / To to this location (`transit-routing.md` § Entry points). The whole control is one light-gray group pill: a larger `directions` route icon in anthracite, then a brand-red segmented pill whose two halves are split by a white hairline — no labels, wording in the tooltip only; hover darkens only the hovered half. The disc glyphs are the **play triangle** (from) and the **stop square** (to), the same pair the map's start and goal pins carry, so one start / stop iconography runs through map pins, popups and the map context menu.
- Line lists have a bounded height. The vertical badge / terminus list in the line popup and the expanded state of the station popup capped so a very busy station or corridor doesn't push the popup off-screen — the list itself scrolls, while the popup header (station name, departures per hour, chevron) stays in place.

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
- **Include self at terminals.** When the station is a terminus of any variant (line starts or ends here) and the station's own name isn't already in the downstream sides, add it as the missing side. Ensures both endpoints of the line always show up — bus terminals, aerial / funicular / ferry termini all read as `Other end ↔ Here` instead of just `Other end`.
- **Loops and one-direction lines.** If, even after adding self, only one side remains (rare — genuine one-direction line with no explicit self), the tooltip drops the `↔` and shows just the terminus list.

### Far-zoom dot behaviour

Same layout applies. When a dot absorbs other stops (see `stops-far-zoom-dot-redesign.md`), the departures-per-hour and line badges reflect what is absorbed at the current zoom — the same principle as today's `lines_json_zN` per-zoom popup content. A parallel per-zoom departures-per-hour value accompanies the per-zoom lines list.

## Line popup

Triggered by clicking on a transit line. Renders as a vertical list of lines, visually the same as the station popup's expanded line list: badge on the left (Saira ExtraBold), route text (`A ↔ B`) on the right, all badges the same width so the route texts left-align.

### Capture set

Multiple lines often overlap at the click point (parallel corridors, shared alignments). The popup collects every line rendered within a small pixel-radius bbox around the click, deduped by `(ref, mode)` so both directions of one line merge into a single row. Ordering matches the station popup: mode rank, then ref.

### Route text per line

For each `(ref, mode)` in the capture set, the two termini across all captured variants of the line become the `A ↔ B` string. If exactly two distinct terminus names appear across variants, format as `A ↔ B`. If more (branching variants terminate at different endpoints), list the unique names joined by ` · ` — no per-station subsumption is applied (there is no station reference here).

## Pill-arrow popup

Triggered by clicking a pill-arrow (the z17+ elongated stop labels with the line drawn through them). Represents a single (station, line) pair — a compact summary of "this line, at this station".

Two sections stacked:

1. **Station name** — title, bold, same as the station popup's title.
2. **Line row** — one row in the same visual grid as the line popup's list: badge on the left, route (`first ↔ last`) on the right.

Data flow: pill-arrow features carry `stop_name` (from the parent station's dot), `first_terminus_name` / `last_terminus_name` (from `line_lookup[osm_id]`), and the line-detail-view identity + camera fit (`line_key`, `line_bbox`) all baked at emission time. No client-side joins. Clicking the badge closes the popup and enters the line detail view for that line — same behaviour as the badges in the station / line popups.

Click priority at z17+: station-label click → full station popup (label bbox check). Pill-arrow click (outside any station-label text) → pill-arrow popup. Everything else → line popup.

## Place popup

Opened when the main search bar moves the map to a geocoded result — a POI or an address (`stop-search.md` § Selection). Deliberately minimal; there is no line or departure data to show.

Contents, stacked:

1. **Title** — the POI's name, or the address itself for an address hit. Preceded by a small kind icon (POI vs address), matching the icons the search list and the routing endpoint rows use.
2. **Address line** — the street address of the place. For an address hit the title already *is* the address, so no second line is drawn. For a POI the address is not part of the forward-search result: it is fetched by a reverse lookup at the POI's coordinate and filled in when it returns, so the popup appears immediately and gains the line a moment later. The reverse lookup follows `geocoding-search.md` § Reverse geocoding, which never yields a POI name — so the second line can never repeat the title.
3. **Route from / to buttons** — as described under Shared conventions. The endpoint they set is a `point` endpoint carrying the popup's title as its display name (no UIC — this is not a station).

## Close-zoom departures popup

TBD — spec to follow. Triggered at z17+ only. Shares the Saira font and click-open behaviour above.

## Constraints

- Per-line terminus info is not currently baked onto stop features. It must flow from step 06 (which knows the stop sequences from `line_stops.json`) through `line_lookup` and onto every stop feature so the tooltip is buildable client-side without extra fetches.
