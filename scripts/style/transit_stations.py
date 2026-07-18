"""Transit stop rendering: far-zoom dots, pill-zoom pills/discs/connectors,
color indicators, close-zoom pill-arrows.

`build_station_layers(cfg)` returns the whole stack in one call — one
function because the layers are heavily interleaved and share zoom
anchors, and splitting them further would just spread the same closures
across two files.
"""


def build_station_layers(cfg) -> list:
    """
    Stop dots per mode group, each appearing at the same zoom as its line.
    Rail stations: larger, deduplicated, visible from zoom 5.
    Other modes: smaller, per-stop, appearing at their line's minzoom.
    All disappear at zoom 16 (close-up design deferred).
    """
    layers = []

    # Two style layers per stop source, drawing the dot as separate entities
    # at the far-zoom and pill-zoom ranges. The pill-zoom layer is the pill
    # design concept's domain — `width_base × zoom` interpolation,
    # untouched here. The far-zoom layer is the
    # `stops-far-zoom-dot-redesign.md` concept's domain — score-driven dot
    # sizes at z7–z12.99 only.

    # Pill-zoom dot (z14+): radius = pill diameter / 2. Matches
    # endpoint-disc radius above (see stops-pill-zoom.md § "Visual style").
    # `source_minzoom` is unused here — the layer's own `minzoom` gates
    # visibility; expression anchors start at z14.
    def dot_radius_pill_zoom(source_minzoom):
        return ["interpolate", ["linear"], ["zoom"],
            14, ["+", 2.25, ["*", ["min", ["get", "width_base"], 5.0], 1.15]],
            15, ["+", 3.0,  ["*", ["min", ["get", "width_base"], 5.0], 1.6]],
            16, ["+", 4.0,  ["*", ["min", ["get", "width_base"], 5.0], 2.2]],
            17, ["+", 7.0,  ["*", ["min", ["get", "width_base"], 5.0], 2.2]],
        ]

    # Far-zoom dot (z7–z12.99): tier-driven diameter. See
    # `.claude/concepts/stops-far-zoom-dot-redesign.md`. Each tier defines
    # a fixed diameter at the z7 and z13 corners; the size interpolates
    # linearly with zoom between those corners. `stop_tier` is baked onto
    # every dot by step 06 (via step 07's `load_stop_scores`); dots with
    # an unknown or missing tier fall through to the `small_bus` default.
    stop_dot_cfg = (cfg.get("transit_pipeline", {})
                       .get("stop_dot_sizing") or {})
    tier_sizes_cfg = stop_dot_cfg.get("tier_sizes") or {}

    # {tier_name: (z7_diameter, z13_diameter)} — corners as configured.
    tier_diameters = {}
    for name, corners in tier_sizes_cfg.items():
        if not isinstance(corners, dict):
            continue
        try:
            tier_diameters[name] = (float(corners.get("z7", 2.0)),
                                    float(corners.get("z13", 4.0)))
        except (TypeError, ValueError):
            continue
    if "small_bus" not in tier_diameters:
        tier_diameters["small_bus"] = (2.0, 4.0)

    def _match_radius_at(zoom):
        """MapLibre `match` on stop_tier returning circle-radius (px) at
        the given integer zoom. Uses linear interpolation between each
        tier's z7 and z13 corner."""
        t = (zoom - 7) / 6.0
        cases = []
        for name, (d7, d13) in tier_diameters.items():
            if name == "small_bus":
                continue
            d = d7 + t * (d13 - d7)
            cases.extend([name, round(d / 2.0, 4)])
        d7_def, d13_def = tier_diameters["small_bus"]
        default_radius = round((d7_def + t * (d13_def - d7_def)) / 2.0, 4)
        return ["match", ["get", "stop_tier"], *cases, default_radius]

    def dot_radius_far_zoom():
        # Outer `interpolate zoom` blends between per-zoom tier lookups. At
        # each integer zoom z ∈ 7..14 the inner `match` picks the tier's
        # diameter (halved to radius). z14 anchor is a linear extrapolation
        # of the z7→z13 slope so the dot keeps growing through z13.99;
        # the layer's `maxzoom: 14` hides everything at z14 and above, so
        # the z14 anchor is only ever reached via interpolation from z13.
        # MapLibre requires `zoom` at the top-level, so the match sits
        # inside each zoom stop.
        stops = []
        for z in range(7, 15):
            stops.extend([z, _match_radius_at(z)])
        return ["interpolate", ["linear"], ["zoom"], *stops]

    stop_groups = [
        ("transit_stops_rail",      5),
        ("transit_stops_tram",     10),
        ("transit_stops_regional",  9),
        ("transit_stops_bus",      11),
    ]

    # Far-zoom stop labels — see .claude/concepts/stop-labels.md § Far-zoom.
    # Per-tier size curve; MapLibre collision (`text-allow-overlap: false` +
    # `symbol-sort-key: label_priority`) does the density control. Tiers not
    # yet participating at a given zoom anchor get text-size 0 → not drawn.
    LABEL_ALL_TIERS = [
        "major_train", "main_train", "important_train",
        "train_station", "small_train",
        "major_mountain", "mountain_stop", "ferry_stop",
        "major_hub", "big_station", "normal_stop", "small_bus",
    ]
    LABEL_SIZE_Z7 = {
        "major_train": 11, "main_train": 10, "important_train": 9,
        "major_mountain": 9, "ferry_stop": 9,
    }
    LABEL_SIZE_Z10 = {
        "major_train": 16, "main_train": 14, "important_train": 12,
        "train_station": 11, "small_train": 11,
        "major_mountain": 11, "ferry_stop": 11,
    }
    LABEL_SIZE_Z12 = {
        "major_train": 20, "main_train": 16, "important_train": 14,
        "train_station": 12, "small_train": 12,
        "major_mountain": 12, "mountain_stop": 10, "ferry_stop": 12,
        "major_hub": 11, "big_station": 10, "normal_stop": 10,
    }
    LABEL_SIZE_Z13 = {
        "major_train": 22, "main_train": 18, "important_train": 15,
        "train_station": 13, "small_train": 13,
        "major_mountain": 13, "mountain_stop": 11, "ferry_stop": 13,
        "major_hub": 13, "big_station": 11,
        "normal_stop": 11,
        # small_bus: never labelled at far-zoom.
    }

    def _label_size_match(sizes):
        cases = []
        for tier in LABEL_ALL_TIERS:
            cases.extend([tier, sizes.get(tier, 0)])
        return ["match", ["get", "stop_tier"], *cases, 0]

    def far_zoom_label_text_size():
        return ["interpolate", ["linear"], ["zoom"],
                7,  _label_size_match(LABEL_SIZE_Z7),
                10, _label_size_match(LABEL_SIZE_Z10),
                12, _label_size_match(LABEL_SIZE_Z12),
                13, _label_size_match(LABEL_SIZE_Z13)]

    # Bold set grows with zoom so the ratio of bold-to-regular labels stays
    # in the ~1/3 range at every zoom band. See `stop-labels.md` § Font size.
    # `big_station`, `mountain_stop`, `ferry_stop` always render SemiBold
    # (independent of the bold set) — a middle weight between the bold
    # train / hub / major_mountain tiers and the regular rest.
    LABEL_SEMIBOLD_TIERS = ("big_station", "mountain_stop", "ferry_stop")
    LABEL_BOLD_Z7 = {"major_train", "main_train"}
    LABEL_BOLD_Z9 = LABEL_BOLD_Z7 | {"important_train"}
    LABEL_BOLD_Z10 = LABEL_BOLD_Z9 | {"train_station"}
    LABEL_BOLD_Z11 = LABEL_BOLD_Z10 | {"major_hub", "major_mountain"}

    def _label_font_match(bold_tiers):
        # SemiBold overrides come FIRST so they win the match even at
        # zoom bands where the tier might otherwise sit in bold_tiers.
        cases = []
        for tier in LABEL_SEMIBOLD_TIERS:
            cases.extend([tier, ["literal", ["Saira SemiBold"]]])
        for tier in bold_tiers:
            cases.extend([tier, ["literal", ["Saira Bold"]]])
        return ["match", ["get", "stop_tier"], *cases,
                ["literal", ["Saira Regular"]]]

    far_zoom_label_text_font = ["step", ["zoom"],
        _label_font_match(LABEL_BOLD_Z7),
        9,  _label_font_match(LABEL_BOLD_Z9),
        10, _label_font_match(LABEL_BOLD_Z10),
        11, _label_font_match(LABEL_BOLD_Z11)]

    for source, source_minzoom in stop_groups:
        # Far-zoom: score-driven layer, z(source_minzoom)–z13.99.
        layers.append({
            "id": f"transit-stop-fill-{source}-far",
            "type": "circle",
            "source": source,
            "source-layer": "transit_stops",
            "minzoom": source_minzoom,
            "maxzoom": 14,
            "paint": {
                "circle-color": "#ffffff",
                "circle-radius": dot_radius_far_zoom(),
                "circle-stroke-color": "#000000",
                "circle-stroke-width": 1.0,
            },
        })
        # Pill-zoom cluster centroid dot: z14+ so the two layers do not overlap.
        # Capped at z17 (exclusive) — close-zoom design takes over.
        layers.append({
            "id": f"transit-stop-fill-{source}",
            "type": "circle",
            "source": source,
            "source-layer": "transit_stops",
            "minzoom": 14,
            "maxzoom": 17,
            "paint": {
                "circle-color": "#ffffff",
                "circle-radius": dot_radius_pill_zoom(source_minzoom),
                "circle-stroke-color": "#000000",
                "circle-stroke-width": 1.0,
            },
        })

    # Far-zoom stop labels, emitted in REVERSE mode-priority order so rail
    # sits last in the layer array. MapLibre's PauseablePlacement iterates
    # symbol layers from last to first, so the last-declared layer places
    # first and every earlier layer yields to it — rail must be last to win
    # cross-layer collisions against tram / regional / bus. `symbol-sort-key:
    # label_priority` handles ordering WITHIN each mode's layer.
    # `text-padding` accepts zoom expressions only — not data-driven — so
    # per-tier padding must go through a filter split. Two sublayers per
    # source: one for normal_stop tier (padding 20) so rural stops of that
    # tier space out from each other, and one for every other tier (padding
    # 4) so majors collide only when they actually touch. Within a source,
    # the "other" sublayer is declared LAST so MapLibre's reverse-order
    # placement places its (higher-priority) tiers first; the "normal"
    # sublayer yields to it.
    def _label_layer(source, source_minzoom, layer_suffix, filter_expr, padding):
        return {
            "id": f"transit-stop-label-{source}-far-{layer_suffix}",
            "type": "symbol",
            "source": source,
            "source-layer": "transit_stops",
            "minzoom": source_minzoom,
            "maxzoom": 14,
            "filter": filter_expr,
            "layout": {
                # display_name = stop_name with city prefix stripped in
                # cities that already carry a train-station label; falls
                # back to stop_name in rural areas or on features emitted
                # before the display_name pass existed. See
                # `.claude/concepts/stop-labels.md` § City-prefix stripping.
                "text-field": ["coalesce",
                    ["get", "display_name"],
                    ["get", "stop_name"]],
                "text-font": far_zoom_label_text_font,
                "text-size": far_zoom_label_text_size(),
                "text-anchor": "left",
                # y = -0.11 em: Saira's line-height metrics push cap-height
                # below MapLibre's line centre — same correction as pill-arrow
                # text (see the close-zoom layers below).
                "text-offset": [0.55, -0.11],
                "text-justify": "left",
                "text-max-width": 8,
                "text-padding": padding,
                "text-allow-overlap": False,
                "text-ignore-placement": False,
                "symbol-sort-key": ["get", "label_priority"],
            },
            "paint": {
                "text-color": "#1a1a1a",
                "text-halo-color": "#ffffff",
                "text-halo-width": 1.5,
                "text-halo-blur": 0.5,
            },
        }

    for source, source_minzoom in reversed(stop_groups):
        layers.append(_label_layer(
            source, source_minzoom, "normal",
            ["==", ["get", "stop_tier"], "normal_stop"], 20))
        layers.append(_label_layer(
            source, source_minzoom, "other",
            ["!=", ["get", "stop_tier"], "normal_stop"], 4))

    # Pill-zoom stop labels (z14–z16.99) — see stop-labels.md § Pill-zoom.
    # Reads `stop_label_anchor` Point features (baked by step 07) from the
    # shared transit_stop_pills source. Anchor sits ~3 m east of the pill
    # construct at its centre-y, so text-anchor "left" with no extra offset
    # is enough — the padding is in world coords. Same normal_stop /
    # everything-else padding split as far-zoom.
    def _pill_label_layer(layer_suffix, extra_filter, padding):
        return {
            "id": f"transit-stop-label-pill-{layer_suffix}",
            "type": "symbol",
            "source": "transit_stop_pills",
            "source-layer": "transit_stop_pills",
            "minzoom": 14,
            "maxzoom": 17,
            "filter": ["all",
                       ["==", ["get", "feature_type"], "stop_label_anchor"],
                       extra_filter],
            "layout": {
                "text-field": ["coalesce",
                    ["get", "display_name"],
                    ["get", "stop_name"]],
                "text-font": far_zoom_label_text_font,
                "text-size": far_zoom_label_text_size(),
                "text-anchor": "left",
                "text-max-width": 8,
                "text-padding": padding,
                "text-allow-overlap": False,
                "text-ignore-placement": False,
                "symbol-sort-key": ["get", "label_priority"],
            },
            "paint": {
                "text-color": "#1a1a1a",
                "text-halo-color": "#ffffff",
                "text-halo-width": 1.5,
                "text-halo-blur": 0.5,
            },
        }

    # Leader line — thin hairline from the main pill to the label anchor.
    # Rendered BELOW the label symbol layers so the text halo covers where
    # the leader meets the anchor.
    layers.append({
        "id": "transit-stop-label-leader",
        "type": "line",
        "source": "transit_stop_pills",
        "source-layer": "transit_stop_pills",
        "minzoom": 14,
        "maxzoom": 17,
        "filter": ["==", ["get", "feature_type"], "stop_label_leader"],
        "paint": {
            "line-color": "#1a1a1a",
            "line-width": 0.8,
            "line-opacity": 0.85,
        },
    })

    # normal_stop declared FIRST so it yields to "other" (last-declared →
    # placed first in MapLibre's reverse-order collision pass).
    layers.append(_pill_label_layer(
        "normal", ["==", ["get", "stop_tier"], "normal_stop"], 20))
    layers.append(_pill_label_layer(
        "other", ["!=", ["get", "stop_tier"], "normal_stop"], 4))

    # Ferry stops follow the same two-tier pattern as every other mode:
    # a low-zoom dot at z9–z13 (rendered through the regional source above)
    # and a medium-zoom endpoint disc + optional connector + GTFS endpoint
    # at z14+ (rendered through the pill paint stack below). The
    # far-zoom dot is emitted at the canonical pier vertex; the pill paint
    # stack carries the connector seam handling. See
    # stops-far-zoom-markers.md § "Ferry far-zoom marker".

    # Hard cut at the appear-zoom — no opacity fade. Uniform z14 for
    # every mode per `stops-pill-zoom.md` § "Dot-to-pill zoom switch".
    PILL_MINZOOM = 14

    # Diameter formula from `stops-pill-zoom.md` § "Visual style":
    #   d(z, wb) = min_d(z) + slope(z) × min(wb, WB_HIGH)
    # WB_HIGH = 5.0 is the dataset's max width_base (config `line_width`
    # top for train). Per-zoom anchors:
    #                 min_d   max_d   slope = (max_d - min_d) / WB_HIGH
    #   z14         4.5     16      2.3
    #   z15         6       22      3.2
    #   z16         8       30      4.4
    #   z17         14      36      4.4
    # Below z14 pills aren't drawn; above z17 the close-zoom design will
    # take over (holds at z17 values for now).
    WB_HIGH = 5.0
    def _wb_clamped():
        return ["min", ["get", "width_base"], WB_HIGH]
    def _parent_wb_clamped():
        return ["min", ["get", "parent_width_base"], WB_HIGH]

    def pill_disc_width():
        return ["interpolate", ["linear"], ["zoom"],
            PILL_MINZOOM,  ["+", 4.5,  ["*", _wb_clamped(), 2.3]],
            15,            ["+", 6.0,  ["*", _wb_clamped(), 3.2]],
            16,            ["+", 8.0,  ["*", _wb_clamped(), 4.4]],
            17,            ["+", 14.0, ["*", _wb_clamped(), 4.4]],
        ]

    # Connector width = pill diameter / 3 — subordinate to the stops at
    # either end. Preserves the connector < line < dot/pill hierarchy.
    def connector_width():
        return ["interpolate", ["linear"], ["zoom"],
            PILL_MINZOOM,  ["+", 1.5,   ["*", _wb_clamped(), 0.767]],
            15,            ["+", 2.0,   ["*", _wb_clamped(), 1.067]],
            16,            ["+", 2.667, ["*", _wb_clamped(), 1.467]],
            17,            ["+", 4.667, ["*", _wb_clamped(), 1.467]],
        ]

    layers.append({
        "id": "transit-stop-pill-casing",
        "type": "line",
        "source": "transit_stop_pills",
        "source-layer": "transit_stop_pills",
        "minzoom": PILL_MINZOOM,
        "maxzoom": 17,
        "filter": ["==", ["get", "feature_type"], "pill"],
        "layout": {"line-cap": "round", "line-join": "round"},
        "paint": {
            "line-color": "#000000",
            # Casing = pill fill + 2.0 for the 1 px black rim on each side.
            "line-width": ["interpolate", ["linear"], ["zoom"],
                PILL_MINZOOM,  ["+", 6.5,  ["*", _wb_clamped(), 2.3]],
                15,            ["+", 8.0,  ["*", _wb_clamped(), 3.2]],
                16,            ["+", 10.0, ["*", _wb_clamped(), 4.4]],
                17,            ["+", 16.0, ["*", _wb_clamped(), 4.4]],
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
        "maxzoom": 17,
        "filter": ["==", ["get", "feature_type"], "connector"],
        "layout": {"line-cap": "round", "line-join": "round"},
        "paint": {
            "line-color": "#000000",
            # Casing = connector fill + 2.0 for the 1 px black rim on each side.
            "line-width": ["interpolate", ["linear"], ["zoom"],
                PILL_MINZOOM,  ["+", 3.5,   ["*", _wb_clamped(), 0.767]],
                15,            ["+", 4.0,   ["*", _wb_clamped(), 1.067]],
                16,            ["+", 4.667, ["*", _wb_clamped(), 1.467]],
                17,            ["+", 6.667, ["*", _wb_clamped(), 1.467]],
            ],
        }
    })

    layers.append({
        "id": "transit-stop-pill-fill",
        "type": "line",
        "source": "transit_stop_pills",
        "source-layer": "transit_stop_pills",
        "minzoom": PILL_MINZOOM,
        "maxzoom": 17,
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
        "maxzoom": 17,
        "filter": ["==", ["get", "feature_type"], "endpoint"],
        "paint": {
            "circle-color": "#ffffff",
            # Radius = pill diameter / 2 = (min_d(z) + slope(z) × min(wb, WB_HIGH)) / 2.
            "circle-radius": ["interpolate", ["linear"], ["zoom"],
                PILL_MINZOOM, ["+", 2.25, ["*", _wb_clamped(), 1.15]],
                15,           ["+", 3.0,  ["*", _wb_clamped(), 1.6]],
                16,           ["+", 4.0,  ["*", _wb_clamped(), 2.2]],
                17,           ["+", 7.0,  ["*", _wb_clamped(), 2.2]],
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
        "maxzoom": 17,
        "filter": ["==", ["get", "feature_type"], "connector"],
        "layout": {"line-cap": "round", "line-join": "round"},
        "paint": {
            "line-color": "#ffffff",
            "line-width": connector_width(),
        }
    })

    # --- Color indicators (z14+) ---------------------------------------------
    # Mini per-color-group dots inside stop dots, endpoint discs, and pills.
    # See `.claude/concepts/stop-color-indicators.md` and
    # `.claude/concepts/stops-pill-zoom-tweaks.md`.
    #
    # Layout: centered row of up to 3 indicators (current data max).
    # Each indicator carries `slot_units` (integer in [-5, +5]; n=1 → {0};
    # n=2 → {-1, +1}; n=3 → {-2, 0, +2}) and `tangent_deg` (0 for
    # dots/discs, pill tangent for pill indicators).
    #
    # Indicators appear at the same zoom as pills (z14) with no opacity
    # fade. Each feature carries `parent_width_base` (the floor-clamped
    # width_base of the parent stop) and `n_indicators` (count in the
    # row) so the text-size expression can shrink the row to fit when
    # the parent is too thin for the default size.
    INDICATOR_MINZOOM = 14

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
    # diameter). See `.claude/concepts/stops-pill-zoom-tweaks.md`
    # § "Indicators must not overflow the parent".

    # Per-zoom anchor: min(default_size_at_z, parent_diameter * margin / row).
    # Parent diameter matches `pill_disc_width()` above:
    #   d(z, wb) = parent_min_d + parent_slope × min(wb, WB_HIGH)
    def _indicator_size_at_zoom(default_size, parent_min_d, parent_slope):
        return ["min",
            default_size,
            ["/",
                ["*",
                    ["+", parent_min_d,
                          ["*", _parent_wb_clamped(), parent_slope]],
                    INDICATOR_INNER_MARGIN,
                ],
                ["get", "row_factor"],
            ],
        ]

    # Parent-diameter anchors mirror `pill_disc_width()` above (min_d
    # 4.5/6/8/14 at z14/z15/z16/z17; slopes 2.3/3.2/4.4/4.4). Default
    # text-size curve stays 9.0 at z14 → 36.0 at z20; intermediate values
    # are linearly interpolated.
    text_size_expr = ["interpolate", ["linear"], ["zoom"],
        14, _indicator_size_at_zoom(9.0,  4.5,  2.3),
        15, _indicator_size_at_zoom(13.5, 6.0,  3.2),
        16, _indicator_size_at_zoom(18.0, 8.0,  4.4),
        17, _indicator_size_at_zoom(22.5, 14.0, 4.4),
        20, _indicator_size_at_zoom(36.0, 14.0, 4.4),
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
        "maxzoom": 17,
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

    # =========================================================================
    # Close-zoom (z17+) — see .claude/concepts/stops-close-zoom.md
    # =========================================================================
    # Hard cut at z16 → z17: pill-zoom / far-zoom stop layers stop at z17
    # (via their own maxzoom), the close-zoom layers below start at z17.
    # The yellow station backdrop is NOT here — it renders below the transit
    # lines via build_close_zoom_backdrop_layers().

    # Geometry-locked sizing: pill-arrow geometry is metres, so borders and labels
    # convert their metre dimensions to px on the map's own exponential
    # scale. 1 m = 2.455 px at z17 (lat 47°, 512px tiles), doubling per zoom.
    PX_PER_M_Z17 = 2.455
    PX_PER_M_Z22 = PX_PER_M_Z17 * 32.0

    def _metric_px(m):
        return ["interpolate", ["exponential", 2], ["zoom"],
                17, m * PX_PER_M_Z17,
                22, m * PX_PER_M_Z22]

    def _font_px_expr(scale=1.0):
        # MapLibre requires ["zoom"] at the top level of interpolate, so a
        # per-band scale factor cannot wrap the whole expression — it multiplies
        # each anchor's per-metre conversion instead.
        return ["interpolate", ["exponential", 2], ["zoom"],
            17, ["*", ["get", "font_m"], scale * PX_PER_M_Z17],
            22, ["*", ["get", "font_m"], scale * PX_PER_M_Z22],
        ]

    font_px_expr = _font_px_expr()

    # Zoom bands (must mirror CLOSE_ZOOM_BANDS in
    # scripts/transit/stops/close_zoom/constants.py):
    # each pill-arrow exists once per band in the tiles; the style shows exactly
    # one band per display-zoom range. Bands B and C share the z18 tiles
    # (z19+ overzooms them), so the zoom gates + band filter do the switch.
    # Band A is the solid variant: whole pill-arrow in the line color with a white
    # border, number only, no disc (step 07 emits none for it).
    #   (band, display minzoom, display maxzoom, dest text-max-width in em,
    #    body fill color, border color)
    # Line breaks are baked into the destination text by step 07 (build-time
    # wrap with abbreviation of over-long words), so MapLibre's own wrapping
    # is disabled via a huge text-max-width on every band.
    CLOSE_ZOOM_STYLE_BANDS = [
        ("A", 17, 18, None, ["get", "color"], "#ffffff"),
        ("B", 18, 19, 1000, "#ffffff", ["get", "color"]),
        ("C", 19, 20, 1000, "#ffffff", ["get", "color"]),
        ("D", 20, 21, 1000, "#ffffff", ["get", "color"]),
        ("E", 21, None, 1000, "#ffffff", ["get", "color"]),
    ]

    for band, band_min, band_max, dest_max_width, body_fill, border_color \
            in CLOSE_ZOOM_STYLE_BANDS:
        def _band_layer(layer):
            layer["source"] = "transit_close_zoom"
            layer["source-layer"] = "transit_close_zoom"
            layer["minzoom"] = band_min
            if band_max is not None:
                layer["maxzoom"] = band_max
            layers.append(layer)

        # 1 & 2. Pill-arrow body fill (line color for the solid band A,
        # white for the duo-tone bands) and border (~0.4 m, scales with
        # the pill-arrow geometry): white on band A, line color on duo-tone.
        # Band A paints border BELOW fill so that overlapping pill-arrows
        # at the same stop have their intruding borders occluded by the
        # neighbour's fill — clean unified silhouette instead of two
        # visible borders crossing each other. Bands B–E keep fill below
        # border because their border IS the line color and needs to sit
        # on top of the white body.
        fill_layer = {
            "id": f"close-zoom-pill-arrow-fill-{band}",
            "type": "fill",
            "filter": ["all",
                       ["==", ["get", "feature_type"], "pill_arrow"],
                       ["==", ["get", "band"], band]],
            "paint": {
                "fill-color": body_fill,
                "fill-antialias": True,
            }
        }
        border_layer = {
            "id": f"close-zoom-pill-arrow-border-{band}",
            "type": "line",
            "filter": ["all",
                       ["==", ["get", "feature_type"], "pill_arrow"],
                       ["==", ["get", "band"], band]],
            "layout": {"line-cap": "round", "line-join": "round"},
            "paint": {
                "line-color": border_color,
                "line-width": _metric_px(0.4),
            }
        }
        if band == "A":
            _band_layer(border_layer)
            _band_layer(fill_layer)
        else:
            _band_layer(fill_layer)
            _band_layer(border_layer)

        # 3. Disc at the round end, filled with the line color (duo-tone
        # bands only; band A emits no disc features).
        _band_layer({
            "id": f"close-zoom-pill-disc-{band}",
            "type": "fill",
            "filter": ["all",
                       ["==", ["get", "feature_type"], "pill_disc"],
                       ["==", ["get", "band"], band]],
            "paint": {
                "fill-color": ["get", "color"],
                "fill-antialias": True,
            }
        })

        # 4. Line number in the disc (white). `font_m`/`text_rot` are baked
        # by step 07 so the label fits the disc and reads right-side-up.
        _band_layer({
            "id": f"close-zoom-pill-ref-{band}",
            "type": "symbol",
            "filter": ["all",
                       ["==", ["get", "feature_type"], "pill_ref"],
                       ["==", ["get", "band"], band]],
            "layout": {
                "text-field": ["get", "ref"],
                "text-font": ["Saira ExtraBold"],
                # Band A holds the number in the whole pill-arrow silhouette
                # (no disc), so it can use a larger font than the disc-bound
                # bands B–E which share step 07's conservative `font_m`.
                "text-size": (_font_px_expr(1.30)
                              if band == "A" else font_px_expr),
                "text-rotate": ["get", "text_rot"],
                "text-rotation-alignment": "map",
                "text-pitch-alignment": "map",
                "text-allow-overlap": True,
                "text-ignore-placement": True,
                "text-padding": 0,
                # Saira's line-height metrics (hhea.descent=-439) push its
                # cap-height below MapLibre's line centre; nudge up ~8% em to
                # restore visual centring within the disc / pill silhouette.
                # Band A additionally shifts along the tangent toward the
                # round end of the pill-arrow. The sign of x flips with the
                # feature's `flipped` bool (baked by step 07) because MapLibre
                # applies text-offset in the text's reader frame, which reverses
                # relative to map coords when the label is flipped 180°.
                "text-offset": (
                    ["case", ["get", "flipped"],
                     ["literal", [0.15, -0.11]],
                     ["literal", [-0.15, -0.11]]]
                    if band == "A" else [0, -0.11]),
            },
            "paint": {
                "text-color": "#ffffff",
            }
        })

        # 5. Destination in black along the white body (bands with
        # destination text only).
        if dest_max_width is not None:
            _band_layer({
                "id": f"close-zoom-pill-dest-{band}",
                "type": "symbol",
                "filter": ["all",
                           ["==", ["get", "feature_type"], "pill_dest"],
                           ["==", ["get", "band"], band]],
                "layout": {
                    "text-field": ["get", "destination"],
                    # Semi Condensed (wdth=87.5) fits more characters in the
                    # available body width. The ~10% size bump compensating
                    # for the narrower letterforms is baked into `font_dest_m`
                    # in close_zoom/constants.py so the wrap budget stays
                    # coherent with the rendered size.
                    "text-font": ["Saira SemiCondensed"],
                    "text-size": font_px_expr,
                    "text-rotate": ["get", "text_rot"],
                    "text-rotation-alignment": "map",
                    "text-pitch-alignment": "map",
                    "text-allow-overlap": True,
                    "text-ignore-placement": True,
                    "text-padding": 0,
                    "text-max-width": dest_max_width,
                    # Same vertical nudge as pill-ref — Saira's line-height
                    # centring sits low relative to cap-height (see ref layer).
                    "text-offset": [0, -0.11],
                    # Left-aligned: step 07 places the anchor at the text's
                    # visual-left (reader-left) end of the text region.
                    # For non-flipped labels that's the disc side of the
                    # pill-arrow; for flipped labels the +180° text-rotate makes
                    # the reader-left end the pill-arrow's tip side.
                    "text-anchor": "left",
                    "text-justify": "left",
                },
                "paint": {
                    "text-color": "#000000",
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
