#!/usr/bin/env python3
"""
Car-Free Map Style Generator
==============================
Reads config.yaml and produces a MapLibre GL style JSON.

Usage:
    python generate_style.py                    # reads ./config.yaml, writes ../static/map-assets/style.json
    python generate_style.py -c myconfig.yaml   # custom config path
    python generate_style.py -o output.json     # custom output path
"""

import argparse
import json
import sys
from pathlib import Path

# Make scripts/style/ importable when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from style.basemap_labels import build_border_layers, build_label_layers
from style.basemap_landuse import (
    build_background_layer,
    build_building_layers,
    build_landuse_layers,
    build_water_layers,
)
from style.basemap_transport import (
    build_bridge_deck_layer,
    build_path_layers,
    build_rail_layers,
    build_road_layers,
)
from style.helpers import load_config
from style.transit_lines import build_close_zoom_backdrop_layers, build_transit_layers
from style.transit_stations import build_station_layers


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
                "url": "pmtiles:///map-assets/tl_lines.pmtiles"
            },
            "transit_stops_rail": {
                "type": "vector",
                "url": "pmtiles:///map-assets/tl_stops_rail.pmtiles"
            },
            "transit_stops_tram": {
                "type": "vector",
                "url": "pmtiles:///map-assets/tl_stops_tram.pmtiles"
            },
            "transit_stops_regional": {
                "type": "vector",
                "url": "pmtiles:///map-assets/tl_stops_regional.pmtiles"
            },
            "transit_stops_bus": {
                "type": "vector",
                "url": "pmtiles:///map-assets/tl_stops_bus.pmtiles"
            },
            "transit_stop_pills": {
                "type": "vector",
                "url": "pmtiles:///map-assets/tl_stop_pills.pmtiles"
            },
            "transit_close_zoom": {
                "type": "vector",
                "url": "pmtiles:///map-assets/tl_close_zoom.pmtiles"
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
            "url": "pmtiles:///map-assets/tl_debug_platforms.pmtiles"
        }
        style["sources"]["transit_debug_stops"] = {
            "type": "vector",
            "url": "pmtiles:///map-assets/tl_debug_stops.pmtiles"
        }
        style["sources"]["transit_debug_bars"] = {
            "type": "vector",
            "url": "pmtiles:///map-assets/tl_debug_bars.pmtiles"
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
    style["layers"].extend(build_close_zoom_backdrop_layers())
    style["layers"].extend(build_transit_layers())
    style["layers"].extend(build_border_layers(cfg))
    style["layers"].extend(build_label_layers(cfg))
    style["layers"].extend(build_station_layers(cfg))

    return style


def main():
    script_dir = Path(__file__).parent
    parser = argparse.ArgumentParser(description="Generate car-free map style")
    parser.add_argument("-c", "--config", default=script_dir / "config.yaml", help="Config YAML path")
    parser.add_argument("-o", "--output", default=script_dir / "../static/map-assets/style.json", help="Output style JSON path")
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
