#!/usr/bin/env python3
"""
Diagnose why an OSM transit route is missing from the map.

Fast mode (default):  reads sidecar JSON files from the last rebuild — instant.
Deep mode (--deep):   loads full GTFS data and re-runs the 4-loop stop
                      assignment for this route, showing per-loop and per-candidate
                      failure reasons.  Takes ~2 min to load.

Usage:
    python3 scripts/transit/diagnose_transit_line.py <osm_id>
    python3 scripts/transit/diagnose_transit_line.py <osm_id> --deep
"""

import json
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
DATA = ROOT / "data" / "transit"
OSM_ROUTES_PATH = ROOT / "data" / "osm" / "routes.geojson"

MODE_TO_BUCKET = {
    "train": "train",
    "tram": "tram", "metro": "metro",
    "bus": "bus", "regional_bus": "bus",
    "ferry": "ferry", "mountain": "mountain",
}


def _load(path):
    return json.loads(path.read_text()) if path.exists() else None


def _hr(c="─", w=70):
    return c * w


# ── Sanity check replication (mirrors _passes_geo_sanity exactly) ─────────────

def _run_sanity_checks(osm_pts, ccoords, stop_meta, osm_stop_nodes, osm_line_km,
                        full_density, skip_upper, _m):
    """
    Run each sanity check and return (check_name, result, detail) triples.
    result is True (pass), False (fail), or None (skipped).
    Mirrors _passes_geo_sanity from 05_score_and_match.py.
    """
    out = []

    # Check 1 — OSM stop names vs GTFS stop names
    gtfs_names = set()
    for s in ccoords:
        sid = s[2] if len(s) > 2 else None
        if sid:
            sname = _m._norm_stop_name(stop_meta.get(sid, ("", ""))[0])
            if sname and len(sname) >= 2:
                gtfs_names.add(sname)
    threshold = max(2, round(len(osm_stop_nodes) * 0.9))
    matches, matched = 0, []
    for node in osm_stop_nodes:
        n = _m._norm_stop_name(node[2] if len(node) > 2 else "")
        if n and len(n) >= 2 and n in gtfs_names:
            matches += 1
            matched.append(n)
    ok1 = matches >= threshold
    out.append((
        "check1 names",
        ok1,
        f"{matches}/{len(osm_stop_nodes)} OSM stop names in GTFS set "
        f"(need {threshold})"
        + (f"  matched: {matched[:4]}" if matched else ""),
    ))

    # Check 2 — density gate + GTFS stops → OSM geometry
    density_ok = True
    ratio = None
    if len(osm_stop_nodes) >= 2 and osm_line_km > 0:
        osm_density = len(osm_stop_nodes) / osm_line_km
        if full_density > 0 and osm_density > 0:
            ratio = full_density / osm_density
        elif osm_density > 0:
            cand_span = sum(
                _m.haversine_km(ccoords[i][0], ccoords[i][1],
                                ccoords[i+1][0], ccoords[i+1][1])
                for i in range(len(ccoords) - 1)
            )
            ratio = (len(ccoords) / cand_span) / osm_density if cand_span > 0 else None
        if ratio is not None:
            density_ok = (ratio >= 0.5) if skip_upper else (0.5 <= ratio <= 2.0)
            density_detail = (
                f"ratio={ratio:.2f} (cand={full_density:.3f} / osm={osm_density:.3f} stops/km)"
                + ("  [upper bound not applied for regional_bus]" if skip_upper else "")
            )
            out.append(("check2 density", density_ok, density_detail))
        else:
            density_ok = True
            out.append(("check2 density", None, "skipped (no density data)"))
    else:
        density_ok = True
        out.append(("check2 density", None,
                     f"skipped (line_km={osm_line_km:.1f}, stop_nodes={len(osm_stop_nodes)})"))

    if density_ok:
        _k2 = min(5, len(ccoords))
        sampled = [ccoords[round(i * (len(ccoords) - 1) / max(1, _k2 - 1))]
                   for i in range(_k2)]
        dists = [_m._min_dist_to_polyline_km(s[0], s[1], osm_pts) for s in sampled]
        close = sum(1 for d in dists if d <= 0.1)
        ok2 = close * 5 >= len(sampled) * 4
        out.append((
            "check2 prox",
            ok2,
            f"{close}/{len(sampled)} GTFS stops ≤100 m from OSM line (need 4/5)  "
            f"[{', '.join(f'{d*1000:.0f} m' for d in dists)}]",
        ))
    else:
        out.append(("check2 prox", None, "skipped (density gate failed)"))

    # Check 3 — OSM stops → GTFS stops
    if len(osm_stop_nodes) >= 2:
        _k3 = min(6, len(osm_stop_nodes))
        sampled_osm = [osm_stop_nodes[round(i * (len(osm_stop_nodes) - 1) / max(1, _k3 - 1))]
                       for i in range(_k3)]
        dists3 = [
            min(_m.haversine_km(p[0], p[1], s[0], s[1]) for s in ccoords)
            for p in sampled_osm
        ]
        close3 = sum(1 for d in dists3 if d <= 0.2)
        ok3 = close3 * 6 >= len(sampled_osm) * 5
        out.append((
            "check3 osm→gtfs",
            ok3,
            f"{close3}/{len(sampled_osm)} OSM stops ≤200 m from a GTFS stop (need 5/6)  "
            f"[{', '.join(f'{d*1000:.0f} m' for d in dists3)}]",
        ))
    else:
        out.append(("check3 osm→gtfs", None, "skipped (fewer than 2 OSM stop nodes)"))

    return out


def _fmt_check(name, result, detail):
    tag = {True: "PASS", False: "FAIL", None: "SKIP"}[result]
    return f"    {name:16s} {tag}: {detail}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("osm_id", help="OSM relation ID to diagnose")
    ap.add_argument("--deep", action="store_true",
                    help="Load full GTFS and re-run 4-loop matching (~2 min)")
    args = ap.parse_args()
    osm_id = str(args.osm_id)

    # ── Sidecar lookups ────────────────────────────────────────────────────────
    transit_geojson = _load(DATA / "transit_lines.geojson")
    dropped_list    = _load(DATA / "main_loop_dropped.json") or []
    excluded_list   = _load(DATA / "sanity_excluded.json") or []

    dropped_by_id  = {str(e["osm_id"]): e for e in dropped_list}
    excluded_by_id = {str(e["osm_id"]): e for e in excluded_list}

    drawn_feat = None
    if transit_geojson:
        for f in transit_geojson["features"]:
            if str(f["properties"].get("osm_id", "")) == osm_id:
                drawn_feat = f
                break

    osm_feat = None
    if OSM_ROUTES_PATH.exists():
        for f in json.loads(OSM_ROUTES_PATH.read_text())["features"]:
            if str(f["properties"].get("osm_id", "")) == osm_id:
                osm_feat = f
                break

    # ── Status ─────────────────────────────────────────────────────────────────
    print(f"\nOSM route {osm_id}")
    print(_hr("="))

    if drawn_feat:
        p = drawn_feat["properties"]
        print(f"STATUS: DRAWN")
        print(f"  ref={p.get('ref')!r}  mode={p.get('mode')}  "
              f"freq_score={p.get('freq_score')}")
        print(f"  name={p.get('name')!r}")
        if not args.deep:
            return

    elif osm_id in dropped_by_id:
        e = dropped_by_id[osm_id]
        reason = e.get("reason", "?")
        if reason == "no_draw":
            nd_reason = e.get("no_draw_reason", "?")
            print(f"STATUS: NO DRAW  (matched GTFS line flagged {nd_reason!r})")
            print(f"  ref={e.get('ref')!r}  mode={e.get('mode')}  name={e.get('name')!r}")
            print(f"  Matched GTFS line: {e.get('matched_line_key')}")
        elif reason == "dedup":
            print(f"STATUS: SUPERSEDED  (already claimed by a direct-ref match)")
            print(f"  ref={e.get('ref')!r}  mode={e.get('mode')}  name={e.get('name')!r}")
        else:
            print(f"STATUS: DROPPED ({reason})")
            print(f"  ref={e.get('ref')!r}  mode={e.get('mode')}  name={e.get('name')!r}")
        if not args.deep:
            print("\n  Re-run with --deep for per-loop candidate diagnostics (~2 min).")
            return

    elif osm_id in excluded_by_id:
        e = excluded_by_id[osm_id]
        print("STATUS: EXCLUDED  (no valid GTFS candidate in any of the 4 loops)")
        print(f"  ref={e.get('ref')!r}  mode={e.get('mode')}  name={e.get('name')!r}")
        if not args.deep:
            print("\n  Re-run with --deep for per-loop candidate diagnostics (~2 min).")
            return

    elif osm_feat:
        p = osm_feat["properties"]
        print("STATUS: EARLY EXIT  (mode not mapped or excluded operator/network)")
        print(f"  ref={p.get('ref')!r}  mode={p.get('mode')}  name={p.get('name')!r}")
        if not args.deep:
            print("\n  Re-run with --deep for per-loop candidate diagnostics (~2 min).")
            return

    else:
        print("STATUS: NOT IN OSM EXTRACT")
        print(f"  No feature with osm_id={osm_id} in routes.geojson.")
        print("  Run the OSM extraction step if the source data has changed.")
        return

    if not args.deep:
        return

    # ── Deep mode ──────────────────────────────────────────────────────────────
    if osm_feat is None:
        print("\nCannot run deep analysis: route not found in routes.geojson.")
        return

    print(f"\n{_hr('=')}")
    print("DEEP ANALYSIS — loading GTFS data (~2 min)...")
    print(_hr("="))

    import importlib.util
    spec = importlib.util.spec_from_file_location("s05", HERE / "05_score_and_match.py")
    _m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_m)

    stop_coords = _m.load_stops()
    stop_meta   = _m.load_stop_meta()
    svc_dates   = _m.load_calendar_dates()
    rt_lookup   = _m.load_routes()
    trip_lookup = _m.load_trips(rt_lookup)
    trip_freq   = _m.load_frequencies()
    line_freq, line_speed, line_canonical = _m.stream_stop_times(
        trip_lookup, stop_coords, svc_dates, trip_freq, stop_meta
    )

    # TEMP: mirror the pre-matching no_draw filter applied in 05_score_and_match.py:main().
    # Without this, the diagnose script would still see no_draw entries in
    # _line_canonical_export and report SETTLED on candidates the real pipeline no longer
    # considers. Remove together with the matching block in 05_score_and_match.py.
    _removed_lk = 0
    for _key in list(_m._line_canonical_export.keys()):
        _kept = [e for e in _m._line_canonical_export[_key] if e.no_draw is None]
        if not _kept:
            del _m._line_canonical_export[_key]
            _removed_lk += 1
        elif len(_kept) != len(_m._line_canonical_export[_key]):
            _m._line_canonical_export[_key] = _kept
    print(f"  TEMP no_draw filter: removed {_removed_lk} empty key(s) from _line_canonical_export")

    for lk in line_canonical:
        _ = line_freq[lk]
    _m.build_gtfs_index(line_freq, line_speed)
    print(f"  {len(_m._line_canonical_export):,} canonical export entries loaded")

    # ── Parse OSM geometry ─────────────────────────────────────────────────────
    p = osm_feat["properties"]
    geom = osm_feat["geometry"]
    if geom["type"] == "MultiLineString":
        osm_pts  = [c for seg in geom["coordinates"] for c in seg]
        osm_segs = geom["coordinates"]
    else:
        osm_pts  = geom["coordinates"]
        osm_segs = None

    ref        = p.get("ref", "")
    # routes.geojson has no 'mode' property; recompute it the same way 05_ does.
    mode       = _m.osm_to_mode(
        p.get("route", ""), ref, p.get("operator", ""),
        p.get("length_km", 0.0), p.get("network", ""),
    )
    bucket     = MODE_TO_BUCKET.get(mode or "", "bus")
    sub_bboxes = _m.build_sub_bboxes(osm_pts)
    osm_from   = p.get("from", "")
    osm_to     = p.get("to", "")
    osm_sn     = p.get("stop_nodes", [])
    osm_lkm    = p.get("line_km") or p.get("raw_length_km", 0.0)
    osm_span   = _m.haversine_km(
        osm_pts[0][0], osm_pts[0][1], osm_pts[-1][0], osm_pts[-1][1]
    )
    skip_upper = (mode == "regional_bus")

    print(f"\nOSM route details:")
    print(f"  ref={ref!r}  mode={mode or '(none)'}  bucket={bucket}")
    print(f"  name={p.get('name')!r}")
    print(f"  from={osm_from!r}  to={osm_to!r}")
    print(f"  osm_line_km={osm_lkm:.1f} km  osm_span={osm_span:.1f} km  "
          f"stop_nodes={len(osm_sn)}")
    if osm_sn:
        names = [s[2] if len(s) > 2 else "?" for s in osm_sn[:6]]
        suffix = f"  … (+{len(osm_sn)-6} more)" if len(osm_sn) > 6 else ""
        print(f"  stop names: {names}{suffix}")

    # ── 4 loops ────────────────────────────────────────────────────────────────
    MAX_SHOW = 15
    settled = False
    settled_no_draw = None

    for loop_level in (1, 2, 3, 4):
        verb = ("SIMPLE STRING", "STRING TRICKS", "GENERIC STRING", "GEO-FALLBACK")[loop_level - 1]
        print(f"\n{_hr()}")
        print(f"LOOP {loop_level} — {verb}")
        print(_hr())

        # ── Collect candidates ─────────────────────────────────────────────────
        if loop_level <= 3:
            keys = _m._loop_keys(loop_level, ref, ref.replace(" ", ""), p.get("name", ""))
            print(f"Keys: {keys or '(none — no applicable trick)'}")
            if not keys:
                print("→ Passes to next loop")
                continue
            candidates = _m._stop_candidates(
                keys, bucket, sub_bboxes, osm_pts, osm_sn, osm_segs,
                stop_coords, osm_span,
            )
        else:
            search_buckets = {bucket}
            if bucket == "mountain":
                search_buckets.add("train")
            _bbox_m = 0.9
            osm_bbox = (
                min(p[0] for p in osm_pts) - _bbox_m,
                min(p[1] for p in osm_pts) - _bbox_m,
                max(p[0] for p in osm_pts) + _bbox_m,
                max(p[1] for p in osm_pts) + _bbox_m,
            )
            raw = []
            for (lk_ref, lk_bucket), lk_cands in _m._line_canonical_export.items():
                if lk_bucket not in search_buckets:
                    continue
                for entry in lk_cands:
                    if not entry.stops:
                        continue
                    first_c = None
                    for sid, *_ in entry.stops:
                        if _m.is_in_service_area(sid):
                            first_c = (stop_coords.get(sid)
                                       or stop_coords.get(sid.split(":")[0]))
                            if first_c:
                                break
                    if first_c and not (osm_bbox[0] <= first_c[0] <= osm_bbox[2] and
                                        osm_bbox[1] <= first_c[1] <= osm_bbox[3]):
                        continue
                    ccoords = []
                    for stop_id, _a, _d in entry.stops:
                        if not _m.is_in_service_area(stop_id):
                            continue
                        c = (stop_coords.get(stop_id)
                             or stop_coords.get(stop_id.split(":")[0]))
                        if c and any(_m.stop_near_bbox(c[0], c[1], sb)
                                     for sb in sub_bboxes):
                            ccoords.append([c[0], c[1], stop_id])
                    if len(ccoords) < 2:
                        continue
                    score = len(ccoords) / len(entry.stops)
                    if score < 0.5:
                        continue
                    raw.append((score, ccoords, entry.stops, entry.line_key, entry.agency_id, entry.trip_group_id, lk_ref, entry.no_draw))

            raw.sort(key=lambda x: (-x[0], -len(x[1])))
            print(f"Geo pool: {len(raw)} candidates pass score≥0.5, capped at 50")
            candidates = []
            for score, ccoords, cand, line_key, agency_id, tg_id, lk_ref, no_draw in raw[:50]:
                fc = [c for sid, *_ in cand
                      if _m.is_in_service_area(sid)
                      and (c := (stop_coords.get(sid)
                                 or stop_coords.get(sid.split(":")[0])))]
                sp = sum(
                    _m.haversine_km(fc[i][0], fc[i][1], fc[i+1][0], fc[i+1][1])
                    for i in range(len(fc) - 1)
                )
                full_density = len(fc) / sp if sp > 0 else 0.0
                ep_0_5 = _m._count_endpoints_covered(
                    osm_pts, ccoords, _m.GEO_SORT_ENDPOINT_KM, osm_sn, osm_segs
                )
                sn, ln, bkt = line_key
                candidates.append(
                    (score, ep_0_5, ccoords, full_density, (sn, ln, bkt, agency_id, tg_id), lk_ref, no_draw)
                )
            candidates.sort(key=lambda x: (-x[0], -x[1], -len(x[2])))

        if not candidates:
            print("→ No candidates found — passes to next loop")
            continue

        drawable_cands = [c for c in candidates if c[6] is None]
        no_draw_cands  = [c for c in candidates if c[6] is not None]
        print(f"Candidates: {len(candidates)}  ({len(drawable_cands)} drawable, {len(no_draw_cands)} no_draw)")

        # ── Evaluate candidates (drawable first, then no_draw fallback) ────────
        shown = 0
        cap_hit = False
        for pass_label, subset in (
            ("drawable", drawable_cands),
            ("no_draw fallback", no_draw_cands),
        ):
            if not subset or cap_hit:
                continue
            if pass_label == "no_draw fallback":
                print(f"\n  ── no_draw fallback pass ({len(no_draw_cands)}) ──")
            for bbox_score, ep_0_5, ccoords, full_density, lkf, lk_ref, no_draw in subset:
                shown += 1
                sn, ln, bkt, aid, _tg = lkf
                ep_5 = _m._count_endpoints_covered(
                    osm_pts, ccoords, _m.ENDPOINT_THRESHOLD_KM, osm_sn, osm_segs
                )
                skip_sanity = (
                    (loop_level == 1 and ep_5 == 2) or
                    (loop_level == 2 and ep_0_5 == 2)
                )

                label = f"{sn!r}/{ln!r}"
                if len(label) > 44:
                    label = label[:41] + "…"
                nd_tag = f"  [no_draw={no_draw!r}]" if no_draw else ""
                print(
                    f"\n  [{shown:2}] {label:44s} "
                    f"score={bbox_score:.2f} ep5={ep_5} ep0.5={ep_0_5} n={len(ccoords)}{nd_tag}"
                )

                if ep_5 == 0:
                    print("       gate FAIL: no GTFS stop within 5 km of either OSM endpoint")
                elif skip_sanity:
                    cond = "ep_5=2" if loop_level == 1 else "ep_0.5=2"
                    print(f"       sanity skipped ({cond}) → SETTLED")
                    settled = True
                    settled_no_draw = no_draw
                else:
                    checks = _run_sanity_checks(
                        osm_pts, ccoords, stop_meta, osm_sn, osm_lkm,
                        full_density, skip_upper, _m,
                    )
                    sanity_ok = _m._passes_geo_sanity(
                        osm_pts, ccoords, stop_meta, osm_from, osm_to, osm_sn, osm_lkm,
                        cand_full_density=full_density, skip_upper_density=skip_upper,
                    )
                    if sanity_ok:
                        print("       sanity PASS → SETTLED")
                        for name, result, detail in checks:
                            print(_fmt_check(name, result, detail))
                        settled = True
                        settled_no_draw = no_draw
                    else:
                        print("       sanity FAIL:")
                        for name, result, detail in checks:
                            print(_fmt_check(name, result, detail))

                if settled:
                    break
                if shown >= MAX_SHOW:
                    rem = len(candidates) - MAX_SHOW
                    if rem > 0:
                        print(f"\n  … {rem} more candidates (all failed)")
                    cap_hit = True
                    break
            if settled or cap_hit:
                break

        if settled:
            print(f"\n→ SETTLED in Loop {loop_level}")
            if settled_no_draw:
                nd_msg = {
                    "low_frequency": (
                        "GTFS line is below MIN_FREQ_SCORE (0.075) — too few trips per day. "
                        "Line is matched but NOT drawn."
                    ),
                }.get(settled_no_draw, f"no_draw={settled_no_draw!r} — line is NOT drawn.")
                print(f"  WARNING: settled candidate has no_draw={settled_no_draw!r}")
                print(f"  → {nd_msg}")
            break
        if candidates:
            print(f"\n→ No candidate settled — passes to next loop")

    if not settled:
        print(f"\n{_hr('=')}")
        print("RESULT: No valid candidate in any loop → EXCLUDED (not drawn)")
        print(_hr("="))


if __name__ == "__main__":
    main()
