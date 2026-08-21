# ── Stop position lines ──────────────────────────────────────────────
# Re-used from the stop/dot placement: the same fitted-to-the-line
# extents the debug platform lines draw, under the same skip rules.
# The skips are what make this find the right line automatically — at
# a terminal the departure-side entry is skipped (skip_first) and the
# ARRIVAL line's extent survives; its geometry approaches along the
# street and ends at the stop, so the slice covers exactly the ground
# behind the stop where the departing queue stands. Never extrapolated.
stop_lines: dict = defaultdict(list)
for osm_id_raw, ls_entry in line_stops.items():
    osm_id = str(osm_id_raw)
    triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
    line = line_lookup.get(osm_id_raw) or line_lookup.get(osm_id)
    if not line or not triplets:
        continue
    mode = line["mode"]
    mo = line.get("mountain_origin")
    if _length_key(mode, mo) not in PILL_CFG.get("default_length_m", {}):
        continue
    polyline = flatten_coords(line["coords"])
    if len(polyline) < 2:
        continue
    skip_first_here = osm_id in skip_first_oids
    skip_last_here = osm_id in skip_last_oids
    last_idx = len(triplets) - 1
    for idx, trip in enumerate(triplets):
        if idx == 0 and skip_first_here:
            continue
        if idx == last_idx and skip_last_here:
            continue
        if len(trip) < 3 or not trip[2]:
            continue
        stop_lon, stop_lat, sid = trip[0], trip[1], trip[2]
        atlas_len = (stop_attrs.get(sid, {}) or {}).get("length")
        ext = _platform_extent(
            stop_lon, stop_lat, polyline, mode, atlas_len, PILL_CFG,
            end_of_platform=(osm_id, sid) in end_of_platform_pairs,
            mountain_origin=mo)
        if ext is None or len(ext) < 2:
            continue
        stop_lines[sid].append(
            {"osm_id": osm_id, "mode": mode,
             "ref": line.get("ref", ""),
             "agency_id": line.get("agency_id", ""),
             "extent": ext})

# ── Collect visits per stop_id (shared with the pre-fill counter) ────
per_stop_visits = _collect_close_zoom_visits(
    line_stops, line_lookup, stop_meta, stop_attrs,
    end_of_platform_pairs, with_extents=True,
    rail_idx=rail_idx, tram_idx=tram_idx)

features = []
# Point cloud per parent station; hulled into the backdrop afterwards.
parent_cloud: dict = defaultdict(list)
# Distinct line colors per parent — the backdrop takes the single line
# color, or a blend when several lines with different colors call.
parent_colors: dict = defaultdict(set)

def _parent_of(sid):
    # Cross-parent grouping (stops-close-zoom.md § Cross-parent grouping):
    # remap raw GTFS parent_station to the pill clusterer's leader parent
    # so multi-parent stations (Gümligen, Melchenbühl (Tram) 8507052 +
    # (Bus) 8577013) render as ONE hull, backdrop, and station label.
    meta = stop_meta.get(sid, {})
    raw = meta.get("uic") or meta.get("parent") or sid
    if parent_leader:
        return parent_leader.get(raw, raw)
    return raw

max_L = max(bc["length_m"] for bc in CLOSE_ZOOM_BANDS.values())
max_step = max(bc["length_m"] + CLOSE_ZOOM_STACK_GAP_M
               for bc in CLOSE_ZOOM_BANDS.values())

def _rightmost(group):
    """The group's rightmost cluster — the one whose on-line stop
    position sits furthest right (in direction of travel) in the frame
    of the group's first cluster."""
    g0 = group[0]
    N0 = (g0["tangent"][1], -g0["tangent"][0])
    P0 = _interp_at(g0["polyline"], g0["dists"], g0["t_stop"])
    path = g0
    best_off = float("-inf")
    for c in group:
        Pj = _interp_at(c["polyline"], c["dists"], c["t_stop"])
        dxm = (Pj[0] - P0[0]) * 111320.0 * g0["cos_lat"]
        dym = (Pj[1] - P0[1]) * 111320.0
        off = dxm * N0[0] + dym * N0[1]
        if off > best_off:
            best_off = off
            path = c
    return path

def _build_group_recs(pool_sids, visits_override=None):
    """Per track (list of stop_ids pooled into one queue for rail
    per-track clustering; a one-item list for every other case):
    variant collapse → direction groups → recs carrying the group's
    chosen path and its stop position line.

    Rail per-track clustering (stops-close-zoom.md § Rail
    per-track clustering): rail visits at one parent are clustered by
    extent shape before entering here. When called from that path,
    `visits_override` carries the cluster's visits explicitly and
    `pool_sids` is the set of distinct sids they occupy — a cluster
    may span multiple GTFS platform_codes (pfaedle collapsed both
    directions onto one shape) or the same platform_code may split
    across two clusters (pfaedle drew the two directions on distinct
    physical tracks). The rep sid — used for atlas length / extent
    lookups — is the pooled sid with the longest atlas length.

    Rail direction ordering (stops-close-zoom.md § Rail): all
    rail clusters at one track form ONE stack. Same-direction pill-arrows
    stay contiguous, and opposite-direction sub-groups sit at
    opposite ends of the stack with their fastest line at the
    outward-most position, so no two adjacent pill-arrows point at each
    other and each sub-group's chevrons point outward from the
    platform middle."""
    if visits_override is not None:
        visits = list(visits_override)
    else:
        visits = []
        for s in pool_sids:
            visits.extend(per_stop_visits.get(s, []))
    if not visits:
        return []
    # When a cluster is given, keep only the stop_lines entries for
    # (osm_id, sid) pairs the cluster actually contains — otherwise the
    # backbone preference cascade would draw candidates from features
    # in a neighbouring cluster.
    line_filter = None
    if visits_override is not None:
        line_filter = {(v["osm_id"], v["sid"]) for v in visits}
    # Rep sid for extent / atlas-length lookups. For a rail pool, the
    # longest atlas length is the full-platform stop_id (a "7" sid
    # over a "7A-C" sid); solo sids pick themselves.
    if len(pool_sids) == 1:
        rep_sid = pool_sids[0]
    else:
        rep_sid = max(
            pool_sids,
            key=lambda s: float(
                (stop_attrs.get(s, {}) or {}).get("length") or 0.0))

    # ── Collapse variants + direction groups (shared stacking rules) ─
    # One pill-arrow per (ref, agency, direction); same-direction clusters
    # share a stack — and, for non-rail, one path: when parallel lines
    # (e.g. tram + bus on the same street) serve the same stop, every
    # pill-arrow in the group follows the RIGHTMOST line so they line
    # up. Opposite directions form their own stack on the other side;
    # rail pools form ONE stack in _rail_direction_order's ordering.
    # The logic itself lives in _collapse_direction_stacks — shared
    # with the pre-fill stack-need counter so counted stack sizes
    # match what is drawn (stop-extent-osm-walk.md § Fill target).
    rail_pool, groups = _collapse_direction_stacks(visits)

    recs = []
    for group in groups:
        if not group:
            continue
        # Rail: path is the fastest forward cluster (cluster[0]) — its
        # tangent orients the queue course. _rightmost across a
        # mixed-direction group would be meaningless (opposite tangents
        # skew the right-normal projection). Non-rail groups are
        # single-direction, so _rightmost picks the rightmost parallel
        # line as before.
        if rail_pool:
            path = group[0]
        else:
            path = _rightmost(group)

        # Backbone: the group's stop position line. Non-rail: the
        # path's OWN extent comes first — since the stop-extent fill
        # gives terminal departures real, correctly-oriented rear
        # ground, a departure with ground is its own best course. The
        # pool (best: the same line's ARRIVAL counterpart, whose
        # geometry ends at the stop and covers the ground behind it)
        # is only the fallback when the departure has none — a
        # borrowed course carries the donor's orientation (Bern
        # Weissenbühl: the same-sid arrival extent pointed the 28's
        # pill-arrow into the terminus even though the departure had its
        # own filled ground). No direction gate on the fallback: at
        # corner terminals the arrival approaches on a different
        # street, near-perpendicular, and that is precisely the
        # ground the queue belongs on. For a rail pool, the search
        # stays pool-first across every pooled sid's stop_lines and,
        # at equal key rank, prefers the LONGEST extent — the full
        # platform beats any sector's sub-extent. Rail extents are
        # then normalised to align with the fastest cluster's tangent
        # (`_orient_rail_extent`) so a borrowed slice running the
        # other way (arrival counterpart at a terminus, longer
        # opposite-direction sibling in the pool) can't mirror every
        # chevron on the platform.
        pool_lines = []
        for s in pool_sids:
            for ln in stop_lines.get(s, []):
                if line_filter is None or (ln["osm_id"], s) in line_filter:
                    pool_lines.append(ln)
        ext = None
        if not rail_pool:
            atlas_len = (stop_attrs.get(rep_sid, {}) or {}).get("length")
            own = _platform_extent(
                path["stop_lon"], path["stop_lat"], path["polyline"],
                path["mode"], atlas_len, PILL_CFG,
                mountain_origin=path["mountain_origin"])
            if own is not None and len(own) >= 2:
                ext = own
        best_key = None
        best_len = -1.0
        if ext is None:
            for cand in pool_lines:
                if cand["osm_id"] == path["osm_id"] and not rail_pool:
                    ext = cand["extent"]
                    break
                key = ((cand["ref"], cand["agency_id"])
                       == (path["ref"], path["agency_id"]),
                       cand["mode"] == path["mode"],
                       cand["osm_id"] == path["osm_id"])
                cand_len = _cum_dist_m(cand["extent"])[-1] if cand["extent"] and len(cand["extent"]) >= 2 else 0.0
                if (best_key is None or key > best_key
                        or (key == best_key and cand_len > best_len)):
                    best_key = key
                    best_len = cand_len
                    ext = cand["extent"]
        if ext is None:
            atlas_len = (stop_attrs.get(rep_sid, {}) or {}).get("length")
            ext = _platform_extent(
                path["stop_lon"], path["stop_lat"], path["polyline"],
                path["mode"], atlas_len, PILL_CFG,
                end_of_platform=(path["osm_id"], rep_sid)
                                in end_of_platform_pairs,
                mountain_origin=path["mountain_origin"])
        # Mountain terminal (funicular / rack / rebucketed_rail): the
        # concept doc requires the pill-arrow at the polyline endpoint,
        # not at the middle of a platform-length slice. _platform_extent
        # returns an end-side slice whose extent[0] sits L metres
        # inward, so eop_rail anchors the pill-arrow inside the polyline
        # rather than at its end. Discard here so the extentless-
        # terminal synthesis branch below re-slices from the endpoint
        # inward with the correct extent[0] = endpoint orientation.
        # See stops-close-zoom.md § "Aerial + funicular terminals".
        if (rail_pool and path.get("is_extentless_terminal")
                and path["mountain_origin"] in
                ("funicular", "rack", "rebucketed_rail")):
            ext = None
        fwd_synth = False
        extentless_kind = None
        if not rail_pool and ext is not None and len(ext) >= 2:
            # Dead-end terminus course (stops-close-zoom.md):
            # every pill-arrow in the queue has zero rear ground on its own
            # line and the borrowed stop position line points against
            # the departures (near-180° — an arrival doubling back at
            # a dead-end road). Using it as the course would fold the
            # queue around the road end (Egg (Vorarlberg) Zentrum), so
            # it is discarded — the synth path below builds the course
            # from the departure's own forward geometry instead, same
            # as when no extent exists at all. Corner terminals (~90°)
            # fail the opposition test; loop termini have self-borrowed
            # rear ground and fail the zero-ground test.
            if all(v["t_stop"] < 0.5 for v in group):
                ax = _unit_chord_metric(ext[0], ext[-1], path["cos_lat"])
                if ax is not None and all(
                        (ax[0] * v["tangent"][0] + ax[1] * v["tangent"][1])
                        < -CLOSE_ZOOM_DIR_CLUSTER_COS for v in group):
                    ext = None
        if (ext is None or len(ext) < 2) and not rail_pool:
            # Terminal platform stretch (stops-close-zoom.md
            # § anchor): no rear ground exists at all — the extent
            # collapsed because the road ends at the stop (Laufenburg
            # (D) KiGa). Synthesize a short forward stub from the
            # path's own polyline so the queue course exists at all;
            # the stretch rule at course build then shifts the whole
            # stack forward onto the real line geometry.
            p_poly, p_dists = path["polyline"], path["dists"]
            t0 = path["t_stop"]
            t1 = min(p_dists[-1], t0 + 2.0)
            if t1 - t0 >= 0.5:
                ext = _slice_polyline(p_poly, p_dists, t0, t1)
                fwd_synth = True
        if (ext is None or len(ext) < 2) and rail_pool:
            # Extentless rail-style modes (stops-close-zoom.md
            # § "Aerial + funicular terminals" and § "Ferry"): aerial
            # cable-car stations and ferry piers have no natural
            # platform extent. Synthesize a slice of the transit
            # line at the stop so the queue course has an axis, and
            # mark the record so the pill-arrow placement code uses the
            # right anchor (endpoint for aerial/funicular terminals,
            # +10 m offset from pier for ferry).
            p_poly, p_dists = path["polyline"], path["dists"]
            poly_max = p_dists[-1]
            L_reach = max_L + max_step * max(len(group), 2)
            if path["mode"] == "ferry":
                pier_t = _ferry_pier_t_on_line(
                    path["stop_lon"], path["stop_lat"],
                    p_poly, p_dists)
                # Solo-ferry de-overlap stagger (see the ferry pier
                # clustering pass): shifts the whole extent forward
                # along this ferry's own polyline so the pill-arrow lands
                # past any earlier-placed solo ferry pill-arrow.
                stagger = float(path.get("ferry_extra_m", 0.0))
                t0 = pier_t
                t1 = min(poly_max, pier_t + L_reach
                         + CLOSE_ZOOM_FERRY_OFFSET_M + stagger)
                if t1 - t0 >= 0.5:
                    ext = _slice_polyline(p_poly, p_dists, t0, t1)
                    extentless_kind = "ferry"
            elif path.get("is_extentless_terminal"):
                # Aerial / funicular terminal: extent slice from the
                # polyline endpoint inward, oriented so the FIRST
                # point is the endpoint (so the eop_rail rule
                # anchors the fastest pill-arrow's back-cap there).
                if path["t_stop"] <= poly_max - path["t_stop"]:
                    # Start-side terminal.
                    t0 = 0.0
                    t1 = min(poly_max, L_reach)
                    if t1 - t0 >= 0.5:
                        ext = _slice_polyline(p_poly, p_dists, t0, t1)
                        extentless_kind = "endpoint"
                else:
                    # End-side terminal — reverse so the polyline
                    # endpoint sits at extent[0].
                    t1 = poly_max
                    t0 = max(0.0, poly_max - L_reach)
                    if t1 - t0 >= 0.5:
                        slice_pts = _slice_polyline(p_poly, p_dists,
                                                    t0, t1)
                        ext = list(reversed(slice_pts))
                        extentless_kind = "endpoint"
            else:
                # Aerial non-terminal (rare, e.g. V-Bahn intermediate):
                # centered slice around projected stop.
                t0 = max(0.0, path["t_stop"] - L_reach / 2.0)
                t1 = min(poly_max, path["t_stop"] + L_reach / 2.0)
                if t1 - t0 >= 0.5:
                    ext = _slice_polyline(p_poly, p_dists, t0, t1)
                    # No special anchor — standard rail centered.
        if ext is None or len(ext) < 2:
            continue
        # Hybrid tram: the tram default extent is backward-anchored
        # ([t - L, t]) whereas rail-style stacks center on the extent
        # middle. Recenter around the projected stop so the stack
        # doesn't sit half a platform length behind the stop.
        if (rail_pool and path.get("is_hybrid_rail_tram")
                and not extentless_kind):
            p_poly, p_dists = path["polyline"], path["dists"]
            ext_len = _cum_dist_m([tuple(p) for p in ext])[-1]
            if ext_len > 0:
                t = path["t_stop"]
                t_start = max(0.0, t - ext_len / 2.0)
                t_end = min(p_dists[-1], t + ext_len / 2.0)
                if t_end - t_start >= 0.5:
                    ext = _slice_polyline(p_poly, p_dists,
                                          t_start, t_end)
        eop_rail = rail_pool and any(
            (path["osm_id"], sid) in end_of_platform_pairs
            for sid in pool_sids)
        # Extentless terminal (aerial + funicular) + ferry all anchor
        # the fastest pill-arrow's back-cap at the extent's first point,
        # same rule as end-of-platform rail terminals. Funicular
        # terminals have an existing extent from _platform_extent
        # whose first point IS the polyline endpoint (start-terminal
        # case), so no synthesis was needed — but the anchor rule
        # still applies.
        if (extentless_kind in ("endpoint", "ferry")
                or path.get("is_extentless_terminal")):
            eop_rail = True
        if rail_pool and extentless_kind not in ("endpoint", "ferry"):
            # Skip rail orientation for extentless_kind == "endpoint"
            # / "ferry" — the extent is already oriented deliberately
            # (endpoint at [0], pier at [0]) and _orient_rail_extent
            # could flip it against the direction of travel.
            ext = _orient_rail_extent(ext, path["tangent"],
                                      path["cos_lat"])
        recs.append({"sid": rep_sid, "group": group, "path": path,
                     "ext": ext, "cut_pts": [], "fwd_synth": fwd_synth,
                     "eop_rail": eop_rail,
                     "extentless_kind": extentless_kind})
    return recs

per_parent_sids: dict = defaultdict(list)
for s in per_stop_visits:
    per_parent_sids[_parent_of(s)].append(s)

