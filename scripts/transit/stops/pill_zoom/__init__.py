"""Pill-zoom (z14–16) stop rendering: coordinate placement, pill assembly,
connectors."""
from stops.pill_zoom.lines import (
    build_indicator_features, cluster_lines, color_luminance,
    count_unique_lines, dominant_line, pill_minzoom,
)
from stops.pill_zoom.make import make_pill_features
from stops.pill_zoom.nn_path import nearest_neighbor_path
from stops.pill_zoom.place import coordinate_dots_global_stab
