"""Transit line layers + close-zoom station backdrop.

The line rendering itself is compact: per-mode casing + colored line pairs.
The close-zoom backdrop sits below the lines so the yellow station tint
shows through the transit ribbon.
"""

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


def build_close_zoom_backdrop_layers() -> list:
    """Station-area tint for the close-zoom design (z17+): one rounded hull
    polygon per parent station, emitted by step 07 with a `bg_color` — the
    line's color, or a blend of all serving lines' colors (MapLibre cannot
    gradient-fill a polygon, so the blend stands in for a gradient).
    Inserted BELOW the transit lines so the tint sits behind them (and
    behind the pill-arrows, which live in build_station_layers)."""
    return [{
        "id": "close-zoom-station-backdrop",
        "type": "fill",
        "source": "transit_close_zoom",
        "source-layer": "transit_close_zoom",
        "minzoom": 17,
        "filter": ["==", ["get", "feature_type"], "backdrop"],
        "paint": {
            "fill-color": ["coalesce", ["get", "bg_color"], "#b340c9"],
            "fill-opacity": 0.35,
            "fill-antialias": True,
        },
    }]
