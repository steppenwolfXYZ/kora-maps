"""Feature-level operations run after the main emission loop:
mountain-line deduplication, aerial reverse-direction synthesis, and small
polyline / trip helpers used from the driver's main().
"""
from collections import defaultdict

from geometry import _bbox_overlap_fraction


# ── Mountain feature deduplication ───────────────────────────────────────────

def _feat_bbox(feat):
    coords = feat["geometry"]["coordinates"]
    if feat["geometry"]["type"] == "MultiLineString":
        pts = [c for seg in coords for c in seg]
    else:
        pts = coords
    if not pts:
        return None
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return (min(lons), min(lats), max(lons), max(lats))


def _n_pts(feat) -> int:
    coords = feat["geometry"]["coordinates"]
    if feat["geometry"]["type"] == "MultiLineString":
        return sum(len(s) for s in coords)
    return len(coords)


def deduplicate_mountain(features: list) -> list:
    """Drop overlapping aerial features (cable cars, gondolas) sharing the same
    ref. Best (most geometry vertices) wins.

    Restricted to mountain_origin == "aerial" (GTFS route_type 5/6). The
    problem this solves is multiple OSM route relations for the same physical
    haul cable. Aerial is exempt from the per-direction split (see
    direction-coverage Mode-exemptions), so the dedup key is `ref` alone.
    Funiculars, rebucketed mountain rail, and every other mode are not
    collapsed.
    """
    aerial_idx = [(i, f) for i, f in enumerate(features)
                  if f["properties"].get("mountain_origin") == "aerial"]
    aerial_set = {i for i, _ in aerial_idx}
    keep = set(i for i in range(len(features)) if i not in aerial_set)

    by_ref: dict = defaultdict(list)
    for i, f in aerial_idx:
        ref = f["properties"]["ref"]
        by_ref[ref].append((i, f, _feat_bbox(f), _n_pts(f)))

    n_dropped = 0
    for ref, group in by_ref.items():
        if not ref:
            for i, f, b, n in group:
                keep.add(i)
            continue
        group.sort(key=lambda x: -x[3])
        kept_bboxes = []
        for i, f, b, n in group:
            if b is None:
                keep.add(i)
                continue
            is_dup = any(_bbox_overlap_fraction(b, kb) >= 0.65 for kb in kept_bboxes)
            if is_dup:
                n_dropped += 1
            else:
                keep.add(i)
                kept_bboxes.append(b)
    if n_dropped:
        print(f"  Aerial dedup: removed {n_dropped} duplicate features")
    return [f for i, f in enumerate(features) if i in keep]


def synthesise_aerial_reverse_directions(features: list,
                                          line_stops_out: dict) -> list:
    """Restore missing return directions on aerial cables.

    `deduplicate_mountain` collapses aerial features per ref on bbox overlap
    alone, which drops the opposite direction of most cables. Without a
    return-direction feature, the close-zoom emission (which skips the last
    stop as an arrival) has no pill to draw at the other terminal — the
    arrival endpoint of the surviving direction. See
    stops-close-zoom.md § "Aerial + funicular terminals".

    Per aerial ref: for each direction_key whose reverse is not present in
    the ref group, synthesise a reversed sibling from the best (most
    vertices) same-direction source — reversed geometry, reversed stop
    sequence, reversed direction_key, new osm_id, `synthesised_reverse`
    flag; all other properties copied. Runs AFTER scoring / salience /
    min_zoom so those computations see only original features and the
    reverse inherits their results — otherwise the reverse would inflate
    its forward twin's competition count (they lie on top of each other)
    and drag salience down.
    """
    aerial = [f for f in features
              if f["properties"].get("mountain_origin") == "aerial"]
    by_ref: dict = defaultdict(list)
    for f in aerial:
        ref = f["properties"].get("ref") or ""
        by_ref[ref].append(f)

    new_features: list = []
    n_synth = 0
    for ref, group in by_ref.items():
        if not ref:
            continue
        by_dk: dict = defaultdict(list)
        for f in group:
            by_dk[f["properties"].get("direction_key", "")].append(f)
        present_keys = set(by_dk.keys())
        for dk, sources in list(by_dk.items()):
            if "-" not in dk:
                continue
            first_uic, last_uic = dk.split("-", 1)
            rev_dk = f"{last_uic}-{first_uic}"
            if rev_dk in present_keys or rev_dk == dk:
                continue
            source = max(sources,
                         key=lambda f: len(f["geometry"].get("coordinates", [])))
            orig_oid = source["properties"]["osm_id"]
            new_oid = f"{orig_oid}r"
            geom = source["geometry"]
            rev_coords = list(reversed(geom.get("coordinates", [])))
            new_props = dict(source["properties"])
            new_props["osm_id"] = new_oid
            new_props["direction_key"] = rev_dk
            new_props["synthesised_reverse"] = True
            new_feat: dict = {}
            for k, v in source.items():
                if k in ("geometry", "properties"):
                    continue
                new_feat[k] = v
            new_feat["type"] = "Feature"
            new_feat["geometry"] = {"type": geom["type"],
                                     "coordinates": rev_coords}
            new_feat["properties"] = new_props
            new_features.append(new_feat)
            orig_entry = line_stops_out.get(orig_oid, {})
            rev_stops = list(reversed(orig_entry.get("stops", [])))
            line_stops_out[new_oid] = {
                "osm_ref":       orig_entry.get("osm_ref", ""),
                "stops":         rev_stops,
                "gtfs_ref":      orig_entry.get("gtfs_ref", ""),
                "direction_key": rev_dk,
            }
            present_keys.add(rev_dk)
            n_synth += 1

    if n_synth:
        print(f"  Aerial reverse synthesis: added {n_synth} reversed features")
    return features + new_features


# ── Pfaedle shape grouping ───────────────────────────────────────────────────

def stops_to_polyline(stop_ids: list, stop_coords: dict) -> list:
    """Build a polyline from a stop_id sequence, dropping unresolved stops."""
    out: list = []
    last = None
    from .stop_identity import uic_of
    for sid in stop_ids:
        c = stop_coords.get(sid) or stop_coords.get(uic_of(sid))
        if not c:
            continue
        if last is not None and c == last:
            continue
        out.append([c[0], c[1]])
        last = c
    return out


def best_trip_in_shape_group(trip_ids: list, trip_lookup: dict,
                              svc_dates: dict) -> str:
    """Pick a representative trip for a shape group — the one with the most
    active service days (proxy for "most canonical")."""
    best = None
    best_score = -1
    for tid in trip_ids:
        t = trip_lookup.get(tid)
        if not t:
            continue
        score = len(svc_dates.get(t["service_id"], set()))
        if score > best_score:
            best_score = score
            best = tid
    return best or trip_ids[0]
