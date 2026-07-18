"""Far-zoom stop-dot dedup: per-zoom pixel-grid thinning of dot features.
See stops-far-zoom-dot-redesign.md."""
import json
from collections import defaultdict
from math import cos, radians

from _state import *  # noqa: F401,F403 — MODE_RANK, MODE_MINZOOM, ...
from _state import _transit_cfg  # underscore names skipped by *
from geometry import haversine_km

# =============================================================================
# Far-zoom dot dedup
# =============================================================================

def apply_stop_dedup(dot_features):
    """Per-zoom-level dedup pass over far-zoom stop dots. See
    `.claude/concepts/stops-far-zoom-dot-redesign.md` § "Dedup of overlapping
    dots".

    For each integer zoom z ∈ {13, 12, …, 7} (descending), each surviving
    dot may absorb touching lower-priority neighbours. Priority is:

      1. Mode hierarchy — train > mountain/ferry > everything else. A
         strictly higher-ranked dot absorbs a lower-ranked neighbour
         regardless of score.
      2. Within the same rank, higher score absorbs lower score.
      3. Tiebreak on equal score by `stop_id` (lower absorbs).

    Absorption is VISUAL only — the absorber's tier and diameter are NOT
    touched. Only the per-zoom popup list (`lines_json_zN`) grows. The
    absorbed dot's `tippecanoe.minzoom` is raised so it disappears at the
    zoom it was eaten and below.

    Mutates `dot_features` in place. Adds `score_z7..score_z13` (debug
    only), `lines_json_z7..lines_json_z13` (popup), and
    `dep_hr_z7..dep_hr_z13` (popup) to participating features.
    """
    sd_cfg = _transit_cfg.get("stop_dot_sizing") or {}
    tier_sizes_cfg = sd_cfg.get("tier_sizes") or {}
    tier_diam = {}
    for name, corners in tier_sizes_cfg.items():
        if not isinstance(corners, dict):
            continue
        try:
            tier_diam[name] = (float(corners.get("z7", 2.0)),
                               float(corners.get("z13", 4.0)))
        except (TypeError, ValueError):
            continue
    default_tier = "small_bus"

    dedup_cfg = _transit_cfg.get("stop_dot_dedup") or {}
    min_spacing_px = float(dedup_cfg.get("min_spacing_px", 2.0))

    EARTH_M = 40075016.7
    MEAN_LAT_DEG = 46.5
    cos_lat = cos(radians(MEAN_LAT_DEG))

    def tier_diameter_at(zoom, tier):
        # Slope-continue past z13 so the far-zoom layer keeps growing
        # linearly through z13.99 (the pill takes over at z14). Clamp
        # only at the lower edge z7.
        corners = tier_diam.get(tier) or tier_diam.get(default_tier, (2.0, 4.0))
        z = max(7.0, float(zoom))
        t = (z - 7.0) / 6.0
        return corners[0] + t * (corners[1] - corners[0])

    max_z13_diam = max((c[1] for c in tier_diam.values()), default=18.0)

    def m_per_px(zoom):
        # MapLibre renders at half the standard Web Mercator m/px (it uses
        # 512-px tiles internally, so a given MapLibre zoom corresponds to
        # one zoom higher under the standard 256-px convention). Verified
        # against `map.project([lng, lat])` at z=13 in the browser.
        return (EARTH_M * cos_lat) / (512.0 * (2 ** zoom))

    # Mode hierarchy for dedup. Higher = stronger absorber.
    def _dedup_rank(mode: str) -> int:
        if mode == "train":
            return 2
        if mode in ("mountain", "ferry"):
            return 1
        return 0

    states = []
    for i, feat in enumerate(dot_features):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        p = feat["properties"]
        base_score = float(p.get("stop_score", 0))
        tier = p.get("stop_tier") or default_tier
        mode = p.get("mode", "")
        rank = _dedup_rank(mode)
        lines_raw = p.get("lines_json") or ""
        try:
            lines = json.loads(lines_raw) if lines_raw else []
            if not isinstance(lines, list):
                lines = []
        except (json.JSONDecodeError, TypeError):
            lines = []
        base_dep_hr = float(p.get("dep_hr", 0.0) or 0.0)
        # Effective minzoom: the lowest zoom at which this dot actually
        # renders. Take max of the layer floor (MODE_MINZOOM, baked into
        # the style as the source's minzoom) and the feature's own
        # tippecanoe.minzoom (which may have been raised by salience).
        # A stop not visible at zoom z must not participate in dedup at
        # zoom z — neither as absorber nor as absorbed.
        layer_floor = MODE_MINZOOM.get(mode, 11)
        tipp_minzoom = int((feat.get("tippecanoe") or {}).get("minzoom", layer_floor))
        eff_minzoom = max(layer_floor, tipp_minzoom)
        states.append({
            "idx": i,
            "lon": float(coords[0]),
            "lat": float(coords[1]),
            "stop_id": str(p.get("stop_id", "") or i),
            "tier": tier,
            "rank": rank,
            "base_score": base_score,
            # Per-zoom score: starts at base, grows with absorption. The
            # absorber's own diameter does NOT read this — it stays fixed
            # via `tier`. Kept for popup / debug diagnostics only.
            "score": {z: base_score for z in range(7, 14)},
            "alive": {z: (z >= eff_minzoom) for z in range(7, 14)},
            "eff_minzoom": eff_minzoom,
            "absorbed_max_z": None,
            "lines_per_z": {z: list(lines) for z in range(7, 14)},
            "lines_dirty": False,
            "dep_hr_per_z": {z: base_dep_hr for z in range(7, 14)},
            "dep_hr_dirty": False,
        })

    if not states:
        return

    n_absorptions = 0
    for z in range(13, 6, -1):
        mpp = m_per_px(z)
        # Cell size in degrees lat covering the max possible touch distance
        # (two largest possible radii + spacing).
        max_touch_px = max_z13_diam + min_spacing_px
        max_touch_m = max_touch_px * mpp
        cell_deg = max(0.001, max_touch_m / 111320.0)

        for _ in range(20):  # inner stability loop; converges in 2–3 normally
            survivors = [s for s in states if s["alive"][z]]
            # Sort by (rank desc, score desc, stop_id asc) — highest priority
            # absorbers processed first so their claim over an area is stable.
            survivors.sort(key=lambda s: (-s["rank"], -s["score"][z], s["stop_id"]))

            grid = defaultdict(list)
            for s in survivors:
                cx = int(s["lon"] / cell_deg)
                cy = int(s["lat"] / cell_deg)
                grid[(cx, cy)].append(s)

            absorbed_any = False
            for sa in survivors:
                if not sa["alive"][z]:
                    continue
                ra = tier_diameter_at(z, sa["tier"]) / 2.0
                cx_a = int(sa["lon"] / cell_deg)
                cy_a = int(sa["lat"] / cell_deg)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for sb in grid.get((cx_a + dx, cy_a + dy), ()):
                            if sb is sa or not sb["alive"][z]:
                                continue
                            # Mode hierarchy gate. Higher-rank never gets
                            # absorbed by lower-rank, regardless of score.
                            if sb["rank"] > sa["rank"]:
                                continue
                            # Within same rank, break by score / stop_id.
                            if sb["rank"] == sa["rank"]:
                                score_a = sa["score"][z]
                                score_b = sb["score"][z]
                                if score_b > score_a:
                                    continue
                                if score_b == score_a and sb["stop_id"] < sa["stop_id"]:
                                    continue
                            rb = tier_diameter_at(z, sb["tier"]) / 2.0
                            dist_m = haversine_km(sa["lon"], sa["lat"],
                                                  sb["lon"], sb["lat"]) * 1000.0
                            dist_px = dist_m / mpp
                            if dist_px > ra + rb + min_spacing_px:
                                continue
                            # B's contribution only propagates down to zooms
                            # where B itself would render. At zooms below B's
                            # effective minzoom, B isn't visible — absorbing
                            # it there has no visual meaning and must not
                            # inflate the absorber's debug score.
                            z_lo_start = max(7, sb["eff_minzoom"])
                            for z_lo in range(z_lo_start, z + 1):
                                sa["score"][z_lo] += sb["score"][z_lo]
                                sb["alive"][z_lo] = False
                                # Merge absorbed lines into the absorber AT
                                # THIS zoom only — the popup at zoom k only
                                # shows lines folded in at or above k, not
                                # the union across every zoom.
                                # Dedup by (ref, mode) at this zoom.
                                sa_lines = sa["lines_per_z"][z_lo]
                                existing_keys = {(ln.get("ref", ""), ln.get("mode", ""))
                                                 for ln in sa_lines}
                                merged_any = False
                                for ln in sb["lines_per_z"][z_lo]:
                                    key = (ln.get("ref", ""), ln.get("mode", ""))
                                    if key in existing_keys:
                                        continue
                                    existing_keys.add(key)
                                    sa_lines.append(ln)
                                    sa["lines_dirty"] = True
                                    merged_any = True
                                # Departures/hour at this zoom: sum absorbee
                                # into absorber. Same principle as lines —
                                # the popup at zoom k reflects everything
                                # folded in at or above k.
                                if sb["dep_hr_per_z"][z_lo] > 0.0:
                                    sa["dep_hr_per_z"][z_lo] += sb["dep_hr_per_z"][z_lo]
                                    sa["dep_hr_dirty"] = True
                            sb["absorbed_max_z"] = (z if sb["absorbed_max_z"] is None
                                                    else max(sb["absorbed_max_z"], z))
                            absorbed_any = True
                            n_absorptions += 1
                            # Absorber diameter does NOT grow with score.
                            # `ra` stays as the tier's fixed radius at z.
            if not absorbed_any:
                break

    n_full = n_partial = n_lines_rewritten = 0
    for s in states:
        feat = dot_features[s["idx"]]
        p = feat["properties"]
        for z in range(7, 14):
            p[f"score_z{z}"] = round(s["score"][z], 4)
        if s["absorbed_max_z"] is not None:
            new_minzoom = s["absorbed_max_z"] + 1
            tipp = feat.setdefault("tippecanoe", {})
            old_minzoom = int(tipp.get("minzoom", 0))
            tipp["minzoom"] = max(old_minzoom, new_minzoom)
            if new_minzoom >= 14:
                n_full += 1
            else:
                n_partial += 1
        if s["lines_dirty"]:
            # Per-zoom lines_json: each `lines_json_zN` reflects the lines
            # this dot represents at zoom N (base lines plus everything
            # absorbed at or above N). Base `lines_json` is left untouched
            # — the pill-zoom layer (z=14+) reads it and shows the dot's
            # native lines without any far-zoom dedup growth.
            for z in range(7, 14):
                lns_sorted = sorted(s["lines_per_z"][z], key=lambda ln: (
                    MODE_RANK.get(ln.get("mode", ""), 99),
                    ln.get("ref", "")))
                p[f"lines_json_z{z}"] = json.dumps(lns_sorted, ensure_ascii=False)
            n_lines_rewritten += 1
        if s["dep_hr_dirty"]:
            # Per-zoom dep_hr mirrors lines_json_zN: the popup at zoom k
            # shows the absorber-plus-absorbed departures folded in at or
            # above k.
            for z in range(7, 14):
                p[f"dep_hr_z{z}"] = round(s["dep_hr_per_z"][z], 3)
    print(f"  Dedup: {n_absorptions:,} absorptions "
          f"({n_full:,} stops fully absorbed at far-zoom, "
          f"{n_partial:,} partially absorbed, "
          f"{n_lines_rewritten:,} absorber popups extended)")
