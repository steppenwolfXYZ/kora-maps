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

        if mode == "tunnel":
            layers.append({
                "id": f"path-paved{suffix}",
                "type": "line",
                "source": "openmaptiles",
                "source-layer": "transportation",
                "minzoom": pc["min_zoom"],
                "filter": ["all",
                    path_filter_base,
                    bf,
                    ["<", ["coalesce", ["get", "layer"], 0], 0]
                ],
                "layout": {"line-cap": "round", "line-join": "round"},
                "paint": {
                    "line-color": pc["tunnel_color"],
                    "line-width": pc["tunnel_width"],
                    "line-opacity": pc["tunnel_opacity"]
                }
            })
            layers.append({
                "id": f"path-passage{suffix}",
                "type": "line",
                "source": "openmaptiles",
                "source-layer": "transportation",
                "minzoom": pc["min_zoom"],
                "filter": ["all",
                    path_filter_base,
                    bf,
                    [">=", ["coalesce", ["get", "layer"], 0], 0]
                ],
                "layout": {"line-cap": "round", "line-join": "round"},
                "paint": {
                    "line-color": pc["color"],
                    "line-width": pc["width"],
                    "line-opacity": ["interpolate", ["linear"], ["zoom"],
                        pc["min_zoom"], 0.4,
                        16, 0.8
                    ]
                }
            })
            continue

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
                    pc["min_zoom"], 0.4,
                    16, 0.8
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
                    pc["min_zoom"], 0.4,
                    16, 0.8
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
        "filter": ["all",
            ["<=", ["get", "rank"], 14],
            ["!", ["match", ["get", "class"],
                ["railway", "bus", "aerialway", "ferry_terminal"], True, False
            ]]
        ],
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

# Per-zoom-level visibility (see .claude/concepts/zoom-level-rules.md): each
# transit_line feature carries `tippecanoe.minzoom = min_zoom` baked in by
# 06_score_and_match.py. The layer floor is a hard absolute cap (z4 — the
# lowest level any train line can reach). Per-feature tippecanoe.minzoom
# does the actual zoom gating; there is no runtime filter or opacity step
# expression. ORDER MATTERS: drawn bottom-to-top — less important modes
# first, so faster/more important lines always render on top.
TRANSIT_MODE_LAYERS = [
    "mountain",
    "regional_bus",
    "bus",
    "ferry",
    "metro",
    "tram",
    "train",
]

TRANSIT_LINE_FLOOR_ZOOM = 4

GTFS_MATCHED_FILTER = ["==", ["get", "gtfs_matched"], True]


def build_transit_layers() -> list:
    layers = []
    floor = TRANSIT_LINE_FLOOR_ZOOM
    for mode in TRANSIT_MODE_LAYERS:
        base_filter = ["all",
                       ["==", ["get", "mode"], mode],
                       GTFS_MATCHED_FILTER]

        # Casing — halo drawn under the color line so lines separate visually.
        casing_color = "#ffffff"
        layers.append({
            "id": f"transit-{mode}-casing",
            "type": "line",
            "source": "transit_lines",
            "source-layer": "transit_lines",
            "minzoom": floor,
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
                    floor,       ["+", ["*", ["get", "width_base"], 0.4], 2.0],
                    14,          ["+", ["get", "width_base"], 2.0],
                    18,          ["+", ["*", ["get", "width_base"], 4.0], 2.0]
                ],
                "line-opacity": 0.9
            }
        })

        # Color line — drawn on top of casing
        layers.append({
            "id": f"transit-{mode}",
            "type": "line",
            "source": "transit_lines",
            "source-layer": "transit_lines",
            "minzoom": floor,
            "filter": base_filter,
            "layout": {
                "line-cap": "round",
                "line-join": "round",
                "line-sort-key": ["coalesce", ["get", "speed_kmh"], 0]
            },
            "paint": {
                "line-color": ["get", "color"],
                "line-width": ["interpolate", ["linear"], ["zoom"],
                    floor,       ["*", ["get", "width_base"], 0.4],
                    14,          ["get", "width_base"],
                    18,          ["*", ["get", "width_base"], 4.0]
                ],
                "line-opacity": 0.85
            }
        })
    return layers


def build_station_layers(cfg) -> list:
    """
    Stop dots per mode group, each appearing at the same zoom as its line.
    Rail stations: larger, deduplicated, visible from zoom 5.
    Other modes: smaller, per-stop, appearing at their line's minzoom.
    All disappear at zoom 16 (close-up design deferred).
    """
    layers = []

    def dot_radius(minzoom):
        return ["interpolate", ["linear"], ["zoom"],
            minzoom, ["*", ["get", "width_base"], 0.3],
            14,      ["*", ["get", "width_base"], 0.75],
            20,      ["*", ["get", "width_base"], 6.0],
        ]

    stop_groups = [
        ("transit_stops_rail",      5),
        ("transit_stops_tram",     10),
        ("transit_stops_regional",  9),
        ("transit_stops_bus",      11),
    ]

    for source, minzoom in stop_groups:
        layer = {
            "id": f"transit-stop-fill-{source}",
            "type": "circle",
            "source": source,
            "source-layer": "transit_stops",
            "minzoom": minzoom,
            "paint": {
                "circle-color": "#ffffff",
                "circle-radius": dot_radius(minzoom),
                "circle-stroke-color": "#000000",
                "circle-stroke-width": 1.0,
            }
        }
        layers.append(layer)

    # Ferry stops follow the same two-tier pattern as every other non-train
    # mode: a low-zoom dot at z9–z12 (rendered through the regional source
    # above) and a medium-zoom endpoint disc + optional connector + GTFS
    # endpoint at z13+ (rendered through the pill paint stack below). The
    # far-zoom dot is emitted at the canonical pier vertex; the pill paint
    # stack carries the connector seam handling. See
    # far-zoom-stop-markers.md § "Ferry far-zoom marker".

    # Hard cut at the appear-zoom — no opacity fade. Uniform z13 for
    # every mode per `.claude/concepts/pill-zoom-stop-tweaks.md`.
    PILL_MINZOOM = 13

    def pill_disc_width():
        return ["interpolate", ["linear"], ["zoom"],
            PILL_MINZOOM,  ["*", ["get", "width_base"], 0.6],
            14,            ["*", ["get", "width_base"], 1.5],
            20,            ["*", ["get", "width_base"], 12.0],
        ]

    def connector_width():
        return ["interpolate", ["linear"], ["zoom"],
            PILL_MINZOOM,  ["*", ["get", "width_base"], 0.3],
            14,            ["*", ["get", "width_base"], 0.75],
            18,            ["*", ["get", "width_base"], 3.0],
        ]

    layers.append({
        "id": "transit-stop-pill-casing",
        "type": "line",
        "source": "transit_stop_pills",
        "source-layer": "transit_stop_pills",
        "minzoom": PILL_MINZOOM,
        "filter": ["==", ["get", "feature_type"], "pill"],
        "layout": {"line-cap": "round", "line-join": "round"},
        "paint": {
            "line-color": "#000000",
            "line-width": ["interpolate", ["linear"], ["zoom"],
                PILL_MINZOOM,  ["+", ["*", ["get", "width_base"], 0.6], 2.0],
                14,            ["+", ["*", ["get", "width_base"], 1.5], 2.0],
                20,            ["+", ["*", ["get", "width_base"], 12.0], 2.0],
            ],
        }
    })

    # Connector casing drawn before pill fill so pill fill covers the junction — no white seam
    layers.append({
        "id": "transit-stop-pill-connector-casing",
        "type": "line",
        "source": "transit_stop_pills",
        "source-layer": "transit_stop_pills",
        "minzoom": PILL_MINZOOM,
        "filter": ["==", ["get", "feature_type"], "connector"],
        "layout": {"line-cap": "round", "line-join": "round"},
        "paint": {
            "line-color": "#000000",
            "line-width": ["interpolate", ["linear"], ["zoom"],
                PILL_MINZOOM,  ["+", ["*", ["get", "width_base"], 0.3], 2.0],
                14,            ["+", ["*", ["get", "width_base"], 0.75], 2.0],
                18,            ["+", ["*", ["get", "width_base"], 3.0], 2.0],
            ],
        }
    })

    layers.append({
        "id": "transit-stop-pill-fill",
        "type": "line",
        "source": "transit_stop_pills",
        "source-layer": "transit_stop_pills",
        "minzoom": PILL_MINZOOM,
        "filter": ["==", ["get", "feature_type"], "pill"],
        "layout": {"line-cap": "round", "line-join": "round"},
        "paint": {
            "line-color": "#ffffff",
            "line-width": pill_disc_width(),
        }
    })

    # Endpoint circles drawn before connector-fill so the connector's colored line
    # covers the white stroke at the junction — no white seam where they meet.
    layers.append({
        "id": "transit-stop-pill-endpoint",
        "type": "circle",
        "source": "transit_stop_pills",
        "source-layer": "transit_stop_pills",
        "minzoom": PILL_MINZOOM,
        "filter": ["==", ["get", "feature_type"], "endpoint"],
        "paint": {
            "circle-color": "#ffffff",
            "circle-radius": ["interpolate", ["linear"], ["zoom"],
                PILL_MINZOOM, ["*", ["get", "width_base"], 0.3],
                14,           ["*", ["get", "width_base"], 0.75],
                20,           ["*", ["get", "width_base"], 6.0],
            ],
            "circle-stroke-color": "#000000",
            "circle-stroke-width": 1.0,
        }
    })

    layers.append({
        "id": "transit-stop-pill-connector",
        "type": "line",
        "source": "transit_stop_pills",
        "source-layer": "transit_stop_pills",
        "minzoom": PILL_MINZOOM,
        "filter": ["==", ["get", "feature_type"], "connector"],
        "layout": {"line-cap": "round", "line-join": "round"},
        "paint": {
            "line-color": "#ffffff",
            "line-width": connector_width(),
        }
    })

    # --- Color indicators (z13+) ---------------------------------------------
    # Mini per-color-group dots inside stop dots, endpoint discs, and pills.
    # See `.claude/concepts/stop-color-indicators.md` and
    # `.claude/concepts/pill-zoom-stop-tweaks.md`.
    #
    # Layout: centered row of up to 3 indicators (current data max).
    # Each indicator carries `slot_units` (integer in [-5, +5]; n=1 → {0};
    # n=2 → {-1, +1}; n=3 → {-2, 0, +2}) and `tangent_deg` (0 for
    # dots/discs, pill tangent for pill indicators).
    #
    # Indicators appear at the same zoom as pills (z13) with no opacity
    # fade. Each feature carries `parent_width_base` (the floor-clamped
    # width_base of the parent stop) and `n_indicators` (count in the
    # row) so the text-size expression can shrink the row to fit when
    # the parent is too thin for the default size.
    INDICATOR_MINZOOM = 13

    # half_spacing_em and vert_em compensate for the "●" glyph's vertical
    # asymmetry inside its em-box. The row span across N indicators is
    # roughly `(0.56*N + 0.14)` em (glyph diameter ~0.7 em, gap between
    # centers 2*half_spacing_em = 0.56 em).
    half_spacing_em = 0.28
    vert_em = -0.1
    INDICATOR_INNER_MARGIN = 0.7  # fraction of parent inner dim usable

    # row_factor (em-units of the indicator row's binding extent) is
    # stamped per feature by the pipeline — `0.70` for pill parents
    # (single glyph diameter through the pill thickness; row length
    # along the pill's long axis is unbounded) and `0.56*n + 0.14`
    # for disc/dot parents (full row span through the round
    # diameter). See `.claude/concepts/pill-zoom-stop-tweaks.md`
    # § "Indicators must not overflow the parent".

    # Per-zoom anchor: min(default_size_at_z, parent_wb * margin * mult / row).
    # MapLibre requires zoom only at the top-level interpolate, so the
    # min/fit math is baked separately into each anchor — `default_size`
    # and `parent_diameter_multiplier` are evaluated at the anchor's
    # specific zoom, and only the resulting expression goes into the
    # interpolate stops.
    def _indicator_size_at_zoom(default_size, parent_diameter_mult):
        return ["min",
            default_size,
            ["/",
                ["*",
                    ["get", "parent_width_base"],
                    parent_diameter_mult * INDICATOR_INNER_MARGIN,
                ],
                ["get", "row_factor"],
            ],
        ]

    # Default text-size curve is anchored at z13 → 4.5 and z20 → 36; the
    # z14 anchor is the linearly interpolated value (9.0). The
    # parent-diameter curve must match `pill_disc_width()` above
    # (PILL_MINZOOM=13 → 0.6, z14 → 1.5, z20 → 12.0) since the indicator
    # row sits inside that diameter.
    text_size_expr = ["interpolate", ["linear"], ["zoom"],
        13, _indicator_size_at_zoom(4.5,  0.6),
        14, _indicator_size_at_zoom(9.0,  1.5),
        20, _indicator_size_at_zoom(36.0, 12.0),
    ]

    text_offset_expr = ["match", ["get", "slot_units"]]
    for k in range(-5, 6):
        text_offset_expr.append(k)
        text_offset_expr.append(["literal", [k * half_spacing_em, vert_em]])
    text_offset_expr.append(["literal", [0.0, vert_em]])  # default

    layers.append({
        "id": "transit-stop-indicator",
        "type": "symbol",
        "source": "transit_stop_pills",
        "source-layer": "transit_stop_pills",
        "minzoom": INDICATOR_MINZOOM,
        "filter": ["==", ["get", "feature_type"], "indicator"],
        "layout": {
            "text-field": "●",
            "text-font": ["Noto Sans Regular"],
            "text-size": text_size_expr,
            "text-offset": text_offset_expr,
            "text-rotate": ["coalesce", ["get", "tangent_deg"], 0],
            "text-rotation-alignment": "map",
            "text-allow-overlap": True,
            "text-ignore-placement": True,
            "text-padding": 0,
        },
        "paint": {
            "text-color": ["get", "color"],
        }
    })

    if not cfg.get("transit_pipeline", {}).get("debug", {}).get("debug_overlay", False):
        return layers

    # Debug overlay (pill-rendering concept): thin black line tracing each
    # platform's full allowed range along the line's polyline. Replaces the
    # previous debug-dot. Per-mode minzooms are baked into the features via
    # tippecanoe, so a single layer covers every mode.
    layers.append({
        "id": "debug-platform-line",
        "type": "line",
        "source": "transit_debug_platforms",
        "source-layer": "transit_debug_platforms",
        "minzoom": 5,
        "layout": {"line-cap": "round", "line-join": "round"},
        "paint": {
            "line-color": "#000000",
            "line-width": 0.6,
            "line-opacity": 0.7,
        }
    })

    # Debug overlay: clickable dot at every stop's GTFS coordinate. Carries
    # the atlas platform length and the list of lines visiting that stop
    # (with origin / destination); rendered as a popup on click. Stabbed
    # dots (those placed onto a max-stab bar) render as solid black fill;
    # non-stabbed dots stay hollow (white fill with black outline).
    layers.append({
        "id": "debug-stop-dot",
        "type": "circle",
        "source": "transit_debug_stops",
        "source-layer": "transit_debug_stops",
        "minzoom": 5,
        "paint": {
            "circle-color": [
                "case",
                ["==", ["get", "stabbed"], True], "#000000",
                "#ffffff"
            ],
            "circle-stroke-color": "#000000",
            "circle-radius": 3,
            "circle-stroke-width": 1,
            "circle-opacity": 0.9,
            "circle-stroke-opacity": 0.9,
        }
    })

    # Debug overlay: thick white line drawn over each max-stab bar so the
    # bar's actual position and orientation are visible at a glance.
    layers.append({
        "id": "debug-max-stab-bar",
        "type": "line",
        "source": "transit_debug_bars",
        "source-layer": "transit_debug_bars",
        "minzoom": 5,
        "layout": {"line-cap": "round"},
        "paint": {
            "line-color": "#ffffff",
            "line-width": 4,
            "line-opacity": 0.9,
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
            },
        },
        "glyphs": g["glyphs"],
        "center": g["center"],
        "zoom": g["zoom"],
        "layers": []
    }

    if g.get("sprite"):
        style["sprite"] = g["sprite"]

    if cfg.get("transit_pipeline", {}).get("debug", {}).get("debug_overlay", False):
        style["sources"]["transit_debug_platforms"] = {
            "type": "vector",
            "url": "pmtiles:///tl_debug_platforms.pmtiles"
        }
        style["sources"]["transit_debug_stops"] = {
            "type": "vector",
            "url": "pmtiles:///tl_debug_stops.pmtiles"
        }
        style["sources"]["transit_debug_bars"] = {
            "type": "vector",
            "url": "pmtiles:///tl_debug_bars.pmtiles"
        }

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
    style["layers"].extend(build_border_layers(cfg))
    style["layers"].extend(build_label_layers(cfg))
    style["layers"].extend(build_station_layers(cfg))

    return style


def main():
    script_dir = Path(__file__).parent
    parser = argparse.ArgumentParser(description="Generate car-free map style")
    parser.add_argument("-c", "--config", default=script_dir / "config.yaml", help="Config YAML path")
    parser.add_argument("-o", "--output", default=script_dir / "../static/style.json", help="Output style JSON path")
    args = parser.parse_args()

    cfg = load_config(args.config)
    cfg["transit_pipeline"] = load_config(script_dir / "transit" / "config.yaml")
    style = generate_style(cfg)

    with open(args.output, "w") as f:
        json.dump(style, f, indent=2, ensure_ascii=False)

    layer_count = len(style["layers"])
    print(f"Generated {args.output} with {layer_count} layers.")


if __name__ == "__main__":
    main()
