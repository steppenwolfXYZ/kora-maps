"""Per-cluster line accounting: line count, minzoom, luminance, dominant
line, cluster_lines, indicator features."""
from collections import defaultdict
from math import atan2, cos, degrees, floor, log, pi, radians, sin, sqrt

from _state import *  # noqa: F401,F403
from _state import _stop_wb  # underscore names skipped by *
from geometry import (
    _cum_dist_m, _directional_tangent_at, _interp_at, _project_meters,
    flatten_coords, haversine_km,
)


def count_unique_lines(cluster_stops):
    """
    Count distinct OSM line IDs in a cluster.
    Each direction of a tram/bus line has its own osm_id, so both directions
    of a bidirectional line count as 2 — correctly triggering a pill.
    """
    return len(set(s.get("osm_id", str(id(s))) for s in cluster_stops))


def pill_minzoom(mode, stop_count):
    """
    Return the zoom level at which pills appear for a stop cluster,
    or None if the cluster should not get a pill (single line).

    Uniform z14 for every mode — see `stops-pill-zoom.md`
    § "Dot-to-pill zoom switch". Design bands A/B/C tag features with
    per-band minzoom/maxzoom on top of this — see the pill-design-band
    bake in `main()`.
    """
    if stop_count < 2:
        return None
    return 14


def color_luminance(hex_color: str) -> float:
    """Perceived luminance of a hex color (lower = darker)."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return 1.0
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def dominant_line(stops_in_cluster):
    """
    Return (color, mode, max_width_base, dominant_stop).
    - Mode: highest-priority type present (MODE_RANK; lower = higher priority; strict).
    - Color: darkest (lowest luminance) among stops of that type.
    - width_base: max across ALL stops, regardless of type.
    """
    best_rank = min(MODE_RANK.get(s["mode"], 99) for s in stops_in_cluster)
    dom_stops = [s for s in stops_in_cluster if MODE_RANK.get(s["mode"], 99) == best_rank]

    best_lum   = 2.0
    best_color = "#888888"
    best_stop  = dom_stops[0]
    for s in dom_stops:
        lum = color_luminance(s["color"])
        if lum < best_lum:
            best_lum   = lum
            best_color = s["color"]
            best_stop  = s

    max_wb = max(s["width_base"] for s in stops_in_cluster)
    return best_color, best_stop["mode"], max_wb, best_stop


def _cluster_station_uics(cluster_stops):
    """Set of parent-UIC / station identifiers this cluster represents.

    Used by the tooltip builder to locate the current station inside each
    variant's stop sequence. Includes both the GTFS parent_station and the
    base (colon-stripped) stop_id, so lookups match regardless of whether
    the variant's sequence carries a parent-station id or a platform-suffixed
    stop_id.
    """
    uics: set = set()
    for s in cluster_stops:
        parent = s.get("parent_station", "")
        if parent:
            uics.add(parent)
        sid = s.get("stop_id", "")
        if sid:
            from gtfs.stop_identity import merge_key_of
            uics.add(merge_key_of(sid))
    return uics


def _station_line_tooltip(variants, station_uics, station_name=""):
    """Build the A ↔ B tooltip for one (ref, mode) group at a station.

    See `.claude/concepts/popups.md` § Line tooltip. Groups variants by which
    downstream direction they head from the station, applies subsumption
    within each direction (drop variants whose terminus is intermediate on a
    longer variant's tail), then joins the two sides with `↔`.

    When the station is a terminus of any variant (line starts or ends
    here), the station itself is added as the missing side so the tooltip
    always shows both endpoints of the line — e.g. at Klein Matterhorn
    the aerial reads "Zermatt ↔ Klein Matterhorn" rather than just
    "Zermatt".
    """
    downstream = []
    station_is_terminus = False
    for v in variants:
        uics = v.get("parent_uics") or []
        pos = -1
        for i, u in enumerate(uics):
            if u and u in station_uics:
                pos = i
                break
        if pos < 0:
            continue
        # Station being FIRST or LAST of any variant qualifies as a
        # terminus role for this line — record it before the
        # "no downstream from LAST position" skip below drops the variant.
        if pos == 0 or pos == len(uics) - 1:
            station_is_terminus = True
        if pos >= len(uics) - 1:
            continue
        forward_tail = uics[pos + 1:]
        downstream.append({
            "forward_tail":  forward_tail,
            "terminus_uic":  forward_tail[-1],
            "terminus_name": v.get("last_terminus_name") or "",
        })

    if not downstream:
        return ""

    n = len(downstream)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    tail_sets = [set(v["forward_tail"]) for v in downstream]
    for i in range(n):
        for j in range(i + 1, n):
            if tail_sets[i] & tail_sets[j]:
                union(i, j)

    groups: dict = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    sides: list = []
    for group_indices in groups.values():
        group = [downstream[i] for i in group_indices]
        surviving = []
        for i, v in enumerate(group):
            subsumed = False
            for j, w in enumerate(group):
                if i == j:
                    continue
                if v["terminus_uic"] in set(w["forward_tail"][:-1]):
                    subsumed = True
                    break
            if not subsumed:
                surviving.append(v)
        seen_names: set = set()
        names: list = []
        for v in surviving:
            name = v["terminus_name"]
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            names.append(name)
        if names:
            sides.append(names)

    # If this station is a terminus of any variant (line starts or ends
    # here) and the station name isn't already listed among the
    # downstream sides, add it as the missing side. Restores both
    # endpoints of the line at terminal stations — bus terminals, aerial
    # / funicular / ferry termini.
    if (station_is_terminus and station_name
            and not any(station_name in side for side in sides)):
        sides.append([station_name])

    if not sides:
        return ""
    if len(sides) == 1:
        return " · ".join(sides[0])
    return " ↔ ".join(" · ".join(names) for names in sides)


def cluster_lines(cluster_stops, line_lookup, oids_by_uic=None):
    """
    Return a sorted list of {ref, color, mode, name, tooltip} dicts for all
    distinct lines serving any stop in the cluster, deduped by (ref, mode).
    Both directions of one line merge into a single entry. Sorted by mode
    rank then ref. Tooltip pre-formatted per `.claude/concepts/popups.md`.

    Badges are sourced from `cluster_stops` — a line only gets a badge if
    at least one of its variants survived the visible-stop dedup. The
    tooltip, however, sources its variants from `oids_by_uic` (all variants
    whose stop sequence touches any of this cluster's parent-UICs, no
    dedup applied). That way the tooltip's downstream directions are
    complete even when the "outbound" variant of a terminal-line got
    dropped from the drawn stops by `compute_terminus_skip_oids`.
    Falls back to cluster_stops-only variant sourcing when `oids_by_uic`
    is not supplied.
    """
    groups: dict = defaultdict(list)
    representative: dict = {}
    for s in cluster_stops:
        oid = s.get("osm_id", "")
        if not oid:
            continue
        info = line_lookup.get(oid)
        if not info:
            continue
        ref  = info.get("gtfs_ref") or info.get("ref", "")
        mode = info.get("mode", "")
        key  = (ref, mode)
        groups[key].append(info)
        if key not in representative:
            representative[key] = {
                "ref":       ref,
                "color":     info.get("color", "#888888"),
                "mode":      mode,
                "name":      info.get("name", ""),
                "agency_id": info.get("agency_id", ""),
            }

    station_uics = _cluster_station_uics(cluster_stops)
    station_name = ""
    for s in cluster_stops:
        n = s.get("stop_name") or ""
        if n:
            station_name = n
            break

    # Broaden the per-(ref, mode) variant set for tooltip generation:
    # include every osm_id at any station_uic matching the same (ref, mode),
    # not just those that survived the visible-stop dedup.
    if oids_by_uic:
        for uic in station_uics:
            for oid in oids_by_uic.get(uic, ()):
                info = line_lookup.get(oid)
                if not info:
                    continue
                key = (info.get("gtfs_ref") or info.get("ref", ""),
                       info.get("mode", ""))
                if key not in groups:
                    continue
                if info not in groups[key]:
                    groups[key].append(info)

    entries = []
    for key, variants in groups.items():
        entry = dict(representative[key])
        entry["tooltip"] = _station_line_tooltip(variants, station_uics, station_name)
        # Line-detail-view payload (line-detail-view.md): canonical keys of
        # every variant in this (ref, mode) badge group (normally one key;
        # more when two agencies share ref+mode), the group's union bbox
        # for the camera fit, and the line-global A ↔ B route text (line
        # popup rule — no per-station subsumption).
        entry["keys"] = sorted({line_key_of(v) for v in variants if line_key_of(v)})
        bbox = None
        for v in variants:
            bb = v.get("group_bbox")
            if not bb:
                continue
            if bbox is None:
                bbox = list(bb)
            else:
                bbox = [min(bbox[0], bb[0]), min(bbox[1], bb[1]),
                        max(bbox[2], bb[2]), max(bbox[3], bb[3])]
        if bbox:
            entry["bbox"] = bbox
        entry["route"] = _line_route_text(variants)
        entries.append(entry)
    return sorted(entries, key=lambda x: (MODE_RANK.get(x["mode"], 99), x["ref"]))


def _line_route_text(variants):
    """Line-global route text across all variants of a badge group: the
    unique terminus names, `A ↔ B` when exactly two, ` · `-joined
    otherwise. Mirrors the line popup's client-side rule (popups.md § Line
    popup / Route text per line)."""
    seen: set = set()
    names: list = []
    for v in variants:
        for name in (v.get("first_terminus_name"), v.get("last_terminus_name")):
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    if len(names) == 2:
        return f"{names[0]} ↔ {names[1]}"
    return " · ".join(names)


def cluster_line_keys(cluster_stops, line_lookup, oids_by_uic=None):
    """Membership string for a station cluster (line-detail-view.md):
    ";"-padded canonical keys of every line whose stop sequence touches the
    station. Variants are sourced from `oids_by_uic` (like the tooltip /
    departures logic) so membership doesn't depend on which variants
    survived the visible-stop dedup; falls back to cluster_stops-only
    sourcing when the index is not supplied."""
    oids: set = set()
    for s in cluster_stops:
        oid = str(s.get("osm_id", ""))
        if oid:
            oids.add(oid)
    if oids_by_uic:
        for uic in _cluster_station_uics(cluster_stops):
            oids.update(oids_by_uic.get(uic, ()))
    keys = []
    for oid in oids:
        info = line_lookup.get(oid)
        if info:
            keys.append(line_key_of(info))
    return line_keys_str(keys)


def cluster_departures_per_hour(cluster_stops, line_lookup, oids_by_uic=None):
    """Sum `f_weighted` across every variant of a line serving this station.

    Sources osm_ids from `oids_by_uic` (per-UIC index of variants touching
    the station), not `cluster_stops`, so departures include variants
    dropped from the visible dot pool by the terminus dedup — a station's
    departure count must not depend on which side of a terminal line got
    drawn. Falls back to a cluster_stops-only sum when `oids_by_uic` is
    not supplied.
    """
    if oids_by_uic:
        station_uics = _cluster_station_uics(cluster_stops)
        seen: set = set()
        total = 0.0
        for uic in station_uics:
            for oid in oids_by_uic.get(uic, ()):
                if oid in seen:
                    continue
                seen.add(oid)
                info = line_lookup.get(oid) or {}
                total += float(info.get("f_weighted", 0.0) or 0.0)
        return total

    total = 0.0
    seen = set()
    for s in cluster_stops:
        oid = s.get("osm_id", "")
        if not oid or oid in seen:
            continue
        seen.add(oid)
        info = line_lookup.get(oid) or {}
        total += float(info.get("f_weighted", 0.0) or 0.0)
    return total


def build_indicator_features(stops_at_location, lon, lat, line_lookup,
                              tangent_deg=0.0,
                              parent_width_base=None, parent_mode=None,
                              parent_type="disc", line_keys=None):
    """
    Emit color-indicator Point features for a single rendered location.

    Groups the stops by color-group (per MODE_TO_COLOR_GROUP), picks the
    fastest line (highest freq_score) within each group, and yields one
    Point feature per group at the parent's center coordinate.

    `tangent_deg` is the orientation of the indicator row in degrees
    (clockwise from east in map space, MapLibre `text-rotate` convention).
    Pass the pill's local tangent angle for pill indicators; leave 0 for
    dot / disc indicators (screen-horizontal row).

    `parent_width_base` / `parent_mode`: the parent stop's effective (clamped)
    width_base and mode. Stamped on every emitted indicator so the style
    can size + shrink the row to fit the parent. When omitted, derived
    from `stops_at_location` via dominant_line + the per-mode floor.

    Each feature carries `color`, `slot_units`, `tangent_deg`,
    `n_indicators`, `parent_width_base`. See
    `.claude/concepts/stop-color-indicators.md` and
    `.claude/concepts/stops-pill-zoom-tweaks.md`.
    """
    by_group: dict = {}
    seen_modes_wb: list = []
    for s in stops_at_location:
        oid = str(s.get("osm_id", ""))
        line = line_lookup.get(oid)
        if not line:
            continue
        mode = line.get("mode", "")
        seen_modes_wb.append((mode, float(line.get("width_base", 1.0))))
        group = MODE_TO_COLOR_GROUP.get(mode)
        if not group:
            continue
        fs = line.get("freq_score", 0.0)
        ref = line.get("gtfs_ref") or line.get("ref", "")
        cur = by_group.get(group)
        cand = (fs, ref, line.get("color", "#888888"))
        if cur is None or (fs > cur[0]) or (fs == cur[0] and ref < cur[1]):
            by_group[group] = cand

    if not by_group:
        return []

    if parent_mode is None or parent_width_base is None:
        # Derive from the visible lines at this location.
        if seen_modes_wb:
            dom_rank = min(MODE_RANK.get(m, 99) for m, _ in seen_modes_wb)
            derived_mode = next(m for m, _ in seen_modes_wb
                                if MODE_RANK.get(m, 99) == dom_rank)
            derived_max_wb = max(wb for _, wb in seen_modes_wb)
        else:
            derived_mode = "bus"
            derived_max_wb = 1.0
        if parent_mode is None:
            parent_mode = derived_mode
        if parent_width_base is None:
            parent_width_base = _stop_wb(derived_max_wb, parent_mode)

    # Station membership for the line-detail-view filter. Callers pass the
    # parent station's full membership string; fall back to the lines
    # visible at this location when not supplied.
    if line_keys is None:
        line_keys = line_keys_str(
            line_key_of(line_lookup[str(s.get("osm_id", ""))])
            for s in stops_at_location
            if line_lookup.get(str(s.get("osm_id", ""))))

    groups_present = [g for g in COLOR_GROUP_ORDER if g in by_group]
    n = len(groups_present)

    feats = []
    # slot_units = 2*i - (n-1) gives a centered, integer-stepped sequence
    # that's symmetric around 0: e.g. n=2 → {-1, +1}; n=3 → {-2, 0, +2};
    # n=6 → {-5, -3, -1, +1, +3, +5}. The style layer applies
    # text-offset = slot_units × half_spacing_em and text-rotate = tangent_deg
    # in map-aligned space, so the row rotates with the parent's tangent.
    # row_factor (em) — the multiple of text-size that the binding
    # dimension of the parent must accommodate. Pill parents bind on
    # their short axis (one glyph diameter through the pill thickness,
    # row length is unbounded along the long axis). Disc/dot parents
    # bind on the full row span (glyph diameters + inter-glyph gaps).
    # See `.claude/concepts/stops-pill-zoom-tweaks.md` § "Indicators
    # must not overflow the parent".
    if parent_type == "pill":
        row_factor = 0.70
    else:
        row_factor = 0.56 * n + 0.14

    for i, group in enumerate(groups_present):
        _fs, _ref, color = by_group[group]
        slot_units = 2 * i - (n - 1)
        feats.append({
            "type": "Feature",
            "tippecanoe": {"minzoom": INDICATOR_MIN_ZOOM},
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "feature_type":      "indicator",
                "color":             color,
                "slot_units":        slot_units,
                "tangent_deg":       round(tangent_deg, 2),
                "n_indicators":      n,
                "row_factor":        round(row_factor, 3),
                "parent_width_base": round(float(parent_width_base), 3),
                "line_keys":         line_keys,
            },
        })
    return feats


# =============================================================================
# Connector curving — symmetric-arc geometry applied to MST connectors after
# pill placement. See `.claude/concepts/stops-pill-zoom.md` § Connector curving.
# =============================================================================
