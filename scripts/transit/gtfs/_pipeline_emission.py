import json
# ── Emit features ────────────────────────────────────────────────────────
features: list = []
line_stops_out: dict = {}
pfaedle_unrouted: list = []
trip_groups_diag: list = []
matched_tg_keys: set = set()
feature_id_counter = 0
# Per-(tg_key, var_key) emission outcome for the comprehensive diagnostic.
diag_emission: dict = {}

# ── City-bus promotion (citybus-landuse-promotion.md) ────────────────────
# Landuse-based regional_bus → city_bus promotion, evaluated per line
# group directly after the number-based rule inside the variant loop.
from gtfs.citybus_promotion import (
    evaluate_polylines as _promo_evaluate,
    load_builtup_grid as _promo_load_grid,
    load_promotion_cfg as _promo_load_cfg,
    pass_threshold as _promo_pass_threshold,
)

_promo_cfg = _promo_load_cfg(load_cfg())
_builtup_cells = _promo_load_grid()
print(f"City-bus promotion: {len(_builtup_cells):,} built-up landuse cells")
_promo_cache: dict = {}
citybus_promotion_diag: list = []


def _citybus_promoted(tg_key, all_trips, short_name, agency_id) -> bool:
    """Group-memoized promotion decision over the union of every shape any
    trip of the group uses, so all variants (directions, branches) agree.
    Groups with seasonal-rescue variants are never promoted — the rescue
    exists only for regional buses and rescued variants that classify as
    city bus get dropped, so promotion would delete the line from the map
    instead of recoloring it (concept § constraints)."""
    if tg_key in _promo_cache:
        return _promo_cache[tg_key]
    promoted = False
    if not regional_bus_rescued.get(tg_key):
        shape_ids = {trip_lookup.get(t, {}).get("shape_id", "")
                     for t in all_trips}
        polylines = [shapes[s] for s in shape_ids if s and s in shapes]
        if polylines:
            share, spread = _promo_evaluate(polylines, _builtup_cells)
            threshold = _promo_pass_threshold(spread, _promo_cfg)
            promoted = share >= threshold
            citybus_promotion_diag.append({
                "ref": short_name,
                "agency_id": agency_id,
                "agency_name": agency_names.get(agency_id, ""),
                "trip_group_id": tg_key[2],
                "share": round(share, 4),
                "spread_km": round(spread, 2),
                "threshold": round(threshold, 4),
                "promoted": promoted,
            })
    _promo_cache[tg_key] = promoted
    return promoted


_ZERO_FREQ = {"f_core": 0.0, "f_eve": 0.0, "f_we": 0.0}
for (line_key, agency_id, tg_id), variant_map in drawable_groups.items():
    short_name, long_name, bucket = line_key
    all_trips = [tid for trips in variant_map.values() for tid in trips]

    tg_key = (line_key, agency_id, tg_id)
    # Group-level raw_freq retained for the diagnostic and as fallback.
    raw_freq  = tg_freq.get(tg_key, _ZERO_FREQ)
    # Winning window for this group's freq gate determines which window's
    # per-variant freq drives thickness — same window everywhere in the
    # group so a seasonal-rescued group's thickness reflects in-season
    # cadence per direction.
    gate_window = freq_gate_window_passed.get(tg_key) or "annual"
    speed_kmh = tg_speed.get(tg_key)
    for var_key, trip_ids in variant_map.items():
        merged_set, direction_key = var_key
        # Pick the rep from the most common platform sub-variant within
        # this direction so the drawn line tracks the dominant platform
        # pattern of the direction it represents. Ties resolve by smallest
        # min trip_id for stable output.
        by_raw: dict = defaultdict(list)
        for tid in trip_ids:
            raw = frozenset(_trip_stops_export.get(tid, ()))
            by_raw[raw].append(tid)
        popular_raw = sorted(
            by_raw.keys(),
            key=lambda r: (
                -sum(_trip_weight_export.get(t, 1) for t in by_raw[r]),
                min(by_raw[r]),
            ),
        )[0]
        popular_trips = by_raw[popular_raw]
        rep_tid = best_trip_in_shape_group(popular_trips, trip_lookup, svc_dates)
        rep_trip = trip_lookup.get(rep_tid, {})
        stop_ids = _trip_stops_export.get(rep_tid, [])

        # Shape fallback: prefer the popular sub-variant; fall back across
        # the rest of the direction sub-partition so a direction where
        # pfaedle routed only an unusual platform isn't silently dropped.
        popular_set = set(popular_trips)
        other_trips = [t for t in trip_ids if t not in popular_set]
        candidates = (
            [rep_tid]
            + [t for t in popular_trips if t != rep_tid]
            + other_trips
        )
        shape_id = ""
        for cand_tid in candidates[:51]:
            sid = trip_lookup.get(cand_tid, {}).get("shape_id", "")
            if sid and sid in shapes:
                shape_id = sid
                break

        route_type = (route_lookup.get(rep_trip.get("route_id", ""), {})
                      .get("type", ""))
        mountain_origin = _mountain_origin(bucket, route_type)
        direction_key_str = f"{direction_key[0]}-{direction_key[1]}"

        polyline = []
        geometry_source = "pfaedle"
        if shape_id:
            polyline = [list(p) for p in shapes[shape_id]]
            length_km = polyline_length_km(polyline)
        elif route_type in _STRAIGHT_LINE_FALLBACK_ROUTE_TYPES:
            polyline = stops_to_polyline(stop_ids, stop_coords)
            length_km = polyline_length_km(polyline)
            geometry_source = "straight_line_fallback"
        else:
            pfaedle_unrouted.append({
                "trip_id": rep_tid,
                "route_id": rep_trip.get("route_id", ""),
                "short_name": short_name,
                "long_name": long_name,
                "bucket": bucket,
                "trip_group_id": tg_id,
                "direction_key": direction_key_str,
            })
            diag_emission[(tg_key, var_key)] = {
                "feature_emitted": False,
                "exclusion_reason": "pfaedle_unrouted",
                "rep_trip_id": rep_tid, "shape_id": "",
                "n_coords": 0, "line_km": 0.0,
            }
            continue

        if len(polyline) < 2:
            diag_emission[(tg_key, var_key)] = {
                "feature_emitted": False,
                "exclusion_reason": "polyline_too_short",
                "rep_trip_id": rep_tid, "shape_id": shape_id,
                "n_coords": len(polyline), "line_km": round(length_km, 2),
            }
            continue

        # Final mode classification.
        mode = gtfs_to_mode(bucket, agency_id,
                            short_name=short_name, length_km=length_km,
                            route_type=route_type)

        # Landuse-based promotion directly after the number rule
        # (citybus-landuse-promotion.md).
        if mode == "regional_bus" and _citybus_promoted(
                tg_key, all_trips, short_name, agency_id):
            mode = "bus"

        # Drop seasonal-rescue bus variants that landed in city `bus`.
        if (var_key in regional_bus_rescued.get(tg_key, ())
                and mode == "bus"):
            diag_emission[(tg_key, var_key)] = {
                "feature_emitted": False,
                "exclusion_reason": "seasonal_rescue_city_bus",
                "rep_trip_id": rep_tid, "shape_id": shape_id,
                "n_coords": len(polyline), "line_km": round(length_km, 2),
            }
            continue

        # Per-variant freq for thickness — see
        # .claude/concepts/seasonal-regional-bus-rescue.md
        # § "Per-variant freq for line thickness". Falls back to group
        # freq if the variant has no per-variant data (shouldn't happen
        # since trip_buf populates both, but safe).
        var_seasonal = var_freq_seasonal.get((tg_key, var_key)) or {}
        variant_raw_freq = var_seasonal.get(gate_window) \
            or var_seasonal.get("annual") or _ZERO_FREQ
        freq_score = compute_freq_score(variant_raw_freq, mode)
        variant_f_weighted = weighted_freq(variant_raw_freq)

        color      = speed_to_color(mode, speed_kmh)
        width_base = score_to_width_base(freq_score, mode)

        feature_id_counter += 1
        feat_id = f"tg{tg_id}_s{feature_id_counter}"

        # Geometry — always LineString for new emission.
        geometry = {"type": "LineString", "coordinates": polyline}
        props = {
            "osm_id":       feat_id,
            "ref":          short_name,
            "name":         long_name,
            "operator":     agency_names.get(agency_id, ""),
            "agency_id":    agency_id,
            "mode":         mode,
            "route_type":   route_type,
            "freq_score":   freq_score,
            "f_weighted":   round(variant_f_weighted, 4),
            "speed_kmh":    speed_kmh,
            "color":        color,
            "width_base":   width_base,
            "line_km":      round(length_km, 1),
            "direction_id": rep_trip.get("direction_id", ""),
            "direction_key": direction_key_str,
            "trip_group_id": tg_id,
            "shape_id":     shape_id or "",
            "gtfs_matched": True,
            "geometry_source": geometry_source,
        }
        if mountain_origin:
            props["mountain_origin"] = mountain_origin
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": props,
        })

        # Per-feature stops.
        stop_entries: list = []
        for sid in stop_ids:
            c = stop_coords.get(sid) or stop_coords.get(sid.split(":")[0])
            if c:
                stop_entries.append([c[0], c[1], sid])
        line_stops_out[feat_id] = {
            "osm_ref": short_name,
            "stops":   stop_entries,
            "gtfs_ref": short_name,
            "direction_key": direction_key_str,
        }

        matched_tg_keys.add(tg_key)
        diag_emission[(tg_key, var_key)] = {
            "feature_emitted": True,
            "exclusion_reason": None,
            "rep_trip_id": rep_tid,
            "shape_id": shape_id,
            "n_coords": len(polyline),
            "line_km": round(length_km, 2),
            "feature_id": feat_id,
            "geometry_source": geometry_source,
        }

    # Diagnostic snapshot for this trip group.
    trip_groups_diag.append({
        "short_name":    short_name,
        "long_name":     long_name,
        "bucket":        bucket,
        "agency_id":     agency_id,
        "agency_name":   agency_names.get(agency_id, ""),
        "trip_group_id": tg_id,
        "trip_count":    len(all_trips),
        "variant_count": len(variant_map),
    })

# Aerial dedup: collapse duplicate haul-cable features (route_type 5/6)
# that share a ref. Drop now-orphaned line_stops entries.
features = deduplicate_mountain(features)
kept_ids = {f["properties"]["osm_id"] for f in features}
line_stops_out = {oid: v for oid, v in line_stops_out.items() if oid in kept_ids}

# ── Per-stop score + tier for far-zoom dot rendering ─────────────────────
# See .claude/concepts/stops-far-zoom-dot-redesign.md. The score is
# aggregated per parent UIC using
#
#   effective_weight × terminal_multiplier × (1 + freq_score)
#
# where `effective_weight` = `mode_weights[mode]`, replaced by the
# matching `train_class_weights` entry for `mode == train`.
# `terminal_multiplier` fires at the feature's first / last stop.
# Per UIC, dedup is by `ref` only — sub-variants of the same line
# collapse to one entry (their highest contribution wins). Loop
# pass-throughs collapse via `seen_uics` — one entry per feature per UIC.
#
# The score feeds the tier assignment (top-down first-match) alongside
# the base line composition at the UIC. Tier is fixed at emit time and
# not touched by the visual dedup pass in step 07.
sds_cfg = cfg.get("stop_dot_sizing") or {}
stop_size_mw = sds_cfg.get("mode_weights") or {}
terminal_multiplier = float(sds_cfg.get("terminal_multiplier", 1.0))
train_class_weights = sds_cfg.get("train_class_weights") or {}
train_default_weight = float(stop_size_mw.get("train", 0.0))
ic_weight = float(train_class_weights.get("ic", train_default_weight))
ir_weight = float(train_class_weights.get("ir", train_default_weight))
re_weight = float(train_class_weights.get("re", train_default_weight))
ic_prefixes = tuple(
    p.upper() for p in
    ((cfg.get("zoom_level_rules") or {}).get("intercity_route_prefixes") or [])
)

tier_thresh = sds_cfg.get("tier_thresholds") or {}
th_major_train      = float(tier_thresh.get("major_train",     100))
th_main_train       = float(tier_thresh.get("main_train",       40))
th_important_train  = float(tier_thresh.get("important_train",  20))
th_train_station    = float(tier_thresh.get("train_station",    10))
th_major_mountain   = float(tier_thresh.get("major_mountain",    2.0))
th_major_hub        = float(tier_thresh.get("major_hub",        15))
th_big_station      = float(tier_thresh.get("big_station",       6))
th_normal_stop      = float(tier_thresh.get("normal_stop",       1.5))

mlc_cfg = sds_cfg.get("mountain_line_count") or {}
mlc_mountain_through = float(mlc_cfg.get("mountain_through", 1.0))
mlc_mountain_term    = float(mlc_cfg.get("mountain_term",    0.9))
mlc_ferry            = float(mlc_cfg.get("ferry",            1.0))
mlc_tram_bus         = float(mlc_cfg.get("tram_bus",         0.5))

tier_overrides: dict = {}
for entry in (sds_cfg.get("tier_overrides") or []):
    try:
        tier_overrides[str(entry["uic"])] = str(entry["tier"])
    except (KeyError, TypeError):
        continue

def _is_ic_ref(ref_upper: str) -> bool:
    if not (ref_upper and ic_prefixes):
        return False
    return any(ref_upper.startswith(pfx) for pfx in ic_prefixes)

def _uic_of(entry):
    sid = entry[2] if len(entry) >= 3 else ""
    if not sid:
        return ""
    meta = stop_meta.get(sid) or stop_meta.get(sid.split(":")[0])
    parent = meta["parent"] if meta else ""
    return parent if parent else sid.split(":")[0]

# stop_contribs[uic][ref] = max contribution at this uic for that line.
# stop_line_index[uic][(ref, mode)] = True if any variant terminates here.
stop_contribs: dict = defaultdict(lambda: defaultdict(float))
stop_line_index: dict = defaultdict(dict)
for f in features:
    p = f["properties"]
    mode = p.get("mode", "")
    mw = float(stop_size_mw.get(mode, 0.0))
    ref = p.get("ref", "") or ""
    ref_upper = ref.upper()
    if mode == "train" and ref_upper:
        if _is_ic_ref(ref_upper):
            mw = ic_weight
        elif ref_upper.startswith("IR"):
            mw = ir_weight
        elif ref_upper.startswith("RE"):
            mw = re_weight
        elif ref_upper.startswith("R") and (
                len(ref_upper) == 1 or not ref_upper[1].isalpha()):
            mw = re_weight
    fs = float(p.get("freq_score", 0.0))
    base_contribution = mw * (1.0 + fs)
    feat_id = p["osm_id"]
    feat_stops = (line_stops_out.get(feat_id) or {}).get("stops") or []
    if len(feat_stops) < 2:
        continue
    first_uic = _uic_of(feat_stops[0])
    last_uic = _uic_of(feat_stops[-1])
    seen_uics: set = set()
    for entry in feat_stops:
        uic = _uic_of(entry)
        if not uic or uic in seen_uics:
            continue
        seen_uics.add(uic)
        is_terminus = (uic == first_uic) or (uic == last_uic)

        # Line composition index (mode-aware, needed for tier evaluation).
        # Terminal-status is aggregated over every variant of the line at
        # this stop: if ANY variant terminates, treat as terminal for the
        # mountain-line count.
        line_mode_key = (ref, mode)
        prev_term = stop_line_index[uic].get(line_mode_key, False)
        stop_line_index[uic][line_mode_key] = prev_term or is_terminus

        # Contribution to score. Only non-zero mode-weights contribute,
        # but the line composition index above must still register a
        # zero-weighted mode (there is no such mode today; every mode in
        # `mode_weights` is > 0, so this is defensive).
        if mw <= 0:
            continue
        mult = terminal_multiplier if is_terminus else 1.0
        contribution = base_contribution * mult
        cur = stop_contribs[uic].get(ref, 0.0)
        if contribution > cur:
            stop_contribs[uic][ref] = contribution

stop_score: dict = {uic: sum(cs.values()) for uic, cs in stop_contribs.items()}
# UICs that appear only through the line-composition index (mw == 0 mode)
# still get a zero score so they land in `small_*` tiers rather than
# falling out entirely.
for uic in stop_line_index:
    stop_score.setdefault(uic, 0.0)

def _assign_tier(uic: str, score: float, line_idx: dict) -> str:
    override = tier_overrides.get(uic)
    if override:
        return override
    modes_at = {m for (_r, m) in line_idx}
    has_train    = "train" in modes_at
    has_metro    = "metro" in modes_at
    has_mountain = "mountain" in modes_at
    has_ferry    = "ferry" in modes_at
    has_tram_bus = bool(modes_at & {"tram", "bus", "regional_bus"})
    has_ic = any(m == "train" and _is_ic_ref((r or "").upper())
                 for (r, m) in line_idx)

    if has_train and has_ic and score >= th_major_train:
        return "major_train"
    if has_train and has_ic and score >= th_main_train:
        return "main_train"
    if has_train and score >= th_important_train:
        return "important_train"
    if has_train and score >= th_train_station:
        return "train_station"
    if has_train:
        return "small_train"
    # (metro tiers deferred — a metro-only stop currently lands in
    # `major_hub` via the metro-OR placeholder below.)
    if has_mountain:
        mlc = 0.0
        for (_r, m), terminates in line_idx.items():
            if m == "mountain":
                mlc += mlc_mountain_term if terminates else mlc_mountain_through
            elif m == "ferry":
                mlc += mlc_ferry
            elif m in ("tram", "bus", "regional_bus"):
                mlc += mlc_tram_bus
        if mlc >= th_major_mountain:
            return "major_mountain"
        return "mountain_stop"
    if has_ferry:
        return "ferry_stop"
    if score >= th_major_hub or has_metro:
        return "major_hub"
    if score >= th_big_station:
        return "big_station"
    if score >= th_normal_stop:
        return "normal_stop"
    if has_tram_bus:
        return "small_bus"
    return "small_bus"

stop_size_records: dict = {}
tier_counts: dict = defaultdict(int)
for uic in sorted(set(stop_score.keys()) | set(stop_line_index.keys())):
    score = stop_score.get(uic, 0.0)
    tier = _assign_tier(uic, score, stop_line_index.get(uic, {}))
    stop_size_records[uic] = {"score": round(score, 4), "tier": tier}
    tier_counts[tier] += 1
OUT_STOP_SCORES.write_text(json.dumps(stop_size_records, ensure_ascii=False))
print(f"  {len(stop_size_records):,} stops scored → {OUT_STOP_SCORES.name}")

# Tier distribution — quick sanity check.
_tier_order = ["major_train", "main_train", "important_train",
               "train_station", "small_train",
               "major_mountain", "mountain_stop", "ferry_stop",
               "major_hub", "big_station", "normal_stop", "small_bus"]
dist_parts = [f"{t}={tier_counts[t]:,}" for t in _tier_order if tier_counts.get(t)]
print(f"  Tier distribution: {', '.join(dist_parts)}")

# Print percentile bands for tuning the threshold cutoffs.
scored_only = [v["score"] for v in stop_size_records.values() if v["score"] > 0]
if scored_only:
    sorted_scores = sorted(scored_only)
    n = len(sorted_scores)
    def _pct(p):
        return sorted_scores[max(0, min(n - 1, int(p * n)))]
    print(f"  stop_score percentiles (non-zero): "
          f"p20 = {_pct(0.20):.2f}, p50 = {_pct(0.50):.2f}, "
          f"p80 = {_pct(0.80):.2f}, p95 = {_pct(0.95):.2f}, "
          f"p99 = {_pct(0.99):.2f}, max = {sorted_scores[-1]:.2f}")

# ── Salience score (geometric, linear-falloff) ───────────────────────────
# See .claude/concepts/zoom-level-rules.md § "Salience score".
# For each line L: sample every `sample_step_m` along its polyline; for
# each sample, find every other line whose mode is in comparators(L)
# whose polyline passes within `radius` of the sample; each match
# contributes (1 − distance / radius); the sample's score is the sum.
# competition_count(L) = mean of per-sample scores.
print("\nComputing line salience (linear-falloff competition density)...")
zr_cfg = _zoom_rules_cfg()
sal_cfg = zr_cfg.get("salience") or {}
sample_step_m = float(sal_cfg.get("sample_step_m", 1000.0))
radius_m_by_mode = {m: float(v) for m, v in
                    (sal_cfg.get("radius_m") or {}).items()}
comparators_raw = sal_cfg.get("comparators") or {}
comparators_by_mode = {m: frozenset(ms)
                       for m, ms in comparators_raw.items()}

def _comparators_for(mode: str) -> frozenset:
    return comparators_by_mode.get(mode, frozenset({mode}))

# Per-feature f_weighted and mode mapping for downstream use.
tg_lookup: dict = {}
for tg_key in tg_freq.keys():
    line_key, aid, tg_id = tg_key
    sn, _ln, _bkt = line_key
    tg_lookup[(sn, aid, tg_id)] = tg_key

f_weighted_by_oid: dict = {}
mode_by_oid: dict = {}
for f in features:
    p = f["properties"]
    # f_weighted was set per-variant during emission — see the
    # per-variant freq concept. Use that value rather than recomputing
    # from the group-level tg_freq.
    fw = float(p.get("f_weighted", 0.0))
    f_weighted_by_oid[p["osm_id"]] = fw
    mode_by_oid[p["osm_id"]] = p["mode"]

# Cache polylines (flat list of (lon, lat)) by oid.
polyline_by_oid: dict = {}
for f in features:
    oid = f["properties"]["osm_id"]
    coords = f["geometry"]["coordinates"]
    if f["geometry"]["type"] == "MultiLineString":
        flat = [tuple(c) for seg in coords for c in seg]
    else:
        flat = [tuple(c) for c in coords]
    polyline_by_oid[oid] = flat

# Sample each polyline every sample_step_m. Stores (lon, lat) per sample.
samples_by_oid: dict = {}
for oid, poly in polyline_by_oid.items():
    if len(poly) < 2:
        samples_by_oid[oid] = []
        continue
    seg_lens_km = []
    for i in range(len(poly) - 1):
        seg_lens_km.append(
            haversine_km(poly[i][0], poly[i][1],
                         poly[i + 1][0], poly[i + 1][1]))
    total_km = sum(seg_lens_km)
    if total_km <= 0:
        samples_by_oid[oid] = [poly[0]]
        continue
    step_km = sample_step_m / 1000.0
    n_samples = max(1, int(total_km / step_km))
    # Distribute n_samples evenly along the polyline (excluding the very
    # endpoints to keep samples representative of the line's "middle").
    # First sample at step_km/2, then every step_km.
    targets = [(i + 0.5) / n_samples * total_km for i in range(n_samples)]
    out = []
    cum = 0.0
    seg = 0
    for t in targets:
        while seg < len(seg_lens_km) - 1 and cum + seg_lens_km[seg] < t:
            cum += seg_lens_km[seg]
            seg += 1
        seg_len = seg_lens_km[seg] or 1e-12
        frac = max(0.0, min(1.0, (t - cum) / seg_len))
        lon = poly[seg][0] + (poly[seg + 1][0] - poly[seg][0]) * frac
        lat = poly[seg][1] + (poly[seg + 1][1] - poly[seg][1]) * frac
        out.append((lon, lat))
    samples_by_oid[oid] = out

# Build a grid index of every sample point keyed by mode for fast
# radius-bounded lookup. Cell size = 1000 m (cuts into degree-equivalents
# at CH latitude).
GRID_M = 1000.0
# Use CH-centric latitude for cell sizing.
lat0 = 46.8
cos_lat0 = cos(radians(lat0))
cell_lat_deg = GRID_M / _M_PER_DEG
cell_lon_deg = cell_lat_deg / cos_lat0

grid_by_mode: dict = defaultdict(lambda: defaultdict(list))
for oid, samples in samples_by_oid.items():
    mode = mode_by_oid.get(oid, "")
    for lon, lat in samples:
        cx = int(floor(lon / cell_lon_deg))
        cy = int(floor(lat / cell_lat_deg))
        grid_by_mode[mode][(cx, cy)].append((oid, lon, lat))

competition_count_by_oid: dict = {}
for oid, samples in samples_by_oid.items():
    if not samples:
        competition_count_by_oid[oid] = 0.0
        continue
    my_mode = mode_by_oid.get(oid, "")
    my_comparators = _comparators_for(my_mode)
    radius_m = radius_m_by_mode.get(my_mode, 5000.0)
    cells_radius = int(ceil(radius_m / GRID_M))
    radius_m_sq = radius_m * radius_m
    per_sample_scores: list = []
    for lon, lat in samples:
        cx = int(floor(lon / cell_lon_deg))
        cy = int(floor(lat / cell_lat_deg))
        nearest_by_other: dict = {}
        for comp_mode in my_comparators:
            g = grid_by_mode.get(comp_mode)
            if not g:
                continue
            for dx in range(-cells_radius, cells_radius + 1):
                for dy in range(-cells_radius, cells_radius + 1):
                    for (other_oid, olon, olat) in g.get((cx + dx, cy + dy), ()):
                        if other_oid == oid:
                            continue
                        mdx = (olon - lon) * cos_lat0 * _M_PER_DEG
                        mdy = (olat - lat) * _M_PER_DEG
                        d_sq = mdx * mdx + mdy * mdy
                        if d_sq > radius_m_sq:
                            continue
                        prev = nearest_by_other.get(other_oid)
                        if prev is None or d_sq < prev:
                            nearest_by_other[other_oid] = d_sq
        score = 0.0
        for d_sq in nearest_by_other.values():
            d = sqrt(d_sq)
            score += 1.0 - d / radius_m
        per_sample_scores.append(score)
    competition_count_by_oid[oid] = (
        sum(per_sample_scores) / len(per_sample_scores)
        if per_sample_scores else 0.0
    )

# Per-mode normalisation: lowest competition → salience = 1.0; highest
# → 0.0; intermediate linear.
salience_by_oid: dict = {}
by_mode_for_sal: dict = defaultdict(list)
for oid, cc in competition_count_by_oid.items():
    by_mode_for_sal[mode_by_oid.get(oid, "")].append(oid)
for mode, oids in by_mode_for_sal.items():
    ccs = [competition_count_by_oid.get(o, 0.0) for o in oids]
    c_min, c_max = min(ccs), max(ccs)
    span = c_max - c_min
    for o in oids:
        if span <= 0:
            salience_by_oid[o] = 1.0
        else:
            cc = competition_count_by_oid.get(o, 0.0)
            salience_by_oid[o] = 1.0 - (cc - c_min) / span

for f in features:
    p = f["properties"]
    oid = p["osm_id"]
    p["salience"] = round(float(salience_by_oid.get(oid, 0.0)), 4)
    p["competition_count"] = round(
        float(competition_count_by_oid.get(oid, 0.0)), 4)

# ── Per-mode line rules → candidate min_zoom ────────────────────────────
# See concept § "Per-mode rules". Each rule at level N adds any line
# matching the condition at that level; lines take the smallest such N.
print("Applying per-mode line rules...")
intercity_prefixes = tuple(
    str(p).upper()
    for p in (zr_cfg.get("intercity_route_prefixes") or ["IC", "ICE", "EC"])
)

def _is_intercity_train(ref: str, mode: str) -> bool:
    if mode != "train":
        return False
    r = (ref or "").strip().upper()
    return any(r.startswith(p) for p in intercity_prefixes)

# Salience top-sets per (mode, pct), precomputed once. Used by the
# per-mode rules below.
def _salience_ranked(mode: str) -> list:
    oids = list(by_mode_for_sal.get(mode, []))
    oids.sort(key=lambda o: (
        -salience_by_oid.get(o, 0.0),
        -f_weighted_by_oid.get(o, 0.0),
        o,
    ))
    return oids
_train_top50 = set(_salience_ranked("train")[
    :max(1, int(round(len(by_mode_for_sal.get("train", [])) * 0.50)))])
_rb_top30 = set(_salience_ranked("regional_bus")[
    :max(1, int(round(len(by_mode_for_sal.get("regional_bus", [])) * 0.30)))])
_rb_top50 = set(_salience_ranked("regional_bus")[
    :max(1, int(round(len(by_mode_for_sal.get("regional_bus", [])) * 0.50)))])

# Per-feature spread (km) — geodesic distance between the two stops
# farthest apart on the line. Also per-feature longest-gap (km) — the
# geodesic distance between the two stops that are CONSECUTIVE IN THE
# STOP SEQUENCE and furthest apart. Used by the ferry rule: a lake line's
# reach is described by its longest water hop between piers, not its
# end-to-end spread.
spread_by_oid: dict = {}
longest_gap_by_oid: dict = {}
line_km_by_oid: dict = {}
for f in features:
    oid = f["properties"]["osm_id"]
    line_km_by_oid[oid] = float(f["properties"].get("line_km") or 0.0)
    entry = line_stops_out.get(oid, {})
    stops = entry.get("stops", []) if isinstance(entry, dict) else entry
    if len(stops) < 2:
        spread_by_oid[oid] = 0.0
        longest_gap_by_oid[oid] = 0.0
        continue
    # Brute force O(n^2) for spread — fine for typical n ≤ 100 stops per
    # line.
    max_d = 0.0
    for i in range(len(stops)):
        for j in range(i + 1, len(stops)):
            d = haversine_km(stops[i][0], stops[i][1],
                             stops[j][0], stops[j][1])
            if d > max_d:
                max_d = d
    spread_by_oid[oid] = max_d
    # Longest gap between two stops adjacent in the stop sequence.
    max_gap = 0.0
    for i in range(len(stops) - 1):
        g = haversine_km(stops[i][0], stops[i][1],
                         stops[i + 1][0], stops[i + 1][1])
        if g > max_gap:
            max_gap = g
    longest_gap_by_oid[oid] = max_gap

# Per-mode line-rule evaluator. Returns (min_zoom, rule_label) per oid.
# Levels evaluated bottom-up (lowest first); first matching level wins.
UNREACHABLE_Z = 13  # Lines that match no rule fall here (effectively hidden).

def _candidate_min_zoom_train(oid: str, p: dict) -> tuple:
    ref = p.get("ref", "")
    if _is_intercity_train(ref, "train"):
        return 4, "intercity"
    if line_km_by_oid.get(oid, 0.0) >= 30.0 and oid in _train_top50:
        return 5, "length>=30km AND salience top50%"
    return 6, "all remaining"

def _candidate_min_zoom_metro(oid: str, p: dict) -> tuple:
    if spread_by_oid.get(oid, 0.0) >= 20.0:
        return 8, "spread>=20km"
    return 9, "all remaining"

def _candidate_min_zoom_ferry(oid: str, p: dict) -> tuple:
    g = longest_gap_by_oid.get(oid, 0.0)
    if g >= 20.0: return 6, "longest_gap>=20km"
    if g >= 10.0: return 7, "longest_gap>=10km"
    if g >=  5.0: return 8, "longest_gap>=5km"
    return 9, "all remaining"

def _candidate_min_zoom_mountain(oid: str, p: dict) -> tuple:
    L = line_km_by_oid.get(oid, 0.0)
    if L >= 15.0:  return  6, "length>=15km"
    if L >=  8.0:  return  7, "length>=8km"
    if L >=  5.0:  return  8, "length>=5km"
    if L >=  2.0:  return  9, "length>=2km"
    if L >=  0.5:  return 10, "length>=0.5km"
    return 11, "all remaining"

def _candidate_min_zoom_regional_bus(oid: str, p: dict) -> tuple:
    s = spread_by_oid.get(oid, 0.0)
    if s >= 25.0 and oid in _rb_top30:
        return 7, "spread>=25km AND salience top30%"
    if s >= 15.0 and oid in _rb_top50:
        return 8, "spread>=15km AND salience top50%"
    if s >= 5.0:
        return 9, "spread>=5km"
    return 10, "all remaining"

def _candidate_min_zoom_tram(oid: str, p: dict) -> tuple:
    if spread_by_oid.get(oid, 0.0) >= 8.0:
        return 9, "spread>=8km"
    return 10, "all remaining"

def _candidate_min_zoom_bus(oid: str, p: dict) -> tuple:
    if spread_by_oid.get(oid, 0.0) >= 5.0:
        return 10, "spread>=5km"
    return 11, "all remaining"

RULE_BY_MODE = {
    "train":        _candidate_min_zoom_train,
    "metro":        _candidate_min_zoom_metro,
    "ferry":        _candidate_min_zoom_ferry,
    "mountain":     _candidate_min_zoom_mountain,
    "regional_bus": _candidate_min_zoom_regional_bus,
    "tram":         _candidate_min_zoom_tram,
    "bus":          _candidate_min_zoom_bus,
}

candidate_mz_by_oid: dict = {}
rule_label_by_oid: dict = {}
for f in features:
    p = f["properties"]
    oid = p["osm_id"]
    mode = p["mode"]
    fn = RULE_BY_MODE.get(mode)
    if fn is None:
        candidate_mz_by_oid[oid] = UNREACHABLE_Z
        rule_label_by_oid[oid] = "no rule for mode"
        continue
    mz, label = fn(oid, p)
    candidate_mz_by_oid[oid] = mz
    rule_label_by_oid[oid] = label
