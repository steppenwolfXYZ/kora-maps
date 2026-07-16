"""Basemap layers: background + landuse + water + buildings.

The visual base of the map — colored areas that everything else renders on
top of. Each `build_*_layers(cfg)` returns a list of MapLibre layer dicts.
"""


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
