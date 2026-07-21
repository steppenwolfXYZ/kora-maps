"""Landuse-based city-bus promotion (citybus-landuse-promotion.md).

Shared logic for the dynamic regional_bus → city_bus promotion:

- the built-up landuse raster (100 m grid of cells covered by built-up
  OSM `landuse` polygons) — rasterization used by step 03 to produce
  `data/osm/builtup_grid_100m.json`, loading used by step 06;
- the corridor metric — side-aware built-up share + spread — evaluated
  over a line group's shaped polylines;
- the spread-coupled pass threshold with its config knobs
  (`citybus_promotion` section in scripts/transit/config.yaml).

The metric is the one validated in the v2 diagnostic
(data/transit/diagnostics/find_citybus_candidates_v2.py); the diagnostic
reads the same config keys so tuning cannot drift from the pipeline.
"""

from __future__ import annotations

import json
import sys
from math import cos, floor, radians, sqrt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GRID_PATH = ROOT / "data" / "osm" / "builtup_grid_100m.json"

# landuse values that count as built-up. Deliberately excludes
# agricultural / green tags (farmland, meadow, forest, orchard, vineyard,
# grass, cemetery, allotments, recreation_ground, quarry, landfill …) —
# their absence from this list is what makes the share meaningful. The
# list defines the metric and is not a tuning knob (concept § raster).
BUILT_UP_LANDUSE = [
    "residential", "commercial", "industrial", "retail",
    "construction", "garages", "railway", "brownfield",
    "education", "institutional",
]
LANDUSE_TAG_FILTER = "a/landuse=" + ",".join(BUILT_UP_LANDUSE)

CELL_M = 100.0             # raster resolution
SAMPLE_STEP_M = 50.0
BAND_RADIUS_M = 500.0

_M_PER_DEG = 111_320.0
_LAT0 = 46.8
_COS_LAT0 = cos(radians(_LAT0))
CELL_LAT_DEG = CELL_M / _M_PER_DEG
CELL_LON_DEG = CELL_LAT_DEG / _COS_LAT0

# Config defaults — the calibrated values from the v2 diagnostic review.
_CFG_DEFAULTS = {
    "pass_anchor_km": 1.0,
    "pass_anchor_share": 0.50,
    "pass_halving_km": 4.0,
    "pass_soften": 0.049,
}


def load_promotion_cfg(cfg: dict) -> dict:
    """The `citybus_promotion` knobs from a parsed config.yaml dict."""
    section = cfg.get("citybus_promotion") or {}
    if not section:
        print("  WARNING: config.yaml has no `citybus_promotion` section — "
              "using built-in defaults.")
    return {k: float(section.get(k, d)) for k, d in _CFG_DEFAULTS.items()}


def pass_threshold(spread_km: float, knobs: dict) -> float:
    """Required built-up share for a given spread. Exponential approach
    to 100%: the remaining gap halves every pass_halving_km, the whole
    curve shifted down by pass_soften."""
    gap = 1.0 - knobs["pass_anchor_share"]
    return (1.0 - gap * 0.5 ** ((spread_km - knobs["pass_anchor_km"])
                                / knobs["pass_halving_km"])
            ) - knobs["pass_soften"]


# ── Raster build / load ─────────────────────────────────────────────────────

def rasterize_polygon(rings, cells: set) -> None:
    """Scanline even-odd fill of one polygon (outer ring + holes) onto the
    grid; adds covered cells to `cells`. A cell is covered when its CENTRE
    lies inside the polygon."""
    grid_rings = []
    gy_min, gy_max = 1e18, -1e18
    for ring in rings:
        gr = [(lon / CELL_LON_DEG, lat / CELL_LAT_DEG) for lon, lat in ring]
        if len(gr) >= 3:
            grid_rings.append(gr)
            for _, gy in gr:
                gy_min = min(gy_min, gy)
                gy_max = max(gy_max, gy)
    if not grid_rings:
        return
    for j in range(int(floor(gy_min - 0.5)), int(floor(gy_max + 0.5)) + 1):
        yc = j + 0.5
        xs = []
        for gr in grid_rings:
            for (x1, y1), (x2, y2) in zip(gr, gr[1:] + gr[:1]):
                if (y1 <= yc < y2) or (y2 <= yc < y1):
                    xs.append(x1 + (yc - y1) / (y2 - y1) * (x2 - x1))
        xs.sort()
        for xa, xb in zip(xs[::2], xs[1::2]):
            for i in range(int(floor(xa + 0.5)), int(floor(xb - 0.5)) + 1):
                cells.add((i, j))


def iter_polygons(geom):
    """Yield ring-lists (outer + holes) for Polygon / MultiPolygon."""
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Polygon":
        yield coords
    elif gtype == "MultiPolygon":
        yield from coords


def save_builtup_grid(cells: set, path: Path = GRID_PATH) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({
        "cell_m": CELL_M, "lat0": _LAT0,
        "landuse": BUILT_UP_LANDUSE,
        "cells": sorted(cells),
    }))
    tmp.replace(path)


def load_builtup_grid(path: Path = GRID_PATH) -> set:
    """The built-up cell set. Missing artifact fails loudly (concept
    § constraints) — promotion must never be skipped silently."""
    if not path.exists():
        sys.exit(f"missing {path} — the built-up landuse raster is built "
                 "by step 03; re-run the pipeline from step 3.")
    data = json.loads(path.read_text())
    if data.get("cell_m") != CELL_M or data.get("lat0") != _LAT0:
        sys.exit(f"{path} was built with a different grid convention "
                 f"(cell_m={data.get('cell_m')}, lat0={data.get('lat0')}) — "
                 "re-run step 03 to rebuild it.")
    return {tuple(c) for c in data["cells"]}


# ── Corridor metric ─────────────────────────────────────────────────────────

# Cell offsets whose centre-to-centre distance is within the band radius —
# the disc evaluated around every line sample, with metre offsets kept for
# the left/right split against the sample's heading.
_R_CELLS = int(BAND_RADIUS_M / CELL_M) + 1
BAND_OFFSETS = [
    (dx, dy, dx * CELL_M, dy * CELL_M)
    for dx in range(-_R_CELLS, _R_CELLS + 1)
    for dy in range(-_R_CELLS, _R_CELLS + 1)
    if (dx * CELL_M) ** 2 + (dy * CELL_M) ** 2 <= BAND_RADIUS_M ** 2
]


def sample_polyline(coords, step_m=SAMPLE_STEP_M):
    """(lon, lat, hx, hy) every step_m metres along the polyline, incl.
    the start point. (hx, hy) is the local travel heading in metre space
    (unit vector of the segment the sample lies on)."""
    out = []
    if len(coords) < 2:
        return out
    first_h = None
    carry = 0.0
    for (lon0, lat0), (lon1, lat1) in zip(coords, coords[1:]):
        mdx = (lon1 - lon0) * _COS_LAT0 * _M_PER_DEG
        mdy = (lat1 - lat0) * _M_PER_DEG
        seg = sqrt(mdx * mdx + mdy * mdy)
        if seg <= 0:
            continue
        hx, hy = mdx / seg, mdy / seg
        if first_h is None:
            first_h = (hx, hy)
            out.append((coords[0][0], coords[0][1], hx, hy))
        d = step_m - carry
        while d <= seg:
            t = d / seg
            out.append((lon0 + (lon1 - lon0) * t,
                        lat0 + (lat1 - lat0) * t, hx, hy))
            d += step_m
        carry = seg - (d - step_m)
    return out


def _convex_hull(pts):
    """Andrew monotone chain on (x, y) metre points."""
    pts = sorted(set(pts))
    if len(pts) <= 2:
        return pts

    def half(seq):
        h = []
        for p in seq:
            while len(h) >= 2 and (
                    (h[-1][0] - h[-2][0]) * (p[1] - h[-2][1])
                    - (h[-1][1] - h[-2][1]) * (p[0] - h[-2][0])) <= 0:
                h.pop()
            h.append(p)
        return h

    lower = half(pts)
    upper = half(reversed(pts))
    return lower[:-1] + upper[:-1]


def _spread_km(cells) -> float:
    """Straight-line distance (km) between the two furthest-apart sample
    cells: convex hull, then brute force over the (small) hull."""
    pts = [((cx + 0.5) * CELL_M, (cy + 0.5) * CELL_M) for cx, cy in cells]
    hull = _convex_hull(pts)
    best = 0.0
    for i, (x1, y1) in enumerate(hull):
        for x2, y2 in hull[i + 1:]:
            d = (x2 - x1) ** 2 + (y2 - y1) ** 2
            if d > best:
                best = d
    return sqrt(best) / 1000.0


def evaluate_polylines(polylines, built: set):
    """(share, spread_km) for the union of the given polylines.

    Share: per deduplicated 50 m sample the 500 m disc is split into the
    left / right half-disc relative to the local heading; the better
    side's built-up fraction wins the sample; the result is the mean over
    samples (side-aware sweep, concept § metric). Returns (0.0, 0.0) for
    empty input."""
    cells: dict = {}
    for coords in polylines:
        for lon, lat, hx, hy in sample_polyline(coords):
            c = (int(floor(lon / CELL_LON_DEG)),
                 int(floor(lat / CELL_LAT_DEG)))
            cells.setdefault(c, (hx, hy))
    if not cells:
        return 0.0, 0.0
    shares = []
    for (cx, cy), (hx, hy) in cells.items():
        lb = lt = rb = rt = 0
        for dx, dy, mx, my in BAND_OFFSETS:
            is_built = (cx + dx, cy + dy) in built
            if hx * my - hy * mx >= 0:
                lt += 1
                lb += is_built
            else:
                rt += 1
                rb += is_built
        shares.append(max(lb / lt if lt else 0.0,
                          rb / rt if rt else 0.0))
    return sum(shares) / len(shares), _spread_km(cells.keys())
