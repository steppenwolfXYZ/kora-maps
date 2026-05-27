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

    # Check 2 – GTFS stops → OSM geometry (5 evenly-spaced GTFS stops, 3/5 within 200 m)
    step2        = max(1, len(ccoords) // 5)
    sampled_gtfs = ccoords[::step2][:5]
    dists2       = [_m._min_dist_to_polyline_km(s[0], s[1], osm_pts) for s in sampled_gtfs]
    close2       = sum(1 for d in dists2 if d <= 0.2)
    ok2          = close2 * 5 >= len(sampled_gtfs) * 3
    lines.append(
        f"  check2 gtfs→osm   {'PASS' if ok2 else 'FAIL'}: "
        f"{close2}/{len(sampled_gtfs)} GTFS stops ≤200 m from OSM line "
        f"[{', '.join(f'{d:.2f}' for d in dists2)} km]"
    )

    # Check 3 – OSM geometry → GTFS stops (5 evenly-spaced OSM points, 3/5 within 200 m)
    step3       = max(1, len(osm_pts) // 5)
    sampled_osm = osm_pts[::step3][:5]
    dists3      = [
        min(_m.haversine_km(p[0], p[1], s[0], s[1]) for s in ccoords)
        for p in sampled_osm
    ]
    close3 = sum(1 for d in dists3 if d <= 0.2)
    ok3    = close3 * 5 >= len(sampled_osm) * 3
    lines.append(
        f"  check3 osm→gtfs   {'PASS' if ok3 else 'FAIL'}: "
        f"{close3}/{len(sampled_osm)} OSM points ≤200 m from GTFS stop "
        f"[{', '.join(f'{d:.2f}' for d in dists3)} km]"
    )

    return lines


def _collect_geo_candidates(osm_pts, bucket, stop_coords):
    """Collect and score all geo-fallback GTFS candidates for an OSM route."""
    sub_bboxes = _m.build_sub_bboxes(osm_pts)
    search_buckets = {bucket}
    if bucket == "mountain":
        search_buckets.add("train")
    geo_candidates = []
    for (lk_ref, lk_bucket), lk_candidates in _m._line_canonical_export.items():
        if lk_bucket not in search_buckets:
            continue
        for (_, cand, _da, _aid) in lk_candidates:
            if not cand:
                continue
            ccoords = []
            for stop_id, _a, _d in cand:
                c = stop_coords.get(stop_id) or stop_coords.get(stop_id.split(":")[0])
                if c and any(_m.stop_near_bbox(c[0], c[1], sb) for sb in sub_bboxes):
                    ccoords.append([c[0], c[1], stop_id])
            if len(ccoords) < 2:
                continue
            score = len(ccoords) / len(cand)
            if score < 0.5:
                continue
            geo_candidates.append((score, ccoords))
    geo_candidates.sort(
        key=lambda x: (-x[0], -_m._count_endpoints_covered(osm_pts, x[1]), -len(x[1]))
    )
    return geo_candidates


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default=None,
                    help="Filter by mode (train, bus, tram, …)")
    ap.add_argument("--json", default=None, metavar="FILE",
                    help="Save full results as JSON to this path")
    args = ap.parse_args()

    # ── Load GTFS data ────────────────────────────────────────────────────────
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

    # ── Load OSM routes for geometry + from/to lookup ─────────────────────────
    osm_routes_path = ROOT / "data" / "osm" / "routes.geojson"
    osm_route_by_id: dict = {}
    if osm_routes_path.exists():
        for rfeat in json.loads(osm_routes_path.read_text())["features"]:
            oid = str(rfeat["properties"].get("osm_id", ""))
            if oid:
                osm_route_by_id[oid] = rfeat
        print(f"  {len(osm_route_by_id):,} OSM routes loaded")

    # ── EXCLUDED: read from sidecar written by 05_score_and_match.py ──────────
    excluded_path = ROOT / "data" / "transit" / "sanity_excluded.json"
    if not excluded_path.exists():
        print(f"ERROR: {excluded_path} not found — run rebuild first.")
        sys.exit(1)
    excluded_raw = json.loads(excluded_path.read_text())
    if args.mode:
        excluded_raw = [e for e in excluded_raw if e["mode"] == args.mode]
    print(f"  {len(excluded_raw):,} excluded entries in sidecar")

    excluded = []
    for entry in excluded_raw:
        osm_id   = str(entry["osm_id"])
        ref      = entry["ref"]
        mode     = entry["mode"]
        osm_name = entry.get("name", "")
        bucket   = MODE_TO_BUCKET.get(mode, "bus")

        osm_feat = osm_route_by_id.get(osm_id)
        if not osm_feat:
            excluded.append({
                "ref": ref, "name": osm_name, "mode": mode, "osm_id": osm_id,
                "from": "", "to": "",
                "n_candidates": 0, "best_score": 0.0, "best_n_stops": 0,
                "diagnose": ["  (OSM geometry not found in routes.geojson)"],
            })
            continue

        osm_from = osm_feat["properties"].get("from", "")
        osm_to   = osm_feat["properties"].get("to", "")
        geom     = osm_feat["geometry"]
        osm_pts  = ([c for seg in geom["coordinates"] for c in seg]
                    if geom["type"] == "MultiLineString" else geom["coordinates"])
        if not osm_pts:
            continue

        geo_candidates = _collect_geo_candidates(osm_pts, bucket, stop_coords)
        if not geo_candidates:
            excluded.append({
                "ref": ref, "name": osm_name, "mode": mode, "osm_id": osm_id,
                "from": osm_from, "to": osm_to,
                "n_candidates": 0, "best_score": 0.0, "best_n_stops": 0,
                "diagnose": ["  (no geo candidates — cannot diagnose checks)"],
            })
            continue

        old_best = geo_candidates[0][1]
        excluded.append({
            "ref": ref, "name": osm_name, "mode": mode, "osm_id": osm_id,
            "from": osm_from, "to": osm_to,
            "n_candidates": len(geo_candidates),
            "best_score": round(geo_candidates[0][0], 3),
            "best_n_stops": len(old_best),
            "diagnose": _diagnose_candidate(osm_pts, old_best, stop_meta, osm_from, osm_to),
        })

    # ── KEPT: lines in transit_lines.geojson that went through geo fallback ───
    kept = []
    n_skip = 0
    transit_out = ROOT / "data" / "transit" / "transit_lines.geojson"
    if transit_out.exists():
        features = json.loads(transit_out.read_text())["features"]
        if args.mode:
            features = [f for f in features if f["properties"]["mode"] == args.mode]

        osm_from_to = {oid: (rf["properties"].get("from", ""), rf["properties"].get("to", ""))
                       for oid, rf in osm_route_by_id.items()}

        for feat in features:
            props    = feat["properties"]
            mode     = props["mode"]
            ref      = props["ref"]
            osm_id   = str(props["osm_id"])
            osm_name = props.get("name", "")
            osm_from, osm_to = osm_from_to.get(osm_id, ("", ""))
            bucket   = MODE_TO_BUCKET.get(mode, "bus")
            ref_norm = ref.replace(" ", "")

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
            if mode == "ferry":
                n_skip += 1; continue

            geom = feat["geometry"]
            osm_pts = ([c for seg in geom["coordinates"] for c in seg]
                       if geom["type"] == "MultiLineString" else geom["coordinates"])
            if not osm_pts:
                n_skip += 1; continue

            sub_bboxes  = _m.build_sub_bboxes(osm_pts)
            osm_span_km = _m.haversine_km(osm_pts[0][0], osm_pts[0][1],
                                           osm_pts[-1][0], osm_pts[-1][1])

            best_coords, _, _lkf = _m._lookup_canonical_stops(
                ref, ref_norm, matched_gtfs_ref, bucket,
                osm_pts, osm_span_km, osm_from, osm_to,
                stop_coords, stop_meta, sub_bboxes,
            )

            if best_coords and _m._covers_endpoints(osm_pts, best_coords):
                n_skip += 1; continue   # direct match, didn't need geo fallback

            geo_candidates = _collect_geo_candidates(osm_pts, bucket, stop_coords)
            if not geo_candidates:
                n_skip += 1; continue

            kept.append({
                "ref": ref, "name": osm_name, "mode": mode, "osm_id": osm_id,
                "from": osm_from, "to": osm_to,
                "n_candidates": len(geo_candidates),
                "best_score": round(geo_candidates[0][0], 3),
                "best_n_stops": len(geo_candidates[0][1]),
            })

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
            if r["n_candidates"]:
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
