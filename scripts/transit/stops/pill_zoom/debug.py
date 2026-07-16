"""Debug overlay writers for the pill-rendering pipeline."""
import json
from collections import defaultdict
from math import cos, radians

from _state import *  # noqa: F401,F403
from _state import _DIAG_BARS, _STABBED_PAIRS  # underscore names skipped by *
from stops.extent import _funicular_snap_override, _length_key, _platform_extent, _resolve_length
from geometry import _cum_dist_m, _interp_at, _project_meters, flatten_coords, haversine_km, snap_to_line


def write_debug_platforms(line_stops: dict, line_lookup: dict,
                           stop_attrs: dict, skip_first_oids: set,
                           skip_last_oids: set,
                           end_of_platform_pairs: set | None = None) -> None:
    """Emit transit_debug_platforms.geojson — one LineString per stop tracing
    the platform's full allowed range along the line's polyline. Debug-only
    overlay; replaces the previous black-dot debug feature.
    """
    cfg = PILL_CFG
    if not cfg.get("default_length_m"):
        print("  No pill_rendering config — debug platforms skipped.")
        return
    eop = end_of_platform_pairs or set()
    feats = []
    for osm_id, ls_entry in line_stops.items():
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        line = line_lookup.get(osm_id)
        if not line:
            continue
        mode = line["mode"]
        mo = line.get("mountain_origin")
        if _length_key(mode, mo) not in cfg["default_length_m"]:
            continue
        polyline = flatten_coords(line["coords"])
        if len(polyline) < 2:
            continue
        skip_first_here = str(osm_id) in skip_first_oids
        skip_last_here = str(osm_id) in skip_last_oids
        last_idx = len(triplets) - 1
        for idx, trip in enumerate(triplets):
            if idx == 0 and skip_first_here:
                continue
            if idx == last_idx and skip_last_here:
                continue
            if len(trip) < 3:
                continue
            stop_lon, stop_lat, stop_id = trip[0], trip[1], trip[2]
            atlas_length = (stop_attrs.get(stop_id, {}) or {}).get("length")
            is_eop = (str(osm_id), stop_id) in eop
            extent = _platform_extent(stop_lon, stop_lat, polyline,
                                       mode, atlas_length, cfg,
                                       end_of_platform=is_eop,
                                       mountain_origin=mo)
            if extent is None or len(extent) < 2:
                continue
            feats.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": MODE_MINZOOM.get(mode, 11)},
                "geometry": {"type": "LineString",
                             "coordinates": [list(p) for p in extent]},
                "properties": {"mode": mode, "stop_id": stop_id},
            })
    OUT_DEBUG_PLATFORMS.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": feats,
    }, ensure_ascii=False))
    print(f"  Debug platforms: {len(feats):,} features → {OUT_DEBUG_PLATFORMS}")


def write_debug_stops(line_stops: dict, line_lookup: dict,
                       stop_attrs: dict, stop_meta: dict,
                       skip_first_oids: set, skip_last_oids: set) -> None:
    """Emit transit_debug_stops.geojson — one Point per (line, stop) pair,
    1:1 with the debug platform lines. The point sits at the GTFS coord
    snapped onto that line's polyline (the same snap-to-line used by the
    pipeline's dot placement), so every debug line has a matching dot and
    every dot has a matching line.

    The popup data is keyed on stop_id and lists every line visiting that
    stop (with origin / destination), regardless of which line's snap this
    particular dot was rendered from.
    """
    cfg = PILL_CFG

    # First pass: per stop_id, build the (deduped) list of lines visiting it
    # plus the stop name. This populates the popup for every dot rendered
    # at this stop, regardless of which line's snap produced the dot.
    by_stop: dict = {}
    for osm_id, ls_entry in line_stops.items():
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        line = line_lookup.get(osm_id)
        if not line or not triplets:
            continue
        mode = line["mode"]
        if mode not in cfg.get("default_length_m", {}):
            continue
        first_trip = triplets[0]
        last_trip = triplets[-1]
        origin_sid = first_trip[2] if len(first_trip) >= 3 else ""
        dest_sid = last_trip[2] if len(last_trip) >= 3 else ""
        origin_name = (stop_meta.get(origin_sid, {}).get("name") or "?")
        dest_name = (stop_meta.get(dest_sid, {}).get("name") or "?")
        line_info = {
            "ref":         line.get("ref", ""),
            "mode":        mode,
            "color":       line.get("color", "#888888"),
            "origin":      origin_name,
            "destination": dest_name,
            "osm_id":      str(osm_id),
        }
        for trip in triplets:
            if len(trip) < 3:
                continue
            sid = trip[2]
            if not sid:
                continue
            entry = by_stop.get(sid)
            if entry is None:
                entry = {
                    "name": stop_meta.get(sid, {}).get("name", ""),
                    "visits": [],
                }
                by_stop[sid] = entry
            entry["visits"].append(line_info)

    per_stop_lines_json: dict = {}
    per_stop_name: dict = {}
    for sid, data in by_stop.items():
        by_key: dict = {}
        order = []
        for v in data["visits"]:
            key = (v["ref"], v["origin"], v["destination"])
            if key not in by_key:
                entry = {
                    "ref":         v["ref"],
                    "mode":        v["mode"],
                    "color":       v["color"],
                    "origin":      v["origin"],
                    "destination": v["destination"],
                    "osm_ids":     [v["osm_id"]],
                }
                by_key[key] = entry
                order.append(key)
            else:
                osm_ids = by_key[key]["osm_ids"]
                if v["osm_id"] not in osm_ids:
                    osm_ids.append(v["osm_id"])
        unique = [by_key[k] for k in order]
        per_stop_lines_json[sid] = json.dumps(unique, ensure_ascii=False)
        per_stop_name[sid] = data["name"]

    # Second pass: one dot per (line, stop) at the snapped position on that
    # line's polyline. 1:1 with debug platform lines (same filtering).
    feats = []
    for osm_id, ls_entry in line_stops.items():
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        line = line_lookup.get(osm_id)
        if not line or not triplets:
            continue
        mode = line["mode"]
        if mode not in cfg.get("default_length_m", {}):
            continue
        polyline = flatten_coords(line["coords"])
        if len(polyline) < 2:
            continue
        skip_first_here = str(osm_id) in skip_first_oids
        skip_last_here = str(osm_id) in skip_last_oids
        last_idx = len(triplets) - 1
        for idx, trip in enumerate(triplets):
            if idx == 0 and skip_first_here:
                continue
            if idx == last_idx and skip_last_here:
                continue
            if len(trip) < 3:
                continue
            lon, lat, sid = trip[0], trip[1], trip[2]
            if not sid:
                continue
            dot_lon, dot_lat = snap_to_line(lon, lat, polyline)
            attrs = stop_attrs.get(sid) or {}
            atlas_len = attrs.get("length") if isinstance(attrs, dict) else None
            stabbed = (str(osm_id), str(sid)) in _STABBED_PAIRS
            feats.append({
                "type": "Feature",
                "tippecanoe": {"minzoom": MODE_MINZOOM.get(mode, 11)},
                "geometry": {"type": "Point", "coordinates": [dot_lon, dot_lat]},
                "properties": {
                    "stop_id":          sid,
                    "stop_name":        per_stop_name.get(sid, ""),
                    "mode":             mode,
                    "platform_length":  atlas_len,
                    "lines_json":       per_stop_lines_json.get(sid, "[]"),
                    "stabbed":          stabbed,
                    "current_osm_id":   str(osm_id),
                },
            })
    OUT_DEBUG_STOPS.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": feats,
    }, ensure_ascii=False))
    stabbed_count = sum(1 for f in feats if f["properties"]["stabbed"])
    print(f"  Debug stops: {len(feats):,} features ({stabbed_count:,} stabbed) "
          f"→ {OUT_DEBUG_STOPS}")


def write_debug_bars() -> None:
    """Emit transit_debug_bars.geojson — one LineString per max-stab bar
    found during cluster processing. Each line spans the perpendicular
    extent of its stabbed dots (plus a small visual margin), so on the map
    the line draws exactly where the bar "is" in 2D.
    """
    feats = []
    for ep1, ep2 in _DIAG_BARS:
        feats.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": 5},
            "geometry": {"type": "LineString",
                         "coordinates": [list(ep1), list(ep2)]},
            "properties": {},
        })
    OUT_DEBUG_BARS.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": feats,
    }, ensure_ascii=False))
    print(f"  Debug bars: {len(feats):,} features → {OUT_DEBUG_BARS}")


# =============================================================================




# =============================================================================
# Pill geometry — nearest-neighbor path through dot positions
# =============================================================================

