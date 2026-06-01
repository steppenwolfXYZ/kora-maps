#!/usr/bin/env python3
"""
Pre-implementation research scan for the GTFS line grouping concept.

Partitions trips by (long_name_norm or short_name, agency_id, bucket), then
within each partition computes connected components on the trip-graph where two
trips are connected iff they share at least 2 merged stop IDs (parent_station
when present, else base UIC).

Reports:
  - Partition / trip totals
  - Distribution of trip-group counts per partition (1, 2, 3, 4+)
  - Distribution of trip-group sizes (trips per group)
  - Empty-long_name fallback collisions
  - Spot checks for known cases (S3 networks, Forchbahn, BOB)
  - Top "many-group" partitions for manual inspection
"""

import csv
import sys
import importlib.util
import gc
import traceback
from pathlib import Path
from collections import defaultdict, Counter
from itertools import combinations

# Force line-buffered stdout so each progress line survives even on hard kill.
sys.stdout.reconfigure(line_buffering=True)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
GTFS = ROOT / "data" / "gtfs"
OUT  = ROOT / "data" / "transit" / "trip_group_scan.json"

# Reuse loaders from 05_score_and_match.py
spec = importlib.util.spec_from_file_location("s05", HERE / "05_score_and_match.py")
_m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_m)


def build_stop_merge_map() -> dict:
    """{stop_id: merged_id} — parent_station when present, else base UIC."""
    merge = {}
    with open(GTFS / "stops.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sid = row["stop_id"]
            if sid.startswith("0000"):
                continue
            parent = row.get("parent_station", "").strip()
            merge[sid] = parent if parent else sid.split(":")[0]
    return merge


def stream_trip_stops(merge: dict, wanted_trips: set) -> dict:
    """{trip_id: frozenset(merged_stop_ids)}. Streams stop_times.txt once.
    Only retains trips whose id is in `wanted_trips`."""
    trip_stops: dict = defaultdict(set)
    with open(GTFS / "stop_times.txt", encoding="utf-8-sig") as f:
        row_count = 0
        for row in csv.DictReader(f):
            tid = row["trip_id"]
            if tid not in wanted_trips:
                row_count += 1
                continue
            sid = row["stop_id"]
            mid = merge.get(sid) or sid.split(":")[0]
            trip_stops[tid].add(mid)
            row_count += 1
            if row_count % 2_000_000 == 0:
                print(f"    {row_count // 1_000_000}M rows...")
    print(f"  freezing {len(trip_stops):,} trip stop sets...")
    return {tid: frozenset(s) for tid, s in trip_stops.items()}


def connected_components(trips: list, trip_stops: dict, min_shared: int = 2) -> list:
    """
    Trips are connected iff they share ≥min_shared merged stop IDs.
    Returns list of lists (each inner list is a connected component).

    Deduplicates trips into distinct stop-set patterns first, then unions
    patterns pairwise. The pattern count P is far smaller than the trip count
    for typical partitions (multiple trips of the same line on the same day
    collapse to one pattern), making the O(P²) pairwise pass tractable even
    for partitions with thousands of trips.
    """
    n = len(trips)
    if n <= 1:
        return [trips[:]] if n == 1 else []

    # Group trips by their merged-stop frozenset
    patterns: dict = defaultdict(list)
    for tid in trips:
        patterns[trip_stops[tid]].append(tid)
    pattern_sets = list(patterns.keys())
    pattern_trips = list(patterns.values())
    P = len(pattern_sets)

    if P == 1:
        return [pattern_trips[0]]

    # Union-find on patterns
    parent = list(range(P))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Pairwise intersection. Early-exit at min_shared shared elements.
    for i in range(P):
        si = pattern_sets[i]
        for j in range(i + 1, P):
            if find(i) == find(j):
                continue
            sj = pattern_sets[j]
            small, big = (si, sj) if len(si) <= len(sj) else (sj, si)
            count = 0
            for s in small:
                if s in big:
                    count += 1
                    if count >= min_shared:
                        union(i, j)
                        break

    groups: dict = defaultdict(list)
    for i in range(P):
        groups[find(i)].extend(pattern_trips[i])
    return list(groups.values())


def main():
    print("Loading routes / trips / stops...")
    routes = _m.load_routes()
    merge  = build_stop_merge_map()
    print(f"  {len(routes):,} routes, {len(merge):,} stops")

    # {trip_id: (long_name_norm_or_short, agency_id, bucket, short_name, long_name)}
    print("Loading trips and partition keys...")
    trip_partition: dict = {}
    fallback_count = 0
    with open(GTFS / "trips.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            r = routes.get(row["route_id"])
            if not r:
                continue
            bucket = _m.gtfs_type_to_bucket(r["type"])
            short_name = r["short_name"]
            long_name  = r.get("long_name", "")
            ln_norm = long_name.replace(" ", "").lower()
            sn_key  = short_name.replace(" ", "").lower()
            if ln_norm:
                pkey_str = ln_norm
                used_fallback = False
            else:
                pkey_str = sn_key
                used_fallback = True
                fallback_count += 1
            pkey = (pkey_str, r.get("agency_id", ""), bucket)
            trip_partition[row["trip_id"]] = (pkey, short_name, long_name, used_fallback)
    print(f"  {len(trip_partition):,} trips assigned to partitions "
          f"({fallback_count:,} via empty-long_name → short_name fallback)")

    print("Streaming stop_times.txt (~1–2 min)...")
    wanted = set(trip_partition.keys())
    trip_stops = stream_trip_stops(merge, wanted)
    print(f"  {len(trip_stops):,} trips with stop sequences")
    del merge
    gc.collect()

    # Group trips by partition
    print("Grouping trips by partition...")
    partition_trips: dict = defaultdict(list)
    partition_meta: dict = {}
    for tid, (pkey, sn, ln, fb) in trip_partition.items():
        if tid not in trip_stops:
            continue
        partition_trips[pkey].append(tid)
        if pkey not in partition_meta:
            partition_meta[pkey] = {"short_name": sn, "long_name": ln, "used_fallback": fb}
    print(f"  {len(partition_trips):,} partitions")

    # Run connectivity per partition
    print("Computing connected components (≥2 shared stops)...")
    import time as _t
    per_partition_groups: dict = {}
    group_count_hist: Counter = Counter()
    group_size_hist: Counter = Counter()
    n_part = len(partition_trips)
    items = sorted(partition_trips.items(), key=lambda kv: -len(kv[1]))  # big first → fail fast
    t0 = _t.time()
    for i, (pkey, trips) in enumerate(items):
        t_part = _t.time()
        comps = connected_components(trips, trip_stops, min_shared=2)
        dt = _t.time() - t_part
        if dt > 2.0:
            print(f"    slow partition: {pkey} n_trips={len(trips)} dt={dt:.1f}s n_groups={len(comps)}")
        per_partition_groups[pkey] = comps
        group_count_hist[len(comps)] += 1
        for c in comps:
            group_size_hist[len(c)] += 1
        if i % 5000 == 0 and i > 0:
            print(f"    {i}/{n_part} partitions ({_t.time()-t0:.0f}s)...")

    print(f"  done. {sum(group_count_hist.values()):,} partitions processed "
          f"in {_t.time()-t0:.0f}s.")

    # Empty-long_name fallback collision check
    # A "collision" = partition that used the fallback has the same partition key as
    # another partition that did not (same (short_name, agency_id, bucket)).
    fb_keys = {p for p, m in partition_meta.items() if m["used_fallback"]}
    non_fb_index: dict = defaultdict(list)   # (short_name_norm, agency_id, bucket) → partitions
    for p, m in partition_meta.items():
        if not m["used_fallback"]:
            sn_norm = m["short_name"].replace(" ", "").lower()
            non_fb_index[(sn_norm, p[1], p[2])].append(p)
    collisions = []
    for p in fb_keys:
        sn_norm = partition_meta[p]["short_name"].replace(" ", "").lower()
        coll = non_fb_index.get((sn_norm, p[1], p[2]), [])
        if coll:
            collisions.append({
                "fallback_partition": list(p),
                "fallback_short_name": partition_meta[p]["short_name"],
                "n_fallback_trips": len(partition_trips[p]),
                "colliding_partitions": [list(c) for c in coll],
            })

    # Spot checks: known cases the concept mentions
    spot = {}

    def find_partitions(short_filter=None, long_filter=None, bucket=None, agency=None):
        hits = []
        for p, m in partition_meta.items():
            if bucket and p[2] != bucket: continue
            if agency and p[1] != agency: continue
            if short_filter and short_filter.lower() not in m["short_name"].lower(): continue
            if long_filter and long_filter.lower() not in m["long_name"].lower(): continue
            hits.append((p, m, len(partition_trips[p]), len(per_partition_groups[p])))
        return hits

    spot["S3_train"] = [
        {"partition": list(p), "short_name": m["short_name"], "long_name": m["long_name"],
         "agency": p[1], "n_trips": nt, "n_groups": ng,
         "group_sizes": sorted([len(g) for g in per_partition_groups[p]], reverse=True)}
        for p, m, nt, ng in find_partitions(short_filter="S3", bucket="train")
        if m["short_name"].strip().upper() in ("S3", "S 3")
    ]
    spot["S6_train"] = [
        {"partition": list(p), "short_name": m["short_name"], "long_name": m["long_name"],
         "agency": p[1], "n_trips": nt, "n_groups": ng,
         "group_sizes": sorted([len(g) for g in per_partition_groups[p]], reverse=True)}
        for p, m, nt, ng in find_partitions(short_filter="S6", bucket="train")
        if m["short_name"].strip().upper() in ("S6", "S 6")
    ]
    spot["Forchbahn_18_tram"] = [
        {"partition": list(p), "short_name": m["short_name"], "long_name": m["long_name"],
         "agency": p[1], "n_trips": nt, "n_groups": ng,
         "group_sizes": sorted([len(g) for g in per_partition_groups[p]], reverse=True)}
        for p, m, nt, ng in find_partitions(short_filter="18", bucket="tram")
        if m["short_name"].strip() == "18"
    ]
    spot["Brunig_PE_LIX"] = [
        {"partition": list(p), "short_name": m["short_name"], "long_name": m["long_name"],
         "agency": p[1], "n_trips": nt, "n_groups": ng,
         "group_sizes": sorted([len(g) for g in per_partition_groups[p]], reverse=True)}
        for p, m, nt, ng in find_partitions(long_filter="LIX", bucket="train")
    ]

    # Top many-group partitions for manual inspection
    many_groups = sorted(
        [(p, partition_meta[p], len(partition_trips[p]), per_partition_groups[p])
         for p in partition_trips],
        key=lambda x: -len(x[3])
    )[:30]
    many_groups_out = [
        {"partition": list(p), "short_name": m["short_name"], "long_name": m["long_name"],
         "agency": p[1], "bucket": p[2], "n_trips": nt, "n_groups": len(g),
         "group_sizes": sorted([len(c) for c in g], reverse=True)[:20]}
        for p, m, nt, g in many_groups
    ]

    # Partitions with the most trips (high-traffic lines, to inspect for over-merging)
    big_partitions = sorted(
        [(p, partition_meta[p], len(partition_trips[p]), per_partition_groups[p])
         for p in partition_trips],
        key=lambda x: -x[2]
    )[:30]
    big_partitions_out = [
        {"partition": list(p), "short_name": m["short_name"], "long_name": m["long_name"],
         "agency": p[1], "bucket": p[2], "n_trips": nt, "n_groups": len(g),
         "group_sizes": sorted([len(c) for c in g], reverse=True)[:20]}
        for p, m, nt, g in big_partitions
    ]

    summary = {
        "totals": {
            "partitions": len(partition_trips),
            "trips":      sum(len(v) for v in partition_trips.values()),
            "fallback_trips":         fallback_count,
            "fallback_partitions":    len(fb_keys),
            "fallback_collisions":    len(collisions),
        },
        "group_count_hist": dict(sorted(group_count_hist.items())),
        "group_size_hist":  dict(sorted(group_size_hist.items())),
        "by_bucket": {},
        "spot_checks": spot,
        "fallback_collisions": collisions[:50],
        "top_many_groups": many_groups_out,
        "top_big_partitions": big_partitions_out,
    }

    # Per-bucket breakdown
    by_bucket: dict = defaultdict(lambda: {"partitions": 0, "trips": 0, "groups": 0,
                                            "multi_group_partitions": 0})
    for p, comps in per_partition_groups.items():
        b = p[2]
        by_bucket[b]["partitions"] += 1
        by_bucket[b]["trips"]      += len(partition_trips[p])
        by_bucket[b]["groups"]     += len(comps)
        if len(comps) > 1:
            by_bucket[b]["multi_group_partitions"] += 1
    summary["by_bucket"] = dict(by_bucket)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    import json
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nResults → {OUT}")

    # Console summary
    print("\n── Totals ──")
    for k, v in summary["totals"].items():
        print(f"  {k:30} {v:,}")

    print("\n── Group count distribution (partitions per N groups) ──")
    for n, c in summary["group_count_hist"].items():
        bar = "█" * min(60, c * 60 // max(summary["group_count_hist"].values()))
        print(f"  {n:>3} groups  {bar} {c:,}")

    print("\n── Group size distribution (groups per trip-count bucket) ──")
    size_buckets = [(1, 1), (2, 2), (3, 5), (6, 10), (11, 25), (26, 50),
                    (51, 100), (101, 500), (501, 10**9)]
    for lo, hi in size_buckets:
        c = sum(v for s, v in summary["group_size_hist"].items() if lo <= s <= hi)
        label = f"{lo}" if lo == hi else (f"{lo}–{hi}" if hi < 10**8 else f"{lo}+")
        print(f"  {label:>8} trips  {c:>6,}")

    print("\n── By bucket ──")
    for b, d in sorted(by_bucket.items()):
        print(f"  {b:<14} {d['partitions']:>6,} part  {d['trips']:>8,} trips  "
              f"{d['groups']:>6,} groups  {d['multi_group_partitions']:>5,} multi")

    print(f"\n── Empty-long_name fallback ──")
    print(f"  partitions using fallback: {len(fb_keys):,}")
    print(f"  collisions with non-fallback partitions: {len(collisions):,}")
    if collisions:
        print("  First 5:")
        for c in collisions[:5]:
            print(f"    {c['fallback_short_name']!r:>10} (n={c['n_fallback_trips']}) "
                  f"↔ {len(c['colliding_partitions'])} non-fb partition(s)")

    print("\n── Spot checks ──")
    for name, hits in spot.items():
        print(f"  {name}: {len(hits)} partition(s)")
        for h in hits:
            print(f"    sn={h['short_name']!r:<8} ln={h['long_name']!r:<20} "
                  f"agency={h['agency']!r:<10} trips={h['n_trips']:>4} "
                  f"groups={h['n_groups']} sizes={h['group_sizes'][:10]}")

    print("\n── Top 10 partitions by group count ──")
    for h in many_groups_out[:10]:
        print(f"  sn={h['short_name']!r:<10} ln={h['long_name']!r:<25} "
              f"bucket={h['bucket']:<10} agency={h['agency']!r:<10} "
              f"trips={h['n_trips']:>4} groups={h['n_groups']:>3} "
              f"sizes={h['group_sizes'][:8]}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
