"""Step 07 pipeline — Phase 2.

Pill construction (rail + non-rail), dedup, close-zoom emission, output
writes. Called from `stops.pipeline_setup.run()` after the setup phase."""
import json
from collections import defaultdict
from itertools import permutations
from math import atan2, cos, degrees, floor, log, pi, radians, sin, sqrt

from _state import *  # noqa: F401,F403
from _state import _stop_wb, _tag_band_features  # underscore names skipped by *
from _state import _set_pill_design_band
from stops.debug_overlay import emit_all as _emit_debug_overlays
from stops.cluster import (
    cluster_rail_stops, cluster_stops_for_pills,
    merge_clusters_by_parent_station,
)
from stops.salience import _resolve_stop_tier, _uic_of
from geometry import (
    _cum_dist_m, _directional_tangent_at, _interp_at, _meters_per_deg,
    _project_meters, flatten_coords, haversine_km, parse_time,
    snap_to_line,
)
from stops.extent import _funicular_snap_override, _platform_extent, _resolve_length
from stop_attributes import STOP_SCORES, compute_terminus_skip_oids, load_stop_scores
from stops.close_zoom import (
    _collect_close_zoom_visits, _stack_need_by_stop,
    write_close_zoom_features,
)
from stops.dot_dedup import apply_stop_dedup
from stops.far_zoom import far_zoom_dot_position
from stops.ferry_snap import (
    _ferry_canonical_snap, _ferry_pier_t_on_line, _obb_overlap,
)
from stops.pill_zoom.geom import (
    PROTECTION_RADIUS_NONRAIL_M, PROTECTION_RADIUS_RAIL_M,
)
from stops.pill_zoom.lines import (
    build_indicator_features, cluster_lines, color_luminance,
    count_unique_lines, dominant_line, pill_minzoom,
)
from stops.pill_zoom.make import make_pill_features
from stops.pill_zoom.nn_path import nearest_neighbor_path
from stops.pill_zoom.place import coordinate_dots_global_stab


def run_pills(*, line_lookup, line_stops, stop_meta, stop_min_zoom,
              stop_attrs, end_of_platform_pairs, fill_diag, filled_oids,
              skip_first_oids, skip_last_oids, rail_idx, tram_idx,
              coords_by_uic, uic_serving, gtfs_stop_features,
              stop_salience):

    print("Building stop dots and pill candidates...")

    rail_pill_raw     = []   # dicts for rail pill clustering (also used for dots)
    all_nonrail_pills = []   # ALL non-rail pill modes combined (tram+bus+metro+regional_bus)
    other_features    = []   # dot features for non-rail, ferry, mountain
    indicator_features = []  # mini per-color-group dots inside stop dots/discs/pills (z16+)
    # Per-line ferry-stop snap candidates; aggregated by parent_station after
    # the per-line loop. See stops-pill-zoom.md § "Ferry stops".
    ferry_candidates  = []

    # --- Mountain / straight-line features with embedded gtfs_stops ---
    for feat in gtfs_stop_features:
        p       = feat["properties"]
        color   = p["color"]
        mode    = p["mode"]
        wb      = p.get("width_base", 3.0)
        coords  = feat["geometry"]["coordinates"]
        minzoom = MODE_MINZOOM.get(mode, 11)
        oid     = str(p.get("osm_id", ""))
        for lon, lat in p["gtfs_stops"]:
            slon, slat = snap_to_line(lon, lat, coords)
            other_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": minzoom},
                "geometry": {"type": "Point", "coordinates": [slon, slat]},
                "properties": {"color": color, "mode": mode,
                               "width_base": _stop_wb(wb, mode)},
            })
            indicator_features.extend(build_indicator_features(
                [{"osm_id": oid, "width_base": wb, "mode": mode}],
                slon, slat, line_lookup,
                parent_width_base=_stop_wb(wb, mode), parent_mode=mode))
        # Mountain/ferry via gtfs_stops: no pills

    # --- Per-line stops ---
    for osm_id, ls_entry in line_stops.items():
        if isinstance(ls_entry, dict):
            stop_coords = ls_entry.get("stops", [])
            if ls_entry.get("gtfs_ref"):
                line_lookup.setdefault(osm_id, {})["gtfs_ref"] = ls_entry["gtfs_ref"]
        else:
            stop_coords = ls_entry
        line = line_lookup.get(osm_id)
        if not line:
            continue

        color      = line["color"]
        mode       = line["mode"]
        mo         = line.get("mountain_origin")
        width_base = line["width_base"]
        coords     = line["coords"]
        minzoom    = MODE_MINZOOM.get(mode, 11)
        flat       = flatten_coords(coords)

        skip_first_here = str(osm_id) in skip_first_oids
        skip_last_here = str(osm_id) in skip_last_oids
        last_idx = len(stop_coords) - 1

        # Rail clustering pool (300 m radius): train, plus mountain origins
        # that share station-scale geometry with rail — rebucketed_rail / rack
        # (centred ±L/2 with OSM rail walk at terminals) and aerial (fixed
        # dot, extent=None; in the rail pool so it co-clusters with rack at
        # Eigergletscher).
        # Funicular goes to the **non-rail** pool below: its endpoint stops
        # are often within 300 m of each other along a short line (Marzilibahn
        # 108 m), which the 300 m rail radius merges into a single centroid
        # dot.  The 50 m non-rail radius keeps each endpoint distinct while
        # still co-clustering with adjacent tram/bus stops (Polybahn at
        # Zürich Central etc.).
        in_rail_pool = (
            mode in RAIL_MODES
            or (mode == "mountain" and mo in MOUNTAIN_RAIL_ORIGINS | {"aerial"})
        )
        funicular_in_nonrail_pool = (mode == "mountain" and mo == "funicular")

        if in_rail_pool:
            for idx, entry in enumerate(stop_coords):
                if idx == 0 and skip_first_here:
                    continue
                if idx == last_idx and skip_last_here:
                    continue
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
                stop_name  = meta.get("name", "")
                parent_sta = meta.get("parent", "")
                slon, slat = snap_to_line(lon, lat, flat)
                atlas_len = (stop_attrs.get(sid, {}) or {}).get("length")
                is_eop = (str(osm_id), sid) in end_of_platform_pairs
                extent = _platform_extent(lon, lat, flat, mode, atlas_len, PILL_CFG,
                                          end_of_platform=is_eop,
                                          mountain_origin=mo)
                rail_pill_raw.append({
                    "lon":            slon,
                    "lat":            slat,
                    "osm_id":         osm_id,
                    "mode":           mode,
                    "color":          color,
                    "width_base":     width_base,
                    "stop_id":        sid,
                    "stop_name":      stop_name,
                    "parent_station": parent_sta,
                    "platform_code":  meta.get("platform_code", ""),
                    "extent":         extent,
                })

        elif mode == "ferry":
            # Defer ferry-stop emission to the post-loop aggregation pass —
            # the canonical on-line position depends on every line visiting
            # the pier, not just this one. See "Ferry stop aggregation" below.
            for idx, entry in enumerate(stop_coords):
                if idx == 0 and skip_first_here:
                    continue
                if idx == last_idx and skip_last_here:
                    continue
                lon, lat = entry[0], entry[1]
                sid      = entry[2] if len(entry) > 2 else ""
                meta     = stop_meta.get(sid, {})
                ferry_candidates.append({
                    "gtfs_lon":       lon,
                    "gtfs_lat":       lat,
                    "stop_id":        sid,
                    "stop_name":      meta.get("name", ""),
                    "parent_station": meta.get("parent", ""),
                    "color":          color,
                    "osm_id":         osm_id,
                    "line":           line,
                    "polyline":       flat,
                    "minzoom":        minzoom,
                })

        elif mode in PILL_MODES or funicular_in_nonrail_pool:
            for idx, entry in enumerate(stop_coords):
                if idx == 0 and skip_first_here:
                    continue
                if idx == last_idx and skip_last_here:
                    continue
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
                stop_name  = meta.get("name", "")
                parent_sta = meta.get("parent", "")
                atlas_len = (stop_attrs.get(sid, {}) or {}).get("length")
                # Funicular: pin the snap to the polyline endpoint when the
                # extent reaches it (mountain-line-pills concept). Otherwise
                # use the regular polyline projection.
                if funicular_in_nonrail_pool:
                    override = _funicular_snap_override(
                        lon, lat, flat, atlas_len, PILL_CFG)
                    cx, cy = override if override is not None else snap_to_line(lon, lat, flat)
                else:
                    cx, cy = snap_to_line(lon, lat, flat)
                extent = _platform_extent(lon, lat, flat, mode, atlas_len, PILL_CFG,
                                          mountain_origin=mo)
                # Dots are generated post-cluster (like rail) to avoid duplicates at low zoom
                all_nonrail_pills.append({
                    "lon":            cx,
                    "lat":            cy,
                    "osm_id":         osm_id,
                    "mode":           mode,
                    "color":          color,
                    "width_base":     width_base,
                    "stop_id":        sid,
                    "stop_name":      stop_name,
                    "parent_station": parent_sta,
                    "extent":         extent,
                })

        else:
            line_lines_json = json.dumps([{"ref": line.get("gtfs_ref") or line.get("ref", ""), "color": color, "mode": mode, "name": line.get("name", "")}])
            for idx, entry in enumerate(stop_coords):
                if idx == 0 and skip_first_here:
                    continue
                if idx == last_idx and skip_last_here:
                    continue
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
                slon, slat = snap_to_line(lon, lat, flat)
                other_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": minzoom},
                    "geometry": {"type": "Point", "coordinates": [slon, slat]},
                    "properties": {
                        "color":          color,
                        "mode":           mode,
                        "width_base":     _stop_wb(width_base, mode),
                        "stop_id":        sid,
                        "stop_name":      meta.get("name", ""),
                        "parent_station": meta.get("parent", ""),
                        "lines_json":     line_lines_json,
                    },
                })
                indicator_features.extend(build_indicator_features(
                    [{"osm_id": str(osm_id), "width_base": width_base, "mode": mode}],
                    slon, slat, line_lookup,
                    parent_width_base=_stop_wb(width_base, mode),
                    parent_mode=mode))

    # --- Ferry stop aggregation (parent_station → one disc) ---------------
    # Group ferry candidates by parent_station (or stop_id when no parent),
    # run the closest-vertex medoid to find the pier's canonical OSM node,
    # and emit pill endpoint / connector features only. No separate dot
    # circle in transit_stops.geojson — every ferry stop renders through
    # the non-rail pill paint stack, so the connector seam handling comes
    # for free. Ferry stops are invisible below z11 (same as bus stops);
    # ferry lines themselves still appear from z9.
    #
    # Two-tier zoom split per pier:
    #
    #   z11–z12  (FERRY_PILL_MZ): every pier shows EXACTLY ONE endpoint
    #     at the canonical-vertex medoid — same "one dot per pier" pattern
    #     bus / tram stops follow at their own PILL_MINZOOM. No GTFS-side
    #     dot, no connector, no per-line detail.
    #
    #   z13+     (FERRY_PAIR_MZ): pier detail appears in addition to the
    #     canonical dot:
    #       * Convergent + split (GTFS↔canonical ≥ collapse_threshold_m):
    #           a GTFS-side endpoint + connector between the two dots.
    #       * Non-convergent (max-vertex-distance > convergence_threshold_m):
    #           per-line endpoints, one at each line's individual closest-
    #           point snap to GTFS. The canonical medoid emitted at z11
    #           still sits on (or very near) one of these — a small
    #           acceptable overlap.
    #       * Convergent + collapsed: nothing extra.
    #
    # See stops-pill-zoom.md § "Ferry stops".
    ferry_by_pier: dict = {}
    for cand in ferry_candidates:
        pier_key = cand["parent_station"] or cand["stop_id"]
        ferry_by_pier.setdefault(pier_key, []).append(cand)

    # Non-ferry drawn stop positions used by the GTFS-coord suppression
    # check in the "Convergent + split" branch below. See stops-pill-zoom.md
    # § "Ferry stops" — GTFS-side suppression.
    non_ferry_stop_coords = [(p["lon"], p["lat"]) for p in rail_pill_raw] + \
                            [(p["lon"], p["lat"]) for p in all_nonrail_pills]

    ferry_pill_features = []
    n_ferry_collapsed = 0
    n_ferry_split = 0
    n_ferry_diverged = 0
    n_ferry_gtfs_suppressed = 0
    # Pill (medium-zoom) and pair (split detail) both appear from z14 — the
    # same zoom every other mode starts at (see stops-pill-zoom.md § "Dot-to-
    # pill zoom switch"). The z9–z13 far-zoom marker for ferry is a low-zoom
    # dot emitted into `other_features` below, matching every other mode's
    # far-zoom behaviour. See stops-far-zoom-markers.md § "Ferry far-zoom
    # marker". Ferry uses a single variant only (no design bands — see
    # stops-pill-zoom.md § "Ferry stops").
    FERRY_PILL_MZ = 14        # convergence-point endpoint, per-line endpoints
    FERRY_PAIR_MZ = 14        # split-case GTFS endpoint + connector
    FERRY_FAR_ZOOM_MZ = MODE_MINZOOM.get("ferry", 9)
    for pier_key, cands in ferry_by_pier.items():
        gtfs_repr = (cands[0]["gtfs_lon"], cands[0]["gtfs_lat"])

        # Aggregate all lines visiting this pier into one lines_json blob —
        # the popup at the pier should list every ferry line, not just the
        # one whose feature spawned the dot.
        lines_seen = set()
        lines_json_list = []
        for c in cands:
            line = c["line"] or {}
            ref = line.get("gtfs_ref") or line.get("ref", "")
            name = line.get("name", "")
            key = (ref, name)
            if key in lines_seen:
                continue
            lines_seen.add(key)
            lines_json_list.append({
                "ref":   ref,
                "color": c["color"],
                "mode":  "ferry",
                "name":  name,
            })
        lines_json_str = json.dumps(lines_json_list)

        rep = cands[0]
        base_props = {
            "color":          rep["color"],
            "mode":           "ferry",
            "width_base":     _stop_wb(FERRY_DOT_WB, "ferry"),
            "stop_id":        rep["stop_id"],
            "stop_name":      rep["stop_name"],
            "parent_station": rep["parent_station"],
            "lines_json":     lines_json_str,
        }
        indicator_stubs = [{"osm_id": str(c["osm_id"]), "mode": "ferry"} for c in cands]

        # Dedup polylines by osm_id — the same line can visit the pier twice
        # (e.g. an arrival + departure entry) and we only want it counted once.
        seen_oids = set()
        polylines = []
        for c in cands:
            oid = c["osm_id"]
            if oid in seen_oids:
                continue
            seen_oids.add(oid)
            polylines.append(c["polyline"])

        canon, max_vertex_dist_m = _ferry_canonical_snap(polylines, gtfs_repr)

        # Far-zoom dot at canonical pier position — the ferry intersection-
        # search result. Rendered through the low-zoom dot paint stack
        # (transit_stops.geojson), matching every other mode's far-zoom
        # behaviour. Maxzoom = FERRY_PILL_MZ - 1 so the dot disappears at
        # exactly the zoom where the medium-zoom endpoint disc takes over.
        other_features.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": FERRY_FAR_ZOOM_MZ,
                            "maxzoom": FERRY_PILL_MZ - 1},
            "geometry": {"type": "Point", "coordinates": [canon[0], canon[1]]},
            "properties": dict(base_props),
        })

        # Medium-zoom canonical-vertex endpoint at FERRY_PILL_MZ. From z13
        # this is the disc the user sees. Split-case GTFS endpoint and
        # per-line endpoints (non-convergent case) also appear from
        # FERRY_PAIR_MZ upward.
        ferry_pill_features.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": FERRY_PILL_MZ},
            "geometry": {"type": "Point", "coordinates": [canon[0], canon[1]]},
            "properties": {**base_props, "feature_type": "endpoint"},
        })
        indicator_features.extend(build_indicator_features(
            indicator_stubs, canon[0], canon[1], line_lookup))

        if max_vertex_dist_m > FERRY_CONVERGE_M:
            # Non-convergent: per-line endpoints at each line's own snap, as
            # detail above FERRY_PAIR_MZ. The canonical-vertex endpoint
            # emitted above is the medoid of the per-line closest-vertices,
            # so at z13+ it sits on (or very near) one of the per-line
            # endpoints — a small acceptable overlap.
            n_ferry_diverged += 1
            for c in cands:
                slon, slat = snap_to_line(c["gtfs_lon"], c["gtfs_lat"],
                                          c["polyline"])
                # Endpoint pull (see _ferry_canonical_snap docstring):
                # prefer the closer polyline endpoint when it's within
                # FERRY_ENDPOINT_PULL_M of the closest-segment snap, so
                # the dot lands on the OSM ferry-pier node rather than a
                # routing waypoint in the water.
                pl = c["polyline"]
                if pl:
                    endpoints = (pl[0], pl[-1])
                    closer_ep = min(endpoints,
                                    key=lambda v: (v[0] - slon) ** 2
                                                  + (v[1] - slat) ** 2)
                    if haversine_km(slon, slat, closer_ep[0], closer_ep[1]) \
                            * 1000.0 <= FERRY_ENDPOINT_PULL_M:
                        slon, slat = float(closer_ep[0]), float(closer_ep[1])
                ferry_pill_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": FERRY_PAIR_MZ},
                    "geometry": {"type": "Point", "coordinates": [slon, slat]},
                    "properties": {**base_props,
                                   "stop_id":      c["stop_id"],
                                   "stop_name":    c["stop_name"],
                                   "feature_type": "endpoint"},
                })
                indicator_features.extend(build_indicator_features(
                    [{"osm_id": str(c["osm_id"])}],
                    slon, slat, line_lookup))
            continue

        dist_m = haversine_km(gtfs_repr[0], gtfs_repr[1],
                              canon[0], canon[1]) * 1000.0
        if dist_m < FERRY_COLLAPSE_M:
            n_ferry_collapsed += 1
            continue

        # GTFS-side suppression: if the canonical pier is closer to a drawn
        # non-ferry transit stop than the GTFS coord is, the GTFS coord
        # points away from the interchange rather than toward it — drawing
        # a second dot + connector there adds no orientation value. Suppress
        # them and emit only the canonical dot. See stops-pill-zoom.md § "Ferry
        # stops" — GTFS-side suppression.
        if non_ferry_stop_coords:
            d_c = min(haversine_km(canon[0], canon[1], lon, lat)
                      for lon, lat in non_ferry_stop_coords)
            d_g = min(haversine_km(gtfs_repr[0], gtfs_repr[1], lon, lat)
                      for lon, lat in non_ferry_stop_coords)
            if d_c < d_g:
                n_ferry_gtfs_suppressed += 1
                continue

        # Convergent + split: add GTFS-side endpoint + connector at the pill
        # detail threshold. The canonical-vertex endpoint (emitted above)
        # plus this GTFS endpoint give the connector a disc at each end; the
        # existing pill paint stack (connector casing → connector fill →
        # endpoint disc) handles the dot↔connector seam at both joints.
        n_ferry_split += 1
        ferry_pill_features.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": FERRY_PAIR_MZ},
            "geometry": {"type": "Point",
                         "coordinates": [gtfs_repr[0], gtfs_repr[1]]},
            "properties": {**base_props, "feature_type": "endpoint"},
        })
        ferry_pill_features.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": FERRY_PAIR_MZ},
            "geometry": {"type": "LineString",
                         "coordinates": [
                             [gtfs_repr[0], gtfs_repr[1]],
                             [canon[0], canon[1]],
                         ]},
            "properties": {**base_props,
                           "width_base":   FERRY_CONNECTOR_WB,
                           "feature_type": "connector"},
        })
    print(f"  Ferry stops: {len(ferry_by_pier):,} piers "
          f"({n_ferry_split:,} split, {n_ferry_collapsed:,} collapsed, "
          f"{n_ferry_diverged:,} per-line fallback, "
          f"{n_ferry_gtfs_suppressed:,} GTFS-side suppressed)")

    # Per-stop-id set of lines (osm_ids), used by cluster_stops_for_pills to
    # block merging of two stops served by the same drawn line. See
    # `pill-cluster-same-line-guard.md`.
    lines_of_stop: dict = defaultdict(set)
    for _oid_k, _ls_v in line_stops.items():
        _seq = _ls_v.get("stops", []) if isinstance(_ls_v, dict) else _ls_v
        for _entry in _seq:
            if len(_entry) > 2 and _entry[2]:
                lines_of_stop[_entry[2]].add(str(_oid_k))

    # --- Rail dots + pills (unified pass) ---
    print(f"  {len(rail_pill_raw):,} raw rail stop positions → clustering...")
    rail_pill_clusters = cluster_stops_for_pills(
        rail_pill_raw, PILL_CLUSTER_RAIL_KM, lines_of_stop)
    rail_pill_clusters = merge_clusters_by_parent_station(rail_pill_clusters)
    print(f"  → {len(rail_pill_clusters):,} rail station clusters")
    # Place dots via tangent grouping + perpendicular sweep along the central
    # member's platform extent (per-group). Stabbed dots get placed on the
    # perpendicular bar; leftovers run through the old algorithm.
    print(f"  Placing rail dots across {len(rail_pill_clusters):,} clusters...")
    for c in rail_pill_clusters:
        # Preserve pre-placement pfaedle snaps for the far-zoom-dot
        # intersection search and tiebreak centre. coordinate_dots_global_stab
        # rewrites lon/lat to the placed positions.
        for s in c:
            s["snap_lon"] = s["lon"]
            s["snap_lat"] = s["lat"]
        coordinate_dots_global_stab(c, PROTECTION_RADIUS_RAIL_M,
                                    LONE_OUTLIER_GAP_RAIL_METRO_M)
    print("  → rail dot placement done")

    rail_features = []
    pill_features_rail = []
    for cluster in rail_pill_clusters:
        stop_count = count_unique_lines(cluster)
        mz = pill_minzoom("train", stop_count)

        color, mode, max_wb, dom_stop = dominant_line(cluster)
        centroid_lon = sum(s["lon"] for s in cluster) / len(cluster)
        centroid_lat = sum(s["lat"] for s in cluster) / len(cluster)
        lines_json_str = json.dumps(cluster_lines(cluster, line_lookup))
        centroid_props = {
            "color":          color,
            "mode":           mode,
            "width_base":     _stop_wb(max_wb, mode),
            "stop_id":        dom_stop.get("stop_id", ""),
            "stop_name":      dom_stop.get("stop_name", ""),
            "parent_station": dom_stop.get("parent_station", ""),
            "lines_json":     lines_json_str,
        }

        if mz is None:
            # Single-line station: one cluster dot at all zooms. Rule chain
            # falls through to the centroid (no pill, no disc, single line
            # ⇒ no intersection).
            rail_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": 5},
                "geometry": {"type": "Point", "coordinates": [centroid_lon, centroid_lat]},
                "properties": centroid_props,
            })
            indicator_features.extend(build_indicator_features(
                cluster, centroid_lon, centroid_lat, line_lookup))
        else:
            # Bake band C first — its features drive the far-zoom-dot
            # decision and the pill-collapse fallback (matches previous
            # behavior). Then bake A and B on top with different
            # PILL_GAP_ANGLED_M / CURVE_MIN_RADIUS_M values, tagged with
            # per-feature `design_band` + tippecanoe zoom range.
            _set_pill_design_band(PILL_DESIGN_BANDS["C"])
            c_feats = make_pill_features(cluster, mz, lines_json_str, line_lookup)
            if c_feats:
                _tag_band_features(c_feats, "C", PILL_DESIGN_BANDS["C"])
                all_band_feats = list(c_feats)
                for _band_id in ("A", "B"):
                    _set_pill_design_band(PILL_DESIGN_BANDS[_band_id])
                    _bfeats = make_pill_features(cluster, mz, lines_json_str, line_lookup)
                    _tag_band_features(_bfeats, _band_id, PILL_DESIGN_BANDS[_band_id])
                    all_band_feats.extend(_bfeats)
                # Far-zoom dot from band C. Rail-like family skips the
                # intersection search; rule picks largest pill (by line
                # count) → largest disc → centroid.
                dot_lon, dot_lat = far_zoom_dot_position(
                    cluster, c_feats, line_lookup,
                    (centroid_lon, centroid_lat), rail_like=True)
                rail_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": 5, "maxzoom": mz - 1},
                    "geometry": {"type": "Point", "coordinates": [dot_lon, dot_lat]},
                    "properties": centroid_props,
                })
                pill_features_rail.extend(all_band_feats)
            else:
                # Multi-line cluster whose pill collapsed (all positions
                # deduped to one point) — no pill is emitted, so the
                # cluster dot stays visible at all zooms at the centroid.
                # Bands A and B share the same dot placement so they
                # collapse identically; no fallback bake needed.
                rail_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": 5},
                    "geometry": {"type": "Point", "coordinates": [centroid_lon, centroid_lat]},
                    "properties": centroid_props,
                })
                indicator_features.extend(build_indicator_features(
                    cluster, centroid_lon, centroid_lat, line_lookup))

    rail_pill_count = len(pill_features_rail)
    print(f"  → {rail_pill_count} rail pill/connector features "
          f"from {len(rail_pill_clusters):,} clusters")

    # ==========================================================================
    # Pill generation (non-rail)
    # ==========================================================================

    pill_features = list(pill_features_rail)

    # --- Non-rail pills (all modes combined → dominant wins) ---
    print(f"  {len(all_nonrail_pills):,} non-rail pill candidates "
          f"(tram+metro+bus+regional combined) → clustering...")
    nonrail_clusters = cluster_stops_for_pills(
        all_nonrail_pills, PILL_CLUSTER_NONRAIL_KM, lines_of_stop)
    nonrail_clusters = merge_clusters_by_parent_station(nonrail_clusters)
    # Same global stabbing placement as rail.
    print(f"  Placing non-rail dots across {len(nonrail_clusters):,} clusters...")
    for c in nonrail_clusters:
        _, dom_mode, _, _ = dominant_line(c)
        lone_outlier_m = (LONE_OUTLIER_GAP_RAIL_METRO_M
                          if dom_mode == "metro"
                          else LONE_OUTLIER_GAP_BUS_TRAM_M)
        # Preserve pre-placement pfaedle snaps for the far-zoom-dot
        # intersection search and tiebreak centre.
        for s in c:
            s["snap_lon"] = s["lon"]
            s["snap_lat"] = s["lat"]
        coordinate_dots_global_stab(c, PROTECTION_RADIUS_NONRAIL_M,
                                    lone_outlier_m)
    print("  → non-rail dot placement done")

    # Emit debug overlays now that all clusters have been processed and the
    # stops.debug_overlay in-memory state (stabbed pairs + diag bars) is
    # populated. No-ops when `debug.debug_overlay` is false; see
    # stops/debug_overlay.py for the delete-checklist.
    _emit_debug_overlays(
        line_stops=line_stops,
        line_lookup=line_lookup,
        stop_attrs=stop_attrs,
        stop_meta=stop_meta,
        skip_first_oids=skip_first_oids,
        skip_last_oids=skip_last_oids,
        end_of_platform_pairs=end_of_platform_pairs,
    )

    nonrail_pill_count = 0
    nonrail_dot_features = []
    for cluster in nonrail_clusters:
        stop_count  = count_unique_lines(cluster)
        color, dom_mode, max_wb, dom_stop = dominant_line(cluster)
        mz = pill_minzoom(dom_mode, stop_count)

        # rail_like decides whether the far-zoom rule runs the intersection
        # search. Mountain rebucketed_rail / rack ride the non-rail pool
        # (rare — most rebucketed_rail trips cluster with regular train), so
        # detect them by dominant mode/mountain_origin here.
        cluster_rail_like = (dom_mode == "train") or (
            dom_mode == "mountain"
            and any(s.get("mountain_origin") in MOUNTAIN_RAIL_ORIGINS
                    for s in cluster))

        centroid_lon = sum(s["lon"] for s in cluster) / len(cluster)
        centroid_lat = sum(s["lat"] for s in cluster) / len(cluster)
        mode_minzoom = min(MODE_MINZOOM.get(s["mode"], 11) for s in cluster)
        lines_json_str = json.dumps(cluster_lines(cluster, line_lookup))
        centroid_props = {
            "color":          color,
            "mode":           dom_mode,
            "width_base":     _stop_wb(max_wb, dom_mode),
            "stop_id":        dom_stop.get("stop_id", ""),
            "stop_name":      dom_stop.get("stop_name", ""),
            "parent_station": dom_stop.get("parent_station", ""),
            "lines_json":     lines_json_str,
        }

        if mz is None:
            # Single-line stop: one cluster dot at all zooms. Rule chain
            # falls through to centroid (intersection needs ≥2 lines).
            nonrail_dot_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": mode_minzoom},
                "geometry": {"type": "Point", "coordinates": [centroid_lon, centroid_lat]},
                "properties": centroid_props,
            })
            indicator_features.extend(build_indicator_features(
                cluster, centroid_lon, centroid_lat, line_lookup))
        else:
            # Bake band C first (its features drive the far-zoom-dot and
            # collapse decision); then bake A and B on top with per-band
            # thresholds. See rail block above for rationale.
            _set_pill_design_band(PILL_DESIGN_BANDS["C"])
            c_feats = make_pill_features(cluster, mz, lines_json_str, line_lookup)
            if c_feats:
                _tag_band_features(c_feats, "C", PILL_DESIGN_BANDS["C"])
                all_band_feats = list(c_feats)
                for _band_id in ("A", "B"):
                    _set_pill_design_band(PILL_DESIGN_BANDS[_band_id])
                    _bfeats = make_pill_features(cluster, mz, lines_json_str, line_lookup)
                    _tag_band_features(_bfeats, _band_id, PILL_DESIGN_BANDS[_band_id])
                    all_band_feats.extend(_bfeats)
                # Non-rail family runs the intersection search first — at
                # a crossroads the dot sits at the junction, not at the
                # platform centroid.
                dot_lon, dot_lat = far_zoom_dot_position(
                    cluster, c_feats, line_lookup,
                    (centroid_lon, centroid_lat),
                    rail_like=cluster_rail_like)
                nonrail_dot_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": mode_minzoom, "maxzoom": mz - 1},
                    "geometry": {"type": "Point", "coordinates": [dot_lon, dot_lat]},
                    "properties": centroid_props,
                })
                pill_features.extend(all_band_feats)
                nonrail_pill_count += len(all_band_feats)
            else:
                # Pill collapsed — cluster dot stays at all zooms at the
                # centroid (no pill, no disc, fall-through case).
                nonrail_dot_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": mode_minzoom},
                    "geometry": {"type": "Point", "coordinates": [centroid_lon, centroid_lat]},
                    "properties": centroid_props,
                })
                indicator_features.extend(build_indicator_features(
                    cluster, centroid_lon, centroid_lat, line_lookup))

    print(f"  → {nonrail_pill_count} non-rail pill/connector features "
          f"from {len(nonrail_clusters):,} clusters")

    # ==========================================================================
    # Apply per-UIC min_zoom to stop dots
    # ==========================================================================
    # Each stop dot in transit_stops.geojson carries a stop_id / parent_station
    # in its properties. Resolve to canonical UIC and override the feature's
    # tippecanoe.minzoom from stop_min_zoom. Dots without a resolvable UIC
    # (mountain/straight-line embedded gtfs_stops without stop_id) keep their
    # mode-derived minzoom.
    dot_features = rail_features + other_features + nonrail_dot_features

    # ==========================================================================
    # Attach per-stop tier + score (stops-far-zoom-dot-redesign.md)
    # ==========================================================================
    # The style reads `stop_tier` and looks up the diameter from a per-tier
    # table. `stop_score` is kept alongside for debug / diagnostics. Both
    # are per parent UIC; for each dot we resolve the UIC from
    # `parent_station` (falling back to the platform-stripped `stop_id`).
    # Dots without a resolvable UIC fall back to `small_bus`.
    stop_scores_lookup = load_stop_scores()
    if stop_scores_lookup:
        n_scored = 0
        for feat in dot_features:
            p = feat["properties"]
            uic = p.get("parent_station") or (
                (p.get("stop_id") or "").split(":")[0])
            record = stop_scores_lookup.get(uic) if uic else None
            if record:
                p["stop_score"] = round(record["score"], 4)
                p["stop_tier"] = record["tier"]
                if record["score"] > 0:
                    n_scored += 1
            else:
                p["stop_score"] = 0.0
                p["stop_tier"] = "small_bus"
        print(f"  stop_score/stop_tier attached to {len(dot_features):,} "
              f"dot features ({n_scored:,} with non-zero score)")
    else:
        print(f"  WARNING: {STOP_SCORES.name} not found — every dot will "
              "render at the smallest tier")
        for feat in dot_features:
            feat["properties"]["stop_score"] = 0.0
            feat["properties"]["stop_tier"] = "small_bus"

    if stop_salience:
        n_applied = 0
        for feat in dot_features:
            p = feat["properties"]
            uic = p.get("parent_station") or (
                (p.get("stop_id") or "").split(":")[0])
            if not uic:
                continue
            sal = stop_salience.get(uic)
            if not sal:
                continue
            tipp = feat.setdefault("tippecanoe", {})
            tipp["minzoom"] = int(sal["min_zoom"])
            p["min_zoom"] = sal["min_zoom"]
            p["tier"] = sal["tier"]
            p["importance_score"] = sal["importance_score"]
            p["urbanness_bracket"] = sal["urbanness_bracket"]
            p["is_intersection"] = sal["is_intersection"]
            p["is_terminus"] = sal["is_terminus"]
            n_applied += 1
        print(f"  min_zoom applied to {n_applied:,}/{len(dot_features):,} dot features")

    # ==========================================================================
    # Far-zoom dot dedup
    # ==========================================================================
    print("Applying far-zoom dot dedup...")
    apply_stop_dedup(dot_features)

    # ==========================================================================
    # Salience diagnostic (per-line salience + per-stop rule placement)
    # ==========================================================================
    OUT_SALIENCE = ROOT / "data" / "transit" / "salience.json"
    line_diag = []
    for oid, info in line_lookup.items():
        if info.get("salience") is None:
            continue
        line_diag.append({
            "osm_id":     oid,
            "ref":        info.get("ref", ""),
            "name":       info.get("name", ""),
            "mode":       info.get("mode", ""),
            "agency_id":  info.get("agency_id", ""),
            "f_weighted": info.get("f_weighted", 0.0),
            "speed_kmh":  info.get("speed_kmh"),
            "salience":   info.get("salience"),
            "min_zoom":   info.get("min_zoom"),
        })
    stop_diag = []
    for uic, v in stop_salience.items():
        stop_diag.append({"uic": uic, **v})
    OUT_SALIENCE.write_text(json.dumps(
        {"lines": line_diag, "stops": stop_diag}, ensure_ascii=False))
    print(f"  Diagnostic: {len(line_diag)} lines, "
          f"{len(stop_diag)} stops → {OUT_SALIENCE}")

    # ==========================================================================
    # Write outputs
    # ==========================================================================

    OUT_DOTS.parent.mkdir(parents=True, exist_ok=True)
    OUT_DOTS.write_text(json.dumps({"type": "FeatureCollection", "features": dot_features}))
    pill_features.extend(ferry_pill_features)
    pill_features.extend(indicator_features)
    OUT_PILLS.write_text(json.dumps({"type": "FeatureCollection", "features": pill_features}))

    print("Emitting close-zoom stop features...")
    write_close_zoom_features(line_stops, line_lookup, stop_meta, stop_attrs,
                              end_of_platform_pairs,
                              skip_first_oids, skip_last_oids,
                              rail_idx=rail_idx, tram_idx=tram_idx)

    # Summary
    mode_counts: dict = defaultdict(int)
    for f in dot_features:
        mode_counts[f["properties"]["mode"]] += 1
    print(f"\n{len(dot_features):,} stop dots → {OUT_DOTS}")
    for m, c in sorted(mode_counts.items(), key=lambda x: -x[1]):
        print(f"  {m:<20} {c:>6,}")

    pill_type_counts: dict = defaultdict(int)
    for f in pill_features:
        pill_type_counts[f["properties"].get("feature_type", "?")] += 1
    print(f"\n{len(pill_features):,} pill features → {OUT_PILLS}")
    for t, c in sorted(pill_type_counts.items(), key=lambda x: -x[1]):
        print(f"  {t:<20} {c:>6,}")
