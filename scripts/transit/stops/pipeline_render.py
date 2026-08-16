"""Step 07 pipeline — Phase 2.

Pill construction (rail + non-rail), dedup, close-zoom emission, output
writes. Called from `stops.pipeline_setup.run()` after the setup phase."""
import json
import multiprocessing
import os
import time
from collections import defaultdict
from itertools import permutations
from math import atan2, cos, degrees, floor, log, pi, radians, sin, sqrt

from _state import *  # noqa: F401,F403
from _state import _stop_wb, _tag_band_features  # underscore names skipped by *
from _state import _set_pill_design_band, _timed
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
    build_indicator_features, cluster_departures_per_hour, cluster_line_keys,
    cluster_lines, color_luminance, count_unique_lines, dominant_line,
    pill_minzoom,
)
from stops.pill_zoom.make import make_pill_features
from stops.pill_zoom.nn_path import nearest_neighbor_path
from stops.pill_zoom.place import coordinate_dots_global_stab


# ── Stop-search coord snap to OSM platforms ─────────────────────────────────
# See transit-routing.md § Endpoint inputs. GTFS parent-station centroids
# often sit on the road centerline (Bern Eigerplatz is the canonical case:
# GTFS coord lands within 2 m of a `highway=primary, sidewalk=separate`
# way and a tram track). MOTIS's OSR foot profile then snaps the FROM/TO
# to the road and applies +45 s per edge for `sidewalk=separate` — the
# walker never boards at the station's own platforms and MOTIS falls back
# to boarding at a distant stop. Snapping each search-index coord onto
# the nearest OSM `public_transport=platform` centroid (via the
# `platform_ways.geojson` extract from step 03) puts the FROM/TO on a
# walkable feature that MOTIS's OSR explicitly whitelists.

SEARCH_INDEX_PLATFORM_SNAP_RADIUS_M = 150.0


def _snap_search_index_to_platforms(search_seen: dict) -> None:
    """Mutate each `search_seen` entry's `c` to the nearest OSM platform-way
    centroid within `SEARCH_INDEX_PLATFORM_SNAP_RADIUS_M`. Entries with no
    platform nearby keep their original coord. Silently skips (with a warn)
    when `platform_ways.geojson` isn't available."""
    if not PLATFORM_WAYS_GEOJSON.exists():
        print(f"  Platform snap: {PLATFORM_WAYS_GEOJSON.name} missing; "
              f"search-index coords unchanged")
        return
    data = json.loads(PLATFORM_WAYS_GEOJSON.read_text())
    centroids: list = []
    for feat in data.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        n = len(coords)
        centroids.append((sum(c[0] for c in coords) / n,
                          sum(c[1] for c in coords) / n))
    if not centroids:
        print(f"  Platform snap: 0 usable platforms in "
              f"{PLATFORM_WAYS_GEOJSON.name}; coords unchanged")
        return

    r = SEARCH_INDEX_PLATFORM_SNAP_RADIUS_M
    cell_y = r / 111320.0
    # cos(47°) ≈ 0.68 near the CH bbox centre — a fixed factor keeps cells
    # square-ish enough that a 3×3 neighborhood always covers the radius.
    cell_x = r / (111320.0 * 0.68)
    grid: dict = defaultdict(list)
    for lon, lat in centroids:
        grid[(int(lon / cell_x), int(lat / cell_y))].append((lon, lat))

    r_sq = r * r
    n_snapped = n_unchanged = 0
    max_shift_m = 0.0
    for entry in search_seen.values():
        lon, lat = entry["c"]
        cos_lat = cos(radians(lat))
        cx0 = int(lon / cell_x)
        cy0 = int(lat / cell_y)
        best_d = r_sq
        best_pt = None
        for gx in (cx0 - 1, cx0, cx0 + 1):
            for gy in (cy0 - 1, cy0, cy0 + 1):
                for plon, plat in grid.get((gx, gy), ()):
                    dx = (lon - plon) * 111320.0 * cos_lat
                    dy = (lat - plat) * 111320.0
                    d = dx * dx + dy * dy
                    if d < best_d:
                        best_d = d
                        best_pt = (plon, plat)
        if best_pt is None:
            n_unchanged += 1
            continue
        entry["c"] = [round(best_pt[0], 6), round(best_pt[1], 6)]
        n_snapped += 1
        shift = sqrt(best_d)
        if shift > max_shift_m:
            max_shift_m = shift
    print(f"  Platform snap: {n_snapped}/{n_snapped + n_unchanged} stations "
          f"snapped (max shift {max_shift_m:.0f} m, radius "
          f"{SEARCH_INDEX_PLATFORM_SNAP_RADIUS_M:.0f} m)")


# ── Non-rail pill bake worker ────────────────────────────────────────────────
# The non-rail 3-band bake is the single biggest chunk of step 07 wall-clock
# (~240 s over 26k clusters). Each cluster's bake is independent (all shared
# state — line_lookup, PILL_DESIGN_BANDS — is read-only inside make_pill_features
# and far_zoom_dot_position), so we fan out to a process pool. The pre-bake
# `coordinate_dots_global_stab` pass — which is what populates the debug
# overlay's in-memory state — runs in the PARENT before the pool is spawned,
# so workers never touch stops.debug_overlay.

_WORKER_LINE_LOOKUP = None
_WORKER_PILL_DESIGN_BANDS = None


def _nonrail_worker_init(line_lookup, pill_design_bands):
    """Called once per worker; stashes shared read-only state in worker
    globals so tasks don't have to re-pickle line_lookup for every cluster.
    Under fork this is essentially a no-op (COW gives workers the parent's
    memory for free); under spawn the initargs are pickled once per worker."""
    global _WORKER_LINE_LOOKUP, _WORKER_PILL_DESIGN_BANDS
    _WORKER_LINE_LOOKUP = line_lookup
    _WORKER_PILL_DESIGN_BANDS = pill_design_bands


def _bake_nonrail_cluster(payload):
    """One cluster → (dot_feat, band_feats_or_None, indicator_feats).
    Mirrors the sequential loop body in run_pills verbatim; only difference
    is that `line_lookup` and `PILL_DESIGN_BANDS` come from worker globals
    populated by `_nonrail_worker_init`."""
    (cluster, mz, cluster_rail_like, mode_minzoom,
     centroid_lon, centroid_lat, centroid_props,
     lines_json_str) = payload
    line_lookup = _WORKER_LINE_LOOKUP
    bands = _WORKER_PILL_DESIGN_BANDS
    cluster_keys_str = centroid_props.get("line_keys", "")

    if mz is None:
        dot_feat = {
            "type": "Feature",
            "tippecanoe": {"minzoom": mode_minzoom},
            "geometry": {"type": "Point",
                         "coordinates": [centroid_lon, centroid_lat]},
            "properties": centroid_props,
        }
        ind = list(build_indicator_features(
            cluster, centroid_lon, centroid_lat, line_lookup,
            line_keys=cluster_keys_str))
        return (dot_feat, None, ind)

    _set_pill_design_band(bands["C"])
    cluster_dep_hr = centroid_props.get("dep_hr", 0.0)
    c_feats = make_pill_features(cluster, mz, lines_json_str, line_lookup,
                                  dep_hr=cluster_dep_hr,
                                  line_keys=cluster_keys_str)
    if not c_feats:
        # Pill collapsed — fall through to centroid dot + indicators.
        dot_feat = {
            "type": "Feature",
            "tippecanoe": {"minzoom": mode_minzoom},
            "geometry": {"type": "Point",
                         "coordinates": [centroid_lon, centroid_lat]},
            "properties": centroid_props,
        }
        ind = list(build_indicator_features(
            cluster, centroid_lon, centroid_lat, line_lookup,
            line_keys=cluster_keys_str))
        return (dot_feat, None, ind)

    _tag_band_features(c_feats, "C", bands["C"])
    all_band_feats = list(c_feats)
    for band_id in ("A", "B"):
        _set_pill_design_band(bands[band_id])
        bfeats = make_pill_features(cluster, mz, lines_json_str, line_lookup,
                                     dep_hr=cluster_dep_hr,
                                     line_keys=cluster_keys_str)
        _tag_band_features(bfeats, band_id, bands[band_id])
        all_band_feats.extend(bfeats)
    dot_lon, dot_lat = far_zoom_dot_position(
        cluster, c_feats, line_lookup,
        (centroid_lon, centroid_lat),
        rail_like=cluster_rail_like)
    dot_feat = {
        "type": "Feature",
        "tippecanoe": {"minzoom": mode_minzoom, "maxzoom": mz - 1},
        "geometry": {"type": "Point", "coordinates": [dot_lon, dot_lat]},
        "properties": centroid_props,
    }
    return (dot_feat, all_band_feats, [])


def _get_nonrail_pool_context():
    """Pick a multiprocessing start method. `fork` is dramatically cheaper
    (COW inheritance of line_lookup + all the pill_zoom modules) and is
    safe for this pure-Python pipeline on both macOS and Linux — the
    codebase touches no threads or GUI runtime before the pool is spawned.
    Fallback to `spawn` if fork is unavailable (some hardened environments
    disable it)."""
    try:
        return multiprocessing.get_context("fork")
    except (ValueError, OSError):
        return multiprocessing.get_context("spawn")


def run_pills(*, line_lookup, line_stops, stop_meta, stop_min_zoom,
              stop_attrs, end_of_platform_pairs, fill_diag, filled_oids,
              skip_first_oids, skip_last_oids, rail_idx, tram_idx,
              coords_by_uic, uic_serving, gtfs_stop_features,
              stop_salience, oids_by_uic=None):

    _t_phase = time.perf_counter()
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
        feat_line_keys = line_keys_str([line_key_of(p)])
        for lon, lat in p["gtfs_stops"]:
            slon, slat = snap_to_line(lon, lat, coords)
            other_features.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": minzoom},
                "geometry": {"type": "Point", "coordinates": [slon, slat]},
                "properties": {"color": color, "mode": mode,
                               "width_base": _stop_wb(wb, mode),
                               "line_keys": feat_line_keys},
            })
            indicator_features.extend(build_indicator_features(
                [{"osm_id": oid, "width_base": wb, "mode": mode}],
                slon, slat, line_lookup,
                parent_width_base=_stop_wb(wb, mode), parent_mode=mode,
                line_keys=feat_line_keys))
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
            for idx, entry in enumerate(stop_coords):
                if idx == 0 and skip_first_here:
                    continue
                if idx == last_idx and skip_last_here:
                    continue
                lon, lat   = entry[0], entry[1]
                sid        = entry[2] if len(entry) > 2 else ""
                meta       = stop_meta.get(sid, {})
                slon, slat = snap_to_line(lon, lat, flat)
                mini_cluster = [{
                    "osm_id":         str(osm_id),
                    "mode":           mode,
                    "stop_id":        sid,
                    "parent_station": meta.get("parent", ""),
                }]
                mini_line_keys = cluster_line_keys(mini_cluster, line_lookup, oids_by_uic)
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
                        "lines_json":     json.dumps(cluster_lines(mini_cluster, line_lookup, oids_by_uic)),
                        "dep_hr":         round(cluster_departures_per_hour(mini_cluster, line_lookup, oids_by_uic), 3),
                        "line_keys":      mini_line_keys,
                    },
                })
                indicator_features.extend(build_indicator_features(
                    [{"osm_id": str(osm_id), "width_base": width_base, "mode": mode}],
                    slon, slat, line_lookup,
                    parent_width_base=_stop_wb(width_base, mode),
                    parent_mode=mode, line_keys=mini_line_keys))

    print(f"  [{time.perf_counter() - _t_phase:6.1f}s] build stop dots + per-line pill candidates")
    _t_phase = time.perf_counter()

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
        # one whose feature spawned the dot. Reuse cluster_lines /
        # cluster_departures_per_hour by constructing a synthetic cluster
        # from the candidates so the (ref, mode) dedup + A↔B tooltip run
        # the same as for rail / non-rail pills.
        rep = cands[0]
        synthetic_cluster = [
            {
                "osm_id":         c["osm_id"],
                "mode":           "ferry",
                "stop_id":        c["stop_id"],
                "parent_station": c["parent_station"],
            }
            for c in cands
        ]
        lines_json_str = json.dumps(cluster_lines(synthetic_cluster, line_lookup, oids_by_uic))
        pier_line_keys = cluster_line_keys(synthetic_cluster, line_lookup, oids_by_uic)
        base_props = {
            "color":          rep["color"],
            "mode":           "ferry",
            "width_base":     _stop_wb(FERRY_DOT_WB, "ferry"),
            "stop_id":        rep["stop_id"],
            "stop_name":      rep["stop_name"],
            "parent_station": rep["parent_station"],
            "lines_json":     lines_json_str,
            "dep_hr":         round(cluster_departures_per_hour(synthetic_cluster, line_lookup, oids_by_uic), 3),
            "line_keys":      pier_line_keys,
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
            indicator_stubs, canon[0], canon[1], line_lookup,
            line_keys=pier_line_keys))

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
                    slon, slat, line_lookup,
                    line_keys=pier_line_keys))
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
    print(f"  [{time.perf_counter() - _t_phase:6.1f}s] ferry stop aggregation")
    _t_phase = time.perf_counter()

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
        lines_json_str = json.dumps(cluster_lines(cluster, line_lookup, oids_by_uic))
        cluster_keys_str = cluster_line_keys(cluster, line_lookup, oids_by_uic)
        centroid_props = {
            "color":          color,
            "mode":           mode,
            "width_base":     _stop_wb(max_wb, mode),
            "stop_id":        dom_stop.get("stop_id", ""),
            "stop_name":      dom_stop.get("stop_name", ""),
            "parent_station": dom_stop.get("parent_station", ""),
            "lines_json":     lines_json_str,
            "dep_hr":         round(cluster_departures_per_hour(cluster, line_lookup, oids_by_uic), 3),
            "line_keys":      cluster_keys_str,
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
                cluster, centroid_lon, centroid_lat, line_lookup,
                line_keys=cluster_keys_str))
        else:
            # Bake band C first — its features drive the far-zoom-dot
            # decision and the pill-collapse fallback (matches previous
            # behavior). Then bake A and B on top with different
            # PILL_GAP_ANGLED_M / CURVE_MIN_RADIUS_M values, tagged with
            # per-feature `design_band` + tippecanoe zoom range.
            _set_pill_design_band(PILL_DESIGN_BANDS["C"])
            cluster_dep_hr = centroid_props["dep_hr"]
            c_feats = make_pill_features(cluster, mz, lines_json_str, line_lookup,
                                          dep_hr=cluster_dep_hr,
                                          line_keys=cluster_keys_str)
            if c_feats:
                _tag_band_features(c_feats, "C", PILL_DESIGN_BANDS["C"])
                all_band_feats = list(c_feats)
                for _band_id in ("A", "B"):
                    _set_pill_design_band(PILL_DESIGN_BANDS[_band_id])
                    _bfeats = make_pill_features(cluster, mz, lines_json_str, line_lookup,
                                                  dep_hr=cluster_dep_hr,
                                                  line_keys=cluster_keys_str)
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
                    cluster, centroid_lon, centroid_lat, line_lookup,
                    line_keys=cluster_keys_str))

    rail_pill_count = len(pill_features_rail)
    print(f"  → {rail_pill_count} rail pill/connector features "
          f"from {len(rail_pill_clusters):,} clusters")
    print(f"  [{time.perf_counter() - _t_phase:6.1f}s] rail cluster + pill bake (3 bands)")
    _t_phase = time.perf_counter()

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
    # Prep per-cluster payloads once, then bake either in parallel (default)
    # or sequentially (when the debug overlay is on — the debug overlay
    # accumulates in-module state in stops.debug_overlay that doesn't cross
    # process boundaries; the pre-bake `coordinate_dots_global_stab` already
    # ran in the parent so the debug OVERLAY output is safe, but keeping
    # things sequential when the debug is on avoids any surprises).
    payloads = []
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
        lines_json_str = json.dumps(cluster_lines(cluster, line_lookup, oids_by_uic))
        centroid_props = {
            "color":          color,
            "mode":           dom_mode,
            "width_base":     _stop_wb(max_wb, dom_mode),
            "stop_id":        dom_stop.get("stop_id", ""),
            "stop_name":      dom_stop.get("stop_name", ""),
            "parent_station": dom_stop.get("parent_station", ""),
            "lines_json":     lines_json_str,
            "dep_hr":         round(cluster_departures_per_hour(cluster, line_lookup, oids_by_uic), 3),
            "line_keys":      cluster_line_keys(cluster, line_lookup, oids_by_uic),
        }
        payloads.append((cluster, mz, cluster_rail_like, mode_minzoom,
                         centroid_lon, centroid_lat, centroid_props,
                         lines_json_str))

    from stops.debug_overlay import DEBUG_ENABLED as _NONRAIL_DEBUG_ON
    n_workers = min(8, max(1, (os.cpu_count() or 2) - 1))
    use_parallel = (not _NONRAIL_DEBUG_ON) and n_workers > 1 and len(payloads) >= 200
    if use_parallel:
        ctx = _get_nonrail_pool_context()
        # Chunksize: aim for ~16 chunks per worker so late-arriving heavy
        # clusters don't leave workers idle at the tail.
        chunksize = max(1, len(payloads) // (n_workers * 16))
        print(f"  Parallel non-rail bake: {n_workers} workers, "
              f"chunksize={chunksize}, start_method={ctx.get_start_method()}")
        with ctx.Pool(
                n_workers,
                initializer=_nonrail_worker_init,
                initargs=(line_lookup, PILL_DESIGN_BANDS)) as pool:
            results = pool.map(_bake_nonrail_cluster, payloads, chunksize)
    else:
        # Sequential fallback — mirrors worker semantics.
        _nonrail_worker_init(line_lookup, PILL_DESIGN_BANDS)
        results = [_bake_nonrail_cluster(p) for p in payloads]

    for dot_feat, band_feats, ind_feats in results:
        nonrail_dot_features.append(dot_feat)
        if band_feats:
            pill_features.extend(band_feats)
            nonrail_pill_count += len(band_feats)
        if ind_feats:
            indicator_features.extend(ind_feats)

    print(f"  → {nonrail_pill_count} non-rail pill/connector features "
          f"from {len(nonrail_clusters):,} clusters")
    print(f"  [{time.perf_counter() - _t_phase:6.1f}s] non-rail cluster + pill bake (3 bands) + debug overlays")
    _t_phase = time.perf_counter()

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
    # `label_priority` (see `stop-labels.md`) is derived from tier + score
    # here so the far-zoom label layer can use it as its symbol-sort-key.
    LABEL_TIER_RANK = {
        "major_train":     0,
        "main_train":      1,
        "important_train": 2,
        "train_station":   3,
        "small_train":     4,
        "major_mountain":  5,
        "ferry_stop":      6,
        "mountain_stop":   7,
        "major_hub":       8,
        "big_station":     9,
        "normal_stop":    10,
        "small_bus":      11,
    }
    def _label_priority(tier, score):
        return LABEL_TIER_RANK.get(tier, 11) * 1000 - float(score or 0.0)

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
            p["label_priority"] = round(
                _label_priority(p["stop_tier"], p["stop_score"]), 4)
        print(f"  stop_score/stop_tier attached to {len(dot_features):,} "
              f"dot features ({n_scored:,} with non-zero score)")
    else:
        print(f"  WARNING: {STOP_SCORES.name} not found — every dot will "
              "render at the smallest tier")
        for feat in dot_features:
            p = feat["properties"]
            p["stop_score"] = 0.0
            p["stop_tier"] = "small_bus"
            p["label_priority"] = round(_label_priority("small_bus", 0.0), 4)

    # ==========================================================================
    # Attach display_name (stop-labels.md § City-prefix stripping)
    # ==========================================================================
    # Bus / tram stops in Swiss GTFS use "City, Streetname" — the city prefix
    # is redundant on the map when a nearby train station labels the city
    # already. Rule: if the stop's `stop_name.split(",")[0]` matches a train
    # station's city key (its full first-comma-segment OR its space-split
    # first word — catches both "Bern" and "Zürich HB" style) within
    # DISPLAY_NAME_RADIUS_KM, drop the prefix. Rural villages without a
    # train station keep their name.
    from stops.close_zoom.text import strip_city_prefix
    DISPLAY_NAME_RADIUS_KM = 25.0

    def _train_station_city_keys(name):
        if not name:
            return set()
        keys = set()
        first_segment = name.split(",")[0].strip()
        if first_segment:
            keys.add(first_segment.lower())
            parts = first_segment.split()
            if parts:
                keys.add(parts[0].lower())
        return keys

    train_city_lookup = defaultdict(list)
    for feat in dot_features:
        if feat["properties"].get("mode") != "train":
            continue
        name = feat["properties"].get("stop_name") or ""
        coord = feat["geometry"]["coordinates"]
        for key in _train_station_city_keys(name):
            train_city_lookup[key].append(coord)

    n_stripped = 0
    for feat in dot_features:
        p = feat["properties"]
        name = p.get("stop_name") or ""
        p["display_name"] = name
        if "," not in name:
            continue
        prefix = name.split(",")[0].strip()
        if not prefix:
            continue
        candidates = train_city_lookup.get(prefix.lower())
        if not candidates:
            continue
        stop_lon, stop_lat = feat["geometry"]["coordinates"]
        for city_lon, city_lat in candidates:
            if haversine_km(stop_lon, stop_lat, city_lon, city_lat) \
                    <= DISPLAY_NAME_RADIUS_KM:
                stripped = strip_city_prefix(name, prefix)
                if stripped and stripped != name:
                    p["display_name"] = stripped
                    n_stripped += 1
                break
    print(f"  display_name: {n_stripped:,} stops had their city prefix "
          f"stripped (within {DISPLAY_NAME_RADIUS_KM:.0f} km of a matching "
          f"train station)")

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
    print(f"  [{time.perf_counter() - _t_phase:6.1f}s] attach stop_score / stop_tier / min_zoom to dots")
    _t_phase = time.perf_counter()

    print("Applying far-zoom dot dedup...")
    apply_stop_dedup(dot_features)
    print(f"  [{time.perf_counter() - _t_phase:6.1f}s] apply_stop_dedup (far-zoom)")
    _t_phase = time.perf_counter()

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

    # Stop-search index (stop-search.md): one entry per unique station
    # (dedup by parent_station UIC), consumed by the client-side search
    # input. Rebuilt every time step 07 writes dots. Mode with the lowest
    # MODE_RANK wins when a station is served by multiple modes; the
    # winning dot's `stop_tier` is carried through for the client's
    # ranking (kept as the pipeline string, not pre-normalised).
    _search_seen: dict[str, dict] = {}
    for f in dot_features:
        props = f.get("properties", {})
        name = props.get("stop_name")
        if not name:
            continue
        uic = props.get("parent_station") or props.get("stop_id")
        if not uic:
            continue
        coords = f.get("geometry", {}).get("coordinates")
        if not coords or len(coords) < 2:
            continue
        mode = props.get("mode") or ""
        rank = MODE_RANK.get(mode, 99)
        existing = _search_seen.get(uic)
        if existing is not None and existing["_rank"] <= rank:
            continue
        _search_seen[uic] = {
            "n": name,
            "u": uic,
            "c": [round(coords[0], 6), round(coords[1], 6)],
            "m": mode,
            "t": props.get("stop_tier") or "",
            "_rank": rank,
        }
    _snap_search_index_to_platforms(_search_seen)
    _search_entries = [
        {"n": e["n"], "u": e["u"], "c": e["c"], "m": e["m"], "t": e["t"]}
        for e in sorted(_search_seen.values(), key=lambda e: e["n"])
    ]
    OUT_STOP_SEARCH_INDEX.parent.mkdir(parents=True, exist_ok=True)
    OUT_STOP_SEARCH_INDEX.write_text(json.dumps(_search_entries, ensure_ascii=False))
    print(f"  Stop-search index: {len(_search_entries)} unique stations → {OUT_STOP_SEARCH_INDEX}")

    pill_features.extend(ferry_pill_features)
    pill_features.extend(indicator_features)

    # ==========================================================================
    # Emit stop_label_anchor + stop_label_leader features (stop-labels.md § Pill-zoom)
    # ==========================================================================
    # Two cases:
    #   Simple  — station has only a dot / endpoint or a single straight pill
    #             (no connector, no bent pill). Label sits 5 m east of the
    #             pill-zoom endpoint / pill's easternmost coord — NOT the
    #             far-zoom dot's coord, which can differ from the pill-zoom
    #             disc position (e.g. Bern, Henkerbrünnli: dot is 15 m south
    #             of the endpoint disc, so anchoring off the dot puts the
    #             label way off).
    #   Complex — has a connector, a bent pill, or multiple pills. Pick the
    #             "main pill" (longest by segment sum — proxy for f_weighted-
    #             ranked; refine later if the proxy fails). Label sits 5 m
    #             east of the main pill's easternmost coord.
    # Eastward padding of the label anchor. The pill / disc renders with a
    # PIXEL radius while the anchor is baked geometry (metres), so a flat
    # metre padding under-clears large discs: at z16 a high-width_base disc
    # is ~15 px ≈ 12 m, swallowing a 5 m offset entirely (Deisswil). The
    # padding therefore covers the rendered radius converted to metres at
    # the band's MINIMUM zoom — the worst case, since a fixed px radius
    # spans the most metres at the band's low edge — plus a clearance.
    # Radius formula mirrors pill_disc_width() in
    # scripts/style/transit_stations.py (dots use the same values directly
    # as circle-radius): radius_px = base + slope × min(width_base, 5).
    LABEL_PADDING_X_M = 5.0        # floor for tiny / width-less features
    LABEL_CLEARANCE_X_M = 2.0
    LABEL_BAND_RADIUS_PX = {"A": (2.25, 1.15), "B": (3.0, 1.6),
                            "C": (4.0, 2.2)}
    PX_PER_M_Z17 = 2.455

    def _label_pad_m(band_id, width_base):
        base, slope = LABEL_BAND_RADIUS_PX[band_id]
        zmin = BAND_ZOOM_RANGES[band_id][0]
        m_per_px = (2.0 ** (17 - zmin)) / PX_PER_M_Z17
        r_px = base + slope * min(float(width_base or 0.0), 5.0)
        return max(LABEL_PADDING_X_M, r_px * m_per_px + LABEL_CLEARANCE_X_M)

    RELEVANT_PILL_TYPES = {"pill", "connector", "endpoint"}

    def _bucket_key(props):
        return props.get("parent_station") or (props.get("stop_id") or "").split(":")[0]

    # Group pill features by (station_key, design_band). Different bands
    # (A: z14, B: z15, C: z16+) can produce different pill layouts and even
    # different simple/complex classifications, so labels are computed per
    # band and then dedup-emitted for tile efficiency.
    BAND_ZOOM_RANGES = {"A": (14, 14), "B": (15, 15), "C": (16, 17)}
    station_pill_features_by_band = defaultdict(lambda: defaultdict(list))
    for f in pill_features:
        p = f.get("properties", {})
        if p.get("feature_type") not in RELEVANT_PILL_TYPES:
            continue
        key = _bucket_key(p)
        if not key:
            continue
        band = p.get("design_band") or "C"
        if band not in BAND_ZOOM_RANGES:
            continue
        station_pill_features_by_band[key][band].append(f)

    # Best (lowest label_priority) dot per parent_station carries the label
    # metadata — a Bern train + tram + bus combined station labels once, at the
    # train tier's font weight and size.
    best_dot_by_key = {}
    for f in dot_features:
        p = f.get("properties", {})
        key = _bucket_key(p)
        if not key or not p.get("stop_name"):
            continue
        prio = p.get("label_priority", 11000.0)
        if key not in best_dot_by_key or prio < best_dot_by_key[key][1]:
            best_dot_by_key[key] = (f, prio)

    def _segment_length_m(coords):
        # Robust against Point-geometry coords (flat [lon, lat]) — a Point
        # has length 0. Only LineString-shaped nested coord lists compute
        # non-zero length.
        if not coords or len(coords) < 2 or not isinstance(coords[0], (list, tuple)):
            return 0.0
        return sum(
            haversine_km(coords[i][0], coords[i][1],
                         coords[i + 1][0], coords[i + 1][1]) * 1000.0
            for i in range(len(coords) - 1))

    def _pill_osm_ids(pill):
        raw = (pill.get("properties") or {}).get("pill_osm_ids") or ""
        return [x for x in raw.split(",") if x]

    def _pill_rank_fweighted(pill):
        """Sum of f_weighted across the pill's distinct logical-line keys
        (ref, mode, agency_id) — same ranking `_largest_pill_or_disc_position`
        in stops/far_zoom.py uses. Direction variants of one route share a
        key and contribute only their max f_weighted, not the sum."""
        fw_by_key = {}
        for oid in _pill_osm_ids(pill):
            info = line_lookup.get(oid)
            if not info:
                continue
            ref = info.get("gtfs_ref") or info.get("ref") or ""
            key = (ref, info.get("mode") or "", info.get("agency_id") or "")
            fw = info.get("f_weighted", 0.0) or 0.0
            if key not in fw_by_key or fw > fw_by_key[key]:
                fw_by_key[key] = fw
        return sum(fw_by_key.values())

    def _easternmost_of(coords_list):
        best = None
        for pt in coords_list:
            if best is None or pt[0] > best[0]:
                best = pt
        return best

    def _pill_is_vertical(coords):
        """True if the pill's first→last endpoint direction is more
        vertical than horizontal (in metric coords, so lon-stretching
        by latitude is accounted for)."""
        if len(coords) < 2:
            return False
        dx_lon = abs(coords[-1][0] - coords[0][0])
        dy_lat = abs(coords[-1][1] - coords[0][1])
        mean_lat = (coords[0][1] + coords[-1][1]) / 2.0
        dx_m = dx_lon * cos(radians(mean_lat))
        return dy_lat > dx_m

    def _polyline_midpoint(coords):
        """Point at half of the polyline's total geodesic length."""
        if len(coords) < 2:
            return coords[0] if coords else None
        total = _segment_length_m(coords)
        if total <= 0:
            return coords[0]
        target = total / 2.0
        cum = 0.0
        for i in range(len(coords) - 1):
            a, b = coords[i], coords[i + 1]
            seg = haversine_km(a[0], a[1], b[0], b[1]) * 1000.0
            if cum + seg >= target:
                t = (target - cum) / seg if seg > 0 else 0.0
                return (a[0] + t * (b[0] - a[0]),
                        a[1] + t * (b[1] - a[1]))
            cum += seg
        return coords[-1]

    # World-coord half-height used to shift the anchor east on angled
    # vertical pills. Less than the full em-box half-height because visible
    # glyphs don't reach the ascender / descender edges — so we give a bit
    # of leeway. Text also can't clip into pill it doesn't reach, so the
    # effective half-height is CAPPED at the pill's own vertical half-extent.
    LABEL_TEXT_HALF_HEIGHT_M = 40.0

    def _pill_anchor_base(coords):
        """Return the pill's anchor base point per the pill-zoom rule.
        Vertical pills → midpoint of the centerline, shifted east by the
        extra distance the pill's slope covers within the smaller of the
        label's half-height or the pill's own vertical half-extent.
        Horizontal pills → easternmost coord (label sits past the east end)."""
        if not coords or len(coords) < 2:
            return coords[0] if coords else None
        if _pill_is_vertical(coords):
            mid = _polyline_midpoint(coords)
            if mid is None:
                return None
            a, b = coords[0], coords[-1]
            mean_lat = (a[1] + b[1]) / 2.0
            dx_m = abs(a[0] - b[0]) * 111320.0 * cos(radians(mean_lat))
            dy_m = abs(a[1] - b[1]) * 111320.0
            if dy_m > 1e-6 and dx_m > 0:
                # Cap effective text half-height at the pill's own half-dy —
                # text beyond the pill's ends can't clip anything, so it
                # doesn't drive the correction.
                effective_half_m = min(LABEL_TEXT_HALF_HEIGHT_M, dy_m / 2.0)
                extra_m = effective_half_m * (dx_m / dy_m)
                return (mid[0] + _lon_offset(mid[1], extra_m), mid[1])
            return mid
        return _easternmost_of(coords)

    def _all_coords(feats):
        pts = []
        for f in feats:
            geom = f.get("geometry") or {}
            gtype = geom.get("type")
            c = geom.get("coordinates") or []
            if gtype == "Point" and len(c) >= 2:
                pts.append(c)
            elif gtype == "LineString":
                pts.extend(c)
        return pts

    def _lon_offset(lat, meters):
        c = cos(radians(lat))
        return meters / (111320.0 * c) if abs(c) > 1e-9 else 0.0

    def _lat_offset(meters):
        return meters / 111320.0

    # If the top candidate's f_weighted is > SCORE_DOMINANCE_THRESHOLD × the
    # runner-up's, anchor off that winner (pill or disc, matches the
    # existing main-pill logic in stops/far_zoom.py). Otherwise fall back to
    # the easternmost point across pills + endpoints — no clear winner,
    # so map-right is the deterministic tie-break.
    SCORE_DOMINANCE_THRESHOLD = 1.25

    def _pillzoom_label_geometry(feats, dot_feat, band_id):
        """Return anchor lon/lat for a station's pill-zoom label. Feats are
        one band's pill/connector/endpoint features; dot_feat is the
        fallback when no pill-zoom geometry exists at all. `band_id`
        drives the eastward padding — the rendered pill/disc radius in
        metres at the band's minimum zoom (see `_label_pad_m`)."""
        pills = [f for f in feats
                 if (f.get("properties") or {}).get("feature_type") == "pill"]
        endpoints = [f for f in feats
                     if (f.get("properties") or {}).get("feature_type") == "endpoint"]
        candidates = pills + endpoints

        def _wb_of(feat):
            return (feat.get("properties") or {}).get("width_base", 0.0)

        def _anchor_from_base(base, width_base):
            pad_m = _label_pad_m(band_id, width_base)
            return (base[0] + _lon_offset(base[1], pad_m), base[1])

        def _base_of(feat):
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if geom.get("type") == "LineString":
                return _pill_anchor_base(coords)
            return coords if len(coords) >= 2 else None

        if candidates:
            # Rank pills AND endpoints together by f_weighted (mode-neutral;
            # see `_pill_rank_fweighted`). Tie-break on longest polyline so
            # zero-weight bands still pick deterministically.
            scored = sorted(
                ((f, _pill_rank_fweighted(f)) for f in candidates),
                key=lambda t: (t[1], _segment_length_m(
                    (t[0].get("geometry") or {}).get("coordinates") or [])),
                reverse=True)
            top_feat, top_score = scored[0]
            second_score = scored[1][1] if len(scored) > 1 else 0.0
            dominant = (
                second_score <= 0
                or top_score > second_score * SCORE_DOMINANCE_THRESHOLD)
            if dominant:
                base = _base_of(top_feat)
                if base:
                    return _anchor_from_base(base, _wb_of(top_feat))
            # Below threshold → fallback: easternmost coord across pills +
            # endpoints (skip connectors so a curved connector's east swing
            # can't win over the actual discs / pills). The easternmost
            # point's owner isn't tracked, so pad for the widest candidate.
            pts = _all_coords(candidates)
            east_pt = _easternmost_of(pts)
            if east_pt is not None:
                return _anchor_from_base(
                    east_pt, max(_wb_of(f) for f in candidates))

        # No pill/endpoint at all → dot fallback (dots render with the
        # same radius formula, so the same padding applies).
        pts = _all_coords([dot_feat])
        east_pt = _easternmost_of(pts)
        return (_anchor_from_base(east_pt, _wb_of(dot_feat))
                if east_pt else None)

    def _round_pt(pt):
        return (round(pt[0], 6), round(pt[1], 6)) if pt else None

    def _bands_to_ranges(bands):
        """Merge contiguous bands into (minzoom, maxzoom) tuples so a common
        anchor across bands becomes ONE tippecanoe feature."""
        ordered = sorted(bands, key=lambda b: BAND_ZOOM_RANGES[b][0])
        ranges = []
        for b in ordered:
            b_min, b_max = BAND_ZOOM_RANGES[b]
            if ranges and b_min == ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], b_max)
            else:
                ranges.append((b_min, b_max))
        return ranges

    n_anchors = 0
    n_missing = 0
    n_per_band_stations = 0  # stations that needed >1 emission (per-band variance)
    for key, (dot_feat, _) in best_dot_by_key.items():
        band_feats = station_pill_features_by_band.get(key, {})
        # Compute per-band anchor. Missing band → skip (no anchor for that zoom).
        per_band = {}
        for band_id in BAND_ZOOM_RANGES:
            feats_b = band_feats.get(band_id, [])
            anchor = _pillzoom_label_geometry(feats_b, dot_feat, band_id)
            if anchor is not None:
                per_band[band_id] = anchor
        if not per_band:
            n_missing += 1
            continue

        # Group bands by rounded anchor so identical positions emit ONE
        # feature covering the union of their zoom ranges.
        groups = defaultdict(list)
        for band_id, a in per_band.items():
            groups[_round_pt(a)].append(band_id)
        if len(groups) > 1:
            n_per_band_stations += 1

        p = dot_feat["properties"]
        label_props = {
            "feature_type":   "stop_label_anchor",
            "stop_name":      p.get("stop_name", ""),
            "display_name":   p.get("display_name") or p.get("stop_name", ""),
            "stop_tier":      p.get("stop_tier", "small_bus"),
            "stop_score":     p.get("stop_score", 0.0),
            "label_priority": p.get("label_priority", 11000.0),
            "parent_station": key,
            "mode":           p.get("mode", ""),
            "line_keys":      p.get("line_keys", ""),
            # Popup payload — mirror of the close-zoom station_label so a
            # click on the pill label text opens the same station popup
            # (see popups.md § Shared conventions).
            "lines_json":     p.get("lines_json", ""),
            "dep_hr":         float(p.get("dep_hr", 0.0) or 0.0),
        }

        for group_bands in groups.values():
            anchor = per_band[group_bands[0]]
            for zmin, zmax in _bands_to_ranges(group_bands):
                pill_features.append({
                    "type": "Feature",
                    "tippecanoe": {"minzoom": zmin, "maxzoom": zmax},
                    "geometry": {"type": "Point",
                                 "coordinates": [anchor[0], anchor[1]]},
                    "properties": label_props,
                })
                n_anchors += 1
    print(f"  stop_label_anchor: emitted {n_anchors:,} anchors "
          f"({n_per_band_stations:,} stations needed multiple emissions "
          f"across bands; {n_missing:,} skipped)")

    OUT_PILLS.write_text(json.dumps({"type": "FeatureCollection", "features": pill_features}))
    print(f"  [{time.perf_counter() - _t_phase:6.1f}s] salience diag + write dot/pill GeoJSON")
    _t_phase = time.perf_counter()

    print("Emitting close-zoom stop features...")
    # Cross-parent grouping (stops-close-zoom.md § Cross-parent grouping):
    # The pill clusterer authoritatively decides which stops belong to one
    # station (spatial + same-line guard). Where its clusters group several
    # GTFS parent_station UICs together — canonical case: Gümligen,
    # Melchenbühl (Tram) 8507052 + (Bus) 8577013 — close-zoom rendering
    # keys hulls, backdrops, and station labels on the CLUSTER LEADER
    # rather than the individual GTFS parents, so the merged station gets
    # one hull and one label. Leader is the far-zoom dot's parent (the
    # dominant stop's parent), so parent_label_info naturally holds the
    # leader's entry.
    parent_leader: dict = {}
    cluster_parents_by_leader: dict = defaultdict(set)
    for cluster in list(rail_pill_clusters) + list(nonrail_clusters):
        parents_in_cluster = {
            s.get("parent_station") for s in cluster if s.get("parent_station")
        }
        if not parents_in_cluster:
            continue
        _, _, _, dom_stop = dominant_line(cluster)
        leader = dom_stop.get("parent_station") or min(parents_in_cluster)
        if leader not in parents_in_cluster:
            leader = min(parents_in_cluster)
        for p in parents_in_cluster:
            parent_leader[p] = leader
        cluster_parents_by_leader[leader] |= parents_in_cluster
    n_merged_clusters = sum(
        1 for parents in cluster_parents_by_leader.values() if len(parents) > 1)
    print(f"  parent_leader: {n_merged_clusters:,} clusters span >1 GTFS parent "
          f"(merged for close-zoom hull / label)")

    # Trailing mode-suffix parenthetical stripped when the cluster spans
    # multiple parents so the merged label reads "Melchenbühl" instead of
    # "Melchenbühl (Tram)". Single-parent clusters keep the suffix — it may
    # be meaningful (e.g. a standalone "(Tram)" station).
    import re as _re
    _MODE_SUFFIX_RE = _re.compile(
        r"\s*\((?:Tram|Bus|Zug|Bahn|Metro|Train|Ferry|Schiff)\)\s*$",
        _re.IGNORECASE)

    def _strip_mode_suffix(name: str) -> str:
        return _MODE_SUFFIX_RE.sub("", name).strip() or name

    # Per-parent label metadata for the close-zoom station label
    # (stop-labels.md § close-zoom): the best dot per parent already
    # carries the post-strip display_name. Keys are leader parents; a
    # cluster spanning multiple parents surfaces one entry (its leader),
    # with the mode-suffix parenthetical stripped from BOTH the visible
    # display_name and the popup-title stop_name so the merged station
    # doesn't announce itself as "(Tram)" when it also covers buses.
    parent_label_info = {}
    for key, (f, _) in best_dot_by_key.items():
        props = f["properties"]
        stop_name = props.get("stop_name", "")
        display = (props.get("display_name") or stop_name)
        if len(cluster_parents_by_leader.get(key, set())) > 1:
            if display:
                display = _strip_mode_suffix(display)
            if stop_name:
                stop_name = _strip_mode_suffix(stop_name)
        parent_label_info[key] = {
            "stop_name":    stop_name,
            "display_name": display,
            "stop_tier":    props.get("stop_tier", "small_bus"),
            "lines_json":   props.get("lines_json", ""),
            "dep_hr":       props.get("dep_hr", 0.0),
        }
    write_close_zoom_features(line_stops, line_lookup, stop_meta, stop_attrs,
                              end_of_platform_pairs,
                              skip_first_oids, skip_last_oids,
                              rail_idx=rail_idx, tram_idx=tram_idx,
                              parent_label_info=parent_label_info,
                              parent_leader=parent_leader)
    print(f"  [{time.perf_counter() - _t_phase:6.1f}s] write_close_zoom_features (visits + polygon bake + write)")

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
