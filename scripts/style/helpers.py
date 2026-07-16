"""Shared helpers for the style-JSON builders.

Layer builders in this package take a parsed config dict and return a list
of MapLibre layer definitions. Anything reused across those builders lives
here: config load, color / width interpolation, road-class filter
expressions, and the walkable-road width groups.
"""
import math

import yaml


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def lerp_color(color_low: str, color_mid: str, color_high: str, t: float) -> str:
    """Return a hex color interpolated across a 3-stop gradient at position t (0..1)."""
    def hex_to_rgb(h):
        h = h.lstrip("#")
        return [int(h[i:i+2], 16) for i in (0, 2, 4)]

    def rgb_to_hex(rgb):
        return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, int(c))) for c in rgb])

    low = hex_to_rgb(color_low)
    mid = hex_to_rgb(color_mid)
    high = hex_to_rgb(color_high)

    if t <= 0.5:
        s = t / 0.5
        rgb = [low[i] + (mid[i] - low[i]) * s for i in range(3)]
    else:
        s = (t - 0.5) / 0.5
        rgb = [mid[i] + (high[i] - mid[i]) * s for i in range(3)]

    return rgb_to_hex(rgb)


def meters_to_pixels(meters: float, zoom: int, lat: float = 46.95) -> float:
    """Convert real-world meters to pixel width at a given zoom level."""
    meters_per_pixel = (156543.03 * math.cos(math.radians(lat))) / (2 ** zoom)
    return meters / meters_per_pixel


# ── Filter helpers ──────────────────────────────────────────────────────────

def class_filter(classes, include=True):
    """Build a match expression for road class filtering."""
    return ["match", ["get", "class"], classes, include, not include]


def brunnel_filter(mode):
    """Filter for tunnel/normal/bridge road rendering modes."""
    if mode == "tunnel":
        return ["==", ["get", "brunnel"], "tunnel"]
    elif mode == "bridge":
        return ["==", ["get", "brunnel"], "bridge"]
    else:
        return ["match", ["get", "brunnel"], ["bridge", "tunnel"], False, True]


# ── Road class constants ────────────────────────────────────────────────────

MOTORWAY_CLASSES = ["motorway", "trunk"]
MAIN_ROAD_CLASSES = ["primary", "secondary"]
PATH_CLASSES = ["path"]
RAIL_CLASSES = ["rail", "transit"]
FERRY_CLASSES = ["ferry"]
WALKABLE_EXCLUDE = MOTORWAY_CLASSES + MAIN_ROAD_CLASSES + RAIL_CLASSES + FERRY_CLASSES

# Walkable road class groups for close-zoom real-width layers.
# Each group becomes its own layer — MapLibre forbids multiple zoom-based
# expressions per paint property, so case+interpolate is not valid.
#
# TODO (post-MVP, requires raw OSM data):
#   With osm2pgsql + PostGIS you can read width=*, lanes=*, sidewalk=* and
#   derive actual road widths per feature. That would let us distinguish a
#   2-lane residential from a 4-lane one, or a pedestrian shopping street
#   from a narrow park path — which is impossible from standard vector tiles.
WALKABLE_WIDTH_GROUPS = [
    ("wide",       ["tertiary"],                                          "tertiary"),
    ("mid",        ["minor", "residential", "unclassified", "living_street"], "residential"),
    # pedestrian zones get their own narrower group — they include everything
    # from wide shopping streets to narrow park paths; no way to distinguish
    # without raw OSM tags (foot=yes, area=yes, surface=*, width=*).
    ("pedestrian", ["pedestrian"],                                        "pedestrian"),
    ("narrow",     ["service", "track"],                                  "service"),
]


# ── Width helpers ───────────────────────────────────────────────────────────

def _width_interp(meters, start_zoom, full_zoom=22, lat=46.95):
    """Exponential zoom interpolation anchored in real-world meters.
    Extends to zoom 22 so the road keeps growing at any practical zoom level."""
    def px(m, z):
        return round(meters_to_pixels(m, z, lat), 2)
    return ["interpolate", ["exponential", 2], ["zoom"],
        start_zoom, px(meters, start_zoom),
        22,         px(meters, 22)
    ]
