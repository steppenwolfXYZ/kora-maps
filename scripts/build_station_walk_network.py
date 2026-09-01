#!/usr/bin/env python3
"""Build the synthetic station walk network for the Valhalla input.

See `.claude/concepts/station-walk-network.md` for the requirements.

Valhalla routes on ways, never on areas, and it snaps a requested
coordinate to the nearest edge in plan view with no idea what is above or
below it. Swiss platforms are mapped as OSM areas, so a platform has no
routable geometry at all — the router happily ends a walk on whatever
edge happens to be closest, which at a stacked station is regularly a
deck two levels up (Bern tracks 9/10, the case this exists to fix).

This script produces two artefacts:

  * `data/osm/station_walk_network.osm.pbf` — synthetic ways welded into
    the real pedestrian graph: one walk line down the long axis of every
    mapped platform, short connectors from that line to every
    level-compatible pedestrian node touching the platform, and lift hubs
    that join the levels a lift shaft serves. Merged into the Valhalla
    input by `preprocess_osm_for_motis.py --valhalla`.

  * `data/osm/quay_anchors.json` — per GTFS quay, the point on its
    platform's walk line nearest the published coordinate. Consumed by
    `preprocess_gtfs_for_motis.py` as its highest-priority snap tier, so
    MOTIS asks Valhalla to walk to a point that is *on* the platform.

Only quays whose walk line is actually connected to the surrounding
network are anchored: an isolated walk line would be a worse snap target
than the status quo, because Valhalla would either fail to route or fall
back to the very edge we are trying to avoid.

Synthetic ids start at `SYNTH_ID_BASE`, far above any live OSM id, so
they never collide and the output stays id-stable across runs.

Idempotent: same inputs → byte-identical outputs.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import osmium

ROOT = Path(__file__).resolve().parent.parent
OSM_DIR = ROOT / "data" / "osm"
GTFS_IN = ROOT / "data" / "gtfs_routed"

SOURCE_PBF = OSM_DIR / "ch_pfaedle.osm.pbf"
RAW_EXTRACT_PBF = OSM_DIR / "station_infra.raw.osm.pbf"
EXTRACT_PBF = OSM_DIR / "station_infra.osm.pbf"
OVERLAY_PBF = OSM_DIR / "station_walk_network.osm.pbf"
ANCHORS_JSON = OSM_DIR / "quay_anchors.json"
COVERAGE_JSON = ROOT / "data" / "transit" / "station_walk_coverage.json"

PFAEDLE_IMAGE = "carfree-pfaedle:latest"

# Ids for synthetic objects. Live OSM node ids are ~1.3e10 and way ids
# ~1.5e9; 9e12 leaves several orders of magnitude of headroom.
SYNTH_ID_BASE = 9_000_000_000_000

# Highway values whose ways are pedestrian-relevant enough to be welding
# candidates. Deliberately broader than "footway": at small stations the
# platform is reached straight off a service road or a residential street.
# `platform` is included on the evidence of the router itself: a walk
# out of Bern, Hirschengraben traverses way 603146021, a bare
# `highway=platform`. So a platform way carrying a highway value needs no
# synthetic twin — only `railway=platform` without one is invisible.
WALKABLE_HIGHWAY = {
    "footway", "path", "steps", "corridor", "pedestrian", "elevator",
    "platform", "living_street", "residential", "unclassified", "service",
    "track", "cycleway", "tertiary", "secondary", "primary", "road",
}

# Platform-ish tag signatures. `railway=platform_edge` is deliberately
# excluded — it traces the track side of the platform, not a walk line.
def is_platform(tags: dict) -> bool:
    return (tags.get("railway") == "platform"
            or tags.get("public_transport") == "platform"
            or tags.get("highway") == "platform")


def is_lift(tags: dict) -> bool:
    return tags.get("highway") == "elevator"


# Slice width (m) used when tracing a platform's centreline. Small enough
# to follow a curved platform, wide enough that vertex noise on a
# straight one doesn't make the line wobble.
CENTRELINE_SLICE_M = 4.0
# Vertices closer than this along the traced line are collapsed.
CENTRELINE_MIN_STEP_M = 1.5
# 1-2-1 smoothing passes over the traced line's lateral offset. Three is
# enough to take the jitter out of a notched platform without rounding
# off a genuinely curved one.
CENTRELINE_SMOOTH_PASSES = 3
# How far outside a platform a pedestrian node may sit and still count as
# touching it. Covers the usual sloppiness where a stair head is drawn a
# little short of the platform edge.
WELD_TOLERANCE_M = 3.0
# Same, for lift shafts — tighter, because a shaft is small and a loose
# radius would hoover up unrelated ways.
LIFT_TOLERANCE_M = 2.0
# A quay farther than this from any platform walk line is left alone.
ANCHOR_MAX_M = 25.0


# ---------------------------------------------------------------- geometry


def metric_frame(lat0: float):
    """Local equirectangular frame — metres, origin at (0, lat0)."""
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 111320.0
    return kx, ky


def to_xy(lon, lat, kx, ky):
    return lon * kx, lat * ky


def to_lonlat(x, y, kx, ky):
    return x / kx, y / ky


def ring_area(pts) -> float:
    a = 0.0
    for i in range(len(pts) - 1):
        a += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return abs(a) / 2.0


def principal_axis(xy):
    """Unit vector along the dominant extent of `xy`, plus the centroid."""
    n = len(xy)
    cx = sum(p[0] for p in xy) / n
    cy = sum(p[1] for p in xy) / n
    sxx = syy = sxy = 0.0
    for x, y in xy:
        dx, dy = x - cx, y - cy
        sxx += dx * dx
        syy += dy * dy
        sxy += dx * dy
    # Dominant eigenvector of the 2x2 covariance matrix, closed form.
    tr, det = sxx + syy, sxx * syy - sxy * sxy
    disc = max(tr * tr / 4.0 - det, 0.0)
    lam = tr / 2.0 + math.sqrt(disc)
    if abs(sxy) > 1e-9:
        ax, ay = lam - syy, sxy
    elif sxx >= syy:
        ax, ay = 1.0, 0.0
    else:
        ax, ay = 0.0, 1.0
    norm = math.hypot(ax, ay) or 1.0
    return (ax / norm, ay / norm), (cx, cy)


def trace_centreline(xy):
    """Trace a line down the long axis of a polygon ring.

    Slices the ring along its principal axis and takes the mid-point of
    each slice's cross-section. On a rectangle this yields the straight
    centre line; on a curved platform it follows the curve, which a
    straight axis-aligned segment would not (it would leave the
    footprint).
    """
    (ax, ay), (cx, cy) = principal_axis(xy)
    px, py = -ay, ax  # perpendicular

    proj = []
    for x, y in xy:
        dx, dy = x - cx, y - cy
        proj.append((dx * ax + dy * ay, dx * px + dy * py))
    t_min = min(p[0] for p in proj)
    t_max = max(p[0] for p in proj)
    length = t_max - t_min
    if length < 1e-6:
        return []

    n_slices = max(2, min(400, int(math.ceil(length / CENTRELINE_SLICE_M))))
    step = length / n_slices
    lo = [None] * n_slices
    hi = [None] * n_slices
    for t, u in proj:
        i = min(n_slices - 1, int((t - t_min) / step))
        lo[i] = u if lo[i] is None else min(lo[i], u)
        hi[i] = u if hi[i] is None else max(hi[i], u)

    # Collect (along-axis, lateral) per slice, then smooth the lateral
    # component. Raw cross-section midpoints swing sideways wherever the
    # footprint changes width — a stair opening, a widened head — and
    # that shows up as a visible zigzag down the middle of the platform.
    # Smoothing only the lateral offset keeps the along-axis progression
    # monotonic, so the line still follows a curved platform.
    ts, us = [], []
    for i in range(n_slices):
        if lo[i] is None:
            continue
        ts.append(t_min + (i + 0.5) * step)
        us.append((lo[i] + hi[i]) / 2.0)
    if len(ts) < 2:
        return []
    for _ in range(CENTRELINE_SMOOTH_PASSES):
        if len(us) < 3:
            break
        sm = [us[0]]
        for i in range(1, len(us) - 1):
            sm.append((us[i - 1] + 2.0 * us[i] + us[i + 1]) / 4.0)
        sm.append(us[-1])
        us = sm
    pts = [(cx + t * ax + u * px, cy + t * ay + u * py)
           for t, u in zip(ts, us)]

    out = [pts[0]]
    for p in pts[1:]:
        if math.dist(p, out[-1]) >= CENTRELINE_MIN_STEP_M:
            out.append(p)
    if len(out) < 2:
        out = [pts[0], pts[-1]]
    return out


def point_in_ring(x, y, ring) -> bool:
    inside = False
    n = len(ring)
    for i in range(n - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
    return inside


def dist_to_ring(x, y, ring) -> float:
    best = float("inf")
    for i in range(len(ring) - 1):
        best = min(best, seg_distance(x, y, ring[i], ring[i + 1])[0])
    return best


def seg_distance(x, y, a, b):
    """(distance, t, point) from (x, y) to segment a→b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    d2 = dx * dx + dy * dy
    t = 0.0 if d2 == 0 else max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / d2))
    px, py = a[0] + t * dx, a[1] + t * dy
    return math.hypot(x - px, y - py), t, (px, py)


def project_on_line(x, y, line):
    """Nearest point on a polyline: (distance, segment index, t, point)."""
    best = (float("inf"), 0, 0.0, line[0])
    for i in range(len(line) - 1):
        d, t, p = seg_distance(x, y, line[i], line[i + 1])
        if d < best[0]:
            best = (d, i, t, p)
    return best


# ------------------------------------------------------------------ levels


def level_set(tags: dict):
    """The set of levels an object occupies.

    OSM writes multi-level objects as "-1;0" (a stair spanning two
    levels) or as a range "0-2". Returns None when the object says
    nothing about its level, which callers treat as "compatible with
    anything" — the ordinary at-grade case.
    """
    raw = tags.get("level")
    if raw is None:
        raw = tags.get("layer")
        if raw is None:
            return None
    out = set()
    for part in str(raw).replace(",", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if "-" in part[1:]:  # a range like "0-2" (leading minus is a sign)
            sep = part.index("-", 1)
            try:
                lo = float(part[:sep])
                hi = float(part[sep + 1:])
            except ValueError:
                continue
            lo, hi = min(lo, hi), max(lo, hi)
            v = lo
            while v <= hi + 1e-9:
                out.add(v)
                v += 1.0
            continue
        try:
            out.add(float(part))
        except ValueError:
            continue
    return out or None


def levels_compatible(platform, candidate) -> bool:
    """Whether a candidate way may be welded to a platform.

    Asymmetric on purpose. A platform that says nothing about its level
    is an ordinary at-grade stop, and most of Switzerland is mapped that
    way — there anything nearby may connect.

    But once a platform *declares* a level, silence from the candidate is
    not agreement, and treating it as agreement is what produced the Bern
    platform-1/2 defect: two `tunnel=building_passage` ways on the Welle
    overpass (`401478505`, `538678571`) carry neither `level` nor
    `layer`, so the permissive rule stitched a level-1 walkway onto a
    level-0 platform. The router then believed you could step off
    Schanzenstrasse onto platform 1 — you cannot — and routed through it.

    So: explicit platform level demands an explicit, intersecting
    candidate level. Every other Bern platform already welds only to
    explicitly tagged geometry (`level=-1;0` underpass and stairs,
    `level=0;1` off the Welle), so the strict branch costs nothing where
    the mapping is good.
    """
    if platform is None:
        return True
    if candidate is None:
        return False
    return bool(platform & candidate)


# -------------------------------------------------------------- extraction


def _dedupe(raw: Path, out: Path) -> None:
    """Drop repeated object ids from the extract.

    `ch_pfaedle.osm.pbf` is a merge of per-country cuts and `osmium
    merge` does not fully dedupe objects that exist in two countries'
    extracts — the same reason pipeline step 03 exports per country
    rather than from the merged file. The reader's location index and
    area assembler both reject a repeated id outright, so they have to
    go. The file is id-sorted, so duplicates are adjacent and remembering
    the previous id per type is enough — no id set, no memory blow-up.
    """
    last = {"n": None, "w": None, "r": None}
    dropped = 0
    with osmium.SimpleWriter(str(out), overwrite=True) as writer:
        for obj in osmium.FileProcessor(str(raw)):
            kind = "n" if obj.is_node() else "w" if obj.is_way() else "r"
            if last[kind] == obj.id:
                dropped += 1
                continue
            last[kind] = obj.id
            if kind == "n":
                writer.add_node(obj)
            elif kind == "w":
                writer.add_way(obj)
            else:
                writer.add_relation(obj)
    print(f"  deduped: dropped {dropped:,} repeated objects")


def run_extract(force: bool) -> None:
    """Cut the station-relevant slice out of the wide-bbox PBF."""
    if EXTRACT_PBF.exists() and not force:
        print(f"extract present, skipping: {EXTRACT_PBF.name}")
        return
    if not SOURCE_PBF.exists():
        sys.exit(f"missing {SOURCE_PBF} — run pipeline step 03 first")
    hw = ",".join(sorted(WALKABLE_HIGHWAY))
    cmd = [
        "docker", "run", "--rm", "-v", f"{ROOT}:/work", "-w", "/work",
        PFAEDLE_IMAGE, "osmium", "tags-filter", "--overwrite",
        "-o", f"/work/{RAW_EXTRACT_PBF.relative_to(ROOT)}",
        f"/work/{SOURCE_PBF.relative_to(ROOT)}",
        "w/railway=platform", "w/public_transport=platform",
        "w/highway=platform",
        "r/railway=platform", "r/public_transport=platform",
        f"w/highway={hw}", "n/highway=elevator",
    ]
    print("  $", " ".join(cmd))
    subprocess.run(cmd, check=True)
    _dedupe(RAW_EXTRACT_PBF, EXTRACT_PBF)
    RAW_EXTRACT_PBF.unlink(missing_ok=True)


# ------------------------------------------------------------------ reading


# Cell size (degrees) of the neighbourhood filter that decides which
# pedestrian nodes are worth keeping in memory. ~40 m of latitude, so a
# node in a platform's cell or any of its eight neighbours is well within
# reach of WELD_TOLERANCE_M. The wide-bbox extract reaches into DE/FR/IT/
# AT and holds tens of millions of pedestrian nodes; without this filter
# the vast majority — nowhere near a platform — would be retained.
NEIGHBOURHOOD_CELL = 0.0005


class Reader:
    """Collect platforms, lifts and pedestrian nodes from the extract.

    Two passes: the first takes platform and lift geometry and records
    which neighbourhoods they occupy, the second takes only pedestrian
    nodes inside those neighbourhoods.
    """

    def __init__(self):
        self.platforms = []   # dicts: ring(lonlat), tags, refs, osm
        self.lifts = []       # dicts: ring(lonlat), tags, osm
        self.ped_nodes = []   # (node_id, lon, lat, level_set)
        self.open_platforms = []  # platforms already mapped as open ways
        self._cells = set()

    def _mark(self, pts) -> None:
        """Mark every cell the outline passes through, not just the cells
        its vertices land in — a platform edge can run a few hundred
        metres between two vertices, and the cells in between hold the
        stair heads we are looking for."""
        c = NEIGHBOURHOOD_CELL
        for i, (lon, lat) in enumerate(pts):
            self._cells.add((int(lon / c), int(lat / c)))
            if i + 1 >= len(pts):
                break
            nlon, nlat = pts[i + 1]
            steps = int(max(abs(nlon - lon), abs(nlat - lat)) / (c / 2.0))
            for k in range(1, steps + 1):
                t = k / (steps + 1.0)
                self._cells.add((int((lon + (nlon - lon) * t) / c),
                                 int((lat + (nlat - lat) * t) / c)))

    def _in_neighbourhood(self, lon, lat) -> bool:
        c = NEIGHBOURHOOD_CELL
        cx, cy = int(lon / c), int(lat / c)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if (cx + dx, cy + dy) in self._cells:
                    return True
        return False

    def read(self, path: Path) -> None:
        self._read_platforms(path)
        self._read_ped_nodes(path)

    def _read_platforms(self, path: Path) -> None:
        fp = osmium.FileProcessor(str(path)).with_areas().with_locations()
        for obj in fp:
            if obj.is_area():
                tags = dict(obj.tags)
                if not (is_platform(tags) or is_lift(tags)):
                    continue
                rings = []
                for outer in obj.outer_rings():
                    pts = [(n.lon, n.lat) for n in outer]
                    if len(pts) >= 4:
                        rings.append(pts)
                if not rings:
                    continue
                ring = max(rings, key=ring_area)
                rec = {
                    "ring": ring,
                    "tags": tags,
                    "levels": level_set(tags),
                    "osm": f"{'w' if obj.from_way() else 'r'}{obj.orig_id()}",
                }
                (self.lifts if is_lift(tags) else self.platforms).append(rec)
                self._mark(ring)
            elif obj.is_way():
                tags = dict(obj.tags)
                hw = tags.get("highway")
                if is_platform(tags) and tags.get("area") != "yes":
                    pts = [(n.location.lon, n.location.lat)
                           for n in obj.nodes if n.location.valid()]
                    if len(pts) >= 2 and pts[0] != pts[-1]:
                        self.open_platforms.append({
                            "line": pts, "tags": tags,
                            "levels": level_set(tags),
                            # A platform way is only part of the routing
                            # graph if it also carries a highway value
                            # Valhalla walks on. `railway=platform` alone
                            # is invisible to the router, so those get a
                            # synthetic twin like the areas do.
                            "routable": hw in WALKABLE_HIGHWAY,
                            "osm": f"w{obj.id}",
                        })
                        self._mark(pts)

    def _read_ped_nodes(self, path: Path) -> None:
        fp = osmium.FileProcessor(str(path)).with_locations()
        for obj in fp:
            if obj.is_way():
                tags = dict(obj.tags)
                if tags.get("highway") not in WALKABLE_HIGHWAY:
                    continue
                lv = level_set(tags)
                for n in obj.nodes:
                    if not n.location.valid():
                        continue
                    if not self._in_neighbourhood(n.location.lon,
                                                  n.location.lat):
                        continue
                    self.ped_nodes.append(
                        (n.ref, n.location.lon, n.location.lat, lv))
            elif obj.is_node():
                tags = dict(obj.tags)
                if (tags.get("highway") == "elevator"
                        and self._in_neighbourhood(obj.location.lon,
                                                   obj.location.lat)):
                    self.ped_nodes.append(
                        (obj.id, obj.location.lon, obj.location.lat,
                         level_set(tags)))


# ------------------------------------------------------------------- build


class Overlay:
    """Accumulates synthetic nodes and ways."""

    def __init__(self):
        self.nodes = []  # (id, lon, lat)
        self.ways = []   # (id, [node ids], {tags})
        self._next = SYNTH_ID_BASE

    def node(self, lon, lat, tags=None) -> int:
        nid = self._next
        self._next += 1
        self.nodes.append((nid, lon, lat, dict(tags or {})))
        return nid

    def way(self, node_ids, tags) -> int:
        wid = self._next
        self._next += 1
        self.ways.append((wid, list(node_ids), dict(tags)))
        return wid


def platform_refs(tags: dict):
    """Platform designations an OSM platform claims ("9;10" → {9, 10})."""
    out = set()
    for key in ("local_ref", "ref"):
        raw = tags.get(key)
        if not raw:
            continue
        for part in str(raw).replace(",", ";").split(";"):
            part = part.strip()
            if part:
                out.add(part)
    return out


def build(reader: Reader, overlay: Overlay):
    """Walk lines, welds and lift hubs. Returns the anchor candidates."""
    if not reader.platforms and not reader.open_platforms:
        sys.exit("no platform geometry in the extract — check the filter")

    lat0 = reader.platforms[0]["ring"][0][1] if reader.platforms else 47.0
    kx, ky = metric_frame(lat0)

    # Spatial hash over pedestrian nodes, ~200 m cells.
    cell = 0.002
    grid = defaultdict(list)
    for nid, lon, lat, lv in reader.ped_nodes:
        grid[(int(lon / cell), int(lat / cell))].append((nid, lon, lat, lv))

    def nodes_near(lon, lat):
        cx, cy = int(lon / cell), int(lat / cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                yield from grid.get((cx + dx, cy + dy), ())

    walk_lines = []   # dicts: line(lonlat), refs, levels, connected, osm
    stats = {"platforms": 0, "welds": 0, "orphans": 0, "lifts": 0,
             "lift_links": 0, "open_platforms": 0, "open_synthesised": 0}

    # --- platforms mapped as areas: trace a walk line, then weld it.
    # Open platform ways that the router cannot see join the same path,
    # using their own polyline instead of a traced centreline.
    synthesise = [(p, None) for p in reader.platforms]
    synthesise += [(p, p["line"]) for p in reader.open_platforms
                   if not p["routable"]]
    for plat, own_line in synthesise:
        if own_line is None:
            ring_xy = [to_xy(lon, lat, kx, ky) for lon, lat in plat["ring"]]
            line_xy = trace_centreline(ring_xy)
        else:
            ring_xy = [to_xy(lon, lat, kx, ky) for lon, lat in own_line]
            line_xy = list(ring_xy)
        if len(line_xy) < 2:
            continue
        stats["platforms" if own_line is None else "open_synthesised"] += 1

        # Weld candidates: pedestrian nodes inside the footprint (or just
        # outside it) whose level is compatible with the platform's.
        outline = plat["ring"] if own_line is None else own_line
        lons = [p[0] for p in outline]
        lats = [p[1] for p in outline]
        seen = set()
        welds = []
        for nid, lon, lat, lv in nodes_near(
                (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2):
            if nid in seen:
                continue
            x, y = to_xy(lon, lat, kx, ky)
            if own_line is None:
                touching = (point_in_ring(x, y, ring_xy)
                            or dist_to_ring(x, y, ring_xy) <= WELD_TOLERANCE_M)
            else:
                touching = (project_on_line(x, y, line_xy)[0]
                            <= WELD_TOLERANCE_M)
            if not touching:
                continue
            if not levels_compatible(plat["levels"], lv):
                continue
            seen.add(nid)
            d, seg, t, p = project_on_line(x, y, line_xy)
            welds.append((seg, t, p, nid))

        welds.sort(key=lambda w: (w[0], w[1]))

        # Build the walk line's node list with weld junctions spliced in
        # at their projected positions, so the connection is topological.
        ids = []
        wi = 0
        junctions = []
        for i, pt in enumerate(line_xy):
            lon, lat = to_lonlat(pt[0], pt[1], kx, ky)
            ids.append(overlay.node(lon, lat))
            while wi < len(welds) and welds[wi][0] == i:
                jx, jy = welds[wi][2]
                jlon, jlat = to_lonlat(jx, jy, kx, ky)
                jid = overlay.node(jlon, jlat)
                ids.append(jid)
                junctions.append((jid, welds[wi][3]))
                wi += 1
        while wi < len(welds):  # projections onto the final vertex
            jx, jy = welds[wi][2]
            jlon, jlat = to_lonlat(jx, jy, kx, ky)
            jid = overlay.node(jlon, jlat)
            ids.append(jid)
            junctions.append((jid, welds[wi][3]))
            wi += 1

        tags = {"highway": "footway", "foot": "yes",
                "kora:platform_walk": "yes", "kora:source": plat["osm"]}
        if plat["tags"].get("level"):
            tags["level"] = plat["tags"]["level"]
        if plat["tags"].get("layer"):
            tags["layer"] = plat["tags"]["layer"]
        overlay.way(ids, tags)

        for jid, nid in junctions:
            overlay.way([nid, jid], {
                "highway": "footway", "foot": "yes",
                "kora:platform_link": "yes", "kora:source": plat["osm"],
                **({"level": plat["tags"]["level"]}
                   if plat["tags"].get("level") else {}),
            })
        stats["welds"] += len(junctions)
        if not junctions:
            stats["orphans"] += 1

        walk_lines.append({
            "line": [to_lonlat(p[0], p[1], kx, ky) for p in line_xy],
            "refs": platform_refs(plat["tags"]),
            "levels": plat["levels"],
            "connected": bool(junctions),
            "osm": plat["osm"],
        })

    # --- platforms already routable as open ways: usable as they stand
    for plat in reader.open_platforms:
        if not plat["routable"]:
            continue  # already synthesised above
        stats["open_platforms"] += 1
        walk_lines.append({
            "line": plat["line"],
            "refs": platform_refs(plat["tags"]),
            "levels": plat["levels"],
            "connected": True,  # a real routable way, already in the graph
            "osm": plat["osm"],
        })

    # --- lifts: one hub joining every level the shaft touches
    for lift in reader.lifts:
        ring_xy = [to_xy(lon, lat, kx, ky) for lon, lat in lift["ring"]]
        lons = [p[0] for p in lift["ring"]]
        lats = [p[1] for p in lift["ring"]]
        clon = (min(lons) + max(lons)) / 2
        clat = (min(lats) + max(lats)) / 2
        touching = []
        seen = set()
        for nid, lon, lat, lv in nodes_near(clon, clat):
            if nid in seen:
                continue
            x, y = to_xy(lon, lat, kx, ky)
            if not (point_in_ring(x, y, ring_xy)
                    or dist_to_ring(x, y, ring_xy) <= LIFT_TOLERANCE_M):
                continue
            seen.add(nid)
            touching.append(nid)
        # A shaft touching fewer than two ways connects nothing.
        if len(touching) < 2:
            continue
        stats["lifts"] += 1
        # The hub carries `highway=elevator` itself: Valhalla prices
        # elevator *nodes* (its elevator_penalty), and an untagged hub
        # would hand out a free vertical shortcut that even an able
        # walker would then prefer over the stairs beside it.
        hub = overlay.node(clon, clat, {
            "highway": "elevator", "wheelchair": "yes",
            "kora:elevator": "yes",
        })
        for nid in touching:
            overlay.way([nid, hub], {
                "highway": "footway", "foot": "yes", "wheelchair": "yes",
                "kora:elevator": "yes", "kora:source": lift["osm"],
            })
        stats["lift_links"] += len(touching)

    return walk_lines, stats


# ----------------------------------------------------------------- anchors


def build_anchors(walk_lines):
    """Project every GTFS quay onto its platform's walk line."""
    stops_path = GTFS_IN / "stops.txt"
    if not stops_path.exists():
        sys.exit(f"missing {stops_path} — run pipeline step 05 first")

    usable = [w for w in walk_lines if w["connected"] and len(w["line"]) >= 2]
    cell = 0.002
    grid = defaultdict(list)
    for w in usable:
        for lon, lat in w["line"]:
            grid[(int(lon / cell), int(lat / cell))].append(w)

    import csv
    anchors = {}
    tiers = defaultdict(int)
    per_station = defaultdict(lambda: defaultdict(int))

    with stops_path.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            code = (row.get("platform_code") or "").strip()
            parent = (row.get("parent_station") or "").strip()
            if not code or not parent:
                continue
            try:
                lat = float(row["stop_lat"])
                lon = float(row["stop_lon"])
            except (KeyError, ValueError):
                continue
            kx, ky = metric_frame(lat)
            x, y = to_xy(lon, lat, kx, ky)

            cx, cy = int(lon / cell), int(lat / cell)
            seen = set()
            best_ref = best_any = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for w in grid.get((cx + dx, cy + dy), ()):
                        if id(w) in seen:
                            continue
                        seen.add(id(w))
                        line_xy = [to_xy(p[0], p[1], kx, ky) for p in w["line"]]
                        d, _, _, p = project_on_line(x, y, line_xy)
                        if d > ANCHOR_MAX_M:
                            continue
                        cand = (d, p, w)
                        if best_any is None or d < best_any[0]:
                            best_any = cand
                        if code in w["refs"] and (best_ref is None
                                                  or d < best_ref[0]):
                            best_ref = cand

            pick, tier = (best_ref, "centerline_ref") if best_ref else \
                         (best_any, "centerline_near") if best_any else \
                         (None, "unanchored")
            if pick is None:
                tiers["unanchored"] += 1
                per_station[parent]["unanchored"] += 1
                continue
            d, p, w = pick
            alon, alat = to_lonlat(p[0], p[1], kx, ky)
            anchors[row["stop_id"]] = {
                "lat": round(alat, 7), "lon": round(alon, 7),
                "tier": tier, "platform": w["osm"], "dist_m": round(d, 1),
            }
            tiers[tier] += 1
            per_station[parent][tier] += 1

    return anchors, dict(tiers), per_station


# ------------------------------------------------------------------ output


def write_overlay(overlay: Overlay, path: Path) -> None:
    with osmium.SimpleWriter(str(path), overwrite=True) as w:
        for nid, lon, lat, tags in overlay.nodes:
            w.add_node(osmium.osm.mutable.Node(
                id=nid, location=(lon, lat), tags=tags))
        for wid, nodes, tags in overlay.ways:
            w.add_way(osmium.osm.mutable.Way(id=wid, nodes=nodes, tags=tags))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force-extract", action="store_true",
                    help="Re-cut the station slice out of the source PBF.")
    args = ap.parse_args()

    run_extract(args.force_extract)

    print(f"reading {EXTRACT_PBF.name} …")
    reader = Reader()
    reader.read(EXTRACT_PBF)
    print(f"  platform areas {len(reader.platforms):,}  "
          f"open platform ways {len(reader.open_platforms):,}  "
          f"lift shafts {len(reader.lifts):,}  "
          f"pedestrian nodes {len(reader.ped_nodes):,}")

    overlay = Overlay()
    walk_lines, stats = build(reader, overlay)
    print(f"  walk lines {stats['platforms']:,} from areas "
          f"+ {stats['open_synthesised']:,} from unroutable ways "
          f"({stats['open_platforms']:,} ways already routable)")
    print(f"  welds {stats['welds']:,}  "
          f"unwelded platforms {stats['orphans']:,}")
    print(f"  lift hubs {stats['lifts']:,}  lift links {stats['lift_links']:,}")

    write_overlay(overlay, OVERLAY_PBF)
    print(f"→ {OVERLAY_PBF}  "
          f"({len(overlay.nodes):,} nodes, {len(overlay.ways):,} ways)")

    anchors, tiers, per_station = build_anchors(walk_lines)
    ANCHORS_JSON.write_text(json.dumps(anchors, indent=0, sort_keys=True))
    print(f"→ {ANCHORS_JSON}  ({len(anchors):,} quays)  tiers: {tiers}")

    COVERAGE_JSON.parent.mkdir(parents=True, exist_ok=True)
    COVERAGE_JSON.write_text(json.dumps({
        "overlay": stats,
        "anchor_tiers": tiers,
        "stations": {k: dict(v) for k, v in sorted(per_station.items())},
    }, indent=1, sort_keys=True))
    print(f"→ {COVERAGE_JSON}")


if __name__ == "__main__":
    main()
