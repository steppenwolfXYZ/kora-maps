"""Platform-extent computation: per-stop extent slices along the line
polyline, length resolution from atlas/config, and the funicular snap
override. Terminal extension lives in terminal_fill.py, borrow tiers in
borrow.py."""
from math import cos, radians, sqrt

from _state import *  # noqa: F401,F403 — shared constants
from geometry import (
    _cum_dist_m, _directional_tangent_at, _project_meters, _slice_polyline,
)


def _length_key(mode: str, mountain_origin):
    """Map (mode, mountain_origin) to a config key under
    pill_rendering.{default,sanity_min,sanity_max}_length_m. Returns None
    when no extent is defined for the stop (ferry; mountain aerial; any
    out-of-scope mode)."""
    if mode == "mountain":
        if mountain_origin in MOUNTAIN_RAIL_ORIGINS:
            return "mountain_rail"
        if mountain_origin == "funicular":
            return "mountain_funicular"
        return None
    return mode


def _resolve_length(mode: str, atlas_length, cfg: dict, mountain_origin=None):
    """Pick the platform length to use for a given mode and atlas value.

    Atlas value is used when it lies within the per-mode sanity range;
    otherwise the per-mode default is returned. Returns None for modes
    that don't carry a platform extent (ferry; mountain aerial).
    """
    key = _length_key(mode, mountain_origin)
    if key is None or key not in cfg.get("default_length_m", {}):
        return None
    smin = cfg["sanity_min_m"][key]
    smax = cfg["sanity_max_m"][key]
    if atlas_length is not None and smin <= atlas_length <= smax:
        return atlas_length
    return cfg["default_length_m"][key]


def _platform_extent(stop_lon, stop_lat, polyline, mode, atlas_length, cfg,
                      end_of_platform=False, mountain_origin=None):
    """Return the (lon, lat) sequence tracing the platform's allowed range
    along its polyline, or None for out-of-scope modes / degenerate geometry.

    Anchoring (per pill-rendering concept):
      • train, metro            — GTFS coord (snapped to polyline) is platform
                                  CENTRE → range = ±L/2.
      • mountain rebucketed_rail / rack / funicular — same as train/metro
                                  (centred ±L/2), but with metro-style
                                  straight-line extrapolation on the missing
                                  side (mountain polylines are not pre-extended
                                  by `_extend_polylines_at_terminals`).
      • tram, bus               — GTFS coord is FRONT of stop → range
                                  = [coord - L, coord].

    Missing-range fill is handled UPSTREAM for both fill-bearing families
    (see stop-extent-osm-walk.md § "Fill runs once, upfront"):
      • train / mountain rail-like: `_extend_polylines_at_terminals`
        pre-extends the polyline along the OSM rail track (capped straight
        when no way matches), so the ±L/2 slice fits. `end_of_platform=True`
        flips the anchoring to asymmetric (case 2, § Rail walk): the walked
        partial was prepended, the outward side takes the walked ground x
        and the inward side absorbs L − x — total range stays L.
      • tram / bus / regional_bus: `_extend_nonrail_polylines_at_terminals`
        pre-extends the polyline via sibling borrow → non-sibling borrow →
        OSM street/tram walk. Here the extent is a plain [t−L, t] slice;
        when the fill came up short the extent stays clipped —
        short-but-true beats long-but-wrong.
      • metro, mountain funicular: unchanged in-place behaviour (symmetric
        straight-line extrapolation / clip-to-polyline respectively).

    Mountain aerial returns None — those stops are fixed-dot in the pill
    pipeline and have no extent.
    """
    if len(polyline) < 2:
        return None
    L = _resolve_length(mode, atlas_length, cfg, mountain_origin=mountain_origin)
    if L is None:
        return None
    dists = _cum_dist_m(polyline)
    poly_max = dists[-1]
    if poly_max <= 0:
        return None
    t = _project_meters(stop_lon, stop_lat, polyline, dists)

    is_centred_extent = (
        mode in ("train", "metro")
        or (mode == "mountain" and mountain_origin in MOUNTAIN_EXTENT_ORIGINS)
    )
    if not is_centred_extent:
        # Tram / bus / regional_bus: backward-anchored range [t-L, t],
        # clipped to the (pre-extended) polyline. A sub-metre residual —
        # the polyline starts at the stop and no fill was found — is no
        # extent at all; returning a collapsed 2-point slice would feed
        # zero-length geometry into pill construction downstream.
        t_start = max(0.0, t - L)
        if t - t_start < 0.5:
            return None
        return list(_slice_polyline(polyline, dists, t_start, t))

    if end_of_platform:
        # Case 2 (stop-extent-osm-walk.md § Rail walk): the OSM track ended
        # before L/2 and the walked partial was prepended upstream, so the
        # outward (short) side takes everything it has — x, the walked
        # ground — and the inward side absorbs the remaining L − x. The
        # range still totals L, just not centred on the snap.
        if poly_max - t >= t:
            # Outward side is backward (start-side terminal).
            x = min(t, L)
            t_start_ideal = t - x
            t_end_ideal = min(poly_max, t + (L - x))
        else:
            # Outward side is forward (end-side terminal).
            x = min(poly_max - t, L)
            t_start_ideal = max(0.0, t - (L - x))
            t_end_ideal = t + x
        return list(_slice_polyline(polyline, dists, t_start_ideal, t_end_ideal))

    half_L = L / 2.0
    t_start_ideal = t - half_L
    t_end_ideal = t + half_L

    on_start = max(0.0, t_start_ideal)
    on_end = min(poly_max, t_end_ideal)
    slice_pts = list(_slice_polyline(polyline, dists, on_start, on_end))

    if mode == "train" or (
            mode == "mountain" and mountain_origin in MOUNTAIN_RAIL_ORIGINS):
        # Train and mountain rail-like (rebucketed_rail / rack) extents rely
        # on the polyline being pre-extended at terminals (OSM walk or capped
        # 50 m straight) by `_extend_polylines_at_terminals`. Don't
        # re-extrapolate here — the concept caps Fallback A at
        # osm_fallback_max_straight_m, so any remaining clip on the missing
        # side must stay clipped.
        return slice_pts

    if mode == "mountain" and mountain_origin == "funicular":
        # Funicular: clip to the polyline. No straight-line extrapolation;
        # when the centred ±L/2 extent would reach a polyline endpoint, use
        # Fallback B-style asymmetric anchoring (polyline side absorbs the
        # full L) so the extent stays within the line shape. The dot's snap
        # is pinned to the same endpoint via `_funicular_snap_override`.
        if t_end_ideal > poly_max:
            t_start = max(0.0, poly_max - L)
            t_end = poly_max
        elif t_start_ideal < 0:
            t_start = 0.0
            t_end = min(poly_max, L)
        else:
            return slice_pts
        return list(_slice_polyline(polyline, dists, t_start, t_end))

    # Metro: keep the symmetric straight-line extrapolation behaviour.
    tan = _directional_tangent_at(polyline, dists, t)
    if tan is None:
        return slice_pts
    dx_per_m, dy_per_m = tan

    pts = []
    if t_start_ideal < 0 and slice_pts:
        # Extrapolate from polyline[0] backwards (against forward tangent)
        missing_m = -t_start_ideal
        wx = slice_pts[0][0] - dx_per_m * missing_m
        wy = slice_pts[0][1] - dy_per_m * missing_m
        pts.append((wx, wy))
    pts.extend(slice_pts)
    if t_end_ideal > poly_max and slice_pts:
        # Extrapolate from polyline[-1] forward (with forward tangent)
        missing_m = t_end_ideal - poly_max
        ex = slice_pts[-1][0] + dx_per_m * missing_m
        ey = slice_pts[-1][1] + dy_per_m * missing_m
        pts.append((ex, ey))
    return pts


def _funicular_snap_override(stop_lon, stop_lat, polyline, atlas_length, cfg):
    """For funicular: when the centred ±L/2 extent would reach a polyline
    endpoint, return that endpoint so the dot's snap pins there instead of
    at the GTFS-coord projection. Returns None when the extent stays inside
    the polyline (regular snap_to_line is fine) or the polyline is degenerate.
    """
    if len(polyline) < 2:
        return None
    L = _resolve_length("mountain", atlas_length, cfg,
                         mountain_origin="funicular")
    if L is None or L <= 0:
        return None
    dists = _cum_dist_m(polyline)
    poly_max = dists[-1]
    if poly_max <= 0:
        return None
    t = _project_meters(stop_lon, stop_lat, polyline, dists)
    half_L = L / 2.0
    if t + half_L >= poly_max:
        return (polyline[-1][0], polyline[-1][1])
    if t - half_L <= 0:
        return (polyline[0][0], polyline[0][1])
    return None


# Window over which the per-stop polyline tangent is averaged. Sized to
# stay inside the platform extent (per-mode default ≤ 35 m for non-rail,
# 100 m for rail) so the averaged direction reflects what's happening at
# the dot, not the chord of the whole extent. For 30 m bus/tram extents,
# 40 m smoothing would spill past the extent into adjacent polyline and
# pull the angle off — see Eigerplatz, where it puts C and D into
# different tangent groups despite both running on the same OSM way.
TANGENT_WINDOW_M = 10.0

