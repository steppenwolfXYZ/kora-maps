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


def cluster_lines(cluster_stops, line_lookup):
    """
    Return a sorted list of {ref, color, mode} dicts for all distinct lines
    serving any stop in the cluster.  Sorted by mode rank then ref.
    """
    seen = {}
    for s in cluster_stops:
        oid = s.get("osm_id", "")
        if oid and oid not in seen:
            info = line_lookup.get(oid, {})
            if info:
                seen[oid] = {
                    "ref":      info.get("gtfs_ref") or info.get("ref", ""),
                    "color":    info.get("color", "#888888"),
                    "mode":     info.get("mode", ""),
                    "name":     info.get("name", ""),
                }
    return sorted(seen.values(), key=lambda x: (MODE_RANK.get(x["mode"], 99), x.get("gtfs_ref") or x["ref"]))


def build_indicator_features(stops_at_location, lon, lat, line_lookup,
                              tangent_deg=0.0,
                              parent_width_base=None, parent_mode=None,
                              parent_type="disc"):
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
            },
        })
    return feats


# =============================================================================
# Connector curving — symmetric-arc geometry applied to MST connectors after
# pill placement. See `.claude/concepts/stops-pill-zoom.md` § Connector curving.
# =============================================================================
