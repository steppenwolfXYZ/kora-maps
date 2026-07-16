"""Terminal polyline extension (the upfront stop-extent fill): the rail
OSM walk at train terminals and the tram/bus/regional_bus fill pass
(sibling borrow -> non-sibling borrow -> OSM street/tram walk). Writes the
extended geometry back into line_lookup; downstream platform extents are
then plain polyline slices. See stop-extent-osm-walk.md."""
from collections import defaultdict
from math import cos, radians, sqrt

from _state import *  # noqa: F401,F403 — shared constants
from geometry import (
    _cum_dist_m, _directional_tangent_at, _project_meters,
    _start_segment_tangent, flatten_coords, haversine_km,
)
from osm.walks import (
    OSM_FALLBACK_MAX_STRAIGHT_M, TERMINAL_SNAP_TOLERANCE_M,
    _osm_rail_walk, _osm_street_walk,
)
from stops.borrow import _borrow_backward_nonsibling, _borrow_backward_segment
from stops.extent import _resolve_length

def _extend_polylines_at_terminals(line_lookup, line_stops, rail_idx,
                                     pill_cfg, stop_attrs):
    """Extend train and mountain rail-like polylines at terminal stops via
    OSM rail walk (Fallback A's capped straight when no way matches).
    Modifies `line_lookup[oid]["coords"]` in place.

    Scope:
      • `mode == "train"` — full rail.
      • `mode == "mountain"` with `mountain_origin in MOUNTAIN_RAIL_ORIGINS`
        (rebucketed_rail / rack) — physical rail (narrow_gauge), present in
        `data/osm/rail_ways.geojson`. Uses `mountain_rail` length config.

    Funicular and aerial mountain origins are skipped: funicular tracks are
    `railway=funicular` (not in step 03's rail extraction), and aerial
    cable cars have no rail geometry at all.

    Returns the set of (osm_id, stop_id) pairs that hit case 2 — the OSM
    walk matched a way but the track ran out before reaching L/2. The
    walked partial IS prepended (the drawn line reaches OSM's true end,
    nothing is fabricated past it) and these stops use asymmetric
    anchoring in `_platform_extent`: the walked ground x outward plus
    L − x inward from the snap, so the range still totals L.
    """
    end_of_platform_pairs: set = set()
    if not pill_cfg.get("default_length_m"):
        return end_of_platform_pairs

    n_walk = n_straight = n_eop = 0

    for oid, info in line_lookup.items():
        mode = info.get("mode")
        mo = info.get("mountain_origin")
        if mode != "train" and not (
                mode == "mountain" and mo in MOUNTAIN_RAIL_ORIGINS):
            continue
        coords = info.get("coords")
        if not coords:
            continue
        flat = flatten_coords(coords)
        if len(flat) < 2:
            continue
        flat = [(c[0], c[1]) for c in flat]
        dists = _cum_dist_m(flat)
        poly_max = dists[-1]
        if poly_max <= 0:
            continue

        ls_entry = line_stops.get(str(oid))
        if ls_entry is None:
            ls_entry = line_stops.get(oid)
        if not ls_entry:
            continue
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        if not triplets:
            continue

        terminals = []
        first_trip = triplets[0]
        if len(first_trip) >= 3:
            terminals.append(("start", first_trip))
        if len(triplets) > 1:
            last_trip = triplets[-1]
            if len(last_trip) >= 3:
                terminals.append(("end", last_trip))

        prepend_coords = None
        append_coords = None

        for which, trip in terminals:
            stop_lon, stop_lat, sid = trip[0], trip[1], trip[2]
            t_snap = _project_meters(stop_lon, stop_lat, flat, dists)
            if which == "start":
                if t_snap > TERMINAL_SNAP_TOLERANCE_M:
                    continue
                t_endpoint = 0.0
                ep_lon, ep_lat = flat[0]
            else:
                if poly_max - t_snap > TERMINAL_SNAP_TOLERANCE_M:
                    continue
                t_endpoint = poly_max
                ep_lon, ep_lat = flat[-1]

            tan = _directional_tangent_at(flat, dists, t_endpoint, window_m=20.0)
            if tan is None:
                continue
            sign = +1.0 if which == "end" else -1.0
            # Normalise tangent to per-metre units (already per-metre in
            # _directional_tangent_at), apply sign to flip for start-end.
            walk_dx = tan[0] * sign
            walk_dy = tan[1] * sign

            atlas_len = (stop_attrs.get(sid, {}) or {}).get("length")
            L = _resolve_length(mode, atlas_len, pill_cfg, mountain_origin=mo)
            if L is None or L <= 0:
                continue
            target_m = L / 2.0

            status, walk_coords = _osm_rail_walk(
                rail_idx, ep_lon, ep_lat, walk_dx, walk_dy, target_m)

            if status == "walk":
                ext = walk_coords
                n_walk += 1
            elif status == "ran_out":
                # Case 2 (stop-extent-osm-walk.md § Rail walk): OSM ended
                # before L/2. Prepend the walked partial — never fabricate
                # geometry past OSM's true end — and flag the stop so
                # _platform_extent splits the range asymmetrically (walked
                # x outward + L − x inward from the snap).
                end_of_platform_pairs.add((str(oid), sid))
                n_eop += 1
                if walk_coords is None or len(walk_coords) < 2:
                    continue
                ext = walk_coords
            else:
                # Fallback A: capped straight extension.
                cap_m = min(target_m, OSM_FALLBACK_MAX_STRAIGHT_M)
                ext_end_lon = ep_lon + walk_dx * cap_m
                ext_end_lat = ep_lat + walk_dy * cap_m
                ext = [(ep_lon, ep_lat), (ext_end_lon, ext_end_lat)]
                n_straight += 1

            if which == "start":
                # Extension goes from ep outward; for prepending we want it
                # to end at ep, so reverse.
                prepend_coords = list(reversed(ext))
            else:
                append_coords = ext

        if prepend_coords is None and append_coords is None:
            continue

        new_flat = []
        if prepend_coords is not None:
            new_flat.extend(prepend_coords[:-1])
        new_flat.extend(flat)
        if append_coords is not None:
            new_flat.extend(append_coords[1:])
        info["coords"] = new_flat

    print(f"  Terminal rail extension: walk={n_walk}, "
          f"straight={n_straight}, end-of-platform={n_eop}")
    return end_of_platform_pairs



# =============================================================================
# Upfront tram/bus missing-range fill (stop-extent-osm-walk.md § "Fill runs
# once, upfront")
# =============================================================================

NONRAIL_FILL_MODES = ("tram", "bus", "regional_bus")


def _extend_nonrail_polylines_at_terminals(line_lookup, line_stops,
                                             tram_idx, street_idx,
                                             pill_cfg, stop_attrs, stop_meta,
                                             sibling_groups, oid_sibling_key,
                                             stack_need, all_lines_index):
    """Run the whole tram/bus/regional_bus missing-range fill once per line,
    before any extent consumer: sibling borrow → non-sibling borrow → OSM
    street/tram walk (no straight fallback — when nothing matches, nothing
    is appended; short-but-true beats long-but-wrong). Whatever geometry the
    fill produces is PREPENDED to `line_lookup[oid]["coords"]` — the drawn
    line must always reach the platform ground its extent covers, and
    downstream platform extents become plain polyline slices with no fill
    logic (same as rail).

    Scope: every line whose backward-anchored extent [t−L, t] would run off
    the polyline start for at least one of its stops (in practice the
    departure terminal, where the polyline begins at the stop). The prepend
    is sized to the largest deficit across the line's stops, so one fill
    covers every stop and every extent consumer.

    Returns (diag_records, filled_oids). diag_records carries one dict per
    line that needed fill, with the fill source and any remaining deficit —
    written to stop_extent_fill.json and read by the offender / shortened
    diagnostics. filled_oids is the set of osm_ids whose coords changed
    (main() writes those into transit_lines_extended.geojson).
    """
    diag: list = []
    filled_oids: set = set()
    if not pill_cfg.get("default_length_m"):
        return diag, filled_oids

    counts: dict = defaultdict(int)

    for oid, info in line_lookup.items():
        mode = info.get("mode")
        if mode not in NONRAIL_FILL_MODES:
            continue
        coords = info.get("coords")
        if not coords:
            continue
        flat = flatten_coords(coords)
        if len(flat) < 2:
            continue
        flat = [(c[0], c[1]) for c in flat]
        dists = _cum_dist_m(flat)
        if dists[-1] <= 0:
            continue

        ls_entry = line_stops.get(str(oid)) or line_stops.get(oid)
        if not ls_entry:
            continue
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry

        # Largest backward deficit across the line's stops, measured
        # against each stop's FILL TARGET (stop-extent-osm-walk.md § Fill
        # target): trams keep the full fixed L; buses extend only as far
        # as the stop's close-zoom pill-arrow stack actually needs, capped
        # at L. Stops without a counted stack (no pill drawn there, e.g.
        # layover bays) conservatively keep L. Only stops near the
        # polyline start can need fill (t is arc-length from the start),
        # so a cheap distance prefilter avoids the O(V) projection for
        # the vast majority of stops.
        start_lon, start_lat = flat[0]
        need = 0.0
        drive = None   # (trip, t, target) of the stop driving the deficit
        for trip in triplets:
            if len(trip) < 3:
                continue
            atlas_len = (stop_attrs.get(trip[2], {}) or {}).get("length")
            L = _resolve_length(mode, atlas_len, pill_cfg)
            if L is None or L <= 0:
                continue
            if mode == "tram":
                target = L
            else:
                target = min(L, stack_need.get((str(oid), trip[2]), L))
            if target <= 0:
                continue
            if haversine_km(trip[0], trip[1],
                            start_lon, start_lat) * 1000.0 > target + 200.0:
                continue
            t = _project_meters(trip[0], trip[1], flat, dists)
            if target - t > need:
                need = target - t
                drive = (trip, t, target)
        if drive is None or need <= 0:
            continue

        trip, t_min, target = drive
        sid = trip[2]
        rec = {
            "osm_id":    str(oid),
            "ref":       info.get("ref", ""),
            "agency_id": info.get("agency_id", ""),
            "mode":      mode,
            "stop_id":   sid,
            "stop_name": (stop_meta.get(sid, {}) or {}).get("name", ""),
            "lon":       trip[0],
            "lat":       trip[1],
            "needed_m":  round(need, 1),
        }

        # Anchor and direction exactly as the per-extent fill used them:
        # the polyline start, with the first non-stub segment's tangent
        # (±2 m averaged tangent at the stop's snap as fallback).
        anchor = flat[0]
        anchor_tan = (_start_segment_tangent(flat, dists)
                      or _directional_tangent_at(flat, dists, t_min,
                                                  window_m=2.0))
        if anchor_tan is None:
            rec["source"] = "none"
            rec["filled_m"] = 0.0
            rec["deficit_m"] = rec["needed_m"]
            diag.append(rec)
            counts["none"] += 1
            continue
        anchor_dx, anchor_dy = anchor_tan

        fill = None       # backward→forward coords ending ~at flat[0]
        source = None
        filled_m = 0.0

        sib_key = oid_sibling_key.get(str(oid))
        siblings = sibling_groups.get(sib_key, []) if sib_key else []
        if siblings:
            seg = _borrow_backward_segment(
                anchor[0], anchor[1], anchor_dx, anchor_dy,
                t_min, target, siblings, str(oid))
            if seg is not None and len(seg) >= 2:
                fill = seg
                source = "sibling_borrow"
                filled_m = need

        if fill is None and all_lines_index is not None:
            own_key = all_lines_index.own_sib_key(str(oid))
            seg = _borrow_backward_nonsibling(
                anchor[0], anchor[1], anchor_dx, anchor_dy,
                t_min, target, str(oid), own_key, all_lines_index)
            if seg is not None and len(seg) >= 2:
                fill = seg
                source = "nonsibling_borrow"
                filled_m = need

        if fill is None:
            way_index = tram_idx if mode == "tram" else street_idx
            status, walk_coords = _osm_street_walk(
                way_index, anchor[0], anchor[1],
                -anchor_dx, -anchor_dy, need)
            if walk_coords is not None and len(walk_coords) >= 2:
                # walk_coords run anchor → outward; the prepend needs
                # backward → forward order ending at the anchor.
                fill = list(reversed(walk_coords))
                if status == "walk":
                    source = "walk"
                    filled_m = need
                else:
                    source = "partial_walk"
                    filled_m = _cum_dist_m(walk_coords)[-1]
            else:
                source = "none"
                filled_m = 0.0

        rec["source"] = source
        rec["filled_m"] = round(filled_m, 1)
        rec["deficit_m"] = round(max(0.0, need - filled_m), 1)
        diag.append(rec)
        counts[source] += 1

        if fill is not None:
            # fill[-1] sits on the polyline start (walk: exactly; borrows:
            # within the 1 m proximity gate) — drop it and join fill[-2] →
            # flat[0] directly, matching the geometry the per-extent borrow
            # used to produce.
            info["coords"] = fill[:-1] + flat
            filled_oids.add(str(oid))

    summary = ", ".join(
        f"{k}={counts[k]}"
        for k in ("sibling_borrow", "nonsibling_borrow", "walk",
                  "partial_walk", "none")
        if counts.get(k)) or "nothing to fill"
    print(f"  Tram/bus stop-extent fill: {summary}")
    return diag, filled_oids
