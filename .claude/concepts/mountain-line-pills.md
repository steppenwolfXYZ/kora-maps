# Mountain Line Pills

## Problem

Mountain lines are excluded from the dot-pill-connector stop rendering system. They always render as a single dot per stop, even at major mountain stations where multiple mountain lines (or mountain + rail) share platforms. This makes mountain stations visually inconsistent with rail stations next to them — Lauterbrunnen renders rail platforms as pills but the co-located Wengernalpbahn platforms as loose dots; Brienz renders the lake-side train platforms as a pill but the Brienz Rothorn Bahn next to them as detached dots; cable-car cascades like Schilthorn (Mürren → Birg → Schilthorn) or the Zermatt aerial nexus (Furi / Trockener Steg / Klein Matterhorn) render every aerial junction as fragmented dots even though multiple lines meet at the same physical station.

The exclusion was introduced in the pill-rendering concept as an "out of scope" line. It was a placeholder while the rail/tram/bus rules were being designed, not a deliberate requirement.

## Requirements

### Scope by `mountain_origin`

The `mountain` bucket is heterogeneous. The rule splits by the `mountain_origin` property already attached to every mountain line feature in step 06:

- **`rebucketed_rail` and `rack`** — handled identically to **rail** (train). They run on physical rail platforms with real platform geometry; many already carry atlas `length` (the rack agencies — Jungfraubahn, Wengernalpbahn, Monte Generoso — supply real 75–287 m platform values). They participate in clustering, pill construction, and connector emission exactly as `train` does.
- **`funicular`** — handled like rail (centred ±L/2 anchoring, joins the rail-pill pipeline), but with **smaller default and sanity values** because funicular platforms are short (~20–40 m, with atlas typically reporting the cabin footprint 5–18 m).
- **`aerial`** — **fixed dot** (see below). Aerial cable-car / gondola / elevator stations have no platform geometry and zero atlas length coverage. Each aerial stop locks its dot to the snapped GTFS coord and joins the pill pipeline with no extent freedom.

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

For `mountain_origin in {rebucketed_rail, rack, funicular}` the GTFS coord is at the **platform centre**; the extent is `[snapped − L/2, snapped + L/2]`. This is the train/metro rule. When the polyline runs out before ±L/2 is satisfied, the missing side is **straight-line-extrapolated along the local tangent** (the metro behaviour), not via the OSM rail walk. Mountain polylines are not pre-extended by `_extend_polylines_at_terminals` — that function stays scoped to `train` — so the extrapolation handles terminus stations directly inside `_platform_extent`.

This is deliberately simpler than the train rule: no OSM walk, no `end_of_platform` Fallback B. The funicular / rack / rebucketed_rail terminal stations are visually small enough that a straight extrapolation at the terminus is acceptable for a first iteration; the rule can be tightened later if it produces visible wrong-direction extents.

### Clustering

In-scope mountain stops (rebucketed_rail / rack / funicular / aerial) join the **rail pill clustering pass**: 300 m radius, same `parent_station` merge, same dot-coordination algorithm, same sweep. This is what gives the desired co-cluster behaviour at shared stations:

- Lauterbrunnen, Grindelwald, Brienz, Capolago, Sierre, Zermatt — train and rack platforms physically interleave; the cluster needs to span both.
- Trockener Steg, Mürren, Bettmeralp Talstation, Pardiel, etc. — multiple aerial lines visit the same UIC; the cluster collects all of them.
- Eigergletscher — Eiger Express (aerial) and Jungfraubahn (rack) share a UIC; the cluster mixes a fixed-dot aerial with an extent-bearing rack platform.

Putting every in-scope mountain origin into the same clustering pool is what makes the last case work without a separate cross-pool merge step.

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
