import json
for parent, parent_sids in per_parent_sids.items():
    recs = []
    # Rail per-track clustering (stops-close-zoom.md § Rail
    # per-track clustering): rail visits with extents cluster by
    # lateral proximity — the extents must run within 1 m of each
    # other over at least CLOSE_ZOOM_RAIL_CLUSTER_MIN_FRAC of the
    # shorter extent's length — transitive via union-find, and each
    # cluster becomes one queue on one physical track. The fraction
    # gate is what prevents a brief crossing at a station-throat
    # switch node (lateral ≈ 0 at the shared point but overlap is
    # only a few metres) from fusing whole platforms — Bern's SBB
    # tracks and the RBS underground tracks cross overhead but their
    # extents run parallel for only a tiny stretch. GTFS
    # platform_code is not consulted for grouping. All other visits
    # — non-rail, or aerial mountain without a fitted extent — stay
    # sid-solo and go through the non-rail same-curb resolution below
    # where applicable. Ferries and aerials are keyed per-line rather
    # than per-sid (see the elif branches), so multiple lines at the
    # same pier / boarding station never share a stack.
    pools: dict = defaultdict(list)
    cluster_candidates = []
    ferry_visits = []
    for sid in sorted(parent_sids):
        visits = per_stop_visits.get(sid, [])
        if not visits:
            continue
        is_rail = any(v["is_rail_like"] for v in visits)
        for v in visits:
            if (is_rail
                    and v.get("extent")
                    and len(v["extent"]) >= 2
                    and v["is_rail_like"]):
                cluster_candidates.append(v)
            elif (v["mode"] == "mountain"
                  and v.get("mountain_origin") == "aerial"):
                # Aerials never pool (stops-close-zoom.md
                # § "Aerials never pool"). Sibling aerials commonly
                # list the same GTFS parent stop_id (Grindelwald
                # Terminal: GGM + Eiger Express both use sid
                # 8505226); pooling by sid would drag the slower
                # aerial onto the fastest's polyline. Key by
                # (osm_id, sid) so each aerial line gets its own
                # solo stack anchored on its own polyline endpoint.
                pools[("A:" + str(v["osm_id"]) + ":" + sid,)].append(v)
            elif v["mode"] == "ferry":
                # Deferred to the ferry pier clustering pass below —
                # ferries at a parent need to be considered together
                # (both for merge and for solo de-overlap staggering).
                ferry_visits.append(v)
            else:
                pools[("S:" + sid,)].append(v)
    # ── Ferry pier clustering + solo de-overlap ────────────────────────
    # stops-close-zoom.md § "Ferries never pool" and § "Ferry
    # pier clustering". Two rules act on the ferry visits at this
    # parent, in order:
    #   1. Merge: ferries whose polylines run laterally within
    #      CLOSE_ZOOM_FERRY_CLUSTER_LATERAL_M for at least
    #      CLOSE_ZOOM_FERRY_CLUSTER_MIN_FRAC of the shorter slice
    #      (measured over the first CLOSE_ZOOM_FERRY_CLUSTER_WINDOW_M
    #      m out of the pier, and pointing the same way within
    #      CLOSE_ZOOM_DIR_CLUSTER_COS) share ONE rail-style pool on
    #      the fastest ferry's polyline — pill-arrows queue behind
    #      each other on the shared line. Canonical case: two ferry
    #      routes leaving Spiez Schiffstation both toward Thun on the
    #      same OSM ferry way.
    #   2. Solo de-overlap: solo ferries (each on their own polyline)
    #      whose pill-arrows would land world-close to another solo
    #      ferry's — the lines diverge but the pill-arrows at
    #      pier + CLOSE_ZOOM_FERRY_OFFSET_M still collide — are shifted
    #      further along their own polyline by (max_L +
    #      CLOSE_ZOOM_STACK_GAP_M) increments until they clear.
    #      Fastest ferry stays at pier + FERRY_OFFSET; slower ones
    #      take turns further out.
    if ferry_visits:
        nf = len(ferry_visits)
        uf = list(range(nf))

        def _fc_find(i):
            while uf[i] != i:
                uf[i] = uf[uf[i]]
                i = uf[i]
            return i

        # Per-ferry pier position and forward slice used for clustering.
        for v in ferry_visits:
            poly, dists = v["polyline"], v["dists"]
            v["_pier_t"] = _ferry_pier_t_on_line(
                v["stop_lon"], v["stop_lat"], poly, dists)
            t_end = min(dists[-1],
                        v["_pier_t"] + CLOSE_ZOOM_FERRY_CLUSTER_WINDOW_M)
            if t_end - v["_pier_t"] >= 0.5:
                v["_pier_slice"] = _slice_polyline(
                    poly, dists, v["_pier_t"], t_end)
            else:
                v["_pier_slice"] = None
        for i in range(nf):
            sA = ferry_visits[i].get("_pier_slice")
            if sA is None or len(sA) < 2:
                continue
            for j in range(i + 1, nf):
                sB = ferry_visits[j].get("_pier_slice")
                if sB is None or len(sB) < 2:
                    continue
                dot = (ferry_visits[i]["tangent"][0]
                        * ferry_visits[j]["tangent"][0]
                       + ferry_visits[i]["tangent"][1]
                        * ferry_visits[j]["tangent"][1])
                if dot < CLOSE_ZOOM_DIR_CLUSTER_COS:
                    continue
                m = _extent_overlap(
                    sA, sB, ferry_visits[i]["cos_lat"],
                    lateral_threshold_m=CLOSE_ZOOM_FERRY_CLUSTER_LATERAL_M)
                if m is None:
                    continue
                if m[0] >= CLOSE_ZOOM_FERRY_CLUSTER_MIN_FRAC:
                    uf[_fc_find(i)] = _fc_find(j)
        clusters_by_root: dict = defaultdict(list)
        for i, v in enumerate(ferry_visits):
            clusters_by_root[_fc_find(i)].append(v)
        solo_ferries = []
        for root, members in clusters_by_root.items():
            if len(members) >= 2:
                pools[("FM:" + str(root),)].extend(members)
            else:
                solo_ferries.append(members[0])
                pools[("F:" + str(members[0]["osm_id"])
                       + ":" + members[0]["sid"],)].append(members[0])
        # Ring-alternating pill-arrow layout
        # (stops-close-zoom.md § "Ring-alternating pill-arrow
        # layout"): each petal is a merged pool (fastest ferry's
        # polyline as axis) or a solo (its own polyline as axis),
        # carrying a queue of drawn pill-arrows. Each pill-arrow has
        # its OWN ring assignment; ring r's world position on a
        # petal is arc distance pier_t + FERRY_OFFSET + max_L/2 +
        # r*step on the petal's axis, tangent sampled from the
        # polyline at that arc distance — no straight-line-from-pier
        # assumption. Iterate rings from the pier outward, OBB-
        # checking every pair of pill-arrows sharing a ring. On
        # conflict, exactly ONE pill-arrow moves one ring outward
        # (its queue-mates behind it cascade to keep their order;
        # the ones before it STAY — the queue develops gaps and the
        # two lines zipper into each other's gaps). First conflict
        # of a pair: the petal with fewer pill-arrows yields.
        # Repeat conflict of the same pair at a later ring:
        # ALTERNATE — whichever petal moved last stays, the other
        # moves. Per-pair tracking keeps independent lines
        # untouched and handles 3-way overlaps pair-by-pair.
        max_L = max(bc["length_m"] for bc in CLOSE_ZOOM_BANDS.values())
        step = max_L + CLOSE_ZOOM_STACK_GAP_M
        # z18 is the reference zoom for the visual overlap check —
        # z17 slight overlap is fine, z18 is where the pill-arrows
        # must read as separate. Band B is the z18 design.
        pill_W = float(CLOSE_ZOOM_BANDS["B"]["width_m"])
        # 0.5 m padding on each OBB half-dimension so near-touches
        # count too — the pill-arrow border and the transit line
        # drawn through the queue eat into the space between bare
        # rectangles that are close-but-not-overlapping.
        _obb_pad_m = 0.5
        pill_center_off = max_L / 2.0
        pill_L_half = max_L / 2.0 + _obb_pad_m
        pill_W_half = pill_W / 2.0 + _obb_pad_m
        # Build petals. The queue entries are the DRAWN pill-arrows,
        # not the raw visits: downstream, _collapse_direction_stacks
        # merges same-(ref, agency) variants into one pill-arrow, so
        # the queue groups member visits by (ref, agency) and orders
        # the groups by speed descending — the same priority order
        # the drawn stack uses (ferry priority is speed_kmh).
        petals = []
        for root, members in clusters_by_root.items():
            subgroups: dict = {}
            for v in members:
                subgroups.setdefault(
                    (v["ref"], v["agency_id"]), []).append(v)
            pills = sorted(
                subgroups.values(),
                key=lambda g: -max((v.get("speed_kmh") or 0.0)
                                   for v in g))
            axis = max(members,
                       key=lambda v: v.get("speed_kmh") or 0.0)
            petals.append({
                "poly": axis["polyline"],
                "dists": axis["dists"],
                "pier_t": axis["_pier_t"],
                "cos_lat": axis["cos_lat"],
                "pills": pills,
                "rings": list(range(len(pills))),
            })
        if len(petals) >= 2:
            # Local metric frame anchored on the first petal's pier
            # position — cos_lat is essentially identical across
            # petals at one parent station, so a shared frame is fine.
            ref_lon, ref_lat = petals[0]["poly"][0][0], petals[0]["poly"][0][1]
            mx_per_deg = 111320.0 * cos(radians(ref_lat))
            my_per_deg = 111320.0

            def _pill_pos_tangent(p, ring):
                arc_t = (p["pier_t"] + CLOSE_ZOOM_FERRY_OFFSET_M
                         + pill_center_off + ring * step)
                dists = p["dists"]
                arc_t = min(dists[-1], arc_t)
                lon, lat = _interp_at(p["poly"], dists, arc_t)
                T = _unit_tangent_metric(
                    p["poly"], dists, arc_t, p["cos_lat"])
                if T is None:
                    T = (1.0, 0.0)
                cx = (lon - ref_lon) * mx_per_deg
                cy = (lat - ref_lat) * my_per_deg
                return cx, cy, T[0], T[1]

            # Iteration cap: a pathological non-diverging pair
            # (which should have merged upstream anyway) cannot
            # loop forever.
            pair_last_mover: dict = {}
            max_iter = 200
            for _ in range(max_iter):
                moved_this_pass = False
                max_ring = max(max(p["rings"]) for p in petals)
                for r in range(max_ring + 1):
                    at_ring = []
                    for pet_i, p in enumerate(petals):
                        for q_i, ring in enumerate(p["rings"]):
                            if ring == r:
                                cx, cy, tx, ty = _pill_pos_tangent(p, r)
                                at_ring.append(
                                    (pet_i, q_i, cx, cy, tx, ty,
                                     len(p["pills"])))
                    # pet_i → queue index of its conflicting
                    # pill-arrow at this ring.
                    movers: dict = {}
                    for i in range(len(at_ring)):
                        (pet_a, q_a, cxa, cya, txa, tya,
                         na) = at_ring[i]
                        for j in range(i + 1, len(at_ring)):
                            (pet_b, q_b, cxb, cyb, txb, tyb,
                             nb) = at_ring[j]
                            if pet_a == pet_b:
                                continue
                            if _obb_overlap(cxa, cya, txa, tya,
                                            pill_L_half, pill_W_half,
                                            cxb, cyb, txb, tyb,
                                            pill_L_half, pill_W_half):
                                key = (min(pet_a, pet_b),
                                       max(pet_a, pet_b))
                                prev = pair_last_mover.get(key)
                                if prev is None:
                                    # First conflict for this pair —
                                    # fewest pill-arrows yields; ties
                                    # break by higher index (stable
                                    # deterministic choice).
                                    if na < nb:
                                        mover, m_q = pet_a, q_a
                                    elif nb < na:
                                        mover, m_q = pet_b, q_b
                                    elif pet_a > pet_b:
                                        mover, m_q = pet_a, q_a
                                    else:
                                        mover, m_q = pet_b, q_b
                                else:
                                    # Repeat conflict — alternate:
                                    # whoever moved last stays, the
                                    # OTHER moves.
                                    if prev == pet_a:
                                        mover, m_q = pet_b, q_b
                                    else:
                                        mover, m_q = pet_a, q_a
                                pair_last_mover[key] = mover
                                if (mover not in movers
                                        or m_q < movers[mover]):
                                    movers[mover] = m_q
                    if movers:
                        for pet_i, q_i in movers.items():
                            p = petals[pet_i]
                            # Only the conflicting pill-arrow moves;
                            # queue-mates behind it cascade to keep
                            # their order, the ones before it stay.
                            p["rings"][q_i] += 1
                            for q2 in range(q_i + 1, len(p["rings"])):
                                if p["rings"][q2] <= p["rings"][q2 - 1]:
                                    p["rings"][q2] = p["rings"][q2 - 1] + 1
                        moved_this_pass = True
                        break  # rescan from ring 0
                if not moved_this_pass:
                    break

        # Apply the resolved rings to visits: each drawn pill-arrow's
        # extra is (assigned ring − natural queue position) · step,
        # stamped on every member visit of its (ref, agency)
        # subgroup so the collapsed cluster head carries it. The
        # extent-synth branch and the pill-arrow placement both read
        # ferry_extra_m per pill-arrow, on top of the queue-index
        # step the drawn stack already applies.
        for petal in petals:
            for q_i, pill_group in enumerate(petal["pills"]):
                extra = (petal["rings"][q_i] - q_i) * step
                if extra > 0:
                    for v in pill_group:
                        v["ferry_extra_m"] = extra
    if cluster_candidates:
        n = len(cluster_candidates)
        cl_uf = list(range(n))

        def _cl_find(i):
            while cl_uf[i] != i:
                cl_uf[i] = cl_uf[cl_uf[i]]
                i = cl_uf[i]
            return i

        for i in range(n):
            extA = cluster_candidates[i]["extent"]
            for j in range(i + 1, n):
                extB = cluster_candidates[j]["extent"]
                m = _extent_overlap(
                    extA, extB, cluster_candidates[i]["cos_lat"],
                    lateral_threshold_m=CLOSE_ZOOM_CURB_LATERAL_RAIL_M)
                if m is None:
                    continue
                if m[0] >= CLOSE_ZOOM_RAIL_CLUSTER_MIN_FRAC:
                    cl_uf[_cl_find(i)] = _cl_find(j)
        for i in range(n):
            pools[("T:" + str(_cl_find(i)),)].append(
                cluster_candidates[i])
    for key in sorted(pools):
        pool_visits = pools[key]
        pool_sids = sorted({v["sid"] for v in pool_visits})
        recs.extend(_build_group_recs(pool_sids,
                                      visits_override=pool_visits))

    # ── Same-curb resolution (non-rail only) ─────────────────────────
    # Canonical case: Bern, Schanzenstrasse — southbound city bus 20
    # at :10001 and southbound regional 100/101 at :10000, a few
    # metres apart on one curb. Rail is handled entirely by the
    # per-track clustering pass above and returns None here.
    def _same_curb(a, b):
        a_rail = a["path"]["is_rail_like"]
        b_rail = b["path"]["is_rail_like"]
        if a_rail != b_rail:
            return None
        if a_rail:
            # Rail is handled upstream by per-track clustering, which
            # already used the 1 m gate on the per-visit extents.
            return None
        dot = (a["path"]["tangent"][0] * b["path"]["tangent"][0]
               + a["path"]["tangent"][1] * b["path"]["tangent"][1])
        if dot < CLOSE_ZOOM_DIR_CLUSTER_COS:
            return None
        lat_tol = CLOSE_ZOOM_CURB_LATERAL_M
        m = _extent_overlap(a["ext"], b["ext"], a["path"]["cos_lat"],
                            lateral_threshold_m=lat_tol)
        # No close samples ⇒ no shared curb (avoids feeding None
        # intervals into _shorten_curb downstream).
        if m is None or m[0] <= 0.0 or m[2] is None:
            return None
        return m

    if len(recs) > 1:
        # Merge pass: transitive (union-find) on the original lines.
        uf = list(range(len(recs)))

        def _find(i):
            while uf[i] != i:
                uf[i] = uf[uf[i]]
                i = uf[i]
            return i

        for i in range(len(recs)):
            for j in range(i + 1, len(recs)):
                m = _same_curb(recs[i], recs[j])
                if m and m[0] > CLOSE_ZOOM_CURB_MERGE_FRAC:
                    uf[_find(i)] = _find(j)
        by_root: dict = defaultdict(list)
        for i in range(len(recs)):
            by_root[_find(i)].append(i)
        new_recs = []
        for idxs in by_root.values():
            if len(idxs) == 1:
                new_recs.append(recs[idxs[0]])
                continue
            members = [recs[i] for i in idxs]
            # One stop: pool the clusters, re-collapse same
            # (ref, agency, direction) across the platform ids,
            # priority-first; rightmost path over the pooled set
            # (non-rail) or fastest-forward (rail); union platform
            # line. For rail, opposite-direction clusters are kept
            # separate here and re-ordered via
            # `_rail_direction_order` after the priority sort so the
            # merged stack still follows the direction-outward rule.
            pooled = []
            for r in members:
                for c in r["group"]:
                    tgt = None
                    for p in pooled:
                        if (p["ref"] != c["ref"]
                                or p["agency_id"] != c["agency_id"]):
                            continue
                        dot = (p["tangent"][0] * c["tangent"][0]
                               + p["tangent"][1] * c["tangent"][1])
                        if dot >= CLOSE_ZOOM_DIR_CLUSTER_COS:
                            tgt = p
                            break
                    if tgt is None:
                        pooled.append(c)
                    else:
                        for dst in c["destinations"]:
                            if dst and dst not in tgt["destinations"]:
                                tgt["destinations"].append(dst)
            pooled.sort(key=lambda c: (-_variant_priority(c),
                                       c["osm_id"]))
            merged_rail = pooled and pooled[0]["is_rail_like"]
            if merged_rail:
                pooled = _rail_direction_order(pooled)
                path = pooled[0]
            else:
                path = _rightmost(pooled)
            ext = _union_extents([r["ext"] for r in members],
                                 path["cos_lat"], chord_w=max_L)
            if merged_rail:
                ext = _orient_rail_extent(ext, path["tangent"],
                                          path["cos_lat"])
            new_recs.append({"sid": path["sid"], "group": pooled,
                             "path": path, "ext": ext, "cut_pts": [],
                             # Union ground is only forward-synthetic
                             # when every member's was.
                             "fwd_synth": all(r.get("fwd_synth")
                                              for r in members),
                             # If any member track was end-of-platform,
                             # the merged track is too.
                             "eop_rail": merged_rail and any(
                                 r.get("eop_rail") for r in members)})
        recs = new_recs

        # Shorten pass — pair by pair on the current (post-merge)
        # geometry, recomputing the overlap before each cut.
        for i in range(len(recs)):
            for j in range(i + 1, len(recs)):
                m = _same_curb(recs[i], recs[j])
                if m is None:
                    continue
                _shorten_curb(recs[i], m[2])
                _shorten_curb(recs[j], m[3])

    # ── Queue course + per-pill-arrow work items ───────────────────────────
    work = []
    for rec in recs:
        group, path, ext = rec["group"], rec["path"], rec["ext"]
        # Queue course: the stop position line (own point order — its
        # front end is the stop end by construction) extended dead
        # straight far enough for the deepest band of this stack.
        # Anchors come from the stop position line ALONE (its forward
        # end for road-mode queues, its middle for rail stacks) — the
        # raw GTFS stop coordinate plays no part in placement.
        reach = (len(group) - 1) * max_step + max_L
        # Terminal platform stretch (stops-close-zoom.md
        # § anchor): at a terminal stop of tram/bus the extent ends
        # where real geometry ends. When the stack needs more rear
        # room than the stop position line offers, the course
        # continues along the REAL forward line geometry (the path's
        # own polyline past the stop) instead of dead straight, and
        # the queue anchor shifts forward by the per-band shortfall —
        # the stack is moved along the line in the direction where
        # line geometry exists, never drawn into the void. Non-
        # terminal stops (rear room on the line well beyond the
        # extent) keep the dead-straight rule.
        rear_ground = (0.0 if rec.get("fwd_synth")
                       else _cum_dist_m([tuple(p) for p in ext])[-1])
        stretch = False
        front_on_m = 0.0
        course_ext = ext
        if (not path["is_rail_like"]
                and reach > rear_ground + 0.1
                and path["t_stop"] <= rear_ground + 10.0):
            p_poly, p_dists = path["polyline"], path["dists"]
            t0 = path["t_stop"]
            t1 = min(p_dists[-1], t0 + (reach - rear_ground) + max_L)
            if t1 - t0 >= 0.5:
                fslice = _slice_polyline(p_poly, p_dists, t0, t1)
                if rec.get("fwd_synth"):
                    # The stub already IS forward geometry — replace
                    # it with the full-length forward slice.
                    course_ext = fslice
                    front_on_m = _cum_dist_m(fslice)[-1]
                else:
                    course_ext = [tuple(p) for p in ext] + fslice[1:]
                    front_on_m = _cum_dist_m(fslice)[-1]
                stretch = True
        # EOP rail queues forward from the buffer, so the course needs
        # forward room to seat the whole stack (reach + a pill-arrow length
        # of margin); the middle-anchored rail stack and the tip-at-
        # front road queues keep the tighter default.
        eop_rail = rec.get("eop_rail", False)
        extentless_kind = rec.get("extentless_kind")
        fwd_m = (reach + 2.0 * max_L if eop_rail
                 else reach / 2.0 + 2.0 * max_L)
        built = _stop_course(course_ext, path["cos_lat"],
                             back_m=reach + 2.0 * max_L,
                             fwd_m=fwd_m,
                             chord_w=max_L,
                             front_on_m=front_on_m)
        if built is None:
            continue
        course, cdists, t_front, t_mid, t_rear = built
        # Widest transit-line width_base among variants in this stack.
        # Used by side-anchored bands to widen the perp offset by half
        # the visible line width so the pill-arrow's inner edge sits
        # just past the line's edge instead of eating into it.
        group_max_wb = max((v.get("width_base") or 0.0) for v in group)
        for k, c in enumerate(group):
            work.append((c, path, course, cdists, t_front, t_mid, t_rear,
                         rec["cut_pts"], k, len(group),
                         stretch, rear_ground, eop_rail,
                         extentless_kind, group_max_wb))

    # Offset placement tracks, shared per (group course, band, side) —
    # valid within this station only (courses are per-group objects).
    track_cache: dict = {}

    for (c, path, course, cdists, t_front, t_mid, t_rear, cut_pts, k, n,
         stretch, rear_ground, eop_rail, extentless_kind,
         group_max_wb) in work:
        # Everything is placed along the group's queue course (stop
        # position line + straight extensions), not the raw line.
        polyline, dists = course, cdists
        cos_lat = path["cos_lat"]
        # Queue anchor on the course: rail stacks center on the stop
        # position line's middle; road-mode queues put the lead tip at
        # its forward end (the vehicle pulled fully forward).
        # End-of-platform rail (stop-extent-osm-walk.md § Rail walk
        # case 2) anchors at the extent's buffer end and queues inward
        # — the fastest pill-arrow sits against the physical end of
        # the tracks. Ferry: same eop_rail rule, but shifted forward
        # by CLOSE_ZOOM_FERRY_OFFSET_M — the extent starts at the
        # pier's on-line position, and the pill-arrow sits 10 m past
        # it in the direction of travel (stops-close-zoom.md
        # § "Ferry").
        if c["is_rail_like"]:
            t_stop = t_rear if eop_rail else t_mid
        else:
            t_stop = t_front
        if extentless_kind == "ferry":
            # Base pier offset + optional solo-ferry stagger (set by
            # the ferry pier clustering + de-overlap pass) so a solo
            # ferry whose pill-arrow would otherwise land on top of another
            # sits further along its own polyline. Merged ferry pool
            # members carry no `ferry_extra_m`, so stagger is 0.
            t_stop = (t_stop + CLOSE_ZOOM_FERRY_OFFSET_M
                      + float(c.get("ferry_extra_m", 0.0)))
        # Merged same-curb groups pool pill-arrows from several platform
        # ids; each pill-arrow keeps its own.
        sid = c["sid"]

        # Side of the line (stops-close-zoom.md § "Side of the
        # line"): bus and tram (non-hybrid) always to the right in
        # direction of travel; rail-style (train + all mountain + ferry
        # + hybrid tram) sits centered on the line itself with no
        # sideways offset — there is no platform side. The GTFS-snap
        # side (`signed_d`) is no longer consulted for placement.
        if c["is_rail_like"]:
            side = 0.0
        else:
            side = 1.0

        dest_full = " / ".join(c["destinations"])
        ref_text = c["ref"] or ""

        common = {
            "mode":           c["mode"],
            "color":          c["color"],
            "ref":            ref_text,
            "stop_id":        sid,
            "parent_station": parent,
        }

        parent_colors[parent].add(c["color"])

        # Rail: the full platform extent joins the hull cloud so the
        # backdrop covers the whole platform.
        if c["is_rail_like"] and c.get("extent"):
            parent_cloud[parent].extend(
                (p[0], p[1]) for p in c["extent"])

        for band_id, bc in CLOSE_ZOOM_BANDS.items():
            L = bc["length_m"]
            W = bc["width_m"]
            R = W / 2.0
            # Full occupied length is exactly L: back cap (R) + body +
            # chevron tip (R). The body is the frame range rear → neck.
            body_len = L - 2.0 * R
            stack_step = L + CLOSE_ZOOM_STACK_GAP_M
            tipp = {"minzoom": bc["tipp_min"], "maxzoom": bc["tipp_max"]}
            # Offset of the pill-arrow CENTER line from the path: consistent
            # clear gap between the line and the pill-arrow's inner edge, on
            # the side chosen above.
            # Bands B–E widen the offset by half the widest transit
            # line in the stack (evaluated at z19, the anchor zoom)
            # so the pill-arrow's inner edge sits past the visible
            # line edge instead of eating into it. Band A is exempt
            # — it's designed to sit on / over the line.
            if band_id == "A":
                line_half_m = 0.0
            else:
                line_px_z19 = group_max_wb * 4.0 + 2.0
                line_half_m = (line_px_z19 / 2.0) / CLOSE_ZOOM_PX_PER_M_Z19
            perp = side * (bc["line_gap_m"] + line_half_m + W / 2.0)

            # Placement track: the path shifted sideways by perp — the
            # curve the pill-arrow centers actually sit on. Stepping, spans
            # and axes are all measured along THIS track, not the
            # centerline: measured on the centerline, every degree of
            # bend stretches the gaps between pill-arrows on the outside of
            # the curve and squeezes them on the inside.
            tkey = (id(course), band_id, side)
            track = track_cache.get(tkey)
            if track is None:
                reach = (n - 1) * stack_step + L
                # Terminal platform stretch shifts the queue up to
                # `reach` forward of the stop — the track must cover
                # that far ahead too. EOP rail queues forward from
                # t_rear, so it needs the same forward coverage.
                fwd_reach = (reach + L if stretch or eop_rail
                             else reach / 2.0 + L)
                back_reach = L if eop_rail else reach + L
                track = _offset_track(polyline, dists,
                                      t_stop - back_reach,
                                      t_stop + fwd_reach,
                                      perp, cos_lat)
                track_cache[tkey] = track if track else False
            if not track:
                continue
            tpts, tdists, tcts = track
            o_stop = _track_pos(t_stop, tcts, tdists)

            # Track span this pill-arrow occupies.
            if c["is_rail_like"] and eop_rail:
                # End-of-platform: fastest (k=0) rear cap sits at the
                # buffer (o_stop), shifted backward past the polyline
                # endpoint by CLOSE_ZOOM_LINE_END_OVERHANG_M so the
                # pill-arrow covers MapLibre's zoom-scaled round line-cap
                # (see stops-close-zoom.md § "End-of-platform
                # line-end overhang"). Body/tip extend inward. Slower
                # pill-arrows queue further inward by one stack_step each.
                o_center = (o_stop + k * stack_step + L / 2.0
                            - CLOSE_ZOOM_LINE_END_OVERHANG_M)
            elif c["is_rail_like"]:
                # Stack centered on the platform middle along the track;
                # fastest (k=0) sits furthest forward.
                o_center = o_stop + (n - 1 - 2 * k) * (stack_step / 2.0)
            else:
                # Same-curb shorten overflow: the queue must not cross
                # a cut boundary (the neighbouring platform line starts
                # there). If it doesn't fit behind the stop, the whole
                # stack shifts forward past the stop point — better in
                # front than overlapping the neighbour.
                o_shift = 0.0
                if stretch:
                    # Terminal platform stretch: shift the stack
                    # forward by this band's shortfall over the real
                    # rear ground, so every pill-arrow sits on line geometry
                    # that actually exists.
                    reach_band = (n - 1) * stack_step + L
                    if reach_band > rear_ground:
                        o_shift = reach_band - rear_ground
                if cut_pts:
                    rear_lim = None
                    for cp in cut_pts:
                        t_cp = _project_meters(cp[0], cp[1],
                                               polyline, dists)
                        o_cp = _track_pos(t_cp, tcts, tdists)
                        if o_cp < o_stop and (rear_lim is None
                                              or o_cp > rear_lim):
                            rear_lim = o_cp
                    if rear_lim is not None:
                        rear_need = o_stop - ((n - 1) * stack_step + L)
                        if rear_need < rear_lim:
                            o_shift = max(o_shift, rear_lim - rear_need)
                # Stack extends upstream from the stop point; the fastest
                # pill-arrow's chevron tip lands exactly on the stop (unless
                # shifted forward by the rule above).
                o_center = o_stop + o_shift - k * stack_step - L / 2.0
            o0 = o_center - L / 2.0
            o1 = o_center + L / 2.0

            # Per-pill-arrow straight frame: the axis is the AVERAGE direction
            # of the track part the pill-arrow occupies (the chord over its
            # own span), anchored at that part's midpoint. A
            # single-point tangent at the stop tilts the whole stack
            # against the line near bends, and deep stack positions can
            # sit tens of metres from the stop.
            A = _point_at_extrap(tpts, tdists, o0)
            B = _point_at_extrap(tpts, tdists, o1)
            dxm = (B[0] - A[0]) * 111320.0 * cos_lat
            dym = (B[1] - A[1]) * 111320.0
            norm = sqrt(dxm * dxm + dym * dym)
            if norm > 1e-9:
                T = (dxm / norm, dym / norm)
            else:
                T = path["tangent"]
            origin = _point_at_extrap(tpts, tdists, o_center)
            # Zero-extent queue orientation (stops-close-zoom.md):
            # when this pill-arrow's own line has no ground behind the stop
            # (its own debug line collapsed to nothing), the queue sits
            # on borrowed ground — typically the arrival counterpart's
            # extent — whose point order may oppose the departure.
            # Check the EXACT segment the pill-arrow runs along: project the
            # pill-arrow's midpoint onto its own departing polyline; when the
            # line runs along that segment (within the lateral offset
            # plus a small slack) and its direction of travel there
            # clearly opposes the pill-arrow axis, flip in place. No tangent
            # at the stop is involved, so turnaround-skewed departure
            # tangents cannot misfire. Canonical case: Les Crosets,
            # télésièges (dead-end road at the chairlift).
            zero_ext_flip = False
            if not c["is_rail_like"] and c["t_stop"] < 0.5:
                q_t = _project_meters(origin[0], origin[1],
                                      c["polyline"], c["dists"])
                q_pt = _interp_at(c["polyline"], c["dists"], q_t)
                q_dist = haversine_km(origin[0], origin[1],
                                      q_pt[0], q_pt[1]) * 1000.0
                if q_dist <= abs(perp) + 3.0:
                    own_tan = _directional_tangent_at(
                        c["polyline"], c["dists"], q_t, window_m=2.0)
                    if own_tan is not None:
                        oxm = own_tan[0] * cos_lat
                        oym = own_tan[1]
                        omag = sqrt(oxm * oxm + oym * oym)
                        if omag > 0 and (T[0] * oxm + T[1] * oym) / omag \
                                < -CLOSE_ZOOM_DIR_CLUSTER_COS:
                            zero_ext_flip = True
            # Rail pool: reverse-direction pill-arrows flip T so their
            # chevron tip points backward along the course (outward
            # toward the negative-o end of the platform). The pill-arrow's
            # map footprint is unchanged — the rectangle rotates 180°
            # around origin — but the chevron and label direction flip
            # to reflect the actual direction of travel. The zero-
            # extent flip above reuses the same mechanism.
            if not c.get("dir_forward", True) or zero_ext_flip:
                T = (-T[0], -T[1])
            N = (T[1], -T[0])  # right normal in direction of travel

            heading_deg_map = (90.0 - degrees(atan2(T[1], T[0]))) % 360.0
            # Label rotation: along the pill-arrow axis, flipped upside-down.
            text_rot = (heading_deg_map - 90.0) % 360.0
            flipped = 90.0 < text_rot < 270.0
            if flipped:
                text_rot = (text_rot + 180.0) % 360.0

            def _frame_pt(dx, dy, origin=origin, T=T, N=N):
                return _local_offset_to_lonlat(
                    origin[0], origin[1],
                    dx * T[0] + dy * N[0], dx * T[1] + dy * N[1], cos_lat)

            # Body range in the pill-arrow's own frame (origin = span middle;
            # the lateral offset is already baked into the track, so the
            # pill-arrow sits ON its frame axis at zero perpendicular offset).
            x_neck = body_len / 2.0
            x_rear = -body_len / 2.0

            built = _build_straight_pill_arrow(origin, T, N, cos_lat,
                                               x_rear, x_neck, 0.0, W)
            if built is None:
                continue
            ring, rear_center = built

            # Destination, pre-wrapped at build time with baked line
            # breaks. The text region runs from the disc's forward edge
            # (plus margin) to the neck (minus margin; a negative tip
            # margin lets text reach into the chevron base).
            region_start = x_rear + R + bc["margin_disc_m"]
            region_end = x_neck - bc["margin_tip_m"]
            text_avail_m = max(region_end - region_start, 0.0)
            dest_text = ""
            if bc["font_dest_m"] and dest_full and text_avail_m > 0.0:
                avail_em = text_avail_m / bc["font_dest_m"]
                dest_text = _wrap_label(dest_full, avail_em,
                                        bc["max_lines"])

            features.append({
                "type": "Feature",
                "tippecanoe": dict(tipp),
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {
                    **common,
                    "feature_type":    "pill_arrow",
                    "band":            band_id,
                    "mountain_origin": c["mountain_origin"] or "",
                    "destination":     dest_text,
                    "n_variants":      len(c["destinations"]),
                    "speed_kmh":       c["speed_kmh"],
                    "osm_id":          c["osm_id"],
                    "heading_deg":     round(heading_deg_map, 2),
                    "stack_idx":       k,
                },
            })

            # Solid band (no destination): the whole pill-arrow renders in the
            # line color with just the centered number — no disc.
            solid = bc["font_dest_m"] is None

            rear_cx, rear_cy = rear_center
            if not solid:
                # Disc at the round end, filled with the line color.
                disc = []
                for i in range(24):
                    a = 2.0 * pi * i / 24.0
                    disc.append(list(_local_offset_to_lonlat(
                        rear_cx, rear_cy, R * cos(a), R * sin(a), cos_lat)))
                disc.append(disc[0])
                features.append({
                    "type": "Feature",
                    "tippecanoe": dict(tipp),
                    "geometry": {"type": "Polygon", "coordinates": [disc]},
                    "properties": {**common,
                                   "feature_type": "pill_disc",
                                   "band":         band_id},
                })

            # Line number: centered in the disc, or in the whole pill-arrow
            # for solid bands. Wide refs (e.g. "IR15") shrink per feature
            # just enough to fit their container; short refs keep the
            # band's nominal size.
            if ref_text and bc["font_ref_m"]:
                if solid:
                    ref_x, ref_y = _frame_pt(0.0, 0.0)
                else:
                    ref_x, ref_y = rear_cx, rear_cy
                ref_font_m = _shrink_ref_font_m(
                    ref_text, bc["font_ref_m"], bc)
                features.append({
                    "type": "Feature",
                    "tippecanoe": dict(tipp),
                    "geometry": {"type": "Point",
                                 "coordinates": [ref_x, ref_y]},
                    "properties": {
                        **common,
                        "feature_type": "pill_ref",
                        "band":         band_id,
                        "font_m":       round(ref_font_m, 3),
                        "text_rot":     round(text_rot, 2),
                        "flipped":      flipped,
                    },
                })

            # Destination text: anchor placed at the text's reader-left
            # edge in both flip states so multi-line text visually
            # left-aligns for the reader in both orientations. That end
            # is the pill-arrow's disc side for non-flipped labels (text
            # reads toward the tip) and the pill-arrow's tip side for
            # flipped labels (text reads toward the disc, after the
            # +180° flip). margin_disc_m controls the padding between
            # disc and non-flipped text start; margin_tip_m sets the
            # wrap-budget end (region_end) for both flip states and
            # the flipped anchor's base position at the neck. Short
            # flipped labels would then sit at the tip and leave a
            # visible gap between text end and disc; flipped_shift_m
            # shifts the flipped anchor toward the disc by that many
            # metres to close the gap. Non-flipped anchor and the
            # shared wrap budget are unaffected.
            if dest_text:
                if flipped:
                    x_text = region_end - bc["flipped_shift_m"]
                else:
                    x_text = region_start
                tx, ty = _frame_pt(x_text, 0.0)
                features.append({
                    "type": "Feature",
                    "tippecanoe": dict(tipp),
                    "geometry": {"type": "Point",
                                 "coordinates": [tx, ty]},
                    "properties": {
                        **common,
                        "feature_type": "pill_dest",
                        "band":         band_id,
                        "destination":  dest_text,
                        "font_m":       round(bc["font_dest_m"], 3),
                        "text_rot":     round(text_rot, 2),
                    },
                })

            # Hull cloud: pill-arrow outline plus the adjacent line section
            # (largest band only — it covers the smaller ones; the line
            # section stays arc-based since the LINE itself may curve).
            if band_id == CLOSE_ZOOM_HULL_BAND:
                cloud = parent_cloud[parent]
                cloud.extend((p[0], p[1]) for p in ring)
                # Map the pill-arrow's track span back to centerline arc
                # positions before sampling the line itself.
                ct0 = _track_pos(o0, tdists, tcts)
                ct1 = _track_pos(o1, tdists, tcts)
                for t in _sample_ts(dists, ct0, ct1):
                    cloud.append(_point_at_extrap(polyline, dists, t))

# ── Backdrop: one rounded hull polygon per parent station ────────────
n_backdrops = 0
for parent, cloud in parent_cloud.items():
    hull_ring = _rounded_hull_polygon(cloud, CLOSE_ZOOM_BACKDROP_PAD_M,
                                      CLOSE_ZOOM_ARC_STEP_DEG)
    if hull_ring is None:
        continue
    colors = sorted(parent_colors.get(parent, set()))
    bg_color = colors[0] if len(colors) == 1 else _blend_colors(colors)
    features.append({
        "type": "Feature",
        "tippecanoe": {"minzoom": 15, "maxzoom": 18},
        "geometry": {"type": "Polygon", "coordinates": [hull_ring]},
        "properties": {
            "feature_type":   "backdrop",
            "parent_station": parent,
            "bg_color":       bg_color,
            "n_colors":       len(colors),
        },
    })
    n_backdrops += 1

OUT_CLOSE_ZOOM.write_text(json.dumps({
    "type": "FeatureCollection",
    "features": features,
}, ensure_ascii=False))
pill_count = sum(1 for f in features if f["properties"]["feature_type"] == "pill_arrow")
print(f"  Close-zoom: {pill_count:,} pill-arrows, {n_backdrops:,} station "
      f"backdrops → {OUT_CLOSE_ZOOM}")
