"""Close-zoom feature writer — the entry point of the close-zoom
rendering pipeline.

The body is very long and doesn't decompose into functions cleanly
(dozens of shared local variables between phases + tight closures). To
keep every file under the 1000-line cap the body is split across two
sibling files that are exec'd in one shared namespace.
"""
import pathlib

from _state import *  # noqa: F401,F403
from stops.extent import _length_key, _platform_extent
from stops.ferry_snap import _ferry_pier_t_on_line, _obb_overlap
from geometry import (
    _cum_dist_m, _directional_tangent_at, _interp_at, _meters_per_deg,
    _project_meters, _slice_polyline, flatten_coords, haversine_km,
)
from stops.close_zoom.constants import *  # noqa: F401,F403
from stops.close_zoom.helpers import (
    _blend_colors, _build_straight_pill_arrow, _closest_way_distance_m,
    _extent_overlap, _is_hybrid_tram_stop, _local_offset_to_lonlat,
    _offset_track, _orient_rail_extent, _point_at_extrap,
    _rail_direction_order, _rounded_hull_polygon, _sample_ts,
    _shorten_curb, _stop_course,
    _track_pos, _union_extents,
    _unit_chord_metric, _unit_tangent_metric, _variant_priority,
)
from stops.close_zoom.text import (
    _shorten_destination, _shrink_ref_font_m, _text_width_em,
    _text_width_em_bold, _wrap_label,
)
from stops.close_zoom.visits import (
    _collapse_direction_stacks, _collect_close_zoom_visits,
    _stack_need_by_stop,
)

_HERE = pathlib.Path(__file__).parent


def write_close_zoom_features(line_stops: dict, line_lookup: dict,
                                stop_meta: dict, stop_attrs: dict,
                                end_of_platform_pairs: set,
                                skip_first_oids: set,
                                skip_last_oids: set,
                                rail_idx=None,
                                tram_idx=None) -> None:
    """Emit transit_close_zoom.geojson — pill-arrow polygons and backdrop
    line-segments that together produce the close-zoom (z17+) station
    representation. See .claude/concepts/stops-close-zoom.md.

    Body is split across `_writer_visits.py` (setup + collect visits) and
    `_writer_render.py` (main render loop + backdrop assembly) — they run
    in one shared namespace via exec() so they see each other's locals.
    """
    scope = dict(globals())
    scope.update(locals())
    import json as _json
    scope.setdefault("json", _json)
    from collections import defaultdict as _dd
    scope.setdefault("defaultdict", _dd)
    for phase in ("_writer_visits.py", "_writer_render.py"):
        path = _HERE / phase
        with open(path) as f:
            exec(compile(f.read(), str(path), "exec"), scope)
