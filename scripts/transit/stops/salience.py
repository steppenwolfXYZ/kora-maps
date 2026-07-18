"""Per-stop salience: urbanness / dwell scoring, stop importance, tier
resolution and min_zoom assignment. See zoom-level-rules.md and the
salience-ranking concept."""
import json
import sys
from collections import defaultdict
from math import cos, floor, log, radians

from _state import *  # noqa: F401,F403 — shared constants (ROOT, MODE_*, ...)
from _state import _M_PER_DEG, _transit_cfg  # underscore names skipped by *
from geometry import (
    _cum_dist_m, _interp_at, _meters_per_deg, _project_meters,
    flatten_coords, haversine_km,
)
from gtfs.loaders import load_stop_meta
from stop_attributes import GTFS_STOPS


# Stop tier hierarchy. Higher index = higher priority for tier assignment
# when a stop is served by multiple modes. Ferry / mountain are evaluated
# only when no hierarchy mode is present.
STOP_TIER_HIERARCHY = ("train", "metro", "tram", "bus", "regional_bus")
STOP_TIER_ISOLATED  = ("ferry", "mountain")
STOP_TIER_RANK = {m: i for i, m in enumerate(STOP_TIER_HIERARCHY)}

# Min_zoom assigned when no per-mode rule matches. Effectively "never visible"
# at any rendered zoom level.
UNREACH_Z = 14


def _resolve_stop_tier(modes_present: set) -> str:
    """Return the tier for a stop served by `modes_present`. Hierarchy
    modes win over isolated pools when both present."""
    best = None
    best_rank = -1
    for m in modes_present:
        r = STOP_TIER_RANK.get(m, -1)
        if r > best_rank:
            best_rank = r
            best = m
    if best is not None:
        return best
    for m in STOP_TIER_ISOLATED:
        if m in modes_present:
            return m
    return ""


# ── Zoom-level rules: data loaders ──────────────────────────────────────────
# See .claude/concepts/zoom-level-rules.md.

BUILDINGS_GEOJSON = ROOT / "data" / "osm" / "buildings.geojson"
GTFS_STOP_TIMES   = ROOT / "data" / "gtfs_routed" / "stop_times.txt"
DWELL_BY_UIC      = ROOT / "data" / "transit" / "dwell_by_uic.json"
OUT_URBANNESS     = ROOT / "data" / "transit" / "urbanness.json"


def _zoom_rules_cfg() -> dict:
    sc = _transit_cfg.get("zoom_level_rules") or {}
    if not sc:
        print("  WARNING: config.yaml has no `zoom_level_rules` section — "
              "stop min_zoom defaults to mode minzoom only.")
    return sc


def _uic_of(sid: str, stop_meta: dict) -> str:
    """Canonical UIC for a stop_id — parent_station if present, else the
    `:`-prefix base of the stop_id (which is the SBB-style UIC)."""
    if not sid:
        return ""
    meta = stop_meta.get(sid) or stop_meta.get(sid.split(":")[0]) or {}
    return meta.get("parent") or sid.split(":")[0]


def load_buildings():
    """Return a flat [(lon, lat), ...] from data/osm/buildings.geojson.
    Format is the custom `{"coords": [[lon, lat], ...]}` blob written by
    03_bbox_osm.py — not strict GeoJSON, just a compact coord list."""
    if not BUILDINGS_GEOJSON.exists():
        print(f"  WARNING: {BUILDINGS_GEOJSON} missing — urbanness brackets "
              "default to rural. Re-run step 03 to populate.")
        return []
    data = json.loads(BUILDINGS_GEOJSON.read_text())
    return [(float(c[0]), float(c[1])) for c in data.get("coords", [])]


def count_buildings_in_radii(coords_by_uic, buildings,
                              r_inner_m, r_outer_m):
    """{uic: (c_inner, c_outer)} via grid bucketing at the outer radius."""
    if not buildings or not coords_by_uic:
        return {uic: (0, 0) for uic in coords_by_uic}
    cell_m = max(r_inner_m, r_outer_m)
    lat0 = 46.8
    cos_lat0 = cos(radians(lat0))
    cell_lat_deg = cell_m / _M_PER_DEG
    cell_lon_deg = cell_lat_deg / cos_lat0
    grid: dict = defaultdict(list)
    for lon, lat in buildings:
        cx = int(floor(lon / cell_lon_deg))
        cy = int(floor(lat / cell_lat_deg))
        grid[(cx, cy)].append((lon, lat))
    r_in_sq = r_inner_m * r_inner_m
    r_out_sq = r_outer_m * r_outer_m
    out: dict = {}
    for uic, (lon, lat) in coords_by_uic.items():
        cx = int(floor(lon / cell_lon_deg))
        cy = int(floor(lat / cell_lat_deg))
        c_in = c_out = 0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (blon, blat) in grid.get((cx + dx, cy + dy), ()):
                    mdx = (blon - lon) * cos_lat0 * _M_PER_DEG
                    mdy = (blat - lat) * _M_PER_DEG
                    d_sq = mdx * mdx + mdy * mdy
                    if d_sq <= r_out_sq:
                        c_out += 1
                        if d_sq <= r_in_sq:
                            c_in += 1
        out[uic] = (c_in, c_out)
    return out


def compute_urbanness(building_counts, urb_cfg):
    """{uic: {c_inner, c_outer, bracket}}. Bracket assigned by evaluating
    rules top-to-bottom (elseif semantics):
        c_outer > city_c500   → city
        c_outer > town_c500   → town
        c_inner > village_c200 → village
        else                  → rural
    See concept § "Urbanness bracket"."""
    city_th    = float(urb_cfg.get("city_c500",    600))
    town_th    = float(urb_cfg.get("town_c500",    300))
    village_th = float(urb_cfg.get("village_c200",  30))
    out: dict = {}
    for uic, (c_in, c_out) in building_counts.items():
        if c_out > city_th:
            b = "city"
        elif c_out > town_th:
            b = "town"
        elif c_in > village_th:
            b = "village"
        else:
            b = "rural"
        out[uic] = {"c200": c_in, "c500": c_out, "bracket": b}
    return out


def compute_dwell_per_uic(stop_meta):
    """{uic: avg_dwell_seconds} — read from data/transit/dwell_by_uic.json,
    which step 06 populates as a side-effect of its stop_times.txt stream
    (see gtfs.identity._dwell_export). Streaming the 1.7 GB routed
    stop_times.txt a second time here was ~60 s of pure-Python CSV
    parsing; piggybacking on step 06 makes step 07 skip it entirely.
    """
    if not DWELL_BY_UIC.exists():
        print(f"  WARNING: {DWELL_BY_UIC} missing — dwell points default "
              "to 0. Re-run step 06 to populate.")
        return {}
    raw = json.loads(DWELL_BY_UIC.read_text())
    return {uic: float(v) for uic, v in raw.items()}


def compute_stop_importance(uic_serving, coords_by_uic,
                             urbanness, dwell_by_uic,
                             nearby_transit_radius_m):
    """Per-stop importance score = dwell + urbanness + nearby_transit + interchange.
    See concept § "Stop importance score". Returns {uic: int}.

    Per-category points are hard-coded in this function; only the radius
    for the nearby-transit category lives in config (see
    `zoom_level_rules.stop_importance.nearby_transit_radius_m`).
    """
    # Per-uic: distinct line-key set. line_key = (ref, agency, mode) here —
    # mode-typed so "Bus 10 BernMobil" and "Train 10 SBB" count as distinct.
    line_keys_by_uic: dict = defaultdict(set)
    bus_tram_keys_by_uic: dict = defaultdict(set)
    modes_by_uic: dict = defaultdict(set)
    for uic, entries in uic_serving.items():
        for e in entries:
            lk = e["line_key"]
            line_keys_by_uic[uic].add(lk)
            modes_by_uic[uic].add(e["mode"])
            if e["mode"] in ("bus", "tram", "regional_bus"):
                bus_tram_keys_by_uic[uic].add(lk)

    # Spatial grid for nearby-transit lookup (train stop → bus/tram lines
    # within radius). Index keys are uic, value is the bus/tram line_key set
    # at that uic.
    lat0 = 46.8
    cos_lat0 = cos(radians(lat0))
    cell_m = nearby_transit_radius_m
    cell_lat_deg = cell_m / _M_PER_DEG
    cell_lon_deg = cell_lat_deg / cos_lat0
    bt_grid: dict = defaultdict(list)
    for uic, keys in bus_tram_keys_by_uic.items():
        coord = coords_by_uic.get(uic)
        if not coord:
            continue
        lon, lat = coord
        cx = int(floor(lon / cell_lon_deg))
        cy = int(floor(lat / cell_lat_deg))
        bt_grid[(cx, cy)].append((uic, lon, lat, keys))
    r_sq = nearby_transit_radius_m * nearby_transit_radius_m

    URBANNESS_POINTS = {"city": 3, "town": 2, "village": 1, "rural": 0}
    out: dict = {}
    for uic in uic_serving:
        score = 0
        # Dwell: > 3 min → 3; > 0 min → 2; else 0.
        dwell = dwell_by_uic.get(uic, 0.0)
        if dwell > 180:
            score += 3
        elif dwell > 0:
            score += 2
        # Urbanness bracket.
        bracket = urbanness.get(uic, {}).get("bracket", "rural")
        score += URBANNESS_POINTS.get(bracket, 0)
        # Nearby transit (train stops only).
        my_modes = modes_by_uic.get(uic, set())
        if "train" in my_modes:
            coord = coords_by_uic.get(uic)
            if coord is not None:
                lon, lat = coord
                cx = int(floor(lon / cell_lon_deg))
                cy = int(floor(lat / cell_lat_deg))
                my_keys = line_keys_by_uic.get(uic, set())
                found_keys: set = set()
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for (_other_uic, olon, olat, keys) in \
                                bt_grid.get((cx + dx, cy + dy), ()):
                            mdx = (olon - lon) * cos_lat0 * _M_PER_DEG
                            mdy = (olat - lat) * _M_PER_DEG
                            if mdx * mdx + mdy * mdy > r_sq:
                                continue
                            found_keys.update(keys - my_keys)
                if len(found_keys) > 3:
                    score += 3
                elif len(found_keys) > 0:
                    score += 2
        # Interchange.
        keys_here = line_keys_by_uic.get(uic, set())
        if len(keys_here) >= 2:
            if "train" in my_modes:
                score += 3
            else:
                score += 2
        out[uic] = score
    return out


def _build_uic_serving(line_lookup, line_stops, stop_meta):
    """Build the per-UIC line-membership index used by every stop-rule
    function. Each entry carries {oid, mode, idx, is_first, is_last,
    line_key}. uic = parent_station if present, else stop_id base.
    """
    uic_serving: dict = defaultdict(list)
    coords_by_uic: dict = {}
    for oid, entry in line_stops.items():
        info = line_lookup.get(str(oid))
        if not info:
            continue
        stops = entry.get("stops", []) if isinstance(entry, dict) else entry
        if not stops:
            continue
        mode = info.get("mode", "")
        ref = info.get("ref", "")
        agency_id = info.get("agency_id", "")
        line_key = (ref, agency_id, mode)
        last_idx = len(stops) - 1
        for idx, stop in enumerate(stops):
            if len(stop) < 3 or not stop[2]:
                continue
            sid = stop[2]
            lon, lat = float(stop[0]), float(stop[1])
            uic = _uic_of(sid, stop_meta)
            if not uic:
                continue
            uic_serving[uic].append({
                "oid": str(oid), "mode": mode, "idx": idx,
                "is_first": idx == 0, "is_last": idx == last_idx,
                "line_key": line_key,
            })
            coords_by_uic.setdefault(uic, (lon, lat))
    return uic_serving, coords_by_uic


def compute_stop_min_zoom(line_lookup, line_stops, stop_meta,
                           importance_by_uic, intercity_oids,
                           uic_serving, coords_by_uic,
                           stop_tier_by_uic=None):
    """Apply per-mode stop rules → candidate min_zoom per UIC, then
    raise to the smallest min_zoom of any line serving the UIC
    (stops-follow-lines). Returns {uic: {min_zoom, rule_label,
    is_intersection, is_terminus, tier}}.

    `stop_tier_by_uic` maps UIC → `stop_tier` string (from step 06's
    stop_size_scores.json). Used by the train z7/z8 tier gates.
    """
    if stop_tier_by_uic is None:
        stop_tier_by_uic = {}
    # Train tier ranks — lower is more prominent.
    TRAIN_TIER_RANK = {
        "major_train": 0,
        "main_train": 1,
        "important_train": 2,
        "train_station": 3,
        "small_train": 4,
    }
    # Per-line cumulative km along the polyline at each stop index.
    cum_km_by_oid: dict = {}
    for oid, entry in line_stops.items():
        stops = entry.get("stops", []) if isinstance(entry, dict) else entry
        if not stops:
            cum_km_by_oid[str(oid)] = []
            continue
        cum = [0.0]
        for i in range(1, len(stops)):
            cum.append(cum[-1] + haversine_km(
                stops[i - 1][0], stops[i - 1][1],
                stops[i][0], stops[i][1]))
        cum_km_by_oid[str(oid)] = cum

    # Per-uic per-line index map (so a line can be checked at a uic without
    # rescanning its stops).
    uic_indices_on_oid: dict = defaultdict(dict)  # (uic, oid) → idx
    for uic, entries in uic_serving.items():
        for e in entries:
            uic_indices_on_oid[(uic, e["oid"])] = e["idx"]

    # Pre-bucket lines by mode for fast lookup.
    oids_by_mode: dict = defaultdict(list)
    for oid, info in line_lookup.items():
        oids_by_mode[info.get("mode", "")].append(str(oid))

    def _line_mz(oid: str) -> int:
        mz = line_lookup.get(oid, {}).get("min_zoom")
        try:
            return int(mz) if mz is not None else UNREACH_Z
        except (TypeError, ValueError):
            return UNREACH_Z

    def _visible_oids_in_mode(mode: str, level: int) -> set:
        return {o for o in oids_by_mode.get(mode, [])
                if _line_mz(o) <= level}

    candidate_mz: dict = {uic: UNREACH_Z for uic in uic_serving}
    rule_label: dict = {uic: "" for uic in uic_serving}
    # `is_intersection` / `is_terminus` are computed against the FINAL set of
    # visible lines (using the final per-line min_zoom). Recorded for diag
    # output; not used to gate further rules.
    is_intersection_flag: dict = {uic: False for uic in uic_serving}
    is_terminus_flag: dict = {uic: False for uic in uic_serving}

    def _maybe_set(uic: str, level: int, label: str):
        if candidate_mz[uic] > level:
            candidate_mz[uic] = level
            rule_label[uic] = label

    # Pre-compute the canonical-UIC stop set per line — used by the
    # intersection rule below to test "how many stops do these two lines share?"
    # See concept § "Metrics referenced below" → is_intersection.
    uic_stops_by_oid: dict = {}
    for oid, entry in line_stops.items():
        stops = entry.get("stops", []) if isinstance(entry, dict) else entry
        s: set = set()
        for stop in stops:
            if len(stop) >= 3 and stop[2]:
                uic = _uic_of(stop[2], stop_meta)
                if uic:
                    s.add(uic)
        uic_stops_by_oid[str(oid)] = s

    # Two lines can share at most this many UIC stops and still count as an
    # intersection. The tolerance keeps parallel-corridor stops out of
    # intersection status while keeping real hubs that happen to share a
    # secondary stop on top of the hub.
    INTERSECTION_MAX_SHARED_STOPS = 2

    def _apply_intersection_or_terminus(mode: str, level: int,
                                        intersection_ok=None,
                                        terminus_ok=None):
        vis = _visible_oids_in_mode(mode, level)
        if not vis:
            return
        for uic, entries in uic_serving.items():
            # Visible mode-entries at this UIC.
            mode_entries = [e for e in entries
                            if e["mode"] == mode and e["oid"] in vis]
            terminus = any(e["is_first"] or e["is_last"]
                           for e in mode_entries)
            # Group by line_key (distinct logical lines). Multiple variants of
            # the same logical line don't count as a separate line for the
            # intersection test.
            oids_by_key: dict = defaultdict(list)
            for e in mode_entries:
                oids_by_key[e["line_key"]].append(e["oid"])
            # Per-line-key UIC set = union over variants.
            stops_by_key: dict = {
                k: set().union(*(uic_stops_by_oid.get(o, set())
                                 for o in oids))
                for k, oids in oids_by_key.items()
            }
            intersection = False
            keys = list(oids_by_key.keys())
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    shared = stops_by_key[keys[i]] & stops_by_key[keys[j]]
                    if len(shared) <= INTERSECTION_MAX_SHARED_STOPS:
                        intersection = True
                        break
                if intersection:
                    break
            if not mode_entries:
                continue
            if intersection:
                is_intersection_flag[uic] = True
            if terminus:
                is_terminus_flag[uic] = True
            gated_intersection = intersection and (
                intersection_ok is None or intersection_ok(uic))
            gated_terminus = terminus and (
                terminus_ok is None or terminus_ok(uic))
            if gated_intersection or gated_terminus:
                _maybe_set(uic, level, f"{mode}: intersection_or_terminus")

    def _apply_intercity_train_stops(level: int):
        # Every stop on a visible intercity train line.
        for oid in intercity_oids:
            if _line_mz(oid) > level:
                continue
            entry = line_stops.get(oid) or line_stops.get(str(oid)) or {}
            stops = entry.get("stops", []) if isinstance(entry, dict) else entry
            for stop in stops:
                if len(stop) < 3 or not stop[2]:
                    continue
                uic = _uic_of(stop[2], stop_meta)
                if uic and uic in candidate_mz:
                    _maybe_set(uic, level, "train: served by intercity line")

    def _apply_importance_greedy(mode: str, level: int, min_km: float):
        vis = _visible_oids_in_mode(mode, level)
        for oid in vis:
            entry = line_stops.get(oid) or line_stops.get(str(oid)) or {}
            stops = entry.get("stops", []) if isinstance(entry, dict) else entry
            cum_km = cum_km_by_oid.get(str(oid), [])
            if not stops or not cum_km:
                continue
            uic_per_idx: list = []
            for stop in stops:
                if len(stop) < 3 or not stop[2]:
                    uic_per_idx.append("")
                else:
                    uic_per_idx.append(_uic_of(stop[2], stop_meta))
            order = sorted(
                range(len(stops)),
                key=lambda i: (
                    -importance_by_uic.get(uic_per_idx[i], 0),
                    uic_per_idx[i] or "",
                    i,
                ),
            )
            accepted_km: list = []
            for i in order:
                uic = uic_per_idx[i]
                if not uic:
                    continue
                ki = cum_km[i] if i < len(cum_km) else 0.0
                if any(abs(ki - aj) < min_km for aj in accepted_km):
                    continue
                accepted_km.append(ki)
                _maybe_set(uic, level,
                           f"{mode}: importance-greedy <= 1 / {min_km:g} km")

    def _apply_all_stops_on_visible_mode(mode: str, base_level: int):
        # Stops on visible lines of `mode` get base_level (or the line's own
        # min_zoom if later — "lines first becoming visible at z11 bring their
        # stops with them at z11"). Used by ferry + mountain stop rules.
        for oid in oids_by_mode.get(mode, []):
            line_mz = _line_mz(oid)
            effective = max(base_level, line_mz)
            entry = line_stops.get(oid) or line_stops.get(str(oid)) or {}
            stops = entry.get("stops", []) if isinstance(entry, dict) else entry
            for stop in stops:
                if len(stop) < 3 or not stop[2]:
                    continue
                uic = _uic_of(stop[2], stop_meta)
                if uic and uic in candidate_mz:
                    _maybe_set(uic, effective,
                               f"{mode}: all stops on visible line")

    def _apply_all_remaining(mode: str, level: int):
        # Every stop on every line of this mode gets capped at level (or the
        # line's min_zoom if later).
        for oid in oids_by_mode.get(mode, []):
            line_mz = _line_mz(oid)
            effective = max(level, line_mz)
            entry = line_stops.get(oid) or line_stops.get(str(oid)) or {}
            stops = entry.get("stops", []) if isinstance(entry, dict) else entry
            for stop in stops:
                if len(stop) < 3 or not stop[2]:
                    continue
                uic = _uic_of(stop[2], stop_meta)
                if uic and uic in candidate_mz:
                    _maybe_set(uic, effective, f"{mode}: all remaining")

    # ── Apply the per-mode tables ───────────────────────────────────────────
    # Train
    def _train_tier_rank(uic: str) -> int:
        tier = stop_tier_by_uic.get(uic, "")
        return TRAIN_TIER_RANK.get(tier, 99)
    _apply_intersection_or_terminus(
        "train", 7,
        intersection_ok=lambda uic: _train_tier_rank(uic) <= TRAIN_TIER_RANK["main_train"],
        terminus_ok=lambda uic: _train_tier_rank(uic) <= TRAIN_TIER_RANK["train_station"],
    )
    _apply_intercity_train_stops(8)
    _apply_intersection_or_terminus(
        "train", 8,
        intersection_ok=lambda uic: _train_tier_rank(uic) <= TRAIN_TIER_RANK["important_train"],
        terminus_ok=lambda uic: False,
    )
    _apply_intersection_or_terminus("train", 9)
    _apply_importance_greedy("train", 9, 5.0)
    _apply_importance_greedy("train", 10, 3.0)
    _apply_all_remaining("train", 11)
    # Metro
    _apply_intersection_or_terminus("metro", 10)
    _apply_importance_greedy("metro", 11, 1.0)
    _apply_all_remaining("metro", 12)
    # Ferry — single rule at z10.
    _apply_all_stops_on_visible_mode("ferry", 10)
    # Mountain — single rule at z10 with line-min_zoom carry.
    _apply_all_stops_on_visible_mode("mountain", 10)
    # Regional bus
    _apply_intersection_or_terminus("regional_bus", 10)
    _apply_importance_greedy("regional_bus", 11, 1.0)
    _apply_all_remaining("regional_bus", 12)
    # Tram
    _apply_intersection_or_terminus("tram", 10)
    _apply_importance_greedy("tram", 11, 1.0)
    _apply_all_remaining("tram", 12)
    # Bus
    _apply_intersection_or_terminus("bus", 10)
    _apply_importance_greedy("bus", 11, 1.0)
    _apply_importance_greedy("bus", 12, 0.5)
    _apply_all_remaining("bus", 13)

    # ── Stops follow lines ──────────────────────────────────────────────────
    final: dict = {}
    for uic, entries in uic_serving.items():
        line_mzs = [_line_mz(e["oid"]) for e in entries]
        min_line = min(line_mzs) if line_mzs else UNREACH_Z
        cand = candidate_mz.get(uic, UNREACH_Z)
        mz = max(cand, min_line)
        modes_here = {e["mode"] for e in entries}
        final[uic] = {
            "min_zoom":        int(mz),
            "candidate_min_zoom": int(cand),
            "rule_label":      rule_label.get(uic, ""),
            "is_intersection": is_intersection_flag.get(uic, False),
            "is_terminus":     is_terminus_flag.get(uic, False),
            "tier":            _resolve_stop_tier(modes_here),
        }
    return final

