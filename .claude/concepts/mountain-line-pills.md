# Mountain Line Pills

## Problem

Mountain lines are excluded from the dot-pill-connector stop rendering system. They always render as a single dot per stop, even at major mountain stations where multiple mountain lines (or mountain + rail) share platforms. This makes mountain stations visually inconsistent with rail stations next to them — Lauterbrunnen renders rail platforms as pills but the co-located Wengernalpbahn platforms as loose dots; Brienz renders the lake-side train platforms as a pill but the Brienz Rothorn Bahn next to them as detached dots.

The exclusion was introduced in the pill-rendering concept as an "out of scope" line. It was a placeholder while the rail/tram/bus rules were being designed, not a deliberate requirement.

## Requirements

### Scope by `mountain_origin`

The `mountain` bucket is heterogeneous. The rule splits by the `mountain_origin` property already attached to every mountain line feature in step 06:

- **`rebucketed_rail` and `rack`** — handled identically to **rail** (train). They run on physical rail platforms with real platform geometry; many already carry atlas `length` (the rack agencies — Jungfraubahn, Wengernalpbahn, Monte Generoso — supply real 75–287 m platform values). They participate in clustering, pill construction, and connector emission exactly as `train` does.
- **`funicular`** — handled like rail (centred ±L/2 anchoring, joins the rail-pill pipeline), but with **smaller default and sanity values** because funicular platforms are short (~20–40 m, with atlas typically reporting the cabin footprint 5–18 m).
- **`aerial`** — **excluded from pills**, same as today. Aerial cable-car / gondola / elevator stations have no platform geometry in the rail-pill sense; their atlas `length` coverage is zero. Each aerial stop emits a single point dot at the snapped GTFS coord.

The straight-line-fallback aerial features (the ones emitted via the `gtfs_stops` line property when pfaedle fails to shape a route_type 1300 / 1303 trip) follow the aerial rule: point dots only, no pill, no connector.

### New configuration keys

`pill_rendering.default_length_m`, `pill_rendering.sanity_min_m`, and `pill_rendering.sanity_max_m` gain two new per-mode-equivalent keys:

- **`mountain_rail`** — used for `mountain_origin in {rebucketed_rail, rack}`. Values match `train` initially: default 100 m, sanity 30 – 700 m.
- **`mountain_funicular`** — used for `mountain_origin == "funicular"`. Smaller values: default 25 m, sanity 10 – 60 m. The 10 m sanity minimum rejects the 5 m cabin-footprint atlas values; the 60 m maximum caps mislabels without truncating real platforms.

Atlas `length` values outside the sanity range fall through to the default, same as for every other mode.

### Anchoring rule for the new in-scope mountain origins

The GTFS coord is at the **platform centre**; the extent is `[snapped − L/2, snapped + L/2]`. This is the train/metro rule. When the polyline runs out before ±L/2 is satisfied, the missing side is **straight-line-extrapolated along the local tangent** (the metro behaviour), not via the OSM rail walk. Mountain polylines are not pre-extended by `_extend_polylines_at_terminals` — that function stays scoped to `train` — so the extrapolation handles terminus stations directly inside `_platform_extent`.

This is deliberately simpler than the train rule: no OSM walk, no `end_of_platform` Fallback B. The funicular / rack / rebucketed_rail terminal stations are visually small enough that a straight extrapolation at the terminus is acceptable for a first iteration; the rule can be tightened later if it produces visible wrong-direction extents.

### Clustering

In-scope mountain stops (rebucketed_rail / rack / funicular) join the **rail pill clustering pass**: 300 m radius, same `parent_station` merge, same dot-coordination algorithm, same sweep. This is what gives the desired co-cluster behaviour at shared stations (Lauterbrunnen, Grindelwald, Brienz, Capolago, Sierre — places where train and mountain platforms physically interleave).

Aerial mountain stops do not enter clustering at all. They emit point dots in the same path as ferry stops and the existing non-PILL_MODES else-branch.

### Visual style

The new mountain pills, connectors, and disc endpoints render with the same white-fill / 1 px black-border style as the existing rail pills. The mode `color` attribute (mountain light yellow `#ffe566`) is still carried on the tile for any future style variant, but is not consumed by the default paint — consistent with every other mode's stop family today.

`MODE_RANK` is unchanged. At mixed train + mountain clusters, train still dominates colour selection (train rank 0 < mountain rank 4), so the pill renders in train red and the popup `lines_json` lists every mountain line that visits the cluster.

## Constraints

- Ferries remain out of scope for pills (unchanged from today). Only the mountain part of the existing "ferry and mountain are out of scope" statements is being lifted.
- `_extend_polylines_at_terminals` stays train-only. Mountain polylines are not OSM-walked, so terminal extents extrapolate straight rather than tracking the actual track curve. Acceptable for the first iteration; revisit if visible artefacts appear at e.g. Pilatus Kulm or Brienzer Rothorn.
- `MODE_RANK` is unchanged. Mixed train + mountain clusters keep train as the dominant mode.
- The two `mountain_origin` values that share a stop ID with `rebucketed_rail` (Brünig-area funiculars at 8530832 / 8501552) carry no atlas length; nothing about the data demands a special case here.
- Aerial fallback features routed through `gtfs_stop_features` continue to emit GTFS-coord point dots. No change to that path.
- The pill-rendering concept doc loses its two "mountain is out of scope" statements; they are replaced with a one-line pointer to this concept describing the new scope.
