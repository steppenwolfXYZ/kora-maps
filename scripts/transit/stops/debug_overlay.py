"""Debug overlay: pill-rendering diagnostics.

Emits three GeoJSON files consumed by the debug layers in
`scripts/style/transit_stations.py`:

  * transit_debug_platforms.geojson — one LineString per (line, stop) tracing
    the platform's full allowed range along the line's polyline.
  * transit_debug_stops.geojson — one Point per (line, stop) at the GTFS
    coordinate snapped onto that line's polyline. Carries the atlas platform
    length and the list of lines visiting that stop (with origin /
    destination) for click-popups. Stabbed dots (placed onto a max-stab bar
    by the pill-placement algorithm) render solid; non-stabbed dots are
    hollow.
  * transit_debug_bars.geojson — one LineString per max-stab bar found
    during cluster processing.

This file is the ONLY place that touches the debug overlay outputs. Toggle
via `debug.debug_overlay` in scripts/transit/config.yaml — when disabled
`emit_all` skips the writes and unlinks stale outputs so step 8 doesn't
build pmtiles from yesterday's data.

Delete-checklist (when the overlay is no longer needed at all):
  1. Delete this file.
  2. Remove the `emit_all` call at the tail of step 7 (stops/pipeline_render.py).
  3. Remove the `record_stabbed_pair` / `record_diag_bar` /
     `diag_bars_len` / `rescale_diag_bars_from` helper calls in
     stops/pill_zoom/options.py and stops/pill_zoom/place.py.
  4. Remove the debug source/layer blocks in scripts/generate_style.py
     and scripts/style/transit_stations.py.
  5. Remove the three `tl_debug_*.pmtiles` blocks in
     scripts/transit/08_build_pmtiles.sh.
  6. Remove the `debug` block from scripts/transit/config.yaml.
"""
import json

from _state import MODE_MINZOOM, PILL_CFG, ROOT
from common import load_transit_cfg
from geometry import flatten_coords, snap_to_line
from stops.extent import _length_key, _platform_extent


# ── Config ──────────────────────────────────────────────────────────────────
DEBUG_ENABLED = bool(
    (load_transit_cfg().get("debug") or {}).get("debug_overlay", False))

# ── Output paths ────────────────────────────────────────────────────────────
_OUT_PLATFORMS = ROOT / "data" / "transit" / "transit_debug_platforms.geojson"
_OUT_STOPS     = ROOT / "data" / "transit" / "transit_debug_stops.geojson"
_OUT_BARS      = ROOT / "data" / "transit" / "transit_debug_bars.geojson"

# ── In-memory tracking populated during pill placement ─────────────────────
# Populated unconditionally: the add-to-set / append-to-list operations are
# effectively free, so keeping the tracking always-on lets the placement
# algorithm stay branch-free. Only the file writes are gated by
# DEBUG_ENABLED.
#
# _STABBED_PAIRS: (osm_id, stop_id) tuples for (line, stop) records placed
#   on a max-stab bar. Read by _write_debug_stops to mark stabbed dots.
# _DIAG_BARS: (endpoint1, endpoint2) tuples for each max-stab bar's
#   perpendicular geometry, written by _write_debug_bars.
_STABBED_PAIRS: set = set()
_DIAG_BARS: list = []


# ── Recorder helpers (called from pill_zoom hot paths) ─────────────────────

def record_stabbed_pair(osm_id, stop_id) -> None:
    """Note a (line, stop) placement onto a max-stab bar."""
    _STABBED_PAIRS.add((str(osm_id), str(stop_id)))


def record_diag_bar(group, option) -> None:
    """Append `option`'s perpendicular bar geometry.

    Called from within the scaled-lon coordinate frame in
    coordinate_dots_global_stab; that driver's finally-block calls
    `rescale_diag_bars_from` to unscale after the cluster is done.
    """
    tx, ty, sigma = option["tx"], option["ty"], option["sigma"]
    nx, ny = -ty, tx
    n_values = [group[k]["lon"] * nx + group[k]["lat"] * ny
                for k in option["scoring"]]
    if len(n_values) < 2:
        return
    n_min, n_max = min(n_values), max(n_values)
    margin = (n_max - n_min) * 0.05 + 1e-6
    n_min -= margin
    n_max += margin
    ep1 = (sigma * tx + n_min * nx, sigma * ty + n_min * ny)
    ep2 = (sigma * tx + n_max * nx, sigma * ty + n_max * ny)
    _DIAG_BARS.append((ep1, ep2))


def diag_bars_len() -> int:
    return len(_DIAG_BARS)


def rescale_diag_bars_from(start_idx: int, cos_lat: float) -> None:
    """Undo the scaled-lon transform on bars appended since `start_idx`."""
    for i in range(start_idx, len(_DIAG_BARS)):
        ep1, ep2 = _DIAG_BARS[i]
        _DIAG_BARS[i] = (
            (ep1[0] / cos_lat, ep1[1]),
            (ep2[0] / cos_lat, ep2[1]),
        )


# ── Writers ────────────────────────────────────────────────────────────────

def _write_debug_platforms(line_stops: dict, line_lookup: dict,
                            stop_attrs: dict, skip_first_oids: set,
                            skip_last_oids: set,
                            end_of_platform_pairs: set | None = None) -> None:
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
    _OUT_PLATFORMS.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": feats,
    }, ensure_ascii=False))
    print(f"  Debug platforms: {len(feats):,} features → {_OUT_PLATFORMS}")


def _write_debug_stops(line_stops: dict, line_lookup: dict,
                        stop_attrs: dict, stop_meta: dict,
                        skip_first_oids: set, skip_last_oids: set) -> None:
    """One Point per (line, stop) pair, 1:1 with the debug platform lines.
    The point sits at the GTFS coord snapped onto that line's polyline (the
    same snap-to-line used by the pipeline's dot placement), so every debug
    line has a matching dot and every dot has a matching line.

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
    _OUT_STOPS.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": feats,
    }, ensure_ascii=False))
    stabbed_count = sum(1 for f in feats if f["properties"]["stabbed"])
    print(f"  Debug stops: {len(feats):,} features ({stabbed_count:,} stabbed) "
          f"→ {_OUT_STOPS}")


def _write_debug_bars() -> None:
    feats = []
    for ep1, ep2 in _DIAG_BARS:
        feats.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": 5},
            "geometry": {"type": "LineString",
                         "coordinates": [list(ep1), list(ep2)]},
            "properties": {},
        })
    _OUT_BARS.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": feats,
    }, ensure_ascii=False))
    print(f"  Debug bars: {len(feats):,} features → {_OUT_BARS}")


# ── Public entry point ─────────────────────────────────────────────────────

def emit_all(*, line_stops, line_lookup, stop_attrs, stop_meta,
             skip_first_oids, skip_last_oids,
             end_of_platform_pairs) -> None:
    """Single entry point invoked once at the tail of step 7. No-ops (and
    unlinks stale outputs so step 8 doesn't rebuild yesterday's pmtiles)
    when DEBUG_ENABLED is false."""
    if not DEBUG_ENABLED:
        for p in (_OUT_PLATFORMS, _OUT_STOPS, _OUT_BARS):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        return
    print("Emitting debug platform extents...")
    _write_debug_platforms(line_stops, line_lookup, stop_attrs,
                           skip_first_oids, skip_last_oids,
                           end_of_platform_pairs)
    print("Emitting debug stop dots...")
    _write_debug_stops(line_stops, line_lookup, stop_attrs, stop_meta,
                       skip_first_oids, skip_last_oids)
    print("Emitting debug max-stab bars...")
    _write_debug_bars()
