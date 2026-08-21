OUT.parent.mkdir(parents=True, exist_ok=True)
cfg = load_cfg()

print("Loading GTFS data...")
stop_coords  = load_stops()
stop_meta    = load_stop_meta()
svc_dates    = load_calendar_dates()
svc_dates_full = load_calendar_dates_full()
route_lookup = load_routes()
agency_names = load_agencies()
mountain_aids = set(cfg.get("mountain_agency_ids", []) or [])
trip_lookup  = load_trips(route_lookup, mountain_aids)
print(f"  {len(stop_coords):,} stop entries, {len(svc_dates):,} service IDs, "
      f"{len(trip_lookup):,} trips, {len(agency_names):,} agencies")
if mountain_aids:
    print(f"  {len(mountain_aids)} agencies rebucketed train→mountain: "
          f"{sorted(mountain_aids)}")
print(f"  {len(svc_dates_full):,} service IDs with full-calendar coverage")

min_active_days_default = int(cfg.get("min_active_days", 150))
min_active_days_by_bucket = {
    b: int(v) for b, v in (cfg.get("min_active_days_by_bucket") or {}).items()
}
min_active_days_regional_bus = int(
    cfg.get("min_active_days_regional_bus", min_active_days_default)
)
unique_stop_min_distance_m = float(
    cfg.get("unique_stop_min_distance_m", 1000)
)
unique_stop_min_share_pct = float(
    cfg.get("unique_stop_min_share_pct", 0.02)
)

def min_active_days_for(bucket: str) -> int:
    return min_active_days_by_bucket.get(bucket, min_active_days_default)

trip_frequencies = load_frequencies()
print(f"  {sum(len(v) for v in trip_frequencies.values()):,} frequency entries "
      f"for {len(trip_frequencies):,} trips")

(tg_freq, tg_freq_seasonal, var_freq_seasonal,
 tg_speed, tg_canon) = stream_stop_times(
    trip_lookup, stop_coords, svc_dates, trip_frequencies, stop_meta)

# Ferry / aerial / funicular trips keep their natural per-trip
# (first_uic, last_uic) direction key — same as every other bucket.
# See .claude/concepts/remove-exempt-direction-key.md.

print("\nLoading pfaedle shapes...")
shapes = load_shapes()
print(f"  {len(shapes):,} shapes loaded")

# ── Group trips per (line_key, agency_id, trip_group_id) by merged stops ─
# The grouping pass partitions by (long_name_norm, agency_id, bucket), so
# trip_group_id is unique only WITHIN a partition. Different agencies with
# the same line_key restart tg_id at 0 — e.g. Bernmobil's bus 10 and
# Stadtbus Winterthur's bus 10 both end up tg_id=0 in their own pools.
# The emission key must include agency_id, otherwise the two cities'
# trips collide and one silently overwrites the other.
# Each variant key is (merged_set, direction_key) so opposite directions of
# the same merged-stop set form distinct variants. Per direction-coverage
# concept: directions are split end-to-end so each gets its own pfaedle
# shape, rep trip, stop list, and filter outcome. Applies to every bucket
# including ferry / aerial / funicular (see remove-exempt-direction-key
# concept).
groups: dict = defaultdict(lambda: defaultdict(list))
variant_counts: dict = defaultdict(lambda: defaultdict(int))
# Per-season weighted variant counts; consumed by the multi-window
# rare-variant filter for groups containing at least one
# regional_bus_rescued variant.
variant_counts_seasonal: dict = {
    s: defaultdict(lambda: defaultdict(int)) for s in SEASONS
}
for tid, (line_key, tg_id, aid) in _trip_group_export.items():
    merged_set = _trip_merged_export.get(tid)
    if merged_set is None:
        continue
    direction_key = _trip_direction_export.get(tid)
    if direction_key is None:
        continue
    tg_key = (line_key, aid, tg_id)
    var_key = (merged_set, direction_key)
    groups[tg_key][var_key].append(tid)
    # Weighted by trip's active-date count so a depot run modelled as a few
    # trip_ids active on a single date counts as much smaller service than
    # the same trip_ids active every weekday. Same weight that the
    # in-stream rare-variant filter already uses.
    variant_counts[tg_key][var_key] += _trip_weight_export.get(tid, 1)
    sweights = _trip_weight_seasonal_export.get(tid) or {}
    for s in SEASONS:
        variant_counts_seasonal[s][tg_key][var_key] += sweights.get(s, 0)

# Snapshot for the comprehensive diagnostic before the active-days,
# rare-group, and rare-variant filters mutate `groups`. Variants are keyed
# by (merged_set, direction_key) — the diagnostic preserves the same key
# so per-direction outcomes can be reported.
diag_original = {
    tg_key: {var_key: list(tids) for var_key, tids in vmap.items()}
    for tg_key, vmap in groups.items()
}

# Per-trip-group origin classification: ferry, mountain (aerial / funicular
# / rebucketed_rail) or None. Drives the gate exemption — aerial and
# funicular trips skip the freq-score and active-days gates, rebucketed
# rail does not. route_type is taken from any trip in the group; trips in
# one (line_key, agency_id, tg_id) share the same route_type in practice.
tg_mountain_origin: dict = {}
tg_route_type: dict = {}
for tg_key, vmap in groups.items():
    line_key, _aid, _tg_id = tg_key
    bucket = line_key[2]
    rt = ""
    for tids in vmap.values():
        if tids:
            rt = (route_lookup.get(trip_lookup.get(tids[0], {})
                                   .get("route_id", ""), {})
                  .get("type", ""))
            break
    tg_mountain_origin[tg_key] = _mountain_origin(bucket, rt)
    tg_route_type[tg_key] = rt

# ── Active-days per variant (concept: active-days-filter) ────────────────
# For each emitted-feature unit (line_key, agency_id, trip_group_id,
# merged_stop_set, direction_key), compute the union of active calendar
# dates across every trip in that variant over the full feed validity
# period. Variants below `min_active_days` are dropped before
# supergroup/rare-variant filters run, so their weighted trips don't
# pollute share calculations. Catches construction-replacement services
# even when several distinct constructions share the same ref or the same
# trip group. Per direction-coverage concept: ferry, aerial, and funicular
# are exempt; rebucketed mountain rail is gated like normal train.
variant_service_ids: dict = defaultdict(set)
# Per-variant total yearly departures for the line service summary: each
# trip contributes its FULL-calendar active-day count. Deliberately not
# `_trip_weight_export` — those weights count sample dates only, and
# dividing them by full-calendar active days would understate cadence
# roughly tenfold.
variant_runs_full: dict = defaultdict(int)
for tid, (lk, tg_id_v, aid) in _trip_group_export.items():
    merged_set = _trip_merged_export.get(tid)
    if merged_set is None:
        continue
    direction_key = _trip_direction_export.get(tid)
    if direction_key is None:
        continue
    t = trip_lookup.get(tid)
    if t:
        vkey_ids = (lk, aid, tg_id_v, merged_set, direction_key)
        variant_service_ids[vkey_ids].add(t["service_id"])
        # A frequencies.txt template trip stands for (window // headway)
        # departures per day, not one — continuous services (gondolas
        # etc.) would otherwise read as "1×/day".
        freq_entries = trip_frequencies.get(tid)
        deps_per_day = 1
        if freq_entries:
            deps_per_day = sum(
                max(0, (end - start) // headway)
                for start, end, headway in freq_entries if headway > 0
            ) or 1
        variant_runs_full[vkey_ids] += (
            deps_per_day * len(svc_dates_full.get(t["service_id"], ())))
variant_active_days: dict = {}
# Per-variant calendar stats for the line service summary (baked into
# line_index.json by step 07): first/last active date plus per-weekday
# active-date counts (Mo..Su). Computed here because svc_dates_full is
# released right below.
variant_date_stats: dict = {}  # vkey → (first_date, last_date, [7 counts])
from datetime import date as _date_cls
_weekday_cache: dict = {}
def _weekday_of(d: str) -> int:
    wd = _weekday_cache.get(d)
    if wd is None:
        wd = _date_cls(int(d[:4]), int(d[4:6]), int(d[6:8])).weekday()
        _weekday_cache[d] = wd
    return wd
# Feed validity period — the yardstick the seasonal check measures line
# date ranges against.
feed_first_date: str = ""
feed_last_date: str = ""
for dates in svc_dates_full.values():
    if not dates:
        continue
    lo, hi = min(dates), max(dates)
    if not feed_first_date or lo < feed_first_date:
        feed_first_date = lo
    if not feed_last_date or hi > feed_last_date:
        feed_last_date = hi
for vkey, sids in variant_service_ids.items():
    u: set = set()
    for sid in sids:
        u |= svc_dates_full.get(sid, set())
    variant_active_days[vkey] = len(u)
    if u:
        wd_counts = [0] * 7
        for d in u:
            wd_counts[_weekday_of(d)] += 1
        variant_date_stats[vkey] = (min(u), max(u), wd_counts)
# svc_dates_full is the heaviest object after stop_times; release.
svc_dates_full.clear()
variant_service_ids.clear()

short_active_variants: dict = defaultdict(set)  # tg_key → {var_key,...}
# Bus variants below `min_active_days` but at or above
# `min_active_days_regional_bus` are kept and tagged. Dropped later at
# emission if the line classifies as city bus.
regional_bus_rescued: dict = defaultdict(set)   # tg_key → {var_key,...}
tg_keys_all_short_active: set = set()
for tg_key in list(groups.keys()):
    line_key, aid, tg_id_v = tg_key
    bucket = line_key[2]
    if _gate_exempt(bucket, tg_mountain_origin.get(tg_key)):
        continue
    vmap = groups[tg_key]
    threshold = min_active_days_for(bucket)
    rescue_floor = (min_active_days_regional_bus if bucket == "bus"
                    else threshold)
    to_drop: list = []
    for var_key in vmap:
        ad = variant_active_days.get(
            (line_key, aid, tg_id_v, var_key[0], var_key[1]), 0
        )
        if ad >= threshold:
            continue
        if bucket == "bus" and ad >= rescue_floor:
            regional_bus_rescued[tg_key].add(var_key)
            continue
        to_drop.append(var_key)
    if not to_drop:
        continue
    short_active_variants[tg_key].update(to_drop)
    for var_key in to_drop:
        del vmap[var_key]
        variant_counts[tg_key].pop(var_key, None)
    if not vmap:
        tg_keys_all_short_active.add(tg_key)
        del groups[tg_key]
n_dropped_var = sum(len(s) for s in short_active_variants.values())
n_rescued_var = sum(len(s) for s in regional_bus_rescued.values())
threshold_summary = f"default={min_active_days_default}"
if min_active_days_by_bucket:
    overrides = ", ".join(f"{b}={v}" for b, v in
                          sorted(min_active_days_by_bucket.items()))
    threshold_summary += f"; {overrides}"
print(f"  {n_dropped_var:,} variants dropped by min_active_days "
      f"({threshold_summary}) "
      f"(across {len(short_active_variants)} trip groups; "
      f"{len(tg_keys_all_short_active)} groups fully dropped)")
print(f"  {n_rescued_var:,} bus variants tentatively rescued "
      f"(active_days in [{min_active_days_regional_bus}, "
      f"{min_active_days_default}); dropped at emission if classified as city bus)")

# ── Supergroup formation + rare-group filter ─────────────────────────────
# A supergroup is a transient classification used only for the rare-group
# drop below: trip groups inside one partition (short_name, agency, bucket)
# that share ≥1 merged stop are unioned. Once classified, drop trip groups
# whose weighted share of their supergroup's total is below 10% (5%
# fallback, then keep-all). Catches depot runs that were isolated as their
# own trip group because they share <2 merged stops with the main service.
tg_total_weight: dict = {
    tg_key: sum(variant_counts[tg_key].values()) for tg_key in groups.keys()
}
tg_merged_union: dict = {}
for tg_key, vmap in groups.items():
    u: set = set()
    for var_key in vmap.keys():
        merged_set = var_key[0]
        u |= merged_set
    tg_merged_union[tg_key] = frozenset(u)

partition_to_tgkeys: dict = defaultdict(list)
for tg_key in groups.keys():
    line_key, aid, _tg_id = tg_key
    sn, ln, bkt = line_key
    ln_norm = ln.replace(" ", "").lower()
    partition_str = ln_norm if ln_norm else sn.replace(" ", "").lower()
    partition_to_tgkeys[(partition_str, aid, bkt)].append(tg_key)

supergroup_id_by_tg: dict = {}        # tg_key → int (sequential)
supergroup_members: dict = defaultdict(list)  # sg_id → [tg_key, ...]
sg_counter = 0
for partition_key, tg_keys in partition_to_tgkeys.items():
    K = len(tg_keys)
    if K == 1:
        sg_id = sg_counter
        sg_counter += 1
        supergroup_id_by_tg[tg_keys[0]] = sg_id
        supergroup_members[sg_id].append(tg_keys[0])
        continue

    parent = list(range(K))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    unions = [tg_merged_union[tg_keys[i]] for i in range(K)]
    for i in range(K):
        for j in range(i + 1, K):
            ri, rj = find(i), find(j)
            if ri == rj:
                continue
            si, sj = unions[i], unions[j]
            small, big = (si, sj) if len(si) <= len(sj) else (sj, si)
            if any(s in big for s in small):
                parent[ri] = rj

    cc_to_id: dict = {}
    for i in range(K):
        root = find(i)
        if root not in cc_to_id:
            cc_to_id[root] = sg_counter
            sg_counter += 1
        sg_id = cc_to_id[root]
        supergroup_id_by_tg[tg_keys[i]] = sg_id
        supergroup_members[sg_id].append(tg_keys[i])

supergroup_total_weight: dict = {
    sg_id: sum(tg_total_weight[tg] for tg in members)
    for sg_id, members in supergroup_members.items()
}

rare_group_dropped: set = set()
rare_group_threshold_by_sg: dict = {}
for sg_id, members in supergroup_members.items():
    if len(members) == 1:
        rare_group_threshold_by_sg[sg_id] = None
        continue
    sg_total = supergroup_total_weight[sg_id]
    threshold_used = None
    for pct in (0.10, 0.05):
        threshold = max(1, sg_total * pct)
        kept = [tg for tg in members if tg_total_weight[tg] >= threshold]
        if kept:
            threshold_used = pct
            kept_set = set(kept)
            for tg in members:
                if tg in kept_set:
                    continue
                # Mountain bucket: same exemption the freq-score gate
                # applies. Never drop mountain via the rare-group filter.
                line_key_drop = tg[0]
                if line_key_drop[2] == "mountain":
                    continue
                rare_group_dropped.add(tg)
            break
    rare_group_threshold_by_sg[sg_id] = threshold_used

for tg_key in rare_group_dropped:
    groups.pop(tg_key, None)

# filter_outcomes[tg_key] = {var_key: (outcome, threshold_pct_used)}
# where outcome ∈ {"kept", "rare_variant", "short_active_period"} and
# var_key = (merged_set, direction_key).
diag_filter: dict = {}

# Record variants dropped by the active-days filter. These don't reach
# the rare-variant loop below (they're already gone from `groups`).
for tg_key, dropped in short_active_variants.items():
    bucket_entry = diag_filter.setdefault(tg_key, {})
    for var_key in dropped:
        bucket_entry[var_key] = ("short_active_period", None)

# Rare-variant filter — two phases for groups with at least one
# regional_bus_rescued variant; legacy single-phase otherwise. See
# .claude/concepts/seasonal-regional-bus-rescue.md.
#
# Phase 1 (share gate):
#   - Rescued-bearing group: 10% per window (annual/winter/summer). No 5%
#     fallback. A variant is "kept-by-share" if it clears 10% in any
#     window.
#   - Other group: legacy 10%/5%-fallback against the annual window only.
# Phase 2 (built only after phase 1 has run for every group):
#   - global_kept_uics = union of parent UICs served by any kept-by-share
#     variant across the whole dataset.
# Phase 3 (rescued-bearing groups only):
#   - Unique-stop rescue. A non-kept variant is rescued if it serves a
#     parent UIC not in global_kept_uics AND that UIC is ≥
#     unique_stop_min_distance_m from every UIC in *this* group's kept-by-
#     share set AND the variant's weighted-share is ≥
#     unique_stop_min_share_pct in at least one window.
#
# Diagnostics:
#   rare_variant_window_passed[(tg_key, var_key)] ∈
#     {"annual", "winter", "summer", "unique_stop", None}
#   rare_variant_threshold_pct_passed[(tg_key, var_key)] = 0.10 / 0.05 /
#     None (None for unique_stop rescues or for drops).
rare_variant_window_passed: dict = {}
rare_variant_threshold_pct_passed: dict = {}

def _pct_pass(counts: dict, vmap_keys, pct: float) -> set:
    total = sum(counts.values())
    threshold = max(1, total * pct)
    return {vk for vk in vmap_keys if counts.get(vk, 0) >= threshold}

def _legacy_rare_variant(counts: dict, vmap_keys) -> tuple:
    """Annual 10% then 5% fallback; if both pass nothing, keep all."""
    for pct in (0.10, 0.05):
        kept = _pct_pass(counts, vmap_keys, pct)
        if kept:
            return kept, pct
    return set(vmap_keys), None

# ── Phase 1: standard share gate for every group ─────────────────────
kept_by_share: dict = {}  # tg_key → set(var_key)
for tg_key, vmap in list(groups.items()):
    is_rescued_group = bool(regional_bus_rescued.get(tg_key))
    if is_rescued_group:
        per_window_kept: dict = {}
        for s in SEASONS:
            counts_s = (variant_counts if s == "annual"
                        else variant_counts_seasonal[s])[tg_key]
            per_window_kept[s] = _pct_pass(counts_s, vmap, 0.10)
        kept: set = set()
        for var_key in vmap:
            for s in SEASONS:
                if var_key in per_window_kept[s]:
                    kept.add(var_key)
                    rare_variant_window_passed[(tg_key, var_key)] = s
                    rare_variant_threshold_pct_passed[(tg_key, var_key)] = 0.10
                    break
        kept_by_share[tg_key] = kept
    else:
        kept, pct = _legacy_rare_variant(variant_counts[tg_key], vmap)
        kept_by_share[tg_key] = kept
        for var_key in kept:
            rare_variant_window_passed[(tg_key, var_key)] = "annual"
            rare_variant_threshold_pct_passed[(tg_key, var_key)] = pct

# ── Phase 2: global kept-by-share parent UIC set ─────────────────────
# var_key[0] is the merged-stop frozenset = the variant's parent UICs.
global_kept_uics: set = set()
for tg_key, kept in kept_by_share.items():
    for var_key in kept:
        global_kept_uics |= var_key[0]

# ── Phase 3: unique-stop rescue (rescued-bearing groups only) ────────
n_unique_stop_rescued = 0
for tg_key, vmap in groups.items():
    if not regional_bus_rescued.get(tg_key):
        continue
    kept = kept_by_share[tg_key]
    group_kept_uics: set = set()
    for vk in kept:
        group_kept_uics |= vk[0]
    # Pre-resolve this group's kept UIC coordinates once.
    group_kept_coords: list = []
    for uic in group_kept_uics:
        c = stop_coords.get(uic)
        if c:
            group_kept_coords.append(c)

    for var_key in vmap:
        if var_key in kept:
            continue
        candidate_uics = var_key[0] - global_kept_uics
        if not candidate_uics:
            continue
        qualifying = False
        for uic in candidate_uics:
            uic_coord = stop_coords.get(uic)
            if uic_coord is None:
                continue
            far_enough = True
            for kept_coord in group_kept_coords:
                if haversine_km(uic_coord[0], uic_coord[1],
                                kept_coord[0], kept_coord[1]) * 1000.0 \
                        < unique_stop_min_distance_m:
                    far_enough = False
                    break
            if far_enough:
                qualifying = True
                break
        if not qualifying:
            continue
        passes_floor = False
        for s in SEASONS:
            counts_s = (variant_counts if s == "annual"
                        else variant_counts_seasonal[s])[tg_key]
            total = sum(counts_s.values())
            share = (counts_s.get(var_key, 0) / total) if total else 0
            if share >= unique_stop_min_share_pct:
                passes_floor = True
                break
        if not passes_floor:
            continue
        kept.add(var_key)
        rare_variant_window_passed[(tg_key, var_key)] = "unique_stop"
        rare_variant_threshold_pct_passed[(tg_key, var_key)] = None
        n_unique_stop_rescued += 1
if n_unique_stop_rescued:
    print(f"  {n_unique_stop_rescued:,} variants rescued by unique-stop rule")

# ── Apply kept_by_share to groups + populate diag_filter ─────────────
for tg_key, vmap in list(groups.items()):
    kept = kept_by_share.get(tg_key, set())
    if kept and kept != set(vmap.keys()):
        groups[tg_key] = {vk: vmap[vk] for vk in kept}
    bucket_entry = diag_filter.setdefault(tg_key, {})
    for var_key in vmap:
        if var_key in kept:
            bucket_entry[var_key] = (
                "kept",
                rare_variant_threshold_pct_passed.get((tg_key, var_key)),
            )
        else:
            rare_variant_window_passed.setdefault((tg_key, var_key), None)
            bucket_entry[var_key] = ("rare_variant", None)

# Trip groups dropped by the supergroup filter never reached the per-variant
# filter loop above; mark any of their variants we haven't already labelled
# (i.e. weren't short_active) as "kept" so the group-level reason
# `rare_group_dropped` is what surfaces for them.
for tg_key in rare_group_dropped:
    bucket_entry = diag_filter.setdefault(tg_key, {})
    for var_key in diag_original.get(tg_key, {}):
        bucket_entry.setdefault(var_key, ("kept", None))

# Pre-filter low-freq groups out so they don't waste downstream work. The
# active-days gate has already run upstream at variant granularity. Per
# direction-coverage concept: ferry + true mountain (aerial/funicular)
# skip the gate; rebucketed rail does not.
#
# Groups containing at least one regional_bus_rescued variant are
# evaluated against three windows (annual / winter Jan-Mar / summer
# Jun-Aug) and pass if the f_weighted in any window exceeds worst_freq.
# The winning window's raw freq becomes the group's effective freq for
# downstream emission and salience — line thickness / visibility track
# in-season cadence rather than annual dilution.
# See .claude/concepts/seasonal-regional-bus-rescue.md.
drawable_groups = {}
freq_gate_window_passed: dict = {}  # tg_key → "annual"|"winter"|"summer"|None
best_freq_map, worst_freq_map = _frequencies()
for (line_key, aid, tg_id), variant_map in groups.items():
    bucket = line_key[2]
    mode_approx = _BUCKET_MODE_APPROX.get(bucket, "regional_bus")
    tg_key = (line_key, aid, tg_id)
    seasonal = tg_freq_seasonal.get(tg_key) or {}
    raw_annual = seasonal.get("annual") \
        or {"f_core": 0.0, "f_eve": 0.0, "f_we": 0.0}
    worst_f = worst_freq_map.get(mode_approx, 0.0)
    exempt = _freq_gate_exempt(bucket, tg_mountain_origin.get(tg_key))
    is_rescued_group = bool(regional_bus_rescued.get(tg_key))
    passed = False
    if exempt:
        passed = True
        freq_gate_window_passed[tg_key] = "annual"
    else:
        windows = SEASONS if is_rescued_group else ("annual",)
        for s in windows:
            raw_s = seasonal.get(s) or {"f_core": 0.0, "f_eve": 0.0, "f_we": 0.0}
            if weighted_freq(raw_s) > worst_f:
                passed = True
                freq_gate_window_passed[tg_key] = s
                if s != "annual":
                    # Rebind the group's effective annual freq to the
                    # winning window so downstream thickness / salience
                    # use in-season cadence.
                    tg_freq[tg_key] = dict(raw_s)
                break
        if not passed:
            freq_gate_window_passed[tg_key] = None
    if passed:
        drawable_groups[tg_key] = variant_map
n_rescued_drawable = sum(1 for k, v in drawable_groups.items()
                         if freq_gate_window_passed.get(k) not in (None, "annual"))
print(f"  {len(drawable_groups):,} drawable (line_key, agency, trip_group) entries "
      f"({n_rescued_drawable} via seasonal window)")

# ── Post-emission split (concept: line-key-split-after-filter.md) ────────
# For each drawable group, run union-find on its SURVIVING variants
# (post-freq/rare-variant/short-active-period filters). If the group
# breaks into ≥2 connected components (variants sharing <2 merged stops
# across the split boundary), spin each component off into its own
# tg_key with a content-hash tg_id, recompute freq per sub-group, and
# drop sub-groups that fall below worst_freq. Prevents the pathology
# where a single line_key covers multiple geographically-disjoint
# corridors after low-freq "bridging" variants get filtered out (e.g.
# SBB IR routes filed without a discriminating name — see the concept).
_split_new = 0
_split_dropped = 0
_split_parents = 0
for _parent_tg_key in list(drawable_groups.keys()):
    _parent_line_key, _parent_aid, _parent_tg_id = _parent_tg_key
    _vmap = drawable_groups[_parent_tg_key]
    _vlist = list(_vmap.keys())
    if len(_vlist) <= 1:
        continue
    _uf = list(range(len(_vlist)))
    def _uf_find(x):
        while _uf[x] != x:
            _uf[x] = _uf[_uf[x]]
            x = _uf[x]
        return x
    _msets = [vk[0] for vk in _vlist]
    for _i in range(len(_vlist)):
        _si = _msets[_i]
        for _j in range(_i + 1, len(_vlist)):
            _ri, _rj = _uf_find(_i), _uf_find(_j)
            if _ri == _rj:
                continue
            _sj = _msets[_j]
            _small, _big = (_si, _sj) if len(_si) <= len(_sj) else (_sj, _si)
            _shared = 0
            for _s in _small:
                if _s in _big:
                    _shared += 1
                    if _shared >= 2:
                        _uf[_ri] = _rj
                        break
    _comps = defaultdict(list)
    for _i in range(len(_vlist)):
        _comps[_uf_find(_i)].append(_vlist[_i])
    if len(_comps) <= 1:
        continue

    _split_parents += 1
    _bucket = _parent_line_key[2]
    _mode_approx = _BUCKET_MODE_APPROX.get(_bucket, "regional_bus")
    _worst_f = worst_freq_map.get(_mode_approx, 0.0)
    _exempt = _freq_gate_exempt(_bucket, tg_mountain_origin.get(_parent_tg_key))
    _is_rescued_parent = bool(regional_bus_rescued.get(_parent_tg_key))
    _parent_speed = tg_speed.get(_parent_tg_key)
    _parent_mo = tg_mountain_origin.get(_parent_tg_key)
    _parent_rt = tg_route_type.get(_parent_tg_key)
    _parent_sg = supergroup_id_by_tg.get(_parent_tg_key)
    _parent_rescued_vars = regional_bus_rescued.get(_parent_tg_key, set())

    # Retire the parent tg_key from every per-tg map that downstream
    # emission / diagnostics read; sub-groups replace it entry-by-entry.
    del drawable_groups[_parent_tg_key]
    freq_gate_window_passed.pop(_parent_tg_key, None)
    tg_freq.pop(_parent_tg_key, None)
    tg_freq_seasonal.pop(_parent_tg_key, None)
    tg_speed.pop(_parent_tg_key, None)
    tg_mountain_origin.pop(_parent_tg_key, None)
    tg_route_type.pop(_parent_tg_key, None)
    supergroup_id_by_tg.pop(_parent_tg_key, None)
    regional_bus_rescued.pop(_parent_tg_key, None)
    _parent_diag_original = diag_original.pop(_parent_tg_key, {})
    _parent_diag_filter = diag_filter.pop(_parent_tg_key, {})
    _parent_variant_counts = variant_counts.pop(_parent_tg_key, {})
    _parent_variant_counts_seasonal = {
        s: variant_counts_seasonal[s].pop(_parent_tg_key, {}) for s in SEASONS
    }
    tg_total_weight.pop(_parent_tg_key, None)

    _new_ids: set = set()
    for _comp_vars in _comps.values():
        # Recompute per-sub-group freq from the retained per-variant
        # seasonal freq. Same window sweep as the initial freq gate:
        # rescued-bearing sub-groups get evaluated over all seasons;
        # others only against the annual window.
        _sub_seasonal = {s: {"f_core": 0.0, "f_eve": 0.0, "f_we": 0.0}
                         for s in SEASONS}
        for _vk in _comp_vars:
            _v_seasonal = var_freq_seasonal.get((_parent_tg_key, _vk)) or {}
            for _s in SEASONS:
                _per = _v_seasonal.get(_s) \
                    or {"f_core": 0.0, "f_eve": 0.0, "f_we": 0.0}
                _sub_seasonal[_s]["f_core"] += _per["f_core"]
                _sub_seasonal[_s]["f_eve"] += _per["f_eve"]
                _sub_seasonal[_s]["f_we"] += _per["f_we"]

        _sub_rescued_vars = {vk for vk in _comp_vars if vk in _parent_rescued_vars}
        _is_rescued_sub = _is_rescued_parent and bool(_sub_rescued_vars)

        _passed = False
        _winning_window = None
        if _exempt:
            _passed = True
            _winning_window = "annual"
        else:
            _windows = SEASONS if _is_rescued_sub else ("annual",)
            for _s in _windows:
                if weighted_freq(_sub_seasonal[_s]) > _worst_f:
                    _passed = True
                    _winning_window = _s
                    break

        if not _passed:
            _split_dropped += 1
            continue

        # Content-hash tg_id from the sub-group's merged UIC union —
        # same hashing scheme as stream_stop_times.
        _comp_uics: set = set()
        for _vk in _comp_vars:
            _comp_uics |= _vk[0]
        _new_tg_id = content_tg_id(_comp_uics)
        if _new_tg_id in _new_ids:
            raise RuntimeError(
                f"post-split content_tg_id collision under "
                f"{_parent_tg_key}: {_new_tg_id}")
        _new_ids.add(_new_tg_id)
        _new_tg_key = (_parent_line_key, _parent_aid, _new_tg_id)

        drawable_groups[_new_tg_key] = {vk: _vmap[vk] for vk in _comp_vars}
        _win_raw = _sub_seasonal[_winning_window]
        tg_freq[_new_tg_key] = dict(_win_raw)
        tg_freq_seasonal[_new_tg_key] = {s: dict(_sub_seasonal[s]) for s in SEASONS}
        tg_speed[_new_tg_key] = _parent_speed
        tg_mountain_origin[_new_tg_key] = _parent_mo
        tg_route_type[_new_tg_key] = _parent_rt
        freq_gate_window_passed[_new_tg_key] = _winning_window
        if _parent_sg is not None:
            supergroup_id_by_tg[_new_tg_key] = _parent_sg
        if _sub_rescued_vars:
            regional_bus_rescued[_new_tg_key] = _sub_rescued_vars

        for _vk in _comp_vars:
            _seasonal = var_freq_seasonal.get((_parent_tg_key, _vk))
            if _seasonal is not None:
                var_freq_seasonal[(_new_tg_key, _vk)] = _seasonal
            variant_counts[_new_tg_key][_vk] = _parent_variant_counts.get(_vk, 0)
            for _s in SEASONS:
                variant_counts_seasonal[_s][_new_tg_key][_vk] = \
                    _parent_variant_counts_seasonal[_s].get(_vk, 0)
            _mset, _dkey = _vk
            _parent_va_key = (_parent_line_key, _parent_aid, _parent_tg_id,
                              _mset, _dkey)
            _new_va_key = (_parent_line_key, _parent_aid, _new_tg_id,
                           _mset, _dkey)
            if _parent_va_key in variant_active_days:
                variant_active_days[_new_va_key] = variant_active_days[_parent_va_key]
            if _parent_va_key in variant_date_stats:
                variant_date_stats[_new_va_key] = variant_date_stats[_parent_va_key]
            if _parent_va_key in variant_runs_full:
                variant_runs_full[_new_va_key] = variant_runs_full[_parent_va_key]
            _rvwp = rare_variant_window_passed.pop((_parent_tg_key, _vk), None)
            if _rvwp is not None:
                rare_variant_window_passed[(_new_tg_key, _vk)] = _rvwp
            _rvpct = rare_variant_threshold_pct_passed.pop(
                (_parent_tg_key, _vk), None)
            if _rvpct is not None:
                rare_variant_threshold_pct_passed[(_new_tg_key, _vk)] = _rvpct

        tg_total_weight[_new_tg_key] = sum(variant_counts[_new_tg_key].values())

        # Diagnostic: seed diag_original + diag_filter for the sub-group
        # so gtfs_groups_full.json shows each sub-group as its own entry.
        diag_original[_new_tg_key] = {
            vk: list(_parent_diag_original.get(vk, []))
            for vk in _comp_vars
        }
        diag_filter[_new_tg_key] = {
            vk: _parent_diag_filter.get(vk,
                ("kept",
                 rare_variant_threshold_pct_passed.get((_new_tg_key, vk))))
            for vk in _comp_vars
        }

        _split_new += 1

if _split_parents:
    print(f"  Post-emission split: {_split_parents} groups → "
          f"{_split_new} sub-groups drawable "
          f"({_split_dropped} dropped by re-checked freq gate)")
