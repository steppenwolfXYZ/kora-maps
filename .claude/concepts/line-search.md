# Line text search

## Problem

The transit-focus search finds stops but not lines. Jumping to a known line by its number (`S9`, `IC8`, tram `10`) requires panning and zooming from memory, then clicking a badge — no direct entry point.

## Requirements

### Visibility and scope
- Line hits appear in the **same search input** as stops. No separate line-only search UI, no mode toggle.
- Same visibility rules as the stop search: transit-focus view only.

### Search index
- A dedicated **line search index**, separate from the stop search index and separate from the line index consumed by the line detail view. Built at pipeline time and shipped as a static JSON asset.
- **Granularity: one entry per `line_key`** — the same grouping the map uses. If a `ref` maps to multiple `line_key`s (e.g. two disjoint `S10` corridors, PostAuto bus `100` in different regions), each is its own entry and each can show up separately in results.
- Each entry carries: the `ref` (the searchable string), the `mode`, the `line_key`, the line color, the route text (end destinations, same string popups use, e.g. `A ↔ B` or `A · B · C`), and the `line_bbox`. Nothing else — this is a search-and-jump index, not a detail-view payload.
- Only lines that are actually drawn appear. Lines dropped upstream (freq gate, filtered agencies, EV routes, etc.) do not appear.

### Match behavior
- Case-insensitive matching against `ref` only. The route text is displayed but is not searched.
- **Exact match only.** `S1` must match only lines whose `ref` folds to `s1`; it must not match `S10`, `S11`, `S12`. No prefix, no substring, no fuzzy.
- Diacritic-insensitive fold applies (consistent with stop search) even though refs are typically ASCII.
- Multi-token queries: line matching is single-token. A query with whitespace does not produce line hits (stops may still match under their existing multi-token rules).

### Ranking against stops
- Line hits and stop hits share one ranked result list. There is no separate section for lines.
- Line hits slot **below high- and mid-tier stop hits, above low-tier stop hits**. Concretely: an exact line match should outrank a low-quality stop hit (e.g. a substring-only match on `small_bus`) but must not displace strong stop hits (a name-prefix match on a major station).
- Achieved by giving a valid exact line hit a fixed match-quality score sitting between the mid stop tiers and the low ones (starting value around 35 on the 0–100 scale, deliberately between stop tiers "some words full match" (40) and "all word-prefix" (30)). Exact value is a starting point; expected to be tuned after observing behaviour.
- Line hits also carry a **mode** signal (same normalisation as stops, drawn from the same `MODE_RANK`) so `S9` train ranks above bus `9` on the same query.
- Line hits carry **no stop-tier signal** (not applicable) and **no distance signal** in the first iteration. Distance can be added later using the line bbox centroid or nearest-edge distance; deferred.
- Cap on total results (stops + lines combined) stays as it is for the stop search; lines compete for those slots.

### Result row
- A line row is visually distinguishable from a stop row so the user knows what they are picking, but sits in the same list.
- Left side: a **line badge** — the coloured rounded rectangle with the ref in Saira ExtraBold, using the same badge styling as popups and the line detail view title bar.
- Right side: the **route text** as the label (end destinations, e.g. `Zürich HB ↔ Uster` or `A · B · C`). This is what disambiguates multiple `line_key`s sharing the same `ref`.
- No mode icon column on line rows (the badge colour already carries the mode's visual identity).

### Selection
- Selecting a line row (Enter on top result, click on any row) **enters line detail view for that `line_key`** — identical behaviour to clicking the line's badge in a popup: the deep-link URL param is set, the camera fits the line's bbox, the title bar appears, all detail-view visual state is applied.
- After selection, the dropdown closes.

## Constraints

- The line search index is regenerated whenever the transit pipeline runs; never maintained separately.
- Search is entirely client-side. No external service.
- Nothing outside transit-focus view depends on this feature.
- The stop search index and its ranking are unchanged by this feature — line hits are additive.
- No new identity model. `line_key` remains the canonical line identity (`ref~agency_id~mode~trip_group_id`).
