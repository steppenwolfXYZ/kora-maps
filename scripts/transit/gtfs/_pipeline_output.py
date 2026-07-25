import json
# ── Line graph + MBST + stop-weighted-average connectivity ──────────────
# See concept § "Line graph and base set" and § "Connectivity".
print("Building line graph (super-UIC clustering + MBST)...")
lg_cfg = zr_cfg.get("line_graph") or {}
cluster_m = float(lg_cfg.get("cluster_threshold_m", 250.0))

# First-seen UIC → coords.
uic_coords: dict = {}
for entry in line_stops_out.values():
    for stop in entry.get("stops", []):
        if len(stop) >= 3 and stop[2]:
            uic = stop[2].split(":")[0]
            uic_coords.setdefault(uic, (float(stop[0]), float(stop[1])))

super_of_uic = _cluster_uics(uic_coords, cluster_m)

# Lines per super-cluster (deduped).
lines_at_super: dict = defaultdict(set)
for oid, entry in line_stops_out.items():
    for stop in entry.get("stops", []):
        if len(stop) >= 3 and stop[2]:
            uic = stop[2].split(":")[0]
            super_id = super_of_uic.get(uic)
            if super_id is not None:
                lines_at_super[super_id].add(oid)

# Per-line station count (length of the per-feature stop list).
station_count: dict = {oid: len(entry.get("stops", []))
                       for oid, entry in line_stops_out.items()}

line_oids: list = [f["properties"]["osm_id"] for f in features]
oid_set: set = set(line_oids)

# Travel duration (minutes) — edge weight basis.
duration_by_oid: dict = {}
for f in features:
    p = f["properties"]
    oid = p["osm_id"]
    km = float(p.get("line_km") or 0.0)
    sp = float(p.get("speed_kmh") or 0.0)
    duration_by_oid[oid] = (km / sp * 60.0) if sp > 0 else 1e9

# Raw line graph: each pair of lines sharing a super-cluster is an edge.
# Used both for (a) finding the intercity base's connected component and
# (b) running the per-line shortest-path-to-base search below.
# Adjacency is deduped — many lines share many super-clusters, but we
# only need one edge between any pair in the graph.
line_graph_adj: dict = defaultdict(set)
for super_id, oids in lines_at_super.items():
    oids_here = [o for o in oids if o in oid_set]
    n = len(oids_here)
    if n < 2:
        continue
    for i in range(n):
        u = oids_here[i]
        for j in range(i + 1, n):
            v = oids_here[j]
            line_graph_adj[u].add(v)
            line_graph_adj[v].add(u)

# Base set: the LARGEST connected component of intercity train lines in
# the RAW line graph. (Computing CCs over MBST edges, as we used to,
# silently fragmented the IC backbone because MBST routed IC↔IC via
# cheaper non-IC connectors and the direct IC↔IC edges were never added.)
feature_by_oid = {f["properties"]["osm_id"]: f for f in features}
intercity_oids = [
    o for o in line_oids
    if _is_intercity_train(
        feature_by_oid[o]["properties"].get("ref", ""),
        mode_by_oid.get(o, ""))
]
intercity_set: set = set(intercity_oids)

visited_ic: set = set()
components: list = []
for o in intercity_oids:
    if o in visited_ic:
        continue
    comp: list = []
    stack = [o]
    visited_ic.add(o)
    while stack:
        u = stack.pop()
        comp.append(u)
        for v in line_graph_adj.get(u, ()):
            if v in intercity_set and v not in visited_ic:
                visited_ic.add(v)
                stack.append(v)
    components.append(comp)
components.sort(key=len, reverse=True)
base_oids: set = set(components[0]) if components else set()

# ── Per-level cluster gating ─────────────────────────────────────────────
# Sole purpose: avoid floating clusters at each zoom level. Algorithm:
#
#   For Z = 4, 5, 6, …, UNREACHABLE_Z:
#     1. eligible_at_Z = {oid already assigned final ≤ Z}
#                       ∪ {oid not yet assigned whose candidate ≤ Z}
#     2. Connected components of the line graph induced on eligible_at_Z.
#     3. main = the CC containing the IC base.
#     4. Newly arrived in main (not yet assigned) → assign Z.
#     5. For each other CC ("isolated cluster"):
#        a. Find the shortest bridge (least non-eligible intermediates)
#           from the cluster to main in the full line graph.
#        b. weighted_avg = stop-weighted average of (cluster + bridge)
#           candidates.
#        c. If avg ≤ Z: accept — pull every bridge line and every cluster
#           line down to Z. Bridge lines become eligible immediately so
#           later clusters at the same Z benefit.
#        d. Else: defer — leave unassigned, retry at Z+1.
#   After all levels: any still-unassigned line is truly disconnected from
#   base in the line graph; assign its candidate as fallback.
import heapq
final_mz_by_oid: dict = {}
for oid in base_oids:
    final_mz_by_oid[oid] = candidate_mz_by_oid.get(oid, UNREACHABLE_Z)

def _shortest_bridge(cluster_set, main_set, eligible_set):
    """Multi-source Dijkstra from cluster outward. Edge cost u→v is 0 if v
    is in eligible_set (or in main_set), else 1. Returns the list of
    non-eligible intermediate lines on the cheapest path from any cluster
    node to any main node, or None if no path exists in the line graph.
    """
    INF = 10**9
    dist: dict = {c: 0 for c in cluster_set}
    parent: dict = {}
    heap: list = [(0, c) for c in cluster_set]
    target = None
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, INF):
            continue
        if u in main_set:
            target = u
            break
        for v in line_graph_adj.get(u, ()):
            step = 0 if (v in eligible_set or v in main_set) else 1
            v_d = d + step
            if v_d < dist.get(v, INF):
                dist[v] = v_d
                parent[v] = u
                heapq.heappush(heap, (v_d, v))
    if target is None:
        return None
    # Reconstruct path target → … → cluster_anchor
    path: list = [target]
    cur = target
    while cur in parent:
        cur = parent[cur]
        path.append(cur)
    path.reverse()
    return [n for n in path
            if n not in cluster_set and n not in main_set
            and n not in eligible_set]

# Iterate zoom levels from base (z4) up to UNREACHABLE_Z. At each Z, the
# "main" component may absorb new clusters either via natural eligibility
# or via pull-down bridges. The loop is bounded by UNREACHABLE_Z so any
# cluster that survives all levels is logged as truly isolated.
for Z in range(4, UNREACHABLE_Z + 1):
    eligible_oids: set = set()
    for oid in line_oids:
        if oid in final_mz_by_oid:
            if final_mz_by_oid[oid] <= Z:
                eligible_oids.add(oid)
        else:
            if candidate_mz_by_oid.get(oid, UNREACHABLE_Z) <= Z:
                eligible_oids.add(oid)

    # CCs in line_graph_adj restricted to eligible_oids.
    visited_cc: set = set()
    comps: list = []
    for o in eligible_oids:
        if o in visited_cc:
            continue
        comp: set = set()
        stack: list = [o]
        visited_cc.add(o)
        while stack:
            u = stack.pop()
            comp.add(u)
            for v in line_graph_adj.get(u, ()):
                if v in eligible_oids and v not in visited_cc:
                    visited_cc.add(v)
                    stack.append(v)
        comps.append(comp)

    main_set: set = set()
    for c in comps:
        if base_oids & c:
            main_set = c
            break
    if not main_set:
        continue

    # New arrivals in main: assign Z.
    for line in main_set:
        if line not in final_mz_by_oid:
            final_mz_by_oid[line] = Z

    # Process each isolated cluster.
    for cluster in comps:
        if cluster is main_set:
            continue
        # Skip clusters whose lines already all got assigned (e.g. as part
        # of a previously-pulled bridge at this Z).
        if all(l in final_mz_by_oid for l in cluster):
            continue
        bridge = _shortest_bridge(cluster, main_set, eligible_oids)
        if bridge is None:
            continue
        nodes_for_avg = list(cluster) + bridge
        total_stops = sum(station_count.get(n, 1) for n in nodes_for_avg)
        if total_stops == 0:
            continue
        numer = sum(station_count.get(n, 1)
                    * candidate_mz_by_oid.get(n, UNREACHABLE_Z)
                    for n in nodes_for_avg)
        avg = numer / total_stops
        if avg > Z:
            # Defer: cluster sits this level out, re-evaluate at Z+1.
            continue
        # Accept: pull bridge + cluster down to Z.
        for line in bridge:
            if line not in final_mz_by_oid:
                final_mz_by_oid[line] = Z
            eligible_oids.add(line)
            main_set.add(line)
        for line in cluster:
            if line not in final_mz_by_oid:
                final_mz_by_oid[line] = Z
            main_set.add(line)

# Truly disconnected (no path to base in the line graph at any Z) → use
# candidate as the visibility level. Mark as isolated for diagnostics.
isolated_oids: set = set()
for oid in line_oids:
    if oid not in final_mz_by_oid:
        final_mz_by_oid[oid] = candidate_mz_by_oid.get(oid, UNREACHABLE_Z)
        isolated_oids.add(oid)

# Apply to features.
for f in features:
    p = f["properties"]
    oid = p["osm_id"]
    mz = int(final_mz_by_oid.get(oid, UNREACHABLE_Z))
    p["min_zoom"] = mz
    p["candidate_min_zoom"] = int(candidate_mz_by_oid.get(oid, UNREACHABLE_Z))
    p["rule_label"] = rule_label_by_oid.get(oid, "")
    f["tippecanoe"] = {"minzoom": mz}

n_iso = len(isolated_oids)
n_iso_mountain = sum(1 for o in isolated_oids
                     if mode_by_oid.get(o) == "mountain")
n_iso_other = n_iso - n_iso_mountain
n_line_edges = sum(len(v) for v in line_graph_adj.values()) // 2
print(f"  Line graph: {len(super_of_uic):,} UICs → "
      f"{len(set(super_of_uic.values())):,} super-clusters, "
      f"{n_line_edges:,} line-graph edges")
base_str = (f"{len(base_oids)} intercity train(s), largest of "
            f"{len(components)} intercity component(s) in line graph"
            if base_oids else "EMPTY (no intercity train lines)")
print(f"  Base set: {base_str}")
n_promoted = sum(1 for oid, mz in final_mz_by_oid.items()
                 if oid not in base_oids and oid not in isolated_oids
                 and mz < candidate_mz_by_oid.get(oid, UNREACHABLE_Z))
print(f"  Connectivity-promoted: {n_promoted:,}  "
      f"Isolated: {n_iso} ({n_iso_mountain} mountain, {n_iso_other} other)")
if features:
    mzs = [int(final_mz_by_oid.get(f["properties"]["osm_id"], UNREACHABLE_Z))
           for f in features]
    cmzs = [int(candidate_mz_by_oid.get(f["properties"]["osm_id"], UNREACHABLE_Z))
            for f in features]
    print(f"  candidate min_zoom: range {min(cmzs)}–{max(cmzs)}, "
          f"mean {sum(cmzs)/len(cmzs):.2f}")
    print(f"  final min_zoom:     range {min(mzs)}–{max(mzs)}, "
          f"mean {sum(mzs)/len(mzs):.2f}")

# ── Write outputs ────────────────────────────────────────────────────────
# Restore return directions dropped by aerial dedup so both terminals
# of every cable get a close-zoom pill-arrow (see
# stops-close-zoom.md § "Aerial + funicular terminals"). Runs
# after scoring/salience/min_zoom so the reverse features inherit the
# forward's values without inflating its own competition count.
features = synthesise_aerial_reverse_directions(features, line_stops_out)
OUT.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
OUT_STOPS.write_text(json.dumps(line_stops_out))
print(f"\n  {len(features):,} features → {OUT}")
print(f"  {sum(len(v['stops']) for v in line_stops_out.values()):,} stops "
      f"across {len(line_stops_out):,} features → {OUT_STOPS}")

# gtfs_unmatched: trip groups with no emitted feature (after grouping).
# Drawable tg_keys come straight from drawable_groups — that dict has
# already passed the freq-score / mountain exemptions used during
# emission, so the difference against matched_tg_keys is exactly the set
# of trip groups that we thought we should draw but pfaedle never shaped
# (or whose polylines collapsed to < 2 coords).
unmatched_tg = set(drawable_groups.keys()) - matched_tg_keys
unmatched_out = []
for tg_key in sorted(unmatched_tg,
                     key=lambda k: (k[0][2], k[0][0], k[0][1], k[1], k[2])):
    line_key, aid, tg_id = tg_key
    short_name, long_name, bucket = line_key
    freq = tg_freq.get(tg_key, {"f_core": 0.0, "f_eve": 0.0, "f_we": 0.0})
    mode_approx = _BUCKET_MODE_APPROX.get(bucket, "regional_bus")
    fs = compute_freq_score(freq, mode_approx)
    unmatched_out.append({
        "short_name":    short_name,
        "long_name":     long_name,
        "bucket":        bucket,
        "agency_id":     aid,
        "trip_group_id": tg_id,
        "f_weighted":    round(weighted_freq(freq), 3),
        "freq_score":    round(fs, 4),
    })
OUT_GTFS_UNMATCHED.write_text(json.dumps(unmatched_out, ensure_ascii=False))
print(f"  GTFS unmatched: {len(unmatched_out)} trip groups with service but no feature → {OUT_GTFS_UNMATCHED}")

OUT_TRIP_GROUPS.write_text(json.dumps(trip_groups_diag, ensure_ascii=False))
print(f"  Trip groups:   {len(trip_groups_diag)} groups → {OUT_TRIP_GROUPS}")

OUT_PFAEDLE_UNROUTED.write_text(json.dumps(pfaedle_unrouted, ensure_ascii=False))
print(f"  Pfaedle unrouted: {len(pfaedle_unrouted)} trips → {OUT_PFAEDLE_UNROUTED}")

# Per-merged-UIC dwell — written here so step 07 doesn't have to walk the
# 1.7 GB stop_times.txt a second time. `_dwell_export` is populated as a
# side effect of stream_stop_times.
from gtfs.identity import _dwell_export
OUT_DWELL_BY_UIC = ROOT / "data" / "transit" / "dwell_by_uic.json"
OUT_DWELL_BY_UIC.write_text(json.dumps(_dwell_export, ensure_ascii=False))
print(f"  Dwell by UIC: {len(_dwell_export)} UICs → {OUT_DWELL_BY_UIC}")

# ── Per-line service summary ─────────────────────────────────────────────
# Reduce the per-emitted-variant raw records (service_raw, emission phase)
# to one summary per canonical line key: operating period (only when
# seasonal, i.e. clearly shorter than the feed validity period), weekday
# mask, first/last departure (average of both directions), runs per
# active day (`rpd` — total yearly departures / days the line actually
# runs, so seasonal and weekday-only service self-normalize), an
# irregular-service flag (`irr`), plus one row per distinct terminus pair
# for the expanded view. Step 07 attaches these to the line_index.json
# entries; all text formatting happens client-side.

def _svc_iso(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"

def _svc_days_mask(wd_counts: list) -> str:
    # A weekday counts as served when it has at least half as many active
    # dates as the line's best weekday — tolerant of holiday gaps without
    # flagging one-off exception dates as regular service.
    mx = max(wd_counts) if wd_counts else 0
    if mx <= 0:
        return "0000000"
    return "".join("1" if c >= 0.5 * mx else "0" for c in wd_counts)

def _svc_day_diff(a: str, b: str) -> int:
    from datetime import date as _d
    return ( _d(int(b[:4]), int(b[4:6]), int(b[6:8]))
           - _d(int(a[:4]), int(a[4:6]), int(a[6:8]))).days

_feed_span_days = (_svc_day_diff(feed_first_date, feed_last_date) + 1
                   if feed_first_date and feed_last_date else 0)

def _svc_rec_rpd(r: dict) -> float:
    return (r["runs"] / r["active_days"]) if r["active_days"] else 0.0

service_out: dict = {}
for svc_key, recs in service_raw.items():
    # Merge opposite directions / duplicates by unordered terminus pair.
    by_pair: dict = {}
    for r in recs:
        pair = tuple(sorted(r["termini"]))
        by_pair.setdefault(pair, []).append(r)

    rows = []
    for pair, rs in by_pair.items():
        # Cadence per direction: same-termini sub-variants pool their runs
        # (they serve the same relation); opposite directions don't — the
        # busiest direction carries the row's runs-per-active-day.
        # First/last departures are taken per direction, then averaged
        # across the (≤2) directions: "average of both ends".
        by_dir: dict = {}
        for r in rs:
            by_dir.setdefault(r["termini"], []).append(r)
        best_rpd = 0.0
        best_irr = False
        dir_firsts: list = []
        dir_lasts: list = []
        for drs in by_dir.values():
            runs = sum(r["runs"] for r in drs)
            days_active = max((r["active_days"] for r in drs), default=0)
            rpd_dir = (runs / days_active) if days_active else 0.0
            # Irregular when the biggest gap between departures dwarfs the
            # typical one (e.g. peak-only commuter service): largest gap
            # over 3× the median gap and over 90 minutes.
            deps = sorted({t for r in drs for t in r["dep_times"]})
            gaps = sorted(b - a for a, b in zip(deps, deps[1:]))
            irr = (len(deps) >= 4
                   and gaps[-1] > max(3 * gaps[len(gaps) // 2], 5400))
            if rpd_dir > best_rpd:
                best_rpd = rpd_dir
                best_irr = irr
            firsts = [r["dep"][0] for r in drs if r["dep"][0] is not None]
            lasts = [r["dep"][1] for r in drs if r["dep"][1] is not None]
            if firsts:
                dir_firsts.append(min(firsts))
            if lasts:
                dir_lasts.append(max(lasts))
        wd = [0] * 7
        first = last = ""
        for r in rs:
            ds = r["date_stats"]
            if ds:
                f, l, counts = ds
                first = f if not first or f < first else first
                last = l if not last or l > last else last
                wd = [max(x, y) for x, y in zip(wd, counts)]
        a, b = max(rs, key=_svc_rec_rpd)["termini"]
        row = {
            "route": f"{a} ↔ {b}" if a != b else a,
            "days": _svc_days_mask(wd),
            "rpd": round(best_rpd, 1),
        }
        if best_irr:
            row["irr"] = True
        if dir_firsts and dir_lasts:
            row["dep"] = [round(sum(dir_firsts) / len(dir_firsts)),
                          round(sum(dir_lasts) / len(dir_lasts))]
        if first and last:
            row["_first"], row["_last"] = first, last
            if (_feed_span_days
                    and _svc_day_diff(first, last) + 1 < 0.75 * _feed_span_days):
                row["from"], row["to"] = _svc_iso(first), _svc_iso(last)
        rows.append(row)
    rows.sort(key=lambda r: -r["rpd"])

    # Group summary: busiest row's cadence and span, union of days / dates.
    g_wd_mask = "".join(
        "1" if any(r["days"][i] == "1" for r in rows) else "0"
        for i in range(7))
    g_first = min((r["_first"] for r in rows if "_first" in r), default="")
    g_last = max((r["_last"] for r in rows if "_last" in r), default="")
    summary = {
        "days": g_wd_mask,
        "rpd": rows[0]["rpd"],
    }
    if rows[0].get("irr"):
        summary["irr"] = True
    if "dep" in rows[0]:
        summary["dep"] = rows[0]["dep"]
    if (g_first and g_last and _feed_span_days
            and _svc_day_diff(g_first, g_last) + 1 < 0.75 * _feed_span_days):
        summary["from"], summary["to"] = _svc_iso(g_first), _svc_iso(g_last)
    summary["variants"] = [
        {k: v for k, v in r.items() if not k.startswith("_")} for r in rows
    ]
    service_out[svc_key] = summary

OUT_LINE_SERVICE = ROOT / "data" / "transit" / "line_service_info.json"
OUT_LINE_SERVICE.write_text(json.dumps(service_out, ensure_ascii=False))
print(f"  Line service info: {len(service_out)} lines "
      f"(feed {feed_first_date}–{feed_last_date}) → {OUT_LINE_SERVICE}")

# ── Comprehensive grouping diagnostic ──────────────────────────────────
# One entry per (line_key, agency_id, trip_group_id) including groups that
# never reached emission (low_frequency). One sub-entry per merged-stop
# variant including those dropped by the rare-variant filter. Read this
# file directly instead of re-running stream_stop_times to debug missing
# or unexpected lines.
diag_out = []
_, worst_freq_map_diag = _frequencies()
for tg_key, var_outcomes in diag_filter.items():
    line_key, aid, tg_id = tg_key
    short_name, long_name, bucket = line_key
    original_vmap = diag_original.get(tg_key, {})
    raw_freq = dict(tg_freq.get(tg_key, {"f_core": 0.0, "f_eve": 0.0, "f_we": 0.0}))
    f_weighted = weighted_freq(raw_freq)
    mode_approx = _BUCKET_MODE_APPROX.get(bucket, "regional_bus")
    fscore = compute_freq_score(raw_freq, mode_approx)
    worst_f_diag = worst_freq_map_diag.get(mode_approx, 0.0)

    mountain_origin = tg_mountain_origin.get(tg_key)
    drawable = (line_key, aid, tg_id) in drawable_groups
    if drawable:
        group_reason = None
    elif tg_key in rare_group_dropped:
        group_reason = "rare_group_dropped"
    elif tg_key in tg_keys_all_short_active:
        group_reason = "short_active_period"
    elif _freq_gate_exempt(bucket, mountain_origin):
        # These should have been drawable; only here if neither emitted
        # nor low-freq. Real shouldn't-happen branch — record it.
        group_reason = "unknown_skipped"
    elif f_weighted <= worst_f_diag:
        group_reason = "low_frequency"
    else:
        group_reason = "unknown_skipped"

    group_trip_total = sum(len(t) for t in original_vmap.values())
    group_weighted_total = tg_total_weight.get(tg_key, 0)
    sg_id = supergroup_id_by_tg.get(tg_key)
    sg_weighted_total = supergroup_total_weight.get(sg_id, 0)
    sg_share = (group_weighted_total / sg_weighted_total) if sg_weighted_total else 0.0
    sg_threshold = rare_group_threshold_by_sg.get(sg_id)
    variant_weighted_total_for_group = sum(variant_counts[tg_key].values())

    variants_out = []
    for var_key, (filt_outcome, threshold_pct) in var_outcomes.items():
        merged_set, direction_key = var_key
        ms_trips = original_vmap.get(var_key, [])
        stations: list = []
        first_terminus = "?"
        last_terminus = "?"
        if ms_trips:
            any_stops = _trip_stops_export.get(ms_trips[0], [])
            for sid in any_stops:
                uic = sid.split(":")[0]
                entry = (stop_meta.get(sid)
                         or stop_meta.get(uic)
                         or {"name": "?"})
                name = entry.get("name") or "?"
                stations.append({"stop_id": sid, "name": name})
            if stations:
                first_terminus = stations[0]["name"]
                last_terminus = stations[-1]["name"]

        em = diag_emission.get((tg_key, var_key), {})
        kept_by_filter = (filt_outcome == "kept")
        # Variant-level outcomes ("short_active_period", "rare_variant")
        # surface directly. Otherwise: if the whole group is gone,
        # propagate the group reason; if the variant survived but never
        # emitted, take the emission reason.
        if filt_outcome in ("short_active_period", "rare_variant"):
            v_reason = filt_outcome
        elif not drawable:
            v_reason = group_reason
        else:
            v_reason = em.get("exclusion_reason")

        share = (len(ms_trips) / group_trip_total) if group_trip_total else 0.0
        ms_weight = variant_counts[tg_key].get(var_key, 0)
        weighted_share = (ms_weight / variant_weighted_total_for_group
                          if variant_weighted_total_for_group else 0.0)
        v_active_days = variant_active_days.get(
            (line_key, aid, tg_id, merged_set, direction_key), 0)

        v_seasonal = var_freq_seasonal.get((tg_key, var_key)) or {}
        v_raw = v_seasonal.get("annual") or _ZERO_FREQ
        v_entry = {
            "direction_key": f"{direction_key[0]}-{direction_key[1]}",
            "trip_count": len(ms_trips),
            "trip_share_pct": round(share * 100, 1),
            "weighted_trip_count": ms_weight,
            "variant_share_of_group": round(weighted_share, 4),
            "active_days": v_active_days,
            "first_terminus": first_terminus,
            "last_terminus": last_terminus,
            "kept_by_variant_filter": kept_by_filter,
            "rare_variant_threshold_pct": threshold_pct,
            "rare_variant_window_passed":
                rare_variant_window_passed.get((tg_key, var_key)),
            "regional_bus_rescued": var_key in regional_bus_rescued.get(tg_key, ()),
            "raw_freq": dict(v_raw),
            "f_weighted": round(weighted_freq(v_raw), 3),
            "exclusion_reason": v_reason,
            "feature_emitted": em.get("feature_emitted", False),
            "stations": stations,
        }
        if em.get("feature_emitted"):
            v_entry["feature_id"] = em.get("feature_id", "")
            v_entry["shape_id"] = em.get("shape_id", "")
            v_entry["n_coords"] = em.get("n_coords", 0)
            v_entry["line_km"] = em.get("line_km", 0.0)
            v_entry["rep_trip_id"] = em.get("rep_trip_id", "")
            v_entry["geometry_source"] = em.get("geometry_source", "pfaedle")
        elif em:
            # Reached emission but didn't produce a feature.
            v_entry["shape_id"] = em.get("shape_id", "")
            v_entry["n_coords"] = em.get("n_coords", 0)
            v_entry["line_km"] = em.get("line_km", 0.0)
            v_entry["rep_trip_id"] = em.get("rep_trip_id", "")
        variants_out.append(v_entry)

    threshold_field = (None if _gate_exempt(bucket, mountain_origin)
                       else min_active_days_for(bucket))
    diag_out.append({
        "ref": short_name,
        "long_name": long_name,
        "bucket": bucket,
        "route_type": tg_route_type.get(tg_key, ""),
        "mountain_origin": mountain_origin,
        "agency_id": aid,
        "agency_name": agency_names.get(aid, ""),
        "trip_group_id": tg_id,
        "total_trip_count": group_trip_total,
        "weighted_trip_count": group_weighted_total,
        "supergroup_id": sg_id,
        "supergroup_weighted_trip_count": sg_weighted_total,
        "group_share_of_supergroup": round(sg_share, 4),
        "rare_group_share_threshold": sg_threshold,
        "raw_freq": raw_freq,
        "f_weighted": round(f_weighted, 3),
        "freq_score": round(fscore, 4),
        "min_active_days_threshold": threshold_field,
        "drawable": drawable,
        "group_exclusion_reason": group_reason,
        "freq_gate_window_passed": freq_gate_window_passed.get(tg_key),
        "variants": variants_out,
    })

diag_out.sort(key=lambda e: (e["bucket"], e["ref"], e["agency_id"],
                              e["trip_group_id"]))
OUT_GROUPS_FULL = ROOT / "data" / "transit" / "gtfs_groups_full.json"
OUT_GROUPS_FULL.write_text(json.dumps(diag_out, ensure_ascii=False))
drawable_count = sum(1 for e in diag_out if e["drawable"])
emitted_count = sum(1 for e in diag_out for v in e["variants"]
                    if v.get("feature_emitted"))
print(f"  Full groups:   {len(diag_out)} entries "
      f"({drawable_count} drawable, {emitted_count} variants emitted) → {OUT_GROUPS_FULL}")

# City-bus promotion audit (citybus-landuse-promotion.md § diagnostics):
# one record per evaluated regional_bus group.
OUT_CITYBUS_PROMO = ROOT / "data" / "transit" / "citybus_promotion.json"
citybus_promotion_diag.sort(key=lambda e: -e["share"])
OUT_CITYBUS_PROMO.write_text(
    json.dumps(citybus_promotion_diag, ensure_ascii=False))
promoted_count = sum(1 for e in citybus_promotion_diag if e["promoted"])
print(f"  City-bus promotion: {promoted_count} of "
      f"{len(citybus_promotion_diag)} evaluated groups promoted "
      f"→ {OUT_CITYBUS_PROMO}")

# Summary
mode_counts: dict = defaultdict(int)
for f in features:
    mode_counts[f["properties"]["mode"]] += 1
print("\nBy mode:")
for m, c in sorted(mode_counts.items(), key=lambda x: -x[1]):
    print(f"  {m:<20} {c:>5}")

scores = [f["properties"]["freq_score"] for f in features
          if f["properties"].get("freq_score") is not None]
if scores:
    buckets = [0] * 10
    for s in scores:
        buckets[min(9, int(s * 10))] += 1
    print("\nFrequency score distribution:")
    for i, c in enumerate(buckets):
        bar = "█" * (c * 40 // max(buckets, default=1))
        print(f"  {i/10:.1f}–{(i+1)/10:.1f}  {bar} {c}")
