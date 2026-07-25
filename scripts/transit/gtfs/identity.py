"""Trip identity and streaming.

`stream_stop_times` is the single streaming pass that reads stop_times.txt,
buckets trips into groups by shared merged-stop overlap, and populates the
module-level `_trip_*_export` maps that the driver's main() reads.
"""
import csv
import hashlib
from collections import defaultdict

from geometry import haversine_km, parse_time


def content_tg_id(merged_uics) -> str:
    """Content-based trip_group_id — 8 hex chars of blake2s over the sorted
    merged-UIC set of the (sub-)group. Same UIC set always yields the same
    id, so line_keys stay stable across pipeline rebuilds (and are naturally
    stable across the post-emission split — the same hash function keys both
    partition-level parent groups and post-split sub-groups)."""
    payload = ",".join(sorted(str(u) for u in merged_uics)).encode("utf-8")
    return hashlib.blake2s(payload, digest_size=4).hexdigest()

from .loaders import GTFS
from .frequency import (
    CORE_END,
    CORE_HOURS,
    CORE_START,
    EVENING_END,
    EVENING_HOURS,
    EVENING_START,
    SEASONS,
    WEEKEND_END,
    WEEKEND_HOURS,
    WEEKEND_START,
    _date_in_season,
    _sample_dates,
    _sample_dates_seasonal,
)

# ── Trip-grouping exports ────────────────────────────────────────────────────

_trip_group_export: dict = {}        # trip_id → (line_key, trip_group_id, agency_id)
_trip_stops_export: dict = {}        # trip_id → [stop_id, ...]   (sequence)
_trip_merged_export: dict = {}       # trip_id → frozenset(merged_stop_id)  (variant identity)
_trip_weight_export: dict = {}       # trip_id → int (≈ trip-runs across calendar)
_trip_weight_seasonal_export: dict = {}  # trip_id → {"annual": n, "winter": n, "summer": n}
_trip_direction_export: dict = {}    # trip_id → (first_merged_uic, last_merged_uic)
_trip_dep_span_export: dict = {}     # trip_id → (dep_lo, dep_hi) seconds — origin
                                     # departure (both equal), or the
                                     # frequencies.txt template window
_dwell_export: dict = {}             # merged_uic → avg dwell (seconds) — piggybacks
                                     # on this streaming pass so step 07 doesn't have
                                     # to walk the 1.7 GB stop_times.txt a second time.


def stream_stop_times(trips, stop_coords, svc_dates, trip_frequencies, stop_meta):
    """One streaming pass → raw trip counts + speed per line, plus trip-group
    partitioning. Populates module-level exports `_trip_group_export`,
    `_trip_stops_export`, `_trip_merged_export`, `_trip_weight_export`,
    and `_trip_direction_export`.
    """
    global _trip_group_export, _trip_stops_export, _trip_merged_export, _trip_weight_export, _trip_direction_export, _trip_dep_span_export, _dwell_export

    stop_merge: dict = {}
    for sid, meta in stop_meta.items():
        parent = meta["parent"]
        stop_merge[sid] = parent if parent else sid.split(":")[0]

    # Per-merged-UIC dwell accumulator (see _dwell_export docstring above).
    # avg dep − arr across every trip-stop row; rows with dep == arr are
    # folded in as 0 so the mean matches step 07's original definition.
    dwell_sum: dict = defaultdict(float)
    dwell_cnt: dict = defaultdict(int)

    wd_set, we_set, n_wd_samples, n_we_samples = _sample_dates()
    season_dates = _sample_dates_seasonal()
    print(f"  Sample dates: {n_wd_samples} weekday + {n_we_samples} weekend "
          f"(winter: {season_dates['winter'][2]}+{season_dates['winter'][3]}, "
          f"summer: {season_dates['summer'][2]}+{season_dates['summer'][3]})")
    print("  Streaming stop_times.txt ...")

    # Per-trip freq contribution buffered here, summed into tg_freq once trip
    # groups are assigned. No per-line_key aggregation exists — trip group is
    # the only line identity in this pipeline (see
    # .claude/concepts/trip-group-as-sole-line-identity.md).
    # Per-season: trip_freq[tid] = {season: (core, eve, we)} for seasons
    # ("annual","winter","summer"). The "annual" entry is the existing value.
    trip_freq: dict = {}

    # Per-trip buffer for the post-stream grouping phase.
    # trip_id → (line_key, agency_id, weight, raw_variant_frozenset,
    #            merged_stop_frozenset, sequence_list)
    trip_buf: dict = {}

    # Per-trip seasonal activity weights. trip_weight_seasonal[tid][season]
    # = number of active calendar dates in that season (annual = total).
    # Consumed by the multi-window rare-variant filter for groups containing
    # at least one regional_bus_rescued variant.
    trip_weight_seasonal: dict = {}

    current_trip_id = None
    current_stops: list = []

    def process_trip(trip_id, stops):
        if not stops or trip_id not in trips:
            return
        trip = trips[trip_id]
        line_key = trip["line_key"]
        service_id = trip["service_id"]
        active_dates = svc_dates.get(service_id, set())
        first_dep = stops[0][3]

        # Count of sample dates this trip is active on, per season. The annual
        # multiplier downscales construction lines (active on 1/26 weekday
        # samples → contributes 1/26 of its raw count); the seasonal versions
        # support the multi-window freq gate for regional-bus-rescued groups.
        per_season_hits = {}
        for s, (wd_s, we_s, _nw, _nwe) in season_dates.items():
            per_season_hits[s] = (
                sum(1 for d in wd_s if d in active_dates),
                sum(1 for d in we_s if d in active_dates),
            )

        core_n = eve_n = we_n = 0
        freq_entries = trip_frequencies.get(trip_id, [])
        if freq_entries:
            _trip_dep_span_export[trip_id] = (
                min(e[0] for e in freq_entries),
                max(e[1] for e in freq_entries),
            )
            for start, end, headway in freq_entries:
                if headway <= 0:
                    continue
                core_n += max(0, (min(end, CORE_END) - max(start, CORE_START)) // headway)
                eve_n  += max(0, (min(end, EVENING_END) - max(start, EVENING_START)) // headway)
                we_n   += max(0, (min(end, WEEKEND_END) - max(start, WEEKEND_START)) // headway)
        else:
            _trip_dep_span_export[trip_id] = (first_dep, first_dep)
            if CORE_START <= first_dep < CORE_END:
                core_n = 1
            elif EVENING_START <= first_dep < EVENING_END:
                eve_n = 1
            if WEEKEND_START <= first_dep < WEEKEND_END:
                we_n = 1
        trip_freq[trip_id] = {
            s: (core_n * wd_h, eve_n * wd_h, we_n * we_h)
            for s, (wd_h, we_h) in per_season_hits.items()
        }

        raw_variant = frozenset(s[1] for s in stops)
        merged_set = frozenset(stop_merge.get(s[1]) or s[1].split(":")[0] for s in stops)
        sequence   = [(s[1], s[2], s[3]) for s in stops]
        annual_w = max(1, len(active_dates))
        winter_w = sum(1 for d in active_dates if _date_in_season(d, "winter"))
        summer_w = sum(1 for d in active_dates if _date_in_season(d, "summer"))
        trip_weight_seasonal[trip_id] = {
            "annual": annual_w, "winter": winter_w, "summer": summer_w,
        }
        trip_buf[trip_id] = (
            line_key, trip.get("agency_id", ""),
            annual_w,
            raw_variant, merged_set, sequence,
        )

    with open(GTFS / "stop_times.txt", encoding="utf-8-sig") as f:
        row_count = 0
        for row in csv.DictReader(f):
            tid = row["trip_id"]
            try:
                arr = parse_time(row["arrival_time"])
                dep = parse_time(row["departure_time"])
            except (ValueError, IndexError):
                continue
            stop_id = row["stop_id"]
            # Synthetic waypoints (gtfs-trip-overrides, insert_waypoint)
            # exist only to steer pfaedle — invisible to everything after
            # routing.
            if stop_id.startswith("WPT:"):
                continue
            uic_for_dwell = stop_merge.get(stop_id) or stop_id.split(":")[0]
            if uic_for_dwell:
                dwell_sum[uic_for_dwell] += max(0, dep - arr)
                dwell_cnt[uic_for_dwell] += 1
            seq = int(row["stop_sequence"])

            if tid != current_trip_id:
                process_trip(current_trip_id, current_stops)
                current_trip_id = tid
                current_stops = []

            current_stops.append((seq, stop_id, arr, dep))
            row_count += 1
            if row_count % 2_000_000 == 0:
                print(f"    {row_count // 1_000_000}M rows...")

        process_trip(current_trip_id, current_stops)
    print(f"  Done. {row_count:,} rows processed, {len(trip_buf):,} trips buffered.")

    _dwell_export = {
        u: dwell_sum[u] / dwell_cnt[u]
        for u in dwell_sum if dwell_cnt[u] > 0
    }
    print(f"  Dwell aggregated for {len(_dwell_export):,} merged UICs.")

    # ── Trip-group partitioning ──────────────────────────────────────────────
    print("  Partitioning trips and computing trip-groups...")
    partition_trips: dict = defaultdict(list)
    for tid, (lk, aid, _w, _rv, _ms, _seq) in trip_buf.items():
        sn, ln, bkt = lk
        ln_norm = ln.replace(" ", "").lower()
        partition_str = ln_norm if ln_norm else sn.replace(" ", "").lower()
        partition_trips[(partition_str, aid, bkt)].append(tid)

    trip_group: dict = {}
    n_groups_total = 0
    for tids in partition_trips.values():
        patterns: dict = defaultdict(list)
        for tid in tids:
            patterns[trip_buf[tid][4]].append(tid)
        pattern_sets = list(patterns.keys())
        pattern_tids = list(patterns.values())
        P = len(pattern_sets)
        if P == 1:
            tg_hash = content_tg_id(pattern_sets[0])
            for tid in pattern_tids[0]:
                trip_group[tid] = tg_hash
            n_groups_total += 1
            continue

        parent = list(range(P))
        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(P):
            si = pattern_sets[i]
            for j in range(i + 1, P):
                ri, rj = find(i), find(j)
                if ri == rj:
                    continue
                sj = pattern_sets[j]
                small, big = (si, sj) if len(si) <= len(sj) else (sj, si)
                count = 0
                for s in small:
                    if s in big:
                        count += 1
                        if count >= 2:
                            parent[ri] = rj
                            break

        # Content-hash tg_ids: each connected component's id is a hash of
        # its union-of-merged-UICs, so re-running the pipeline on the same
        # feed yields the same line_keys (see .claude/concepts/line-key-
        # split-after-filter.md). Uniqueness within a partition is asserted
        # below; parent (this) and post-split sub-groups (in
        # _pipeline_grouping.py) share the same hashing scheme.
        component_uics: dict = {}
        for i in range(P):
            r = find(i)
            component_uics.setdefault(r, set()).update(pattern_sets[i])
        cc_ids: dict = {}
        for root, uics in component_uics.items():
            cc_ids[root] = content_tg_id(uics)
        if len(set(cc_ids.values())) != len(cc_ids):
            raise RuntimeError(
                f"content_tg_id collision inside a single partition: "
                f"{sorted(cc_ids.values())}"
            )
        for i in range(P):
            root = find(i)
            for tid in pattern_tids[i]:
                trip_group[tid] = cc_ids[root]
        n_groups_total += len(cc_ids)

    print(f"  {len(partition_trips):,} partitions → {n_groups_total:,} trip-groups")

    # ── Per-tg_key aggregation ───────────────────────────────────────────────
    # tg_key = (line_key, agency_id, trip_group_id) — the only line identity in
    # this pipeline. Frequency and the canonical trip are both summed/picked
    # here, in the single loop over trip_buf, against the trip-group partition
    # built above. No second partition, no name-based fallback.
    # tg_freq[tg_key][season] = [core_sum, eve_sum, we_sum] across trips.
    tg_freq: dict = defaultdict(
        lambda: {s: [0, 0, 0] for s in SEASONS}
    )
    # var_freq[(tg_key, var_key)][season] = [core_sum, eve_sum, we_sum] —
    # parallel to tg_freq but aggregated per variant for per-direction
    # thickness; see .claude/concepts/seasonal-regional-bus-rescue.md
    # § "Per-variant freq for line thickness".
    var_freq: dict = defaultdict(
        lambda: {s: [0, 0, 0] for s in SEASONS}
    )
    tg_canon: dict = {}  # tg_key → {"canon_score": int, "stops": [(sid, arr, dep), ...]}

    for tid, (lk, aid, weight, raw_variant, merged_set, sequence) in trip_buf.items():
        tg = trip_group.get(tid)
        if tg is None:
            continue
        tg_key = (lk, aid, tg)
        # Expose per-trip group identity, stop sequence, and merged-stop variant
        # for downstream emission and per-group shape dedup.
        _trip_group_export[tid] = (lk, tg, aid)
        _trip_stops_export[tid] = [s[0] for s in sequence]
        _trip_merged_export[tid] = merged_set
        _trip_weight_export[tid] = weight
        _trip_weight_seasonal_export[tid] = trip_weight_seasonal.get(tid)
        first_sid = sequence[0][0]
        last_sid = sequence[-1][0]
        first_uic = stop_merge.get(first_sid) or first_sid.split(":")[0]
        last_uic = stop_merge.get(last_sid) or last_sid.split(":")[0]
        direction_key = (first_uic, last_uic)
        _trip_direction_export[tid] = direction_key
        var_key = (merged_set, direction_key)

        tc = trip_freq.get(tid)
        if tc is not None:
            tgf = tg_freq[tg_key]
            vf = var_freq[(tg_key, var_key)]
            for s in SEASONS:
                c, e, w = tc[s]
                bucket = tgf[s]
                bucket[0] += c
                bucket[1] += e
                bucket[2] += w
                vbucket = vf[s]
                vbucket[0] += c
                vbucket[1] += e
                vbucket[2] += w

        n = len(sequence)
        canon_score = n * weight
        existing = tg_canon.get(tg_key)
        if existing is None or canon_score > existing["canon_score"]:
            tg_canon[tg_key] = {"canon_score": canon_score, "stops": sequence}

    trip_buf.clear()
    trip_freq.clear()

    # Compute per-trip-group speed from each group's canonical trip.
    tg_speed: dict = {}
    for tg_key, canon in tg_canon.items():
        stops = canon["stops"]
        if len(stops) < 2:
            continue
        segments: list = [[]]
        for i, (stop_id, arr, dep) in enumerate(stops):
            segments[-1].append((stop_id, arr, dep))
            if 0 < i < len(stops) - 1 and (dep - arr) > 600:
                segments.append([])

        seg_speeds = []
        for seg in segments:
            if len(seg) < 2:
                continue
            total_time = seg[-1][2] - seg[0][2]
            if total_time <= 0:
                continue
            total_dist = sum(
                haversine_km(
                    *(stop_coords.get(seg[j][0]) or stop_coords.get(seg[j][0].split(":")[0]) or (0,0)),
                    *(stop_coords.get(seg[j+1][0]) or stop_coords.get(seg[j+1][0].split(":")[0]) or (0,0)),
                )
                for j in range(len(seg) - 1)
                if (stop_coords.get(seg[j][0]) or stop_coords.get(seg[j][0].split(":")[0]))
                and (stop_coords.get(seg[j+1][0]) or stop_coords.get(seg[j+1][0].split(":")[0]))
            )
            if total_dist > 0:
                seg_speeds.append(total_dist / (total_time / 3600))

        if seg_speeds:
            tg_speed[tg_key] = round(sum(seg_speeds) / len(seg_speeds), 1)

    # Normalise tg_freq from "trip × sample-days-hit" totals to trips/hour per
    # window. f_core = trips_in_core_window / (n_weekday_samples · core_hours)
    # — the trip group's average trips-per-hour during the core window across
    # weekday sample dates. Same for eve and we.
    #
    # `tg_freq_out` holds the annual (legacy) values, used by every gate that
    # is not regional-bus-rescue aware.
    # `tg_freq_seasonal_out` adds per-season {f_core, f_eve, f_we} for the
    # multi-window freq gate. Seasons with zero sample dates collapse to 0
    # rather than dividing by zero — a rescued group with no sample dates in
    # a given window cannot pass that window's gate.
    tg_freq_out: dict = {}
    tg_freq_seasonal_out: dict = {}
    season_norms = {
        s: (max(1, ns[2]) * CORE_HOURS,
            max(1, ns[2]) * EVENING_HOURS,
            max(1, ns[3]) * WEEKEND_HOURS)
        for s, ns in season_dates.items()
    }
    for tg_key, per_season in tg_freq.items():
        seasonal = {}
        for s, (c, e, w) in per_season.items():
            cn, en, wen = season_norms[s]
            seasonal[s] = {
                "f_core": c / cn,
                "f_eve":  e / en,
                "f_we":   w / wen,
            }
        tg_freq_seasonal_out[tg_key] = seasonal
        tg_freq_out[tg_key] = seasonal["annual"]

    # Normalise var_freq the same way. var_freq_seasonal_out[(tg_key, var_key)]
    # mirrors tg_freq_seasonal_out's per-season {f_core, f_eve, f_we} shape.
    var_freq_seasonal_out: dict = {}
    for key, per_season in var_freq.items():
        seasonal = {}
        for s, (c, e, w) in per_season.items():
            cn, en, wen = season_norms[s]
            seasonal[s] = {
                "f_core": c / cn,
                "f_eve":  e / en,
                "f_we":   w / wen,
            }
        var_freq_seasonal_out[key] = seasonal

    return (tg_freq_out, tg_freq_seasonal_out,
            var_freq_seasonal_out, tg_speed, tg_canon)
