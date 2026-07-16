# Stop-extent OSM walk (rail, tram, bus)

## Problem

When a stop's platform extent needs more range than the trip polyline provides, the pipeline must obtain the missing geometry from somewhere. Rail already solves this well: it walks the OSM rail way under the stop and extends both the extent and the drawn line along the real track. Buses and trams have no equivalent — after the borrow tiers fail they extrapolate dead straight, cutting across buildings wherever the street curves (canonical case: Feldis/Veulden, Dorfplatz). This concept holds the OSM-walk mechanism for all ground modes: the implemented rail walk (moved here from `stops-pill-zoom.md`) and its generalization to streets and tram tracks.

## Requirements

### Way networks

- **Rail** (train, mountain rebucketed_rail / rack) walks `data/osm/rail_ways.geojson` — `railway=rail,light_rail,narrow_gauge`. Existing artifact.
- **Trams** walk a new `data/osm/tram_ways.geojson` — `railway=tram` plus `railway=light_rail` (shared corridors; low relevance in Switzerland but harmless) plus `railway=narrow_gauge`: tram-classified lines can leave the city tram grid and continue on their own narrow-gauge line (the Forchbahn — a tram within Zürich, an S-Bahn-style narrow-gauge railway beyond), so their terminals can sit on narrow-gauge track.
- **Buses** (bus, regional_bus) walk a new `data/osm/street_ways.geojson` — highway classes a bus can drive: motorway, trunk, primary, secondary, tertiary, residential, unclassified, service, living_street, bus_guideway, and the corresponding `_link` classes. Each way carries its highway class and name (both feed the same-street rule below).

### Extraction (pipeline step 03)

The rail extract exists; the two new artifacts follow the same processing pattern: per-country cut, way-ID dedup on merge, atomic `.tmp` rename, idempotency tiering so a stale-only extract reruns without redoing the slow merge. The GeoJSONs are pipeline artifacts (refreshed whenever step 03 runs), not checked in. Step 07 loads them and builds a spatial index for nearest-way lookups; it does not parse the PBF directly.

The two new artifacts are additionally clipped at extraction time to buffers of `streets_stop_buffer_m` (default 150 m) around all GTFS stop coordinates — only stop surroundings are ever walked, and unclipped street data would be orders of magnitude larger than the rail extract.

### Rail walk (implemented; moved from `stops-pill-zoom.md`)

Applies to train and mountain `rebucketed_rail` / `rack`. When the polyline does not extend ±L/2 around the snapped coord `p`, the clipped side's missing arc-length is filled by the first of the following that succeeds. The rule is separate from the tram / bus borrow tiers (documented in `stops-pill-zoom.md` § missing-range fill) because these modes typically sit on a single track with no good borrow candidate, but do have unambiguous OSM rail geometry under `p` to walk along.

1. **OSM rail walk.** At `p` with tangent `T` (the polyline's last-segment tangent at `p`), identify the OSM rail way under the polyline by combining proximity (within `osm_match_radius_m` of `p`) and tangent alignment (within `osm_match_max_tangent_diff_deg` of `T`, mod π). When a way matches, walk it in the clipped direction (away from where the polyline is) for the missing arc-length and use that segment as the fill. At interior nodes and junctions, the walk continues along the outgoing edge whose tangent best matches the incoming direction.
2. **OSM way runs out.** When step 1 matches a way but the way reaches its end before the missing arc-length is fulfilled, treat the walked partial as the actual end of the platform. Prepend the walked portion to the polyline — do not fabricate any extension past OSM's true end. The extent then covers the full L, split as `x` metres outward (whatever was walked) plus `L − x` metres inward from the snap. Train polylines are always longer than the platform, so no truncation fallback is needed. `p` is then no longer at the centre of the range — an explicit exception to the centred-anchor rule. Close-zoom pill-arrow placement further exploits this case (see `stops-close-zoom.md`): the rail stack anchors the fastest pill-arrow at the extent's buffer end instead of centring on its middle.
3. **No matching OSM way.** When step 1 finds no way satisfying both gates, fall back to a straight-line extrapolation in the polyline's tangent direction at `p`, with the fill length capped at `osm_fallback_max_straight_m` (`50 m`). The polyline side is **not** extended to compensate, so the total range length can be less than L in this case.
4. **No fill.** If the polyline is too short to compute a usable tangent, the range is left as whatever on-polyline geometry exists (possibly collapsed).

The tangent gate disambiguates parallel tracks at multi-platform stations: the way physically closest to `p` is often a neighbouring platform's track running parallel to the line of travel, and only the matching-tangent way represents the trip's actual track.

When multiple lines terminate at the same physical platform, each independently walks the OSM way under its own pfaedle endpoint. The same OSM way is the natural match for every line snapping into one platform, so the resulting line extensions visually coincide without explicit cross-line coordination.

### Walk tier for tram / bus / regional_bus (new)

New final tier of the tram/bus missing-range fill, after the sibling and non-sibling borrows, **replacing the straight-line tangent extrapolation, which is removed entirely**. Way match at the anchor `A`: proximity within `road_match_radius_m` (default 5 m) and tangent alignment within `road_match_max_tangent_diff_deg` (default 45°, mod π) against the anchor direction `E` — deliberately wider than rail's 15°, because road vehicles turn much tighter than trains and the anchor tangent can be skewed by an imminent turn.

The cases below are numbered to match the rail walk's — same triggers, mode-specific outcomes:

1. **OSM street/tram walk.** When a way matches, the walk proceeds backward (the `−E` side) for the missing arc-length. At junctions it **stays on the same street**: it continues onto a way only when that way has the same or a similar road class ("similarly sized") and its direction continues the incoming direction; a mere name change does not break continuation. The walk never turns onto a clearly different-class way or one that diverges sharply — reaching such a junction ends the walk (case 2).
2. **Way runs out.** If the network ends — or the same-street rule stops the walk — before the missing arc-length is filled, the extent keeps the walked portion and ends there; the extent may end up shorter than L. (Rail instead drops the fill and absorbs the full L on the polyline side; buses and trams have nothing to absorb into — the polyline already ends at the anchor.)
3. **No matching OSM way.** Nothing is appended — no straight fallback of any kind (rail keeps its capped 50 m straight here). The extent stays whatever on-polyline geometry exists. Short-but-true beats long-but-wrong.
4. **No fill.** If the polyline is too short to compute a usable tangent, the extent is left as whatever on-polyline geometry exists (possibly collapsed) — same as rail.

### Fill target (tram / bus / regional_bus)

How much backward range the fill must produce. On-polyline rear ground counts toward the target; the fill only tops up the difference.

- **Tram**: the full platform length L (default 35 m), fixed. A fixed-length track extension reads fine at tram terminals without turnaround loops (Geneva's stub terminals), and a need-based target would buy nothing visually.
- **Bus / regional_bus**: `min(stack need, L)` — extending a street by the full 30 m when a single pill-arrow needs half of that looks overdone on the map, so the extension is exactly as long as the close-zoom display requires, capped at L. The **stack need** is the ground the stop's pill-arrow queue actually occupies at the largest close-zoom band: `n × pill length + (n − 1) × gap`, where `n` is the exact number of pill-arrows drawn at the stop. `n` must be produced by the same grouping rules the close-zoom construction applies (departures only, layover skip, direction grouping, same-line-same-direction collapse) — shared logic, so the count cannot drift from what is actually drawn.
- **Debug extent = extension.** The debug platform extent is whatever ground exists after the fill — no separate full-L geometry is kept, so the debug line and the drawn line always end together.
- What the target deliberately does not cover — queues later pooled longer by same-curb merging (which runs on finished geometry), or a stack need above L — is handled by the terminal platform stretch (`stops-close-zoom.md`), which shifts the queue forward onto existing line geometry.
- Rail and metro are unaffected.

### Line-shape extension

Whenever an extent gains geometry beyond the trip's own polyline — from the sibling borrow, the non-sibling borrow, or the OSM walk — the same geometry is also appended to the trip's rendered line polyline at that end. The drawn line must always reach the platform ground its extent covers. Per case: rail extends the line in cases 1 and 3 (full walk segment or capped straight) and by case 2's walked portion (whatever OSM had before running out); tram/bus extend the line in case 1 and by case 2's walked portion, and never invent straight geometry.

### Fill runs once, upfront

The whole tram/bus fill — sibling borrow, non-sibling borrow, OSM walk, and the resulting line-shape extension — runs **once per terminal stop, early in the stop-building step, before any extent consumer**, exactly where the rail walk already runs. The line-shape extension must happen exactly once per line, and every extent consumer (debug overlays, pill construction, close-zoom, backdrop) must see the same geometry; computing the fill lazily per consumer makes the polyline mutation fragile (double-extension risk, consumers disagreeing), while computing it once upfront makes that structurally impossible — downstream, extents find the full range on the polyline and become plain slices with no fill logic, same as rail today. Avoiding the repeated recomputation across consumers is a side benefit.

### Diagnostics

The offender diagnostic script gains a second output: a link list of stops whose extent ended up **shorter than its fill target** (see § Fill target) because neither borrow nor walk could fill the full range (`pill_shortened.txt` alongside `pill_offenders.txt`), for manual review after the change.

## Constraints

- The rail walk's behavior is not modified by this concept — only its documentation home moved here; `stops-pill-zoom.md` points here instead of duplicating it.
- Metro's symmetric extrapolation is unchanged. Ferry and aerial are unaffected.
- Borrow tiers keep priority for tram/bus/regional_bus: sibling borrow → non-sibling borrow → OSM walk. The walk only runs where both borrows fail. The borrow tiers themselves remain documented in `stops-pill-zoom.md`.
- The ~25 manually verified offenders (unmarked entries in the first 50 of the annotated offender list) all have a street or tram track under the former straight extension — the walk must succeed at these; an extent coming up short there is a bug, not an acceptable outcome.
- Performance must stay negligible: extraction is cached with step 03's idempotency, and the step-07 walk runs only for the few hundred stops where borrows fail.
- The close-zoom pill-arrow consequence of shorter extents (terminal stacks longer than the platform line) is covered in `stops-close-zoom.md`, not here.
