"""Basemap transportation layers: rail, road, path, bridge deck.

Every function returns a list of MapLibre layer dicts consumed by the
main assembler. The bridge deck sits between the normal-mode and
bridge-mode calls of rail / road / path so features on a bridge render
above the deck and features under a bridge render below it.
"""
from .helpers import (
    MAIN_ROAD_CLASSES,
    MOTORWAY_CLASSES,
    PATH_CLASSES,
    RAIL_CLASSES,
    WALKABLE_EXCLUDE,
    WALKABLE_WIDTH_GROUPS,
    _width_interp,
    brunnel_filter,
    class_filter,
    lerp_color,
    meters_to_pixels,
)


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
    p = cfg["palette"]
    rw = r["real_widths"]

    layers = []

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
    layers = []

    if modes is None:
        modes = ["tunnel", "normal", "bridge"]

    # Polygons in this class are pedestrian areas, painted by
    # build_pedestrian_area_layer as a fill. A line layer would render them
    # as their outline, which looks exactly like a footway and isn't one.
    path_filter_base = ["all",
        class_filter(PATH_CLASSES),
        ["!=", ["geometry-type"], "Polygon"],
    ]

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


def build_pedestrian_area_layer(cfg):
    """Pedestrian squares and plazas as a surface fill.

    These come through as Polygons in the `transportation` layer's
    `class=path` bucket, mixed in with ordinary footway lines. Painted with
    the landuse block rather than with the paths so that every street, rail
    and path line draws on top — the square is ground the others cross, not
    a feature that covers them.
    """
    pc = cfg["paths"]
    return {
        "id": "pedestrian-area",
        "type": "fill",
        "source": "openmaptiles",
        "source-layer": "transportation",
        "minzoom": pc["area_min_zoom"],
        "filter": ["all",
            class_filter(PATH_CLASSES),
            ["==", ["geometry-type"], "Polygon"],
        ],
        "paint": {"fill-color": pc["area_color"]},
    }
