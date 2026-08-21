"""Close-zoom visit collection + stack-need counter."""
from collections import defaultdict
from math import cos, radians, sqrt

from gtfs.stop_identity import merge_key_of

from _state import *  # noqa: F401,F403
from stops.extent import _platform_extent
from geometry import (
    _cum_dist_m, _directional_tangent_at, _interp_at, _project_meters,
    flatten_coords, haversine_km,
)
from stops.close_zoom.constants import *  # noqa: F401,F403
from stops.close_zoom.helpers import (
    _is_hybrid_tram_stop, _rail_direction_order,
    _unit_tangent_metric, _variant_priority,
)
from stops.close_zoom.text import _shorten_destination


def _collapse_direction_stacks(visits):
    """Shared close-zoom stacking rules: collapse visits into one cluster
    per (ref, agency, direction of travel) — same line number + agency
    within the 45° tangent gate merge into one pill-arrow, destinations collected
    across the merged variants, highest-priority variant first (frequency
    for road modes, speed for rail — see _variant_priority) — then group
    same-direction clusters into stacks. Rail pools form ONE stack in
    _rail_direction_order's ordering; non-rail groups split per direction.

    Returns (rail_pool, groups). Each cluster carries `member_oids` — every
    osm_id collapsed into it, head (drawn) variant first.

    Used by BOTH the pill-arrow construction (_build_group_recs) and the
    pre-fill stack-need counter (_stack_need_by_stop) so the counted stack
    sizes cannot drift from what is actually drawn — see
    stop-extent-osm-walk.md § Fill target.
    """
    visits = sorted(visits, key=lambda v: (-_variant_priority(v),
                                           v["osm_id"]))
    clusters = []
    for v in visits:
        merged = False
        for c in clusters:
            if c["ref"] != v["ref"] or c["agency_id"] != v["agency_id"]:
                continue
            dot = (c["tangent"][0] * v["tangent"][0]
                   + c["tangent"][1] * v["tangent"][1])
            if dot >= CLOSE_ZOOM_DIR_CLUSTER_COS:
                if v["destination"] and v["destination"] not in c["destinations"]:
                    c["destinations"].append(v["destination"])
                c["member_oids"].append(v["osm_id"])
                merged = True
                break
        if not merged:
            c = dict(v)
            c["destinations"] = [v["destination"]] if v["destination"] else []
            c["dir_forward"] = True
            c["member_oids"] = [v["osm_id"]]
            clusters.append(c)

    rail_pool = bool(clusters) and clusters[0]["is_rail_like"]
    if rail_pool:
        groups = [_rail_direction_order(clusters)]
    else:
        groups = []
        for c in clusters:
            placed = False
            for g in groups:
                dot = (g[0]["tangent"][0] * c["tangent"][0]
                       + g[0]["tangent"][1] * c["tangent"][1])
                if dot >= CLOSE_ZOOM_DIR_CLUSTER_COS:
                    g.append(c)
                    placed = True
                    break
            if not placed:
                groups.append([c])
    return rail_pool, groups


def _collect_close_zoom_visits(line_stops, line_lookup, stop_meta,
                                stop_attrs=None,
                                end_of_platform_pairs=None,
                                with_extents=False,
                                rail_idx=None,
                                tram_idx=None):
    """Collect the close-zoom pill-arrow visits per stop_id — one entry per
    (line, departure stop) under the close-zoom skip rules (departures
    only, layover-departure dedup), carrying the tangent, priority and
    destination data the stacking rules read.

    Shared between write_close_zoom_features (rendering) and the pre-fill
    stack-need counter (stop-extent-osm-walk.md § Fill target): both see
    the same visits, so the counted stack sizes match what is drawn. The
    counter runs BEFORE the tram/bus fill (the fill target depends on the
    counts), rendering after — geometry-derived fields (t_stop, tangent)
    therefore differ slightly between the two runs at filled terminals;
    the grouping reads tangents only through the 45° direction gate, which
    absorbs the fill's tangent-matched joins.

    `with_extents` adds the rail platform extent per visit (needs
    stop_attrs + end_of_platform_pairs) — rendering-only data the counter
    skips.

    `rail_idx` / `tram_idx` (optional): when provided, tram stops whose
    shaped position lies close to a `railway=narrow_gauge` / `light_rail`
    OSM way (closer than any nearby `railway=tram` way) are flagged
    `is_hybrid_rail_tram=True` and treated as rail-style. See
    stops-close-zoom.md § "Hybrid tram detection". Missing indices
    disable the check (all trams stay tram-style).
    """
    eop = end_of_platform_pairs or set()

    # Per-line destination display names.
    line_dest = {}
    for oid, entry in line_stops.items():
        triplets = entry.get("stops", []) if isinstance(entry, dict) else entry
        if not triplets or len(triplets[-1]) < 3:
            continue
        last_sid = triplets[-1][2]
        line_dest[str(oid)] = stop_meta.get(last_sid, {}).get("name", "")

    # Loop-line apexes (stops-close-zoom.md § Text): when first and
    # last stop share a UIC, "to <terminus>" is useless at the terminus
    # itself (Bad Zurzach buses 1-4 all showing "Bahnhof" at Bahnhof).
    # Stops before the apex — the stop geographically furthest from the
    # terminus — show the apex as destination instead; the apex and every
    # later stop keep the terminus. Stops sharing the terminus UIC are
    # never apex candidates: loops may pass through the terminus mid-route
    # (Bad Zurzach bus 4), and picking that call would relabel the whole
    # outbound leg with the terminus name.
    line_loop_apex = {}   # osm_id → (apex_idx, apex_name)
    for oid, entry in line_stops.items():
        triplets = entry.get("stops", []) if isinstance(entry, dict) else entry
        n = len(triplets)
        if n < 3 or len(triplets[0]) < 3 or len(triplets[-1]) < 3:
            continue
        first_sid, last_sid = triplets[0][2], triplets[-1][2]
        if not first_sid or not last_sid:
            continue
        term_uic = merge_key_of(first_sid)
        if term_uic != merge_key_of(last_sid):
            continue
        t_lon, t_lat = triplets[0][0], triplets[0][1]
        cos_lat = cos(radians(t_lat))
        if cos_lat <= 0.0:
            cos_lat = 1.0
        best = None
        for i in range(1, n - 1):
            sid = triplets[i][2] if len(triplets[i]) >= 3 else ""
            if not sid or merge_key_of(sid) == term_uic:
                continue
            dx = (triplets[i][0] - t_lon) * 111320.0 * cos_lat
            dy = (triplets[i][1] - t_lat) * 111320.0
            d2 = dx * dx + dy * dy
            if best is None or d2 > best[0]:
                best = (d2, i, sid)
        if best is None:
            continue
        apex_name = stop_meta.get(best[2], {}).get("name", "")
        if apex_name:
            line_loop_apex[str(oid)] = (best[1], apex_name)

    per_stop_visits: dict = defaultdict(list)
    for osm_id_raw, ls_entry in line_stops.items():
        osm_id = str(osm_id_raw)
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        line = line_lookup.get(osm_id_raw) or line_lookup.get(osm_id)
        if not line:
            continue
        mode = line["mode"]
        mo = line.get("mountain_origin")
        if mode not in CLOSE_ZOOM_PILL_MODES:
            continue
        polyline = flatten_coords(line["coords"])
        if len(polyline) < 2:
            continue
        dists = _cum_dist_m(polyline)
        if dists[-1] <= 0:
            continue
        # Layover-departure dedup — the departure-side mirror of the
        # pill/dot arrival-drop rule 2 (see compute_terminus_skip_oids):
        # the feature's FIRST stop is skipped when it has no platform_code
        # and the same feature calls again at the same UIC at a
        # platform-coded stop later (non-final, so the revisit itself gets
        # the pill-arrow). Canonical case: Bern bus 30 departs the bare :10001
        # layover, then serves platform :A of the same station — without
        # the skip the line shows twice at the station. NOT copied from
        # the dot side: skip_first_oids, which drops the departure whenever
        # any sibling ARRIVES at the same stop_id — fine for dots (the
        # arrival dot survives) but fatal here, where arrivals get no pill-arrow
        # and the departure pill-arrow is the line's only presence at a terminus.
        skip_first_layover = False
        first_sid = triplets[0][2] if len(triplets[0]) >= 3 else ""
        if first_sid and not (stop_meta.get(first_sid, {})
                              or {}).get("platform_code"):
            first_uic = merge_key_of(first_sid)
            for later in triplets[1:-1]:
                l_sid = later[2] if len(later) >= 3 else ""
                if (l_sid and merge_key_of(l_sid) == first_uic
                        and (stop_meta.get(l_sid, {})
                             or {}).get("platform_code")):
                    skip_first_layover = True
                    break

        last_idx = len(triplets) - 1
        for idx, trip in enumerate(triplets):
            if len(trip) < 3:
                continue
            # Departures only: the line's last stop is an arrival — no pill-arrow
            # there ("17 to Bern, Bahnhof" at Bern, Bahnhof makes no sense).
            if idx == last_idx:
                continue
            if idx == 0 and skip_first_layover:
                continue
            stop_lon, stop_lat, sid = trip[0], trip[1], trip[2]
            if not sid:
                continue
            cos_lat = cos(radians(stop_lat))
            if cos_lat <= 0.0:
                cos_lat = 1.0
            t_stop = _project_meters(stop_lon, stop_lat, polyline, dists)
            T = _unit_tangent_metric(polyline, dists, t_stop, cos_lat)
            if T is None:
                continue
            N = (T[1], -T[0])  # right normal in direction of travel
            # Signed lateral offset of the raw GTFS coord from the line:
            # positive = stop sits right of the line in direction of travel.
            sx, sy = _interp_at(polyline, dists, t_stop)
            dxm = (stop_lon - sx) * 111320.0 * cos_lat
            dym = (stop_lat - sy) * 111320.0
            signed_d = dxm * N[0] + dym * N[1]
            # Hybrid tram detection (stops-close-zoom.md § "Hybrid
            # tram detection"): a tram stop sitting on narrow_gauge /
            # light_rail infrastructure (Forchbahn outside Zürich's inner
            # network) is treated as rail-style at exactly that stop. The
            # check runs on the tram's projected position on its own shape.
            is_hybrid_rail_tram = False
            if mode == "tram" and rail_idx is not None:
                is_hybrid_rail_tram = _is_hybrid_tram_stop(
                    sx, sy, rail_idx, tram_idx)
            is_rail_like = (
                mode in CLOSE_ZOOM_RAIL_MODES
                or (mode == "mountain"
                    and mo in CLOSE_ZOOM_RAIL_MOUNTAIN_ORIGINS)
                or is_hybrid_rail_tram
            )
            # Mountain terminal detection (stops-close-zoom.md
            # § "Aerial + funicular terminals"): at a terminal stop the
            # pill-arrow anchors AT the polyline endpoint. Primary check
            # is the trip's first-stop index — departures-only above
            # skips idx == last_idx, so idx == 0 is the only terminal
            # position that reaches here. The metric fallback handles
            # rare pfaedle shapes that overshoot the terminal stop.
            # Applies to aerial, funicular, and rack / rebucketed_rail
            # cog railways; non-terminal mid-stops (V-Bahn intermediate,
            # funicular mid-stops) fall through to the centered rule.
            is_extentless_terminal = False
            if mode == "mountain" and mo in ("aerial", "funicular", "rack",
                                              "rebucketed_rail"):
                if (idx == 0
                        or t_stop <= CLOSE_ZOOM_TERMINAL_SNAP_M
                        or dists[-1] - t_stop <= CLOSE_ZOOM_TERMINAL_SNAP_M):
                    is_extentless_terminal = True
            # Full platform extent along the line (atlas length; same logic
            # as the debug platform overlay) — feeds the backdrop hull so
            # the yellow area covers the whole platform, not just the
            # pill-arrow span. Rendering-only; skipped for the counter run.
            extent = None
            if is_rail_like and with_extents:
                atlas_len = ((stop_attrs or {}).get(sid, {}) or {}).get("length")
                extent = _platform_extent(
                    stop_lon, stop_lat, polyline, mode, atlas_len, PILL_CFG,
                    end_of_platform=(osm_id, sid) in eop,
                    mountain_origin=mo)
            per_stop_visits[sid].append({
                "sid":                    sid,
                "osm_id":                 osm_id,
                "mode":                   mode,
                "mountain_origin":        mo,
                "color":                  line["color"],
                "ref":                    line.get("ref", ""),
                "agency_id":              line.get("agency_id", ""),
                "speed_kmh":              line.get("speed_kmh") or 0.0,
                "f_weighted":             line.get("f_weighted") or 0.0,
                "width_base":             float(line.get("width_base") or 0.0),
                "polyline":               polyline,
                "dists":                  dists,
                "t_stop":                 t_stop,
                "stop_lon":               stop_lon,
                "stop_lat":               stop_lat,
                "cos_lat":                cos_lat,
                "tangent":                T,
                "signed_d":               signed_d,
                "is_rail_like":           is_rail_like,
                "is_hybrid_rail_tram":    is_hybrid_rail_tram,
                "is_extentless_terminal": is_extentless_terminal,
                "extent":                 extent,
                "destination":            _shorten_destination(
                    (line_loop_apex[osm_id][1]
                     if osm_id in line_loop_apex
                     and idx < line_loop_apex[osm_id][0]
                     else line_dest.get(osm_id, "")),
                    stop_meta.get(sid, {}).get("name", "")),
            })
    return per_stop_visits


def _stack_need_by_stop(per_stop_visits):
    """Per (osm_id, stop_id): the ground the stop's close-zoom pill-arrow
    queue occupies at the largest band — (n − 1) · step + pill-arrow length for
    the direction stack the line's pill-arrow belongs to (n = drawn pill-arrows
    in that stack). Every osm_id collapsed into a stack maps to its
    stack's reach, so lines whose variant merged into another pill-arrow still
    get the right target. Rail pools are skipped — the tram/bus fill
    target is the only reader (stop-extent-osm-walk.md § Fill target).
    """
    max_L = max(bc["length_m"] for bc in CLOSE_ZOOM_BANDS.values())
    max_step = max(bc["length_m"] + CLOSE_ZOOM_STACK_GAP_M
                   for bc in CLOSE_ZOOM_BANDS.values())
    need: dict = {}
    for sid, visits in per_stop_visits.items():
        rail_pool, groups = _collapse_direction_stacks(visits)
        if rail_pool:
            continue
        for g in groups:
            reach = (len(g) - 1) * max_step + max_L
            for c in g:
                for oid in c["member_oids"]:
                    need[(str(oid), sid)] = reach
    return need


