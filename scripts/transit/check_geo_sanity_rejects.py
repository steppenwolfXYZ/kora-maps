#!/usr/bin/env python3
"""
Diagnostic: list OSM transit lines excluded by the geo-fallback sanity check.

Compares old geo-fallback behaviour (highest bbox-score wins, no sanity check)
to new behaviour (_passes_geo_sanity required).  Prints every line where the
old logic would have assigned stops but the new logic rejected all candidates,
along with per-check diagnostics so thresholds can be tuned.

Note: loads all GTFS data — takes ~1-2 min (same as 05_score_and_match.py).

Usage:
    python3 scripts/transit/check_geo_sanity_rejects.py [--mode MODE] [--json FILE]

Options:
    --mode MODE    Only check lines of this mode (train, bus, tram, …).
    --json FILE    Also save full results as JSON (default: no file output).
"""

import sys
import json
import re
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

# Import helpers from 05_score_and_match without running main()
import importlib.util
_spec = importlib.util.spec_from_file_location("s05", HERE / "05_score_and_match.py")
_m    = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)

MODE_TO_BUCKET = {
    "train": "train", "tram": "tram", "metro": "metro",
    "bus": "bus", "regional_bus": "bus",
    "ferry": "ferry", "mountain": "mountain",
}


def _diagnose_candidate(osm_pts, ccoords, stop_meta, osm_from, osm_to) -> list:
    """Return a list of per-check result strings for the given candidate."""
    lines = []

    # Check 1 – terminal name matching (≥1/3 of stops, min 2)
    norm_from = _m._norm_stop_name(osm_from)
    norm_to   = _m._norm_stop_name(osm_to)
    if (norm_from and len(norm_from) >= 4) or (norm_to and len(norm_to) >= 4):
        threshold = max(2, len(ccoords) // 3)
        matches = 0
        matched_names = []
        for s in ccoords:
            sid   = s[2] if len(s) > 2 else None
            sname = _m._norm_stop_name(stop_meta.get(sid, ("", ""))[0]) if sid else ""
            if norm_from and len(norm_from) >= 4 and norm_from in sname:
                matches += 1; matched_names.append(sname)
            elif norm_to and len(norm_to) >= 4 and norm_to in sname:
                matches += 1; matched_names.append(sname)
        ok1 = matches >= threshold
        lines.append(
            f"  check1 names      {'PASS' if ok1 else 'FAIL'}: "
            f"from='{norm_from}' to='{norm_to}' "
            f"{matches}/{len(ccoords)} stops matched (need {threshold})"
            + (f" → {matched_names[:3]}" if matched_names else "")
        )
    else:
        lines.append(
            f"  check1 names      SKIP: from/to too short or empty "
            f"('{norm_from}' / '{norm_to}')"
        )

    # Check 2 – endpoint coverage
    start, end = osm_pts[0], osm_pts[-1]
    d_start = min(_m.haversine_km(s[0], s[1], start[0], start[1]) for s in ccoords)
    d_end   = min(_m.haversine_km(s[0], s[1], end[0],   end[1])   for s in ccoords)
    ok2 = d_start <= _m.ENDPOINT_THRESHOLD_KM and d_end <= _m.ENDPOINT_THRESHOLD_KM
    lines.append(
        f"  check2 endpoints  {'PASS' if ok2 else 'FAIL'}: "
        f"nearest-to-start={d_start:.1f} km, nearest-to-end={d_end:.1f} km "
        f"(threshold {_m.ENDPOINT_THRESHOLD_KM} km)"
    )

    # Check 3 – sampled proximity
    step    = max(1, len(ccoords) // 5)
    sampled = ccoords[::step][:5]
    dists   = [_m._min_dist_to_polyline_km(s[0], s[1], osm_pts) for s in sampled]
    close   = sum(1 for d in dists if d <= 0.5)
    ok3     = close * 2 >= len(sampled)
    lines.append(
        f"  check3 proximity  {'PASS' if ok3 else 'FAIL'}: "
        f"{close}/{len(sampled)} stops ≤500 m from OSM line "
        f"[{', '.join(f'{d:.2f}' for d in dists)} km]"
    )

    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default=None,
                    help="Filter by mode (train, bus, tram, …)")
    ap.add_argument("--json", default=None, metavar="FILE",
                    help="Save full results as JSON to this path")
    args = ap.parse_args()

    # ── Load GTFS data (mirrors 05_score_and_match.main()) ───────────────────
    print("Loading GTFS data (this takes ~1-2 min)...")
    stop_coords      = _m.load_stops()
    stop_meta        = _m.load_stop_meta()
    svc_dates        = _m.load_calendar_dates()
    route_lookup     = _m.load_routes()
    trip_lookup      = _m.load_trips(route_lookup)
    trip_frequencies = _m.load_frequencies()
    line_freq, line_speed, line_canonical = _m.stream_stop_times(
        trip_lookup, stop_coords, svc_dates, trip_frequencies
    )
    for lk in line_canonical:
        _ = line_freq[lk]
    gtfs_index, gtfs_long_index = _m.build_gtfs_index(line_freq, line_speed)
    print(f"  {len(gtfs_index):,} GTFS entries loaded")

    # ── Load output features from transit_lines.geojson ──────────────────────
    transit_out = ROOT / "data" / "transit" / "transit_lines.geojson"
    if not transit_out.exists():
        print(f"ERROR: {transit_out} not found — run 05_score_and_match.py first.")
        sys.exit(1)
    features = json.loads(transit_out.read_text())["features"]
    print(f"  {len(features):,} output features to check")
    if args.mode:
        features = [f for f in features if f["properties"]["mode"] == args.mode]
        print(f"  {len(features):,} after --mode={args.mode} filter")

    # ── Load from/to tags from raw OSM routes (transit_lines.geojson lacks them) ──
    osm_routes_path = ROOT / "data" / "osm" / "routes.geojson"
    osm_from_to: dict = {}
    if osm_routes_path.exists():
        for rfeat in json.loads(osm_routes_path.read_text())["features"]:
            oid = str(rfeat["properties"].get("osm_id", ""))
            if oid:
                osm_from_to[oid] = (
                    rfeat["properties"].get("from", ""),
                    rfeat["properties"].get("to", ""),
                )
        print(f"  {len(osm_from_to):,} from/to values loaded from routes.geojson")

    # ── Run the stop-assignment loop in diagnostic mode ───────────────────────
    excluded = []   # lines rejected by sanity check
    kept     = []   # lines accepted by sanity check
    n_skip   = 0    # lines that never reached the geo fallback

    for feat in features:
        props    = feat["properties"]
        mode     = props["mode"]
        ref      = props["ref"]
        osm_id   = str(props["osm_id"])
        osm_name = props.get("name", "")
        osm_from, osm_to = osm_from_to.get(osm_id, ("", ""))
        bucket   = MODE_TO_BUCKET.get(mode, "bus")
        ref_norm = ref.replace(" ", "")

        # Mirror the GTFS lookup cascade from 05_score_and_match.py
        gtfs = gtfs_index.get((bucket, ref))
        matched_gtfs_ref = ref if gtfs else None
        if gtfs is None:
            for k_ref in [ref_norm, ref.upper(), ref.lower(), ref_norm.upper()]:
                gtfs = gtfs_index.get((bucket, k_ref))
                if gtfs:
                    matched_gtfs_ref = k_ref; break
        if gtfs is None:
            for lk in [(bucket, ref_norm), (bucket, ref_norm.upper())]:
                gtfs = gtfs_long_index.get(lk)
                if gtfs:
                    matched_gtfs_ref = ref_norm; break
        if gtfs is None:
            for token in osm_name.split(":")[0].strip().split():
                if token != ref and len(token) <= 6:
                    gtfs = (gtfs_index.get((bucket, token)) or
                            gtfs_index.get((bucket, token.upper())))
                    if gtfs:
                        matched_gtfs_ref = token; break
        if gtfs is None:
            m_alpha = re.match(r'^([A-Za-z ]+)\d', ref)
            if m_alpha:
                alpha = m_alpha.group(1).strip()
                if alpha and alpha != ref:
                    gtfs = (gtfs_index.get((bucket, alpha)) or
                            gtfs_index.get((bucket, alpha.upper())))
                    if gtfs:
                        matched_gtfs_ref = alpha

        if gtfs is None and mode not in ("ferry", "mountain"):
            n_skip += 1; continue

        # Extract OSM geometry
        geom = feat["geometry"]
        osm_pts = ([c for seg in geom["coordinates"] for c in seg]
                   if geom["type"] == "MultiLineString" else geom["coordinates"])
        if not osm_pts:
            n_skip += 1; continue

        sub_bboxes  = _m.build_sub_bboxes(osm_pts)
        osm_start   = osm_pts[0]
        osm_end     = osm_pts[-1]
        osm_span_km = _m.haversine_km(osm_start[0], osm_start[1], osm_end[0], osm_end[1])

        # Use the shared canonical-lookup function (same code as main pipeline).
        # This includes used_name_fallback tracking and Trigger 1 sanity check.
        best_coords, _used_fallback = _m._lookup_canonical_stops(
            ref, ref_norm, matched_gtfs_ref, bucket,
            osm_pts, osm_span_km, osm_from, osm_to,
            stop_coords, stop_meta, sub_bboxes,
        )

        # Did this line reach the geo fallback?
        if best_coords and _m._covers_endpoints(osm_pts, best_coords):
            n_skip += 1; continue   # direct match, no geo fallback fired

        if mode == "ferry":
            n_skip += 1; continue   # ferry geo fallback pooling is different

        search_buckets = {bucket}
        if bucket == "mountain":
            search_buckets.add("train")

        # Collect all geo candidates (same scoring as 05_score_and_match.py)
        geo_candidates = []
        for (lk_ref, lk_bucket), lk_candidates in _m._line_canonical_export.items():
            if lk_bucket not in search_buckets:
                continue
            for (_, cand, _da) in lk_candidates:
                if not cand:
                    continue
                ccoords = []
                for stop_id, _a, _d in cand:
                    c = (stop_coords.get(stop_id) or
                         stop_coords.get(stop_id.split(":")[0]))
                    if c and any(_m.stop_near_bbox(c[0], c[1], sb) for sb in sub_bboxes):
                        ccoords.append([c[0], c[1], stop_id])
                if len(ccoords) < 2:
                    continue
                score = len(ccoords) / len(cand)
                geo_candidates.append((score, ccoords))

        if not geo_candidates:
            n_skip += 1; continue   # no candidates at all — not a filter issue

        geo_candidates.sort(key=lambda x: -x[0])

        # Old logic: highest scorer wins
        old_best = geo_candidates[0][1]

        # New logic: first sanity-passing candidate
        new_best = []
        for _score, _ccoords in geo_candidates[:20]:
            if _m._passes_geo_sanity(osm_pts, _ccoords, stop_meta, osm_from, osm_to):
                new_best = _ccoords; break

        entry = {
            "ref": ref, "name": osm_name, "mode": mode, "osm_id": osm_id,
            "from": osm_from, "to": osm_to,
            "n_candidates": len(geo_candidates),
            "best_score": round(geo_candidates[0][0], 3),
            "best_n_stops": len(old_best),
            "new_n_stops": len(new_best),
        }

        if old_best and not new_best:
            entry["diagnose"] = _diagnose_candidate(
                osm_pts, old_best, stop_meta, osm_from, osm_to
            )
            excluded.append(entry)
        else:
            kept.append(entry)

    # ── Report ────────────────────────────────────────────────────────────────
    total_geo = len(excluded) + len(kept)
    print(f"\n{'='*70}")
    print(f"GEO SANITY FILTER DIAGNOSTIC")
    print(f"{'='*70}")
    print(f"Lines that reached geo fallback : {total_geo}")
    print(f"  KEPT  (sanity passed)         : {len(kept)}")
    print(f"  EXCLUDED (all checks failed)  : {len(excluded)}")
    print(f"  Skipped (direct match / ferry / no candidates): {n_skip}")

    if excluded:
        print(f"\n{'─'*70}")
        print(f"EXCLUDED LINES — detail:")
        print(f"{'─'*70}")
        for r in sorted(excluded, key=lambda x: (x["mode"], x["ref"])):
            print(f"\n[{r['mode']:12}] ref={r['ref']!r:10}  {r['name']}")
            print(f"  osm_id     : {r['osm_id']}")
            print(f"  from → to  : {r['from']} → {r['to']}")
            print(f"  candidates : {r['n_candidates']}  best score={r['best_score']:.2f}  "
                  f"best had {r['best_n_stops']} stops")
            for line in r["diagnose"]:
                print(line)
    else:
        print("\nNo lines excluded — sanity filter accepted all geo-fallback candidates.")

    if args.json:
        out = {
            "excluded": excluded,
            "kept": kept,
            "n_skip": n_skip,
        }
        Path(args.json).write_text(json.dumps(out, indent=2, ensure_ascii=False))
        print(f"\nFull results saved to {args.json}")


if __name__ == "__main__":
    main()
