"""Basemap borders + text labels.

Country / state borders and every text label on the base map (POI, water,
streets, countries, states, cities / towns / suburbs). Transit stops are
labelled separately in `transit_stations.py`.
"""


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
