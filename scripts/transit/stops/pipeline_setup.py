"""Step 07 pipeline body — `run()` is the former `main()` function of
step 07. Kept in its own module so the driver stays a thin ~400 line
file.

All shared constants (paths, MODE_*, PILL_CFG, ...) come in via
`from _state import *`; every helper it needs is imported explicitly."""
import colorsys
import json
import time
from collections import defaultdict
from itertools import permutations
from math import atan2, cos, degrees, floor, log, pi, radians, sin, sqrt

from _state import *  # noqa: F401,F403
from _state import _set_pill_design_band, _timed
from stops.cluster import (
    cluster_rail_stops, cluster_stops_for_pills,
    merge_clusters_by_parent_station,
)
from stops.salience import (
    OUT_URBANNESS,
    _build_uic_serving, _resolve_stop_tier, _uic_of, _zoom_rules_cfg,
    compute_dwell_per_uic, compute_stop_importance, compute_stop_min_zoom,
    compute_urbanness, count_buildings_in_radii, load_buildings,
)
from stops.borrow import _AllLinesIndex
from stops.terminal_fill import (
    _extend_nonrail_polylines_at_terminals, _extend_polylines_at_terminals,
)
from geometry import (
    _cum_dist_m, _directional_tangent_at, _interp_at, _meters_per_deg,
    _project_meters, _slice_polyline, _start_segment_tangent,
    flatten_coords, haversine_km, parse_time, snap_to_line,
)
from gtfs.loaders import load_stop_meta
from stop_attributes import (
    compute_terminus_skip_oids, load_atlas_attributes,
    load_stop_scores, load_stop_sloid, write_stop_attributes_diag,
)
from osm.walks import _load_way_index
from stops.close_zoom import (
    _collect_close_zoom_visits, _stack_need_by_stop,
    write_close_zoom_features,
)
from stops.dot_dedup import apply_stop_dedup
from stops.far_zoom import far_zoom_dot_position
from stops.ferry_snap import (
    _ferry_canonical_snap, _ferry_pier_t_on_line, _obb_overlap,
)
from stops.pill_zoom.lines import (
    build_indicator_features, cluster_lines, color_luminance,
    count_unique_lines, dominant_line, pill_minzoom,
)
from stops.pill_zoom.make import make_pill_features
from stops.pill_zoom.nn_path import nearest_neighbor_path
from stops.pill_zoom.place import coordinate_dots_global_stab


# Line-detail view (line-detail-view.md): saturation kept on the baked
# `color_desat` variant of each line's color — non-selected lines render
# with it while the view is open. Hue and lightness stay untouched.
LINE_DETAIL_DESAT_KEEP = 0.3


def _desaturate_hex(hex_color: str, keep: float) -> str:
    r = int(hex_color[1:3], 16) / 255
    g = int(hex_color[3:5], 16) / 255
    b = int(hex_color[5:7], 16) / 255
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    r, g, b = colorsys.hls_to_rgb(h, l, s * keep)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def run():
    _t_total = time.perf_counter()
    print("Loading lines...")
    with _timed("load lines + build line_lookup"):
        lines_data = json.loads(LINES.read_text())
        line_lookup = {}
        gtfs_stop_features = []
        for feat in lines_data["features"]:
            p   = feat["properties"]
            oid = str(p.get("osm_id", ""))
            if oid:
                line_lookup[oid] = {
                    "color":           p["color"],
                    "mode":            p["mode"],
                    "mountain_origin": p.get("mountain_origin"),
                    "width_base":      p.get("width_base", 3.0),
                    "freq_score":      p.get("freq_score", 0.0),
                    "f_weighted":      p.get("f_weighted", 0.0),
                    "speed_kmh":       p.get("speed_kmh"),
                    "salience":        p.get("salience"),
                    "min_zoom":        p.get("min_zoom"),
                    "coords":          feat["geometry"]["coordinates"],
                    "ref":             p.get("ref", ""),
                    "name":            p.get("name", ""),
                    "agency_id":       p.get("agency_id", ""),
                    "trip_group_id":   p.get("trip_group_id", ""),
                }
            if p.get("gtfs_stops"):
                gtfs_stop_features.append(feat)
        print(f"  {len(line_lookup):,} lines, {len(gtfs_stop_features):,} with embedded gtfs_stops")

    # Sibling index for the missing-range fill rule (tram/bus/regional_bus):
    # {(ref, agency_id, mode) → [(osm_id, flat_polyline)]}. The proximity
    # gate inside _borrow_backward_segment does the real filtering; this
    # index just bounds the search to same-line variants.
    #
    # The all-lines spatial index alongside is the non-sibling-borrow
    # backing, widened to any drawn polyline. Both are consumed only by the
    # upfront fill pass (_extend_nonrail_polylines_at_terminals) — the
    # borrowed geometry is prepended to the line polyline there, so
    # _platform_extent itself carries no fill logic. Built from the
    # pre-fill polylines, so donors are deterministic regardless of fill
    # order.
    with _timed("build sibling + all-lines spatial index"):
        sibling_groups: dict = defaultdict(list)
        oid_sibling_key: dict = {}
        all_lines_index = _AllLinesIndex()
        for oid_s, info in line_lookup.items():
            key = (info.get("ref", ""), info.get("agency_id", ""), info.get("mode", ""))
            flat_poly = flatten_coords(info["coords"])
            if len(flat_poly) >= 2:
                sibling_groups[key].append((oid_s, flat_poly))
                oid_sibling_key[oid_s] = key
                all_lines_index.add(oid_s, key, flat_poly, _cum_dist_m(flat_poly))

    print("Loading stop coordinates and metadata...")
    with _timed("load line_stops + stop_meta"):
        line_stops = json.loads(LINE_STOPS.read_text())
        stop_meta  = load_stop_meta()
        print(f"  {len(line_stops):,} lines with stops, {len(stop_meta):,} GTFS stop entries")

    # Enrich line_lookup with per-variant parent-UIC sequence + terminus
    # names so the station popup can build A ↔ B tooltips with subsumption
    # without re-joining line_stops on the client. See
    # `.claude/concepts/popups.md` § Line tooltip.
    with _timed("enrich line_lookup with parent_uics + terminus names"):
        for oid, info in line_lookup.items():
            stops_seq = (line_stops.get(oid) or {}).get("stops") or []
            parent_uics: list = []
            for entry in stops_seq:
                sid = entry[2] if len(entry) >= 3 else ""
                meta = stop_meta.get(sid) or stop_meta.get(sid.split(":")[0], {})
                uic = meta.get("parent") or (sid.split(":")[0] if sid else "")
                parent_uics.append(uic)
            info["parent_uics"] = parent_uics
            first_name = ""
            last_name = ""
            if parent_uics:
                first_uic = parent_uics[0]
                last_uic  = parent_uics[-1]
                first_meta = stop_meta.get(first_uic, {}) if first_uic else {}
                last_meta  = stop_meta.get(last_uic, {}) if last_uic else {}
                first_name = first_meta.get("name", "")
                last_name  = last_meta.get("name", "")
                if not first_name and stops_seq:
                    first_sid = stops_seq[0][2] if len(stops_seq[0]) >= 3 else ""
                    m = stop_meta.get(first_sid) or stop_meta.get(first_sid.split(":")[0], {})
                    first_name = m.get("name", "")
                if not last_name and stops_seq:
                    last_sid = stops_seq[-1][2] if len(stops_seq[-1]) >= 3 else ""
                    m = stop_meta.get(last_sid) or stop_meta.get(last_sid.split(":")[0], {})
                    last_name = m.get("name", "")
            info["first_terminus_name"] = first_name
            info["last_terminus_name"]  = last_name

    # Reverse index: parent-UIC → osm_ids whose stop sequence touches that
    # UIC. Used by cluster_lines / cluster_departures_per_hour to source
    # ALL variants of a line at a station (including those the terminus
    # dedup pass drops from the visible-stop pool). Rendering dedup must
    # not decide which lines a station is served by.
    with _timed("build oids_by_uic index"):
        oids_by_uic: dict = defaultdict(list)
        for oid, info in line_lookup.items():
            seen_uics: set = set()
            for uic in info.get("parent_uics") or []:
                if uic and uic not in seen_uics:
                    seen_uics.add(uic)
                    oids_by_uic[uic].append(oid)

    # ── Zoom-level rules: per-mode stop min_zoom ─────────────────────────────
    # See .claude/concepts/zoom-level-rules.md.
    print("Building per-UIC line index...")
    with _timed("_build_uic_serving"):
        uic_serving, coords_by_uic = _build_uic_serving(
            line_lookup, line_stops, stop_meta)
        print(f"  {len(uic_serving):,} canonical UICs across "
              f"{sum(len(v) for v in uic_serving.values()):,} (line, stop) pairs")

    zr_cfg = _zoom_rules_cfg()

    # Urbanness — building counts at two radii per UIC.
    print("Loading OSM building centroids...")
    with _timed("load_buildings (254 MB JSON parse)"):
        buildings = load_buildings()
        print(f"  {len(buildings):,} building centroids")
    urb_cfg = zr_cfg.get("urbanness") or {}
    r_in = float(urb_cfg.get("radius_inner_m", 200))
    r_out = float(urb_cfg.get("radius_outer_m", 500))
    print(f"  Counting buildings within {r_in:g}m / {r_out:g}m per UIC...")
    with _timed("count_buildings_in_radii"):
        building_counts = count_buildings_in_radii(coords_by_uic, buildings,
                                                   r_in, r_out)
    urbanness = compute_urbanness(building_counts, urb_cfg)
    OUT_URBANNESS.write_text(json.dumps(urbanness, ensure_ascii=False))
    bracket_counts = defaultdict(int)
    for v in urbanness.values():
        bracket_counts[v["bracket"]] += 1
    print(f"  Urbanness brackets: " +
          ", ".join(f"{k}={v}" for k, v in sorted(bracket_counts.items())) +
          f" → {OUT_URBANNESS}")

    # Dwell per UIC — read from data/transit/dwell_by_uic.json which step 06
    # populates as a side-effect of its stop_times.txt stream.
    print("Loading per-UIC dwell (from step 06 output)...")
    with _timed("compute_dwell_per_uic (reads dwell_by_uic.json)"):
        dwell_by_uic = compute_dwell_per_uic(stop_meta)
        if dwell_by_uic:
            avgs = list(dwell_by_uic.values())
            print(f"  {len(dwell_by_uic):,} UICs with dwell data; "
                  f"mean {sum(avgs)/len(avgs):.1f}s, "
                  f"max {max(avgs):.0f}s")

    # Stop importance score (4 categories, sum).
    si_cfg = zr_cfg.get("stop_importance") or {}
    nt_radius = float(si_cfg.get("nearby_transit_radius_m", 1000))
    with _timed("compute_stop_importance"):
        importance_by_uic = compute_stop_importance(
            uic_serving, coords_by_uic, urbanness, dwell_by_uic, nt_radius)
    imp_counts = defaultdict(int)
    for s in importance_by_uic.values():
        imp_counts[s] += 1
    print(f"  Importance scores: " +
          ", ".join(f"{k}={imp_counts[k]}" for k in sorted(imp_counts.keys())))

    # Intercity oid set (matches the train rule in 06).
    intercity_prefixes_cfg = zr_cfg.get("intercity_route_prefixes") or \
        ["IC", "ICE", "EC"]
    intercity_prefixes = tuple(str(p).upper() for p in intercity_prefixes_cfg)
    intercity_oids: set = set()
    for oid, info in line_lookup.items():
        if info.get("mode") != "train":
            continue
        r = (info.get("ref") or "").strip().upper()
        if any(r.startswith(p) for p in intercity_prefixes):
            intercity_oids.add(str(oid))

    # Stop tier lookup (per parent UIC) from step 06's stop_size_scores.json —
    # used by train z7/z8 tier gates. Empty dict if step 06 hasn't emitted it
    # (in which case tier-gated rules effectively reject every train stop and
    # the z9 catch-all takes over).
    stop_scores_lookup = load_stop_scores()
    stop_tier_by_uic = {uic: v["tier"] for uic, v in stop_scores_lookup.items()}

    print("Applying per-mode stop rules...")
    with _timed("compute_stop_min_zoom"):
        stop_min_zoom = compute_stop_min_zoom(
            line_lookup, line_stops, stop_meta,
            importance_by_uic, intercity_oids,
            uic_serving, coords_by_uic,
            stop_tier_by_uic=stop_tier_by_uic,
        )
    if stop_min_zoom:
        mzs = [v["min_zoom"] for v in stop_min_zoom.values()]
        mz_counts = defaultdict(int)
        for v in mzs:
            mz_counts[v] += 1
        print(f"  {len(stop_min_zoom):,} UICs scored. "
              f"min_zoom distribution: " +
              ", ".join(f"z{k}={mz_counts[k]}"
                        for k in sorted(mz_counts.keys())))

    # Pack into `stop_salience` shape used by the rest of main() — every
    # downstream block reads `min_zoom` and the few diagnostic keys below.
    stop_salience: dict = {}
    for uic, v in stop_min_zoom.items():
        stop_salience[uic] = {
            "min_zoom":           v["min_zoom"],
            "candidate_min_zoom": v["candidate_min_zoom"],
            "rule_label":         v["rule_label"],
            "is_intersection":    v["is_intersection"],
            "is_terminus":        v["is_terminus"],
            "tier":               v["tier"],
            "importance_score":   importance_by_uic.get(uic, 0),
            "urbanness_bracket":  urbanness.get(uic, {}).get("bracket", "rural"),
        }

    print("Loading atlas platform attributes...")
    with _timed("write_stop_attributes_diag (atlas + diag)"):
        stop_attrs = write_stop_attributes_diag(line_stops)

    print("Loading OSM rail ways for terminal extension...")
    with _timed("_load_way_index(rail, 34 MB)"):
        rail_idx = _load_way_index(RAIL_WAYS_GEOJSON, "rail")

    print("Extending train and mountain rail-like polylines at terminal stops...")
    with _timed("_extend_polylines_at_terminals (rail)"):
        end_of_platform_pairs = _extend_polylines_at_terminals(
            line_lookup, line_stops, rail_idx, PILL_CFG, stop_attrs)

    print("Loading OSM tram and street ways for the stop-extent fill...")
    with _timed("_load_way_index(tram, 3.6 MB, all_nodes)"):
        tram_idx = _load_way_index(TRAM_WAYS_GEOJSON, "tram",
                                   index_all_nodes=True)
    with _timed("_load_way_index(street, 149 MB, all_nodes)"):
        street_idx = _load_way_index(STREET_WAYS_GEOJSON, "street",
                                     index_all_nodes=True)

    # Fill targets (stop-extent-osm-walk.md § Fill target): count the
    # close-zoom pill-arrow stacks BEFORE the fill so bus/regional_bus
    # extensions are exactly as long as the drawn queue needs (capped at
    # L). Same visit collection + stacking rules the close-zoom rendering
    # uses later (post-fill, with extents).
    print("Counting close-zoom pill-arrow stacks for fill targets...")
    with _timed("_collect_close_zoom_visits (pre-fill counter, first of two runs)"):
        pre_fill_visits = _collect_close_zoom_visits(
            line_stops, line_lookup, stop_meta,
            rail_idx=rail_idx, tram_idx=tram_idx)
    with _timed("_stack_need_by_stop"):
        stack_need = _stack_need_by_stop(pre_fill_visits)
    print(f"  {len(stack_need):,} (line, stop) fill targets from "
          f"{len(pre_fill_visits):,} stops with pill-arrows")

    print("Extending tram/bus polylines at terminal stops (stop-extent fill)...")
    with _timed("_extend_nonrail_polylines_at_terminals"):
        fill_diag, filled_oids = _extend_nonrail_polylines_at_terminals(
            line_lookup, line_stops, tram_idx, street_idx,
            PILL_CFG, stop_attrs, stop_meta, sibling_groups, oid_sibling_key,
            stack_need, all_lines_index)
    OUT_STOP_EXTENT_FILL.write_text(json.dumps(fill_diag, ensure_ascii=False))
    print(f"  {len(fill_diag):,} fill records → {OUT_STOP_EXTENT_FILL}")

    # Per-line-group union bbox (line-detail-view.md): all variants of a
    # (ref, agency_id, mode, trip_group_id) group share one bbox covering
    # every variant's full geometry, so the client can fit the camera to
    # the whole line. Computed AFTER the terminal fills so extensions are
    # covered. Stored on line_lookup (for the popup badge entries built in
    # run_pills) and stamped on every line feature below.
    with _timed("compute per-line-group bboxes + line keys"):
        group_bbox: dict = {}
        for oid, info in line_lookup.items():
            key = line_key_of(info)
            info["line_key"] = key
            for lon, lat in flatten_coords(info["coords"]):
                bb = group_bbox.get(key)
                if bb is None:
                    group_bbox[key] = [lon, lat, lon, lat]
                else:
                    if lon < bb[0]: bb[0] = lon
                    if lat < bb[1]: bb[1] = lat
                    if lon > bb[2]: bb[2] = lon
                    if lat > bb[3]: bb[3] = lat
        for info in line_lookup.values():
            bb = group_bbox.get(info.get("line_key", ""))
            if bb:
                info["group_bbox"] = [round(v, 5) for v in bb]

    # Sync extended polylines into lines_data and write the FULL feature set
    # (extended and untouched lines alike) to transit_lines_extended.geojson —
    # step 08's pmtile build reads that file. The step-06 transit_lines.geojson
    # input is never modified. Scope of the geometry patch: train + mountain
    # rail-like (rebucketed_rail / rack) plus every tram/bus line the
    # stop-extent fill actually prepended geometry to.
    with _timed("sync extended polylines + write LINES_EXTENDED"):
        n_synced = 0
        for feat in lines_data["features"]:
            props = feat.get("properties") or {}
            mode = props.get("mode")
            mo = props.get("mountain_origin")
            oid = str(props.get("osm_id", ""))
            info = line_lookup.get(oid) if oid else None
            # Desaturated color for the line-detail view's non-selected
            # lines. Stamped on every feature that carries a color.
            color = props.get("color")
            if color:
                props["color_desat"] = _desaturate_hex(
                    color, LINE_DETAIL_DESAT_KEEP)
                feat["properties"] = props
            # Stamp terminus names on every line feature so the line popup
            # can render "A ↔ B" without a side-channel lookup. Written even
            # when the geometry itself doesn't get extended below.
            if info:
                props["first_terminus_name"] = info.get("first_terminus_name") or ""
                props["last_terminus_name"]  = info.get("last_terminus_name") or ""
                # Line-detail-view identity + camera fit (line-detail-view.md):
                # canonical key of the feature's (ref, agency_id, mode,
                # trip_group_id) group and the group's union bbox as
                # "minLon,minLat,maxLon,maxLat".
                props["line_key"] = info.get("line_key", "")
                bb = info.get("group_bbox")
                if bb:
                    props["line_bbox"] = ",".join(str(v) for v in bb)
                feat["properties"] = props
            is_rail_scope = mode == "train" or (
                mode == "mountain" and mo in MOUNTAIN_RAIL_ORIGINS)
            if not is_rail_scope and oid not in filled_oids:
                continue
            if not oid or not info or "coords" not in info:
                continue
            feat["geometry"]["type"] = "LineString"
            feat["geometry"]["coordinates"] = [list(c) for c in info["coords"]]
            n_synced += 1
        LINES_EXTENDED.write_text(json.dumps(lines_data, ensure_ascii=False))
        print(f"  Wrote {n_synced:,} extended polylines → {LINES_EXTENDED.name} "
              f"({LINES.name} left pristine)")

    # Line index (line-detail-view.md § Deep link): line_key → {ref, mode,
    # color, bbox, route}. Consumed client-side to resolve a ?line=<key>
    # URL param into a full LineDetailSelection payload. Aggregates from
    # the lines_data features written above; every feature already carries
    # line_key + line_bbox (per-group union bbox is identical on all
    # variants) + termini names + color/mode/ref.
    with _timed("write OUT_LINE_INDEX"):
        line_index: dict[str, dict] = {}
        for feat in lines_data["features"]:
            props = feat.get("properties") or {}
            key = props.get("line_key") or ""
            if not key:
                continue
            entry = line_index.get(key)
            if entry is None:
                bbox_str = props.get("line_bbox") or ""
                bbox_parts = bbox_str.split(",") if bbox_str else []
                bbox: list[float] | None = None
                if len(bbox_parts) == 4:
                    try:
                        bbox = [float(v) for v in bbox_parts]
                    except ValueError:
                        bbox = None
                if bbox is None:
                    continue
                entry = {
                    "ref":     props.get("ref") or "",
                    "mode":    props.get("mode") or "",
                    "color":   props.get("color") or "#888888",
                    "bbox":    bbox,
                    "termini": [],
                }
                line_index[key] = entry
            for tk in ("first_terminus_name", "last_terminus_name"):
                name = props.get(tk) or ""
                if name and name not in entry["termini"]:
                    entry["termini"].append(name)
        out: dict[str, dict] = {}
        for key, entry in line_index.items():
            termini = entry["termini"]
            if len(termini) == 2:
                route = f"{termini[0]} ↔ {termini[1]}"
            else:
                route = " · ".join(termini)
            out[key] = {
                "ref":   entry["ref"],
                "mode":  entry["mode"],
                "color": entry["color"],
                "bbox":  entry["bbox"],
                "route": route,
            }
        OUT_LINE_INDEX.parent.mkdir(parents=True, exist_ok=True)
        OUT_LINE_INDEX.write_text(json.dumps(out, ensure_ascii=False))
        print(f"  Line index: {len(out):,} lines → {OUT_LINE_INDEX}")

    with _timed("compute_terminus_skip_oids"):
        skip_first_oids, skip_last_oids = compute_terminus_skip_oids(
            line_stops, line_lookup, stop_meta)
        print(f"  Terminus dedup: {len(skip_first_oids):,} departure-side entries "
              f"will be omitted from rendering (popup retains both directions)")
        print(f"  Arrival drop (tram/bus/regional_bus): {len(skip_last_oids):,} "
              f"unpaired or layover-shadowed arrival entries omitted from pill construction")

    from stops.pipeline_render import run_pills
    run_pills(
        line_lookup=line_lookup,
        line_stops=line_stops,
        stop_meta=stop_meta,
        stop_min_zoom=stop_min_zoom,
        stop_attrs=stop_attrs,
        end_of_platform_pairs=end_of_platform_pairs,
        fill_diag=fill_diag,
        filled_oids=filled_oids,
        skip_first_oids=skip_first_oids,
        skip_last_oids=skip_last_oids,
        rail_idx=rail_idx,
        tram_idx=tram_idx,
        coords_by_uic=coords_by_uic,
        uic_serving=uic_serving,
        gtfs_stop_features=gtfs_stop_features,
        stop_salience=stop_salience,
        oids_by_uic=oids_by_uic,
    )
    _dt_total = time.perf_counter() - _t_total
    print(f"\n  ═══════════════════════════════════════════════════════")
    print(f"  Step 07 total wall-clock: {_dt_total:.1f}s ({_dt_total/60:.1f} min)")
    print(f"  ═══════════════════════════════════════════════════════")
