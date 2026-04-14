#!/usr/bin/env python3
"""
Car-Free Map Style Generator
==============================
Reads config.yaml and produces a MapLibre GL style JSON.

Usage:
    python generate_style.py                    # reads ./config.yaml, writes ../static/style.json
    python generate_style.py -c myconfig.yaml   # custom config path
    python generate_style.py -o output.json     # custom output path
"""

import argparse
import json
import math
import sys
from pathlib import Path

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


# =============================================================================
# Filter helpers
# =============================================================================

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


# =============================================================================
# Road class constants
# =============================================================================

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


# =============================================================================
# Width helpers
# =============================================================================

def _width_interp(meters, start_zoom, full_zoom=22, lat=46.95):
    """Exponential zoom interpolation anchored in real-world meters.
    Extends to zoom 22 so the road keeps growing at any practical zoom level."""
    def px(m, z):
        return round(meters_to_pixels(m, z, lat), 2)
    return ["interpolate", ["exponential", 2], ["zoom"],
        start_zoom, px(meters, start_zoom),
        22,         px(meters, 22)
    ]


# =============================================================================
# Layer builders
# =============================================================================

def build_background_layer(cfg):
    return {
        "id": "background",
        "type": "background",
        "paint": {
            "background-color": cfg["palette"]["background"]
        }
    }


def build_landuse_layers(cfg):
    p = cfg["palette"]
    lu = cfg["landuse"]
    min_z = lu["min_zoom"]
    op_lo = lu["opacity_low_zoom"]
    op_hi = lu["opacity_high_zoom"]

    layers = []

    landuse_colors = [
        ("landuse-residential", "residential", p["urban_area"]),
        ("landuse-industrial",  "industrial",  p["industrial"]),
        ("landuse-commercial",  "commercial",  p["commercial"]),
        ("landuse-cemetery",    "cemetery",    lu["cemetery_color"]),
        ("landuse-hospital",    "hospital",    lu["hospital_color"]),
        ("landuse-school",      "school",      lu["school_color"]),
        ("landuse-farmland",    "farmland",    p["farmland"]),
        ("landuse-meadow",      "meadow",      p["meadow"]),
    ]

    for layer_id, class_name, color in landuse_colors:
        layers.append({
            "id": layer_id,
            "type": "fill",
            "source": "openmaptiles",
            "source-layer": "landuse",
            "minzoom": min_z,
            "filter": ["==", ["get", "class"], class_name],
            "paint": {
                "fill-color": color,
                "fill-opacity": ["interpolate", ["linear"], ["zoom"],
                    min_z, op_lo,
                    min_z + 3, op_hi
                ]
            }
        })

    landcover_colors = [
        ("landcover-forest", "wood",  p["forest"]),
        ("landcover-grass",  "grass", p["park"]),
        ("landcover-sand",   "sand",  p["sand_beach"]),
        ("landcover-ice",    "ice",   p["glacier"]),
    ]

    for layer_id, class_name, color in landcover_colors:
        layers.append({
            "id": layer_id,
            "type": "fill",
            "source": "openmaptiles",
            "source-layer": "landcover",
            "minzoom": min_z,
            "filter": ["==", ["get", "class"], class_name],
            "paint": {
                "fill-color": color,
                "fill-opacity": ["interpolate", ["linear"], ["zoom"],
                    min_z, 0.4,
                    min_z + 4, 0.7
                ]
            }
        })

    layers.append({
        "id": "park-fill",
        "type": "fill",
        "source": "openmaptiles",
        "source-layer": "park",
        "minzoom": min_z,
        "paint": {
            "fill-color": p["park"],
            "fill-opacity": ["interpolate", ["linear"], ["zoom"],
                min_z, 0.4,
                12, 0.7
            ]
        }
    })

    if lu.get("park_outline"):
        layers.append({
            "id": "park-outline",
            "type": "line",
            "source": "openmaptiles",
            "source-layer": "park",
            "minzoom": 10,
            "paint": {
                "line-color": lu["park_outline_color"],
                "line-width": 1,
                "line-opacity": 0.6
            }
        })

    return layers


def build_water_layers(cfg):
    p = cfg["palette"]
    w = cfg["water"]
    layers = []

    layers.append({
        "id": "water-fill",
        "type": "fill",
        "source": "openmaptiles",
        "source-layer": "water",
        "paint": {"fill-color": p["water"]}
    })

    if w.get("lake_outline"):
        layers.append({
            "id": "water-outline",
            "type": "line",
            "source": "openmaptiles",
            "source-layer": "water",
            "minzoom": 8,
            "paint": {
                "line-color": p["water_outline"],
                "line-width": 1,
                "line-opacity": 0.5
            }
        })

    layers.append({
        "id": "waterway-river",
        "type": "line",
        "source": "openmaptiles",
        "source-layer": "waterway",
        "minzoom": w["river_min_zoom"],
        "filter": ["match", ["get", "class"], ["river", "canal"], True, False],
        "paint": {
            "line-color": p["water"],
            "line-width": ["interpolate", ["linear"], ["zoom"],
                w["river_min_zoom"], w["river_min_width"],
                14, 4, 18, 10
            ]
        }
    })

    layers.append({
        "id": "waterway-stream",
        "type": "line",
        "source": "openmaptiles",
        "source-layer": "waterway",
        "minzoom": w["stream_min_zoom"],
        "filter": ["match", ["get", "class"], ["stream", "ditch", "drain"], True, False],
        "paint": {
            "line-color": p["water"],
            "line-width": ["interpolate", ["linear"], ["zoom"],
                w["stream_min_zoom"], 0.5, 18, 3
            ]
        }
    })

    return layers


def build_building_layers(cfg):
    p = cfg["palette"]
    b = cfg["buildings"]

    return [{
        "id": "buildings-fill",
        "type": "fill",
        "source": "openmaptiles",
        "source-layer": "building",
        "minzoom": b["min_zoom"],
        "paint": {
            "fill-color": p["buildings"],
            "fill-opacity": ["interpolate", ["linear"], ["zoom"],
                b["min_zoom"], 0.5, 16, 0.8
            ]
        }
    }, {
        "id": "buildings-outline",
        "type": "line",
        "source": "openmaptiles",
        "source-layer": "building",
        "minzoom": b["min_zoom"],
        "paint": {
            "line-color": p["building_outline"],
            "line-width": 0.5,
            "line-opacity": 0.6
        }
    }]


def build_rail_layers(cfg, modes=None):
    """Rail infrastructure as neutral background.
    Split by brunnel mode so bridge rail renders above the bridge deck."""
    if modes is None:
        modes = ["tunnel", "normal", "bridge"]
    p = cfg["palette"]
    layers = []

    for mode in modes:
        bf = brunnel_filter(mode)
        suffix = "" if mode == "normal" else f"-{mode}"
        opacity = p["tunnel_opacity"] if mode == "tunnel" else p["rail_opacity"]
        layers.append({
            "id": f"rail{suffix}",
            "type": "line",
            "source": "openmaptiles",
            "source-layer": "transportation",
            "minzoom": 8,
            "filter": ["all", class_filter(RAIL_CLASSES), bf],
            "layout": {"line-cap": "butt", "line-join": "round"},
            "paint": {
                "line-color": p["rail"],
                "line-width": ["interpolate", ["linear"], ["zoom"], 8, 0.75, 14, 2.0],
                "line-opacity": opacity,
            }
        })

    return layers


def build_bridge_deck_layer(cfg):
    """Single solid gray deck for ALL bridge transportation features at all zooms.
    Rendered between normal-mode and bridge-mode road layers: roads below the
    bridge cover the deck (normal mode renders before this), roads on the bridge
    render on top (bridge mode renders after). No per-class variants — a single
    layer avoids hollow/donut artifacts. Width is 1.5px minimum at far zoom,
    then meter-based (15m) from zoom 14 onwards."""
    p = cfg["palette"]
    return [{
        "id": "bridge-deck",
        "type": "line",
        "source": "openmaptiles",
        "source-layer": "transportation",
        "minzoom": 8,
        "filter": ["==", ["get", "brunnel"], "bridge"],
        "layout": {"line-cap": "butt", "line-join": "round"},
        "paint": {
            "line-color": p["bridge_casing"],
            "line-width": ["interpolate", ["exponential", 2], ["zoom"],
                8,  1.5,
                13, 1.5,
                14, round(meters_to_pixels(15, 14), 2),
                22, round(meters_to_pixels(15, 22), 1)
            ],
            "line-opacity": p["bridge_deck_opacity"]
        }
    }]


def build_road_layers(cfg, modes=None):
    """Road layers with three-tier hierarchy, bridge/tunnel variants,
    real-width rendering at close zoom, and separated path treatment."""
    r = cfg["roads"]
    w = cfg["walkability"]
    p = cfg["palette"]
    rw = r["real_widths"]
    rw_min_z = rw["min_zoom"]
    rw_full_z = rw["full_zoom"]

    layers = []

    def px(meters, zoom):
        return round(meters_to_pixels(meters, zoom), 2)

    if modes is None:
        modes = ["tunnel", "normal", "bridge"]

    for mode in modes:
        bf = brunnel_filter(mode)
        suffix = "" if mode == "normal" else f"-{mode}"
        opacity_mult = p["tunnel_opacity"] if mode == "tunnel" else 1.0

        # =================================================================
        # 1. MOTORWAY
        # =================================================================
        mw = r["motorway"]

        layers.append({
            "id": f"road-motorway-line{suffix}",
            "type": "line",
            "source": "openmaptiles",
            "source-layer": "transportation",
            "minzoom": mw["min_zoom"],
            "maxzoom": mw["area_min_zoom"],
            "filter": ["all", class_filter(MOTORWAY_CLASSES), bf],
            "paint": {
                "line-color": mw["line_color"],
                "line-width": 1,
                "line-dasharray": mw["line_dasharray"],
                "line-opacity": opacity_mult
            }
        })

        layers.append({
            "id": f"road-motorway-fill{suffix}",
            "type": "line",
            "source": "openmaptiles",
            "source-layer": "transportation",
            "minzoom": mw["area_min_zoom"],
            "filter": ["all", class_filter(MOTORWAY_CLASSES), bf],
            "layout": {"line-cap": "butt", "line-join": "round"},
            "paint": {
                "line-color": mw["fill_color"],
                "line-width": _width_interp(rw["motorway"], mw["area_min_zoom"]),
                "line-opacity": opacity_mult
            }
        })

        # =================================================================
        # 2. MAIN ROADS
        # =================================================================
        mr = r["main_road"]

        layers.append({
            "id": f"road-main-line{suffix}",
            "type": "line",
            "source": "openmaptiles",
            "source-layer": "transportation",
            "minzoom": mr["min_zoom"],
            "maxzoom": mr["area_min_zoom"],
            "filter": ["all", class_filter(MAIN_ROAD_CLASSES), bf],
            "layout": {"line-cap": "round", "line-join": "round"},
            "paint": {
                "line-color": mr["line_color"],
                "line-width": ["step", ["zoom"], 1, 13, 2],
                "line-opacity": opacity_mult
            }
        })

        layers.append({
            "id": f"road-main-fill{suffix}",
            "type": "line",
            "source": "openmaptiles",
            "source-layer": "transportation",
            "minzoom": mr["area_min_zoom"],
            "filter": ["all", class_filter(MAIN_ROAD_CLASSES), bf],
            "layout": {"line-cap": "butt", "line-join": "round"},
            "paint": {
                "line-color": mr["fill_color"],
                "line-width": _width_interp(rw["primary"], mr["area_min_zoom"]),
                "line-opacity": opacity_mult
            }
        })

        # =================================================================
        # 3. WALKABLE STREETS
        # =================================================================
        wk = r["walkable"]
        walkability_color_expr = _build_walkability_color_expression(cfg["walkability"])
        walkability_width_expr = _build_walkability_width_expression(cfg["walkability"])

        walkable_filter = ["all",
            class_filter(WALKABLE_EXCLUDE + PATH_CLASSES, include=False),
            bf
        ]

        # Mid-zoom: symbolic lines (bridge mode skipped; bridges shown by bridge-deck layer)
        if mode != "bridge":
            layers.append({
                "id": f"road-walkable-midline{suffix}",
                "type": "line",
                "source": "openmaptiles",
                "source-layer": "transportation",
                "minzoom": wk["line_min_zoom"],
                "maxzoom": wk["area_min_zoom"],
                "filter": walkable_filter,
                "layout": {"line-cap": "round", "line-join": "round"},
                "paint": {
                    "line-color": walkability_color_expr,
                    "line-width": walkability_width_expr,
                    "line-opacity": ["interpolate", ["linear"], ["zoom"],
                        wk["line_min_zoom"], 0.3 * opacity_mult,
                        14, 0.8 * opacity_mult
                    ]
                }
            })

        # Close zoom: one layer per width group
        # TODO (post-MVP): proper intersection fix requires pre-computing
        # junction polygons in PostGIS and rendering them as a separate
        # fill layer — this is how high-end styles (e.g. Mapbox Streets)
        # achieve clean intersections.
        for grp_label, grp_classes, grp_width_key in WALKABLE_WIDTH_GROUPS:
            meters = rw[grp_width_key]
            grp_filter = ["all",
                ["match", ["get", "class"], grp_classes, True, False],
                bf
            ]
            layers.append({
                "id": f"road-walkable-fill-{grp_label}{suffix}",
                "type": "line",
                "source": "openmaptiles",
                "source-layer": "transportation",
                "minzoom": wk["area_min_zoom"],
                "filter": grp_filter,
                "layout": {"line-cap": "butt", "line-join": "round"},
                "paint": {
                    "line-color": walkability_color_expr,
                    "line-width": _width_interp(meters, wk["area_min_zoom"]),
                    "line-opacity": opacity_mult
                }
            })

    return layers


def build_path_layers(cfg, modes=None):
    """Paths, footways, cycleways — separate from walkable streets."""
    pc = cfg["paths"]
    p = cfg["palette"]
    layers = []

    if modes is None:
        modes = ["tunnel", "normal", "bridge"]

    path_filter_base = class_filter(PATH_CLASSES)

    for mode in modes:
        bf = brunnel_filter(mode)
        suffix = "" if mode == "normal" else f"-{mode}"
        opacity = p["tunnel_opacity"] if mode == "tunnel" else 1.0

        layers.append({
            "id": f"path-paved{suffix}",
            "type": "line",
            "source": "openmaptiles",
            "source-layer": "transportation",
            "minzoom": pc["min_zoom"],
            "filter": ["all",
                path_filter_base,
                bf,
                ["any",
                    ["==", ["get", "surface"], "paved"],
                    ["!", ["has", "surface"]]
                ]
            ],
            "layout": {"line-cap": "round", "line-join": "round"},
            "paint": {
                "line-color": pc["color"],
                "line-width": pc["width"],
                "line-opacity": ["interpolate", ["linear"], ["zoom"],
                    pc["min_zoom"], 0.4 * opacity,
                    16, 0.8 * opacity
                ]
            }
        })

        layers.append({
            "id": f"path-unpaved{suffix}",
            "type": "line",
            "source": "openmaptiles",
            "source-layer": "transportation",
            "minzoom": pc["min_zoom"],
            "filter": ["all",
                path_filter_base,
                bf,
                ["==", ["get", "surface"], "unpaved"]
            ],
            "layout": {"line-cap": "butt", "line-join": "round"},
            "paint": {
                "line-color": pc["color_unpaved"],
                "line-width": pc["width"],
                "line-dasharray": pc["unpaved_dasharray"],
                "line-opacity": ["interpolate", ["linear"], ["zoom"],
                    pc["min_zoom"], 0.4 * opacity,
                    16, 0.8 * opacity
                ]
            }
        })

    return layers


def _build_walkability_color_expression(w):
    c_low  = w["color_low"]
    c_mid  = w["color_mid"]
    c_high = w["color_high"]

    scores_and_conditions = [
        (1.0,  ["==", ["get", "subclass"], "pedestrian"]),
        (0.85, ["==", ["get", "subclass"], "living_street"]),
        (0.55, ["==", ["get", "class"], "minor"]),
        (0.50, ["==", ["get", "class"], "residential"]),
        (0.45, ["==", ["get", "class"], "service"]),
        (0.40, ["==", ["get", "class"], "tertiary"]),
        (0.35, ["==", ["get", "class"], "track"]),
    ]

    expr = ["case"]
    for score, condition in scores_and_conditions:
        color = lerp_color(c_low, c_mid, c_high, score)
        expr.append(condition)
        expr.append(color)
    expr.append(lerp_color(c_low, c_mid, c_high, 0.30))
    return expr


def _build_walkability_width_expression(w):
    mz = w["mid_zoom_lines"]
    return ["case",
        ["any",
            ["==", ["get", "subclass"], "pedestrian"],
            ["==", ["get", "subclass"], "living_street"],
        ], mz["width_high"],
        ["any",
            ["==", ["get", "class"], "minor"],
            ["==", ["get", "class"], "residential"],
        ], mz["width_mid"],
        mz["width_low"]
    ]


def build_border_layers(cfg):
    b = cfg["borders"]

    return [{
        "id": "border-country",
        "type": "line",
        "source": "openmaptiles",
        "source-layer": "boundary",
        "minzoom": 0,
        "filter": ["all",
            ["==", ["get", "admin_level"], 2],
            ["!=", ["get", "maritime"], 1],
            ["!=", ["get", "disputed"], 1]
        ],
        "layout": {"line-cap": "round", "line-join": "round"},
        "paint": {
            "line-color": b["country_color"],
            "line-width": b["country_width"],
            "line-dasharray": b["country_dasharray"]
        }
    }, {
        "id": "border-state",
        "type": "line",
        "source": "openmaptiles",
        "source-layer": "boundary",
        "minzoom": b["state_min_zoom"],
        "filter": ["all",
            ["==", ["get", "admin_level"], 4],
            ["!=", ["get", "maritime"], 1]
        ],
        "layout": {"line-cap": "round", "line-join": "round"},
        "paint": {
            "line-color": b["state_color"],
            "line-width": b["state_width"],
            "line-dasharray": b["state_dasharray"]
        }
    }]


def build_label_layers(cfg):
    l = cfg["labels"]
    p = cfg["palette"]
    s = l["size_scale"]

    # Layer order: last layer wins collisions.
    # Priority (lowest → highest): poi, water, streets, places, states, countries

    layers = []

    # ── POI labels (lowest priority) ────────────────────────────────────
    layers.append({
        "id": "label-poi",
        "type": "symbol",
        "source": "openmaptiles",
        "source-layer": "poi",
        "minzoom": l["poi_min_zoom"],
        "filter": ["<=", ["get", "rank"], 14],
        "layout": {
            "text-field": ["coalesce", ["get", "name:latin"], ["get", "name"]],
            "text-font": [l["font"]],
            "text-size": 8 * s,
            "text-max-width": 6,
            "text-anchor": "top",
            "text-offset": [0, 0.4]
        },
        "paint": {
            "text-color": "#666666",
            "text-halo-color": p["label_halo"],
            "text-halo-width": 1.0,
            "text-opacity": 0.75
        }
    })

    # ── Waterway labels — rivers & canals ────────────────────────────────
    layers.append({
        "id": "label-waterway",
        "type": "symbol",
        "source": "openmaptiles",
        "source-layer": "waterway",
        "minzoom": 8,
        "filter": ["all",
            ["has", "name"],
            ["match", ["get", "class"], ["river", "canal"], True, False]
        ],
        "layout": {
            "text-field": ["coalesce", ["get", "name:latin"], ["get", "name"]],
            "text-font": [l["font_italic"]],
            "text-size": ["interpolate", ["linear"], ["zoom"],
                8, 10 * s, 14, 13 * s
            ],
            "symbol-placement": "line",
            "symbol-spacing": 400,
            "text-rotation-alignment": "map",
            "text-max-angle": 30
        },
        "paint": {
            "text-color": p["label_water"],
            "text-halo-color": "#ffffffaa",
            "text-halo-width": l["halo_width"]
        }
    })

    # ── Water area labels — lakes, bays (LineString outlines in these tiles)
    layers.append({
        "id": "label-water-area",
        "type": "symbol",
        "source": "openmaptiles",
        "source-layer": "water_name",
        "minzoom": 6,
        "maxzoom": 14,
        "filter": ["match", ["get", "class"],
            ["lake", "sea", "ocean", "reservoir", "bay", "strait"], True, False
        ],
        "layout": {
            "text-field": ["coalesce", ["get", "name:latin"], ["get", "name"]],
            "text-font": [l["font_italic"]],
            "text-size": ["interpolate", ["linear"], ["zoom"],
                6, 9 * s, 9, 15 * s, 13, 12 * s
            ],
            "text-max-width": 10,
            "symbol-placement": "line",
            "symbol-spacing": 600,
            "text-rotation-alignment": "map",
            "text-max-angle": 30
        },
        "paint": {
            "text-color": p["label_water"],
            "text-halo-color": "#ffffffaa",
            "text-halo-width": l["halo_width"]
        }
    })

    # ── Street labels ────────────────────────────────────────────────────
    layers.append({
        "id": "label-street",
        "type": "symbol",
        "source": "openmaptiles",
        "source-layer": "transportation_name",
        "minzoom": l["street_min_zoom"],
        "layout": {
            "text-field": ["coalesce", ["get", "name:latin"], ["get", "name"]],
            "text-font": [l["font"]],
            "text-size": 10 * s,
            "symbol-placement": "line",
            "text-rotation-alignment": "map",
            "text-max-angle": 30
        },
        "paint": {
            "text-color": p["label_color"],
            "text-halo-color": p["label_halo"],
            "text-halo-width": l["halo_width"],
            "text-opacity": 0.8
        }
    })

    # ── Country labels ───────────────────────────────────────────────────
    layers.append({
        "id": "label-country",
        "type": "symbol",
        "source": "openmaptiles",
        "source-layer": "place",
        "minzoom": l["country_min_zoom"],
        "maxzoom": l.get("country_max_zoom", 10),
        "filter": ["==", ["get", "class"], "country"],
        "layout": {
            "text-field": ["coalesce", ["get", "name:latin"], ["get", "name"]],
            "text-font": [l["font_bold"]],
            "text-size": ["interpolate", ["linear"], ["zoom"],
                2, 10 * s, 5, 16 * s, 8, 20 * s
            ],
            "text-max-width": 8,
            "text-transform": "uppercase",
            "text-letter-spacing": 0.1,
            "symbol-sort-key": ["coalesce", ["get", "rank"], 100],
        },
        "paint": {
            "text-color": p["label_color"],
            "text-halo-color": p["label_halo"],
            "text-halo-width": l["halo_width"]
        }
    })

    # ── State/region labels ──────────────────────────────────────────────
    layers.append({
        "id": "label-state",
        "type": "symbol",
        "source": "openmaptiles",
        "source-layer": "place",
        "minzoom": l["state_min_zoom"],
        "maxzoom": l.get("state_max_zoom", 9),
        "filter": ["==", ["get", "class"], "state"],
        "layout": {
            "text-field": ["coalesce", ["get", "name:latin"], ["get", "name"]],
            "text-font": [l["font_italic"]],
            "text-size": ["interpolate", ["linear"], ["zoom"],
                4, 9 * s, 8, 13 * s
            ],
            "text-max-width": 8,
            "text-transform": "uppercase",
            "text-letter-spacing": 0.15
        },
        "paint": {
            "text-color": "#555555",
            "text-halo-color": p["label_halo"],
            "text-halo-width": l["halo_width"]
        }
    })

    # ── Places: single merged layer ───────────────────────────────────────
    # symbol-sort-key only works within one layer. Multiple layers means
    # cities and towns never compete on sort key — MapLibre evaluates
    # placement per tile bucket so cities can be displaced by towns from
    # adjacent tiles regardless of layer order.
    #
    # text-font: ["literal", [...]] returns an array from a case expression.
    # text-size: single interpolate with case expressions as stop outputs —
    #   data-driven outputs are valid; only zoom-nested-in-zoom is forbidden.
    #
    # Sort key (lower = higher priority, placed first):
    #   national capital:  0 + rank  (Bern = 5)
    #   city:            100 + rank
    #   town:          10000 + rank  (Ostermundigen = 10011)
    #   village:       20000 + rank
    #   suburb:        30000 + rank

    is_capital   = ["all", ["==", ["get", "class"], "city"], ["==", ["get", "capital"], 2]]
    is_city      = ["==", ["get", "class"], "city"]
    # Large towns (Thun, Biel, Fribourg, Köniz ~30–50k): rank ≤ 12 within town class
    # Rank data: Biel=8, Fribourg=10, Thun=11, Köniz=12 → Ostermundigen=13+ excluded
    is_lg_town   = ["all", ["==", ["get", "class"], "town"], ["<=", ["coalesce", ["get", "rank"], 99], 12]]
    is_town      = ["==", ["get", "class"], "town"]
    is_village   = ["==", ["get", "class"], "village"]
    is_suburb    = ["match", ["get", "class"], ["suburb", "neighbourhood", "quarter"], True, False]

    layers.append({
        "id": "label-place",
        "type": "symbol",
        "source": "openmaptiles",
        "source-layer": "place",
        "minzoom": l["city_min_zoom"],
        "filter": ["match", ["get", "class"],
            ["city", "town", "village", "suburb", "neighbourhood", "quarter"], True, False
        ],
        "layout": {
            "text-field": ["coalesce", ["get", "name:latin"], ["get", "name"]],
            "text-font": ["case",
                is_city,    ["literal", [l["font_bold"]]],
                is_lg_town, ["literal", [l["font_bold"]]],
                ["literal", [l["font"]]]
            ],
            # Zoom stops shifted one level earlier vs before.
            # 5 size tiers: capital > city > large-town > town/village > suburb
            "text-size": ["interpolate", ["exponential", 1.2], ["zoom"],
                3,  ["case", is_capital, 10*s, is_city, 9*s, 5*s],
                6,  ["case", is_capital, 15*s, is_city, 12*s, is_lg_town, 10*s, is_town, 10*s, 7*s],
                8,  ["case", is_capital, 17*s, is_city, 14*s, is_lg_town, 12*s, is_town, 11*s, is_village, 9*s, 7*s],
                11, ["case", is_capital, 20*s, is_city, 17*s, is_lg_town, 14*s, is_town, 13*s, is_village, 12*s, 11*s],
                13, ["case", is_capital, 22*s, is_city, 19*s, is_lg_town, 16*s, is_town, 14*s, is_village, 13*s, 13*s]
            ],
            "text-max-width": 8,
            "text-transform": ["case", is_suburb, "uppercase", "none"],
            "text-letter-spacing": ["case", is_suburb, 0.1, 0],
            "symbol-sort-key": ["case",
                is_capital, ["+", 0,     ["coalesce", ["get", "rank"], 100]],
                is_city,    ["+", 100,   ["coalesce", ["get", "rank"], 100]],
                is_town,    ["+", 10000, ["coalesce", ["get", "rank"], 100]],
                is_village, ["+", 20000, ["coalesce", ["get", "rank"], 100]],
                            ["+", 30000, ["coalesce", ["get", "rank"], 100]]
            ],
        },
        "paint": {
            "text-color": ["case",
                is_city,   "#000000",
                is_suburb, "#666666",
                p["label_color"]
            ],
            "text-halo-color": p["label_halo"],
            "text-halo-width": l["halo_width"]
        }
    })

    return layers


# =============================================================================
# Transit layer
# =============================================================================

# (mode value in GeoJSON, minzoom)
# ORDER MATTERS: drawn bottom-to-top — less important modes first,
# so faster/more important lines always render on top.
TRANSIT_MODE_LAYERS = [
    ("mountain",      8),
    ("regional_bus",  8),
    ("bus",          11),
    ("ferry",         8),
    ("metro",         9),
    ("tram",         10),
    ("train",         5),
]

GTFS_MATCHED_FILTER = ["==", ["get", "gtfs_matched"], True]

def build_transit_layers() -> list:
    layers = []
    for mode, minzoom in TRANSIT_MODE_LAYERS:
        base_filter = ["all", ["==", ["get", "mode"], mode], GTFS_MATCHED_FILTER]

        # Casing — halo drawn under the color line so lines separate visually.
        casing_color = "#ffffff"
        layers.append({
            "id": f"transit-{mode}-casing",
            "type": "line",
            "source": "transit_lines",
            "source-layer": "transit_lines",
            "minzoom": minzoom,
            "filter": base_filter,
            "layout": {
                "line-cap": "round",
                "line-join": "round",
                # Slower lines rendered below faster ones within each mode group
                "line-sort-key": ["coalesce", ["get", "speed_kmh"], 0]
            },
            "paint": {
                "line-color": casing_color,
                "line-width": ["interpolate", ["linear"], ["zoom"],
                    minzoom,     ["*", ["get", "width_base"], 0.4 + 0.6],
                    14,          ["+", ["get", "width_base"], 1.5],
                    18,          ["+", ["*", ["get", "width_base"], 4.0], 2.0]
                ],
                "line-opacity": ["interpolate", ["linear"], ["zoom"],
                    minzoom,       0.0,
                    minzoom + 1.5, 0.9
                ]
            }
        })

        # Color line — drawn on top of casing
        layers.append({
            "id": f"transit-{mode}",
            "type": "line",
            "source": "transit_lines",
            "source-layer": "transit_lines",
            "minzoom": minzoom,
            "filter": base_filter,
            "layout": {
                "line-cap": "round",
                "line-join": "round",
                "line-sort-key": ["coalesce", ["get", "speed_kmh"], 0]
            },
            "paint": {
                "line-color": ["get", "color"],
                "line-width": ["interpolate", ["linear"], ["zoom"],
                    minzoom,     ["*", ["get", "width_base"], 0.4],
                    14,          ["get", "width_base"],
                    18,          ["*", ["get", "width_base"], 4.0]
                ],
                "line-opacity": ["interpolate", ["linear"], ["zoom"],
                    minzoom,       0.0,
                    minzoom + 1.5, 0.85
                ]
            }
        })
    return layers


def build_station_layers() -> list:
    """
    Stop dots per mode group, each appearing at the same zoom as its line.
    Rail stations: larger, deduplicated, visible from zoom 5.
    Other modes: smaller, per-stop, appearing at their line's minzoom.
    All disappear at zoom 16 (close-up design deferred).
    """
    layers = []

    # (source, minzoom, fill_radii)
    # Each group has its own PMTiles file with the correct --minimum-zoom baked in,
    # so tippecanoe cannot drop features below that zoom.
    # Circle radius is data-driven: radius = width_base at zoom 14 (= line width in px),
    # so dot diameter = 2 × line width. Same zoom scaling as the transit line layers.
    def dot_radius(minzoom):
        return ["interpolate", ["linear"], ["zoom"],
            minzoom, ["*", ["get", "width_base"], 0.4],
            14,      ["get", "width_base"],
            18,      ["*", ["get", "width_base"], 4.0],
        ]

    # Dots fade IN at minzoom and then stay visible permanently.
    # Pills render on top (painter order) and cover dots via their white casing.
    # No fade-out: stops with no pill remain visible; stops with a pill are hidden
    # underneath it.
    stop_groups = [
        ("transit_stops_rail",      5),
        ("transit_stops_tram",     10),
        ("transit_stops_regional",  9),
        ("transit_stops_bus",      11),
    ]

    for source, minzoom in stop_groups:
        # Ferry is stored in the regional PMTiles but has its own dedicated layer below.
        extra_filter = [["!=", ["get", "mode"], "ferry"]] if source == "transit_stops_regional" else []
        layer_filter = ["all"] + extra_filter if extra_filter else None

        layer = {
            "id": f"transit-stop-fill-{source}",
            "type": "circle",
            "source": source,
            "source-layer": "transit_stops",
            "minzoom": minzoom,
            "paint": {
                "circle-color": ["get", "color"],
                "circle-radius": dot_radius(minzoom),
                "circle-opacity": ["interpolate", ["linear"], ["zoom"],
                    minzoom,       0.0,
                    minzoom + 1.0, 1.0,
                ]
            }
        }
        if layer_filter:
            layer["filter"] = layer_filter
        layers.append(layer)

    # Ferry stops: own layer, data-driven radius, no fade-out (ferry has no pills).
    layers.append({
        "id": "transit-stop-fill-ferry",
        "type": "circle",
        "source": "transit_stops_regional",
        "source-layer": "transit_stops",
        "minzoom": 9,
        "filter": ["==", ["get", "mode"], "ferry"],
        "paint": {
            "circle-color": ["get", "color"],
            "circle-radius": dot_radius(9),
            "circle-opacity": ["interpolate", ["linear"], ["zoom"],
                9, 0.0, 10.0, 1.0
            ]
        }
    })

    # --- Pill layers (LineString features from tl_stop_pills.pmtiles) ---
    # Three layers in painter order: casing → fill → connector.
    # All use the same source/source-layer; feature_type property distinguishes them.
    # width_base on the feature encodes the dominant line width; pill = ×2, connector = ×1.
    PILL_MINZOOM = 11  # earliest any pill appears (train with ≥5 stops)

    def pill_width(multiplier):
        return ["interpolate", ["linear"], ["zoom"],
            PILL_MINZOOM,     ["*", ["get", "width_base"], multiplier * 0.4],
            14,               ["*", ["get", "width_base"], multiplier],
            18,               ["*", ["get", "width_base"], multiplier * 4.0],
        ]

    def pill_opacity(appear_zoom):
        return ["interpolate", ["linear"], ["zoom"],
            appear_zoom,        0.0,
            appear_zoom + 1.0,  1.0,
        ]

    # Casing (white outline behind the colored fill)
    layers.append({
        "id": "transit-stop-pill-casing",
        "type": "line",
        "source": "transit_stop_pills",
        "source-layer": "transit_stop_pills",
        "minzoom": PILL_MINZOOM,
        "filter": ["==", ["get", "feature_type"], "pill"],
        "layout": {
            "line-cap": "round",
            "line-join": "round",
        },
        "paint": {
            "line-color": "#ffffff",
            "line-width": ["interpolate", ["linear"], ["zoom"],
                PILL_MINZOOM,  ["*", ["get", "width_base"], 0.4 * 2 + 1.5],
                14,            ["+", ["*", ["get", "width_base"], 2], 1.5],
                18,            ["+", ["*", ["get", "width_base"], 8.0], 2.0],
            ],
            "line-opacity": pill_opacity(PILL_MINZOOM),
        }
    })

    # Pill fill
    layers.append({
        "id": "transit-stop-pill-fill",
        "type": "line",
        "source": "transit_stop_pills",
        "source-layer": "transit_stop_pills",
        "minzoom": PILL_MINZOOM,
        "filter": ["==", ["get", "feature_type"], "pill"],
        "layout": {
            "line-cap": "round",
            "line-join": "round",
        },
        "paint": {
            "line-color": ["get", "color"],
            "line-width": pill_width(2),
            "line-opacity": pill_opacity(PILL_MINZOOM),
        }
    })

    # Connector (half the pill width, same color, round caps)
    layers.append({
        "id": "transit-stop-pill-connector",
        "type": "line",
        "source": "transit_stop_pills",
        "source-layer": "transit_stop_pills",
        "minzoom": PILL_MINZOOM,
        "filter": ["==", ["get", "feature_type"], "connector"],
        "layout": {
            "line-cap": "round",
            "line-join": "round",
        },
        "paint": {
            "line-color": ["get", "color"],
            "line-width": pill_width(1),
            "line-opacity": pill_opacity(PILL_MINZOOM),
        }
    })

    # --- DEBUG: fixed-size 1.5px black dots at every raw stop position ---
    # Use these to diagnose pill clustering (one dot per raw stop, before clustering).
    # Remove this block once pills look correct.
    debug_stop_sources = [
        ("transit_stops_rail",      5),   # rail stops visible from zoom 5
        ("transit_stops_tram",     10),
        ("transit_stops_regional",  9),
        ("transit_stops_bus",      11),
    ]
    for src, dbg_minzoom in debug_stop_sources:
        layers.append({
            "id": f"debug-stop-dot-{src}",
            "type": "circle",
            "source": src,
            "source-layer": "transit_stops",
            "minzoom": dbg_minzoom,
            "paint": {
                "circle-color": "#000000",
                "circle-radius": 1.5,
                "circle-opacity": 0.8,
            }
        })

    return layers


# =============================================================================
# Main assembly
# =============================================================================

def generate_style(cfg) -> dict:
    g = cfg["global"]

    source_type = g.get("tile_source_type", "tiles")
    if source_type == "tilejson":
        source_def = {"type": "vector", "url": g["tile_source"]}
    else:
        source_def = {"type": "vector", "tiles": [g["tile_source"]], "maxzoom": 14}

    style = {
        "version": 8,
        "name": g["name"],
        "sources": {
            "openmaptiles": source_def,
            "transit_lines": {
                "type": "vector",
                "url": "pmtiles:///tl_lines.pmtiles"
            },
            "transit_stops_rail": {
                "type": "vector",
                "url": "pmtiles:///tl_stops_rail.pmtiles"
            },
            "transit_stops_tram": {
                "type": "vector",
                "url": "pmtiles:///tl_stops_tram.pmtiles"
            },
            "transit_stops_regional": {
                "type": "vector",
                "url": "pmtiles:///tl_stops_regional.pmtiles"
            },
            "transit_stops_bus": {
                "type": "vector",
                "url": "pmtiles:///tl_stops_bus.pmtiles"
            },
            "transit_stop_pills": {
                "type": "vector",
                "url": "pmtiles:///tl_stop_pills.pmtiles"
            }
        },
        "glyphs": g["glyphs"],
        "center": g["center"],
        "zoom": g["zoom"],
        "layers": []
    }

    if g.get("sprite"):
        style["sprite"] = g["sprite"]

    style["layers"].append(build_background_layer(cfg))
    style["layers"].extend(build_landuse_layers(cfg))
    style["layers"].extend(build_water_layers(cfg))
    style["layers"].extend(build_building_layers(cfg))
    style["layers"].extend(build_rail_layers(cfg, modes=["tunnel", "normal"]))
    style["layers"].extend(build_road_layers(cfg, modes=["tunnel", "normal"]))
    style["layers"].extend(build_path_layers(cfg, modes=["tunnel", "normal"]))
    style["layers"].extend(build_bridge_deck_layer(cfg))
    style["layers"].extend(build_rail_layers(cfg, modes=["bridge"]))
    style["layers"].extend(build_road_layers(cfg, modes=["bridge"]))
    style["layers"].extend(build_path_layers(cfg, modes=["bridge"]))
    style["layers"].extend(build_transit_layers())
    style["layers"].extend(build_station_layers())
    style["layers"].extend(build_border_layers(cfg))
    style["layers"].extend(build_label_layers(cfg))

    return style


def main():
    script_dir = Path(__file__).parent
    parser = argparse.ArgumentParser(description="Generate car-free map style")
    parser.add_argument("-c", "--config", default=script_dir / "config.yaml", help="Config YAML path")
    parser.add_argument("-o", "--output", default=script_dir / "../static/style.json", help="Output style JSON path")
    args = parser.parse_args()

    cfg = load_config(args.config)
    style = generate_style(cfg)

    with open(args.output, "w") as f:
        json.dump(style, f, indent=2, ensure_ascii=False)

    layer_count = len(style["layers"])
    print(f"Generated {args.output} with {layer_count} layers.")


if __name__ == "__main__":
    main()
