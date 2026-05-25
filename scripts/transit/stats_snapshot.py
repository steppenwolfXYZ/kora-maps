#!/usr/bin/env python3
"""
Transit pipeline statistics snapshot.
Run before and after architecture changes to detect regressions.

Usage:
    python3 scripts/transit/stats_snapshot.py
    python3 scripts/transit/stats_snapshot.py --json   # machine-readable output
"""

import json
import sys
import statistics
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path(__file__).parent.parent.parent / "data" / "transit"

ARGS = set(sys.argv[1:])
AS_JSON = "--json" in ARGS


def load(name):
    p = DATA / name
    with open(p) as f:
        return json.load(f)


def geojson_features(name):
    return load(name)["features"]


# ── helpers ──────────────────────────────────────────────────────────────────

def pct(n, total):
    return f"{n/total*100:.1f}%" if total else "n/a"


def dist_stats(values):
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min":   min(values),
        "max":   max(values),
        "mean":  round(statistics.mean(values), 2),
        "median": round(statistics.median(values), 2),
    }


def print_section(title):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def print_table(rows, headers=None):
    if not rows:
        return
    if headers:
        rows = [headers] + rows
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


# ── section collectors ────────────────────────────────────────────────────────

def stats_lines():
    feats = geojson_features("transit_lines.geojson")
    total = len(feats)
    by_mode = Counter(f["properties"]["mode"] for f in feats)
    gtfs_matched = sum(1 for f in feats if f["properties"].get("gtfs_matched"))
    no_match = total - gtfs_matched

    freq_by_mode = defaultdict(list)
    speed_by_mode = defaultdict(list)
    km_by_mode = defaultdict(list)
    for f in feats:
        p = f["properties"]
        m = p["mode"]
        if p.get("freq_score") is not None:
            freq_by_mode[m].append(p["freq_score"])
        if p.get("speed_kmh") is not None:
            speed_by_mode[m].append(p["speed_kmh"])
        if p.get("line_km") is not None:
            km_by_mode[m].append(p["line_km"])

    return {
        "total": total,
        "gtfs_matched": gtfs_matched,
        "no_gtfs_match": no_match,
        "by_mode": dict(by_mode),
        "freq_by_mode": {m: dist_stats(v) for m, v in freq_by_mode.items()},
        "speed_by_mode": {m: dist_stats(v) for m, v in speed_by_mode.items()},
        "km_by_mode": {m: dist_stats(v) for m, v in km_by_mode.items()},
    }


def stats_line_stops():
    d = load("line_stops.json")
    stop_counts = [len(v.get("stops", [])) for v in d.values()]
    empty = sum(1 for c in stop_counts if c == 0)
    return {
        "total_osm_ids": len(d),
        "with_stops": len(d) - empty,
        "without_stops": empty,
        "stop_count_dist": dist_stats(stop_counts),
    }


def stats_pills():
    feats = geojson_features("transit_stop_pills.geojson")
    total = len(feats)
    by_type = Counter(f["properties"].get("feature_type") for f in feats)
    pills = [f for f in feats if f["properties"].get("feature_type") == "pill"]
    by_mode = Counter(f["properties"].get("mode") for f in pills)
    stop_counts = [f["properties"].get("stop_count", 1) for f in pills]
    unique_stop_ids = len({f["properties"].get("stop_id") for f in pills})
    connector_lens = []
    for f in feats:
        if f["properties"].get("feature_type") == "connector":
            coords = f["geometry"]["coordinates"]
            if len(coords) == 2:
                import math
                dx = (coords[1][0] - coords[0][0]) * 111_320 * math.cos(math.radians(coords[0][1]))
                dy = (coords[1][1] - coords[0][1]) * 110_540
                connector_lens.append(math.sqrt(dx*dx + dy*dy))
    return {
        "total_features": total,
        "by_feature_type": dict(by_type),
        "pills_by_mode": dict(by_mode),
        "unique_pill_stop_ids": unique_stop_ids,
        "lines_per_pill_dist": dist_stats(stop_counts),
        "connector_length_m_dist": dist_stats([round(v, 1) for v in connector_lens]),
    }


def stats_dots():
    feats = geojson_features("transit_stops.geojson")
    total = len(feats)
    by_mode = Counter(f["properties"].get("mode") for f in feats)
    unique_stop_ids = len({f["properties"].get("stop_id") for f in feats})
    return {
        "total": total,
        "by_mode": dict(by_mode),
        "unique_stop_ids": unique_stop_ids,
    }


def stats_excluded():
    d = load("sanity_excluded.json")
    by_mode = Counter(e.get("mode") for e in d)
    return {
        "total": len(d),
        "by_mode": dict(by_mode),
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    results = {
        "lines":       stats_lines(),
        "line_stops":  stats_line_stops(),
        "pills":       stats_pills(),
        "dots":        stats_dots(),
        "excluded":    stats_excluded(),
    }

    if AS_JSON:
        print(json.dumps(results, indent=2))
        return

    # ── Lines ────────────────────────────────────────────────────────────────
    L = results["lines"]
    print_section("LINES  (transit_lines.geojson)")
    print(f"  Total lines:     {L['total']}")
    print(f"  GTFS matched:    {L['gtfs_matched']}  ({pct(L['gtfs_matched'], L['total'])})")
    print(f"  No GTFS match:   {L['no_gtfs_match']}  ({pct(L['no_gtfs_match'], L['total'])})")
    print()
    rows = []
    for mode in sorted(L["by_mode"]):
        n = L["by_mode"][mode]
        freq = L["freq_by_mode"].get(mode, {})
        spd = L["speed_by_mode"].get(mode, {})
        km = L["km_by_mode"].get(mode, {})
        rows.append([
            mode,
            str(n),
            f"{freq.get('mean','–')} (med {freq.get('median','–')})" if freq.get("count") else "–",
            f"{spd.get('mean','–')} (med {spd.get('median','–')})" if spd.get("count") else "–",
            f"{km.get('mean','–')} (med {km.get('median','–')})" if km.get("count") else "–",
        ])
    print_table(rows, ["mode", "count", "freq_score (mean/med)", "speed_kmh (mean/med)", "line_km (mean/med)"])

    # ── Line stops ───────────────────────────────────────────────────────────
    LS = results["line_stops"]
    print_section("LINE STOPS  (line_stops.json)")
    print(f"  OSM IDs total:     {LS['total_osm_ids']}")
    print(f"  With stops:        {LS['with_stops']}  ({pct(LS['with_stops'], LS['total_osm_ids'])})")
    print(f"  Without stops:     {LS['without_stops']}  ({pct(LS['without_stops'], LS['total_osm_ids'])})")
    d = LS["stop_count_dist"]
    print(f"  Stop count:        min={d.get('min')}  mean={d.get('mean')}  median={d.get('median')}  max={d.get('max')}")

    # ── Pills ────────────────────────────────────────────────────────────────
    P = results["pills"]
    print_section("STOP PILLS  (transit_stop_pills.geojson)")
    print(f"  Total features:    {P['total_features']}")
    for ft, n in sorted(P["by_feature_type"].items()):
        print(f"    {ft:<12} {n}")
    print(f"  Unique stop IDs (pills): {P['unique_pill_stop_ids']}")
    d = P["lines_per_pill_dist"]
    print(f"  Lines/pill:        min={d.get('min')}  mean={d.get('mean')}  median={d.get('median')}  max={d.get('max')}")
    d = P["connector_length_m_dist"]
    print(f"  Connector len (m): min={d.get('min')}  mean={d.get('mean')}  median={d.get('median')}  max={d.get('max')}")
    print()
    rows = [[mode, str(n)] for mode, n in sorted(P["pills_by_mode"].items())]
    print_table(rows, ["mode", "pill count"])

    # ── Dots ─────────────────────────────────────────────────────────────────
    D = results["dots"]
    print_section("STOP DOTS  (transit_stops.geojson)")
    print(f"  Total dots:        {D['total']}")
    print(f"  Unique stop IDs:   {D['unique_stop_ids']}")
    print()
    rows = [[mode, str(n)] for mode, n in sorted(D["by_mode"].items())]
    print_table(rows, ["mode", "dot count"])

    # ── Excluded ─────────────────────────────────────────────────────────────
    E = results["excluded"]
    print_section("SANITY EXCLUDED  (sanity_excluded.json)")
    print(f"  Total excluded:    {E['total']}")
    print()
    rows = [[mode, str(n)] for mode, n in sorted(E["by_mode"].items())]
    print_table(rows, ["mode", "excluded count"])

    print()


if __name__ == "__main__":
    main()
