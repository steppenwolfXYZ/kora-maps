# Mountain Line Pills

## Problem

Mountain lines are excluded from the dot-pill-connector stop rendering system. They always render as a single dot per stop, even at major mountain stations where multiple mountain lines (or mountain + rail) share platforms. This makes mountain stations visually inconsistent with rail stations next to them — Lauterbrunnen renders rail platforms as pills but the co-located Wengernalpbahn platforms as loose dots; Brienz renders the lake-side train platforms as a pill but the Brienz Rothorn Bahn next to them as detached dots; cable-car cascades like Schilthorn (Mürren → Birg → Schilthorn) or the Zermatt aerial nexus (Furi / Trockener Steg / Klein Matterhorn) render every aerial junction as fragmented dots even though multiple lines meet at the same physical station.

The exclusion was introduced in the pill-rendering concept as an "out of scope" line. It was a placeholder while the rail/tram/bus rules were being designed, not a deliberate requirement.

## Requirements

### Scope by `mountain_origin`

The `mountain` bucket is heterogeneous. The rule splits by the `mountain_origin` property already attached to every mountain line feature in step 06:

- **`rebucketed_rail` and `rack`** — handled identically to **rail** (train). They run on physical rail platforms with real platform geometry; many already carry atlas `length` (the rack agencies — Jungfraubahn, Wengernalpbahn, Monte Generoso — supply real 75–287 m platform values). They participate in clustering, pill construction, and connector emission exactly as `train` does.
- **`funicular`** — centred ±L/2 anchoring with **smaller default and sanity values** (funicular platforms are short, ~20–40 m; atlas typically reports the cabin footprint 5–18 m). Two funicular-specific rules:
  - **Extent clipped to the polyline.** No straight-line tangent extrapolation. When the centred extent would reach a polyline endpoint, the asymmetric Fallback B anchor applies: the polyline side absorbs the full L (range = `[poly_max − L, poly_max]` or `[0, L]`). When the polyline is shorter than L, the extent is the full polyline.
  - **Dot pinned to polyline endpoint when extent reaches it.** Whenever the extent uses the asymmetric anchor above, the stop's drawn dot is pinned to that polyline endpoint instead of the GTFS-coord projection. So a funicular endpoint stop always renders at the end of the line rather than somewhere inside the platform extent.
  - **Non-rail clustering pool (50 m radius).** Funiculars do **not** join the rail clustering pool. Short funicular lines (Marzilibahn ≈ 108 m) routinely have both endpoint stops within the rail pool's 300 m radius, which collapses them into a single cluster — and because a single-line funicular cluster has `stop_count == 1`, the cluster renders as a centroid dot in the middle of the line instead of two endpoint dots. The 50 m non-rail radius keeps each endpoint as its own cluster while still co-clustering with adjacent tram/bus stops (Polybahn at Zürich Central, Rigiblickbahn at Zürich Seilbahn Rigiblick).
- **`aerial`** — **fixed dot** (see below). Aerial cable-car / gondola / elevator stations have no platform geometry and zero atlas length coverage. Each aerial stop locks its dot to the snapped GTFS coord and joins the pill pipeline with no extent freedom. Aerial features are additionally **exempt from terminus dedup**: at cascade stations where one section ends and another begins (Stockhornbahn lower → upper aerial at Chrindi, Niederhornbahn funicular → upper aerial at Beatenberg), the GTFS feed assigns a single bare UIC across both sections, but each section is a separate physical aerialway. One dot per (line geometry, stop_id) — an aerial feature is never dropped by terminus dedup, and an aerial arrival never causes a paired departure to be dropped either.

### The fixed-dot concept

A **fixed dot** is a stop whose dot position is locked to the snapped GTFS coord and never moves. It enters the cluster like any other stop, but participates in the dot-placement algorithm only as a fixed anchor:

- It has no extent polyline. `extent` is `None`.
- **Tangent grouping** — a fixed dot has no extent tangent. Any tangent-equality / angle check involving it is **considered failed**: it does not join any tangent group, and it cannot be a member of a bar-sweep group.
- **Perpendicular bar sweep** — fixed dots are skipped entirely from the sweep. They cannot be central members, scoring members, or covered members of a bar. Bars are only constructed across extent-bearing platforms.
- **Leftover fill** — a fixed dot is **pre-placed** at its locked position before the leftover fill runs. The fill never moves it. The fill still uses already-placed dots (including fixed ones) as anchors for placing the *remaining* extent-bearing leftovers.
- **NN-path, pill split, connectors** — fixed dots are vertices of the NN-path exactly like extent-placed dots. They can be interior pill vertices, pill endpoints, or singleton groups (rendered as unanchored discs).

A pill is formed from two or more dots in the cluster regardless of whether those dots are extent-placed or fixed. The existing gap-split rule (`PILL_GAP_STRAIGHT_M` / `PILL_GAP_ANGLED_M`) decides whether adjacent NN-path segments stay inside one pill or split into separate pills joined by a connector — the rule applies to fixed-dot–fixed-dot segments, fixed-dot–extent-dot segments, and extent-dot–extent-dot segments uniformly. Two fixed dots within `PILL_GAP_ANGLED_M` of each other on the NN-path therefore form a 2-vertex pill; further apart, they remain singleton dots joined by a connector.

When all positions in a cluster collapse to a single point (the common case for shared aerial UICs at Trockener Steg, Furi, etc., where multiple lines all use the same `stop_id` and snap to the same coord), the existing cluster-centroid-dot fallback applies — one dot stands in for the cluster across all zooms, the popup `lines_json` lists every contributing line. No pill is emitted because there are no distinct positions to span.

### New configuration keys

`pill_rendering.default_length_m`, `pill_rendering.sanity_min_m`, and `pill_rendering.sanity_max_m` gain two new per-mode-equivalent keys (aerial has no length config since it produces fixed dots only):

- **`mountain_rail`** — used for `mountain_origin in {rebucketed_rail, rack}`. Values match `train` initially: default 100 m, sanity 30 – 700 m.
- **`mountain_funicular`** — used for `mountain_origin == "funicular"`. Smaller values: default 25 m, sanity 10 – 60 m. The 10 m sanity minimum rejects the 5 m cabin-footprint atlas values; the 60 m maximum caps mislabels without truncating real platforms.

Atlas `length` values outside the sanity range fall through to the default, same as for every other mode.

### Anchoring rule for the extent-bearing mountain origins

For `mountain_origin in {rebucketed_rail, rack, funicular}` the GTFS coord is at the **platform centre**; the extent is `[snapped − L/2, snapped + L/2]`. Missing-side behaviour differs by origin:

- **rebucketed_rail / rack** — same as train: the polyline is pre-extended at terminal stops by `_extend_polylines_at_terminals` via the OSM rail walk (rack/rebucketed_rail tracks are `railway=narrow_gauge`, present in step 03's rail extraction). When the OSM walk reaches the end of the way (Fallback B: Brienzer Rothorn, Pilatus Kulm, Jungfraujoch and similar summit termini), the asymmetric anchor applies: polyline side absorbs the full L, no line extension. The line itself is also extended via the same walk so the drawn line reaches the platform.
- **funicular** — extent is clipped to the polyline; no extrapolation and no OSM walk (funicular tracks are `railway=funicular`, not in step 03's rail extraction). See the funicular bullet under "Scope by mountain_origin" for the full clip + endpoint-pin rule.

### Clustering

Mountain stops join one of two pill clustering pools based on `mountain_origin`:

- **Rail pool (300 m radius):** rebucketed_rail, rack, aerial. The 300 m radius is what makes the train ↔ rack co-cluster work at shared stations (Lauterbrunnen, Grindelwald, Brienz, Capolago, Sierre, Zermatt), where train platforms and rack platforms physically interleave. Aerial sits in the same pool so it co-clusters with rack at Eigergletscher (Eiger Express ↔ Jungfraubahn share UIC 8507361). Within-aerial co-clusters at Trockener Steg, Mürren, Bettmeralp Talstation, Pardiel etc. work uniformly here.
- **Non-rail pool (50 m radius):** funicular. The smaller radius keeps funicular endpoint stops distinct on short lines while still allowing co-clustering with tram/bus at the bottom-station case (Polybahn ↔ Zürich Central, Rigiblickbahn ↔ Zürich Seilbahn Rigiblick). The two obscure funicular ↔ rebucketed_rail co-cluster cases (VerticAlp at Brünig-area UICs) are forgone — the funiculars there render in the non-rail pool and the rebucketed_rail in the rail pool, with two slightly offset dots at each shared station.

### Straight-line-fallback aerial features

Aerial features whose pfaedle routing failed (route_type 1300 / 1303) are currently emitted via the `gtfs_stop_features` path in step 07: the line property carries a `gtfs_stops` array of GTFS coordinates and step 07 writes one point feature per coord. These features **also enter the pill pipeline as fixed dots**, with their positions locked to the GTFS coords. Each such stop becomes a fixed-dot candidate in the rail clustering pool, identical in shape to a pfaedle-shaped aerial stop. The straight-line geometry of the line itself is unchanged.

If a fallback-aerial stop coincides with a pfaedle-shaped aerial stop or any other mountain stop, normal clustering merges them. If it stands alone, it renders as a single dot with no pill — same visible result as today.

### Visual style

The new mountain pills, connectors, and disc endpoints render with the same white-fill / 1 px black-border style as the existing rail pills. The mode `color` attribute (mountain light yellow `#ffe566`) is still carried on the tile for any future style variant, but is not consumed by the default paint — consistent with every other mode's stop family today.

`MODE_RANK` is unchanged. At mixed train + mountain clusters, train still dominates colour selection (train rank 0 < mountain rank 4), so the pill renders in train red and the popup `lines_json` lists every mountain line that visits the cluster.

## Constraints

- Ferries remain out of scope for pills (unchanged from today). Only the mountain part of the existing "ferry and mountain are out of scope" statements is being lifted.
- `_extend_polylines_at_terminals` stays train-only. Mountain polylines are not OSM-walked, so terminal extents extrapolate straight rather than tracking the actual track curve. Acceptable for the first iteration; revisit if visible artefacts appear at e.g. Pilatus Kulm or Brienzer Rothorn.
- `MODE_RANK` is unchanged. Mixed train + mountain clusters keep train as the dominant mode.
- Aerial stops never move from their GTFS coord. The dot-coordination algorithm must treat them as pre-placed anchors, not as candidates for the sweep or fill placement.
- A fixed dot's "angle check failed" rule applies to any place in the dot-placement code that compares a stop's local tangent against another stop's tangent or a bar's tangent. The fixed dot is excluded — never grouped, never on a bar.
- The pill-rendering concept doc loses its two "mountain is out of scope" statements; they are replaced with a one-line pointer to this concept describing the new scope.
