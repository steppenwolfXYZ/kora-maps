#!/usr/bin/env python3
"""
Step 03 — Cut OSM PBF to the Switzerland bbox + extract rail ways for pill walking.

Cuts each Geofabrik country PBF from step 02 to the bbox declared in
scripts/transit/config.yaml, then merges the bbox-sized slices into a single
file fed to pfaedle in step 05. After the merge, runs `osmium tags-filter`
+ `osmium export` to produce GeoJSONs of rail, tram, and street ways used by
step 07's stop-extent OSM walks (see stop-extent-osm-walk.md). Step 07
consumes the GeoJSONs only; it does not parse the PBF.

The bbox covers Switzerland + a 1–2 km margin past CH's outermost tips and
intentionally captures a foreign sliver (Domodossola, Konstanz, Annemasse,
Lörrach, Bregenz, ...). Step 02 downloads every neighbouring country so that
sliver is non-empty here.

Uses osmium-tool inside the pfaedle Docker container so the host needs only
Docker. Outputs:

    data/osm/ch_pfaedle.osm.pbf
    data/osm/rail_ways.geojson
    data/osm/tram_ways.geojson
    data/osm/street_ways.geojson
    data/osm/platform_ways.geojson
    data/osm/buildings.geojson
    data/osm/builtup_grid_100m.json

`tram_ways.geojson` and `street_ways.geojson` back the tram / bus stop-extent
walk (stop-extent-osm-walk.md). Both are clipped to buffers of
`streets_stop_buffer_m` (config.yaml) around all GTFS stop coordinates —
only stop surroundings are ever walked, and unclipped street data would be
orders of magnitude larger than the rail extract. Street ways keep their
`highway` and `name` tags (they feed the walk's same-street rule).

`buildings.geojson` carries only building centroids (Point features, no
geometry beyond `[lon, lat]`). It feeds the urbanness bracket in step 07
(zoom-level-rules concept § "Urbanness bracket"): each canonical UIC counts
how many buildings sit within 200 m and 500 m to derive a city / town /
village / rural bracket.

`builtup_grid_100m.json` is the built-up landuse raster: OSM `landuse`
polygons of the built-up classes rasterized onto a 100 m grid. It feeds the
regional_bus → city_bus promotion in step 06 (citybus-landuse-promotion.md)
and is shared with the v2 candidate diagnostic. Class list, grid convention,
and rasterization live in gtfs/citybus_promotion.py.

`platform_ways.geojson` carries OSM ways tagged `public_transport=platform`
(covering both bus/tram `highway=platform` and train `railway=platform`
mappings). Step 07 uses them to snap each station's search-index coord onto
a walkable platform (transit-routing.md § Endpoint inputs) so MOTIS's OSR
doesn't start walkers on `sidewalk=separate` road centerlines.

Idempotent: skips if all outputs are newer than every input. Pass --force to rerun.
"""

import csv
import json
import subprocess
import sys
from collections import defaultdict
from math import cos, radians
from pathlib import Path

import yaml

from gtfs.citybus_promotion import (
    GRID_PATH as OUT_BUILTUP_GRID,
    LANDUSE_TAG_FILTER,
    iter_polygons,
    rasterize_polygon,
    save_builtup_grid,
)

ROOT = Path(__file__).resolve().parents[2]
OSM_DIR = ROOT / "data" / "osm"
CFG_PATH = ROOT / "scripts" / "transit" / "config.yaml"

COUNTRY_PBFS = [
    "switzerland-latest.osm.pbf",
    "liechtenstein-latest.osm.pbf",
    "germany-latest.osm.pbf",
    "france-latest.osm.pbf",
    "italy-latest.osm.pbf",
    "austria-latest.osm.pbf",
]
OUT_PBF = OSM_DIR / "ch_pfaedle.osm.pbf"
OUT_RAIL_GEOJSON = OSM_DIR / "rail_ways.geojson"
OUT_TRAM_GEOJSON = OSM_DIR / "tram_ways.geojson"
OUT_STREET_GEOJSON = OSM_DIR / "street_ways.geojson"
OUT_BUILDINGS_GEOJSON = OSM_DIR / "buildings.geojson"
OUT_PLATFORM_GEOJSON = OSM_DIR / "platform_ways.geojson"
GTFS_STOPS = ROOT / "data" / "gtfs" / "stops.txt"
# Railway tags whose ways step 07 walks at terminal train stops. Subway/tram/
# funicular are excluded — they aren't used by train-bucket lines. See
# stop-extent-osm-walk.md § "Rail walk".
RAIL_TAG_FILTER = "w/railway=rail,light_rail,narrow_gauge"
# Tram network for the tram stop-extent walk: railway=tram plus light_rail
# (shared corridors; low relevance in Switzerland but harmless) plus
# narrow_gauge — tram-classified lines can continue on their own
# narrow-gauge railway beyond the city grid (Forchbahn), so their
# terminals can sit on narrow-gauge track.
TRAM_TAG_FILTER = "w/railway=tram,light_rail,narrow_gauge"
# Street network for the bus stop-extent walk: highway classes a bus can
# drive, plus the corresponding _link classes.
STREET_HIGHWAY_CLASSES = [
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "residential", "unclassified", "service", "living_street",
    "bus_guideway",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
    "tertiary_link",
]
STREET_TAG_FILTER = "w/highway=" + ",".join(STREET_HIGHWAY_CLASSES)
# Buildings: both closed-way buildings and building=* relations
# (multipolygons covering stations, malls, etc.). osmium export's
# add-centroid=force collapses them to a single representative Point each so
# the output stays small (~50 MB for CH + neighbours within the bbox).
BUILDING_TAG_FILTER = "wr/building"
# Transit platforms: ways with public_transport=platform (catches both
# highway=platform + public_transport=platform (bus/tram) and railway=platform
# + public_transport=platform (train) mappings). Only ways — platform nodes
# without a way are floating POIs that don't help the walk-graph snap.
PLATFORM_TAG_FILTER = "w/public_transport=platform"


def load_bbox() -> tuple:
    cfg = yaml.safe_load(CFG_PATH.read_text())
    b = cfg["osm_bbox"]
    return b["min_lon"], b["min_lat"], b["max_lon"], b["max_lat"]


def load_image() -> str:
    cfg = yaml.safe_load(CFG_PATH.read_text())
    return cfg.get("pfaedle", {}).get("image", "carfree-pfaedle:latest")


def docker_run(image: str, *args: str) -> None:
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{ROOT}:/work",
        "-w", "/work",
        image,
        *args,
    ]
    print("  $", " ".join(cmd))
    subprocess.run(cmd, check=True)


def relpath(p: Path) -> str:
    return str(p.relative_to(ROOT))


def newer_than(target: Path, sources: list) -> bool:
    """True if target exists and is newer than every source."""
    if not target.exists():
        return False
    t = target.stat().st_mtime
    return all(s.exists() and s.stat().st_mtime <= t for s in sources)


def is_valid_geojson(path: Path, min_lines: int = 1000) -> bool:
    """True if path parses as a GeoJSON FeatureCollection with a plausible
    number of LineStrings. Detects partial files left behind by a crashed
    osmium-export run — osmium streams features incrementally and may write
    thousands of point features (level crossings etc.) before hitting the
    duplicate-node error, so a structural type-check alone is fooled.
    `min_lines` is per-artifact: well above any crashed-mid-write count but
    below the artifact's real feature count."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if data.get("type") != "FeatureCollection":
        return False
    features = data.get("features")
    if not isinstance(features, list):
        return False
    n_lines = sum(
        1 for f in features
        if (f.get("geometry") or {}).get("type") == "LineString"
    )
    return n_lines >= min_lines


def is_valid_buildings_geojson(path: Path) -> bool:
    """Validate the buildings.geojson output. Buildings are exported as Points
    (centroids); CH alone has multi-million buildings, so demand at least
    100 000 to catch crashed-mid-write files."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if data.get("type") != "FeatureCollection":
        return False
    features = data.get("features")
    if not isinstance(features, list):
        return False
    n_pts = sum(
        1 for f in features
        if (f.get("geometry") or {}).get("type") == "Point"
    )
    return n_pts >= 100_000


def is_valid_builtup_grid(path: Path) -> bool:
    """Validate the built-up landuse raster. The bbox holds a few hundred
    thousand built-up 100 m cells; demand a plausible floor to catch
    crashed-mid-write files."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    cells = data.get("cells")
    return isinstance(cells, list) and len(cells) >= 50_000


def load_stop_grid(buffer_m: float):
    """Spatial grid over all GTFS stop coordinates (data/gtfs/stops.txt) for
    the buffer-clip test. Cell sizes are chosen so a 3×3 cell neighborhood
    always covers `buffer_m` around a point. Returns (grid, cell_x, cell_y)
    where grid maps (cx, cy) → [(lon, lat), ...]."""
    if not GTFS_STOPS.exists():
        sys.exit(f"missing {GTFS_STOPS} — run 01_download_gtfs.py first")
    cell_y = buffer_m / 111320.0
    # Lon cells sized for the bbox's highest latitude (cos 47.83° ≈ 0.67).
    cell_x = buffer_m / (111320.0 * 0.62)
    grid: dict = defaultdict(list)
    n = 0
    with open(GTFS_STOPS, encoding="utf-8-sig", newline="") as fp:
        for row in csv.DictReader(fp):
            try:
                lon = float(row["stop_lon"])
                lat = float(row["stop_lat"])
            except (KeyError, TypeError, ValueError):
                continue
            grid[(int(lon / cell_x), int(lat / cell_y))].append((lon, lat))
            n += 1
    print(f"  Stop buffer grid: {n:,} GTFS stop coords, "
          f"{len(grid):,} cells, {buffer_m:g} m buffer")
    return grid, cell_x, cell_y


def _near_stop(lon, lat, grid, cell_x, cell_y, buffer_m) -> bool:
    cx = int(lon / cell_x)
    cy = int(lat / cell_y)
    cos_lat = cos(radians(lat))
    b_sq = buffer_m * buffer_m
    for gx in (cx - 1, cx, cx + 1):
        for gy in (cy - 1, cy, cy + 1):
            for slon, slat in grid.get((gx, gy), ()):
                dx = (lon - slon) * 111320.0 * cos_lat
                dy = (lat - slat) * 111320.0
                if dx * dx + dy * dy <= b_sq:
                    return True
    return False


def _clip_to_buffers(coords, grid, cell_x, cell_y, buffer_m) -> list:
    """Split a way's coordinate list into the runs of vertices that lie
    within `buffer_m` of any GTFS stop, each run extended one vertex past
    the buffer on both sides (so segments crossing the boundary survive)
    and overlapping runs merged. Returns a list of coordinate runs (each
    ≥ 2 vertices, z components stripped); empty when the way never comes
    near a stop."""
    flags = [_near_stop(c[0], c[1], grid, cell_x, cell_y, buffer_m)
             for c in coords]
    n = len(coords)
    ranges: list = []
    i = 0
    while i < n:
        if not flags[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and flags[j + 1]:
            j += 1
        a, b = max(0, i - 1), min(n - 1, j + 1)
        if ranges and a <= ranges[-1][1] + 1:
            ranges[-1][1] = b
        else:
            ranges.append([a, b])
        i = j + 1
    return [[[c[0], c[1]] for c in coords[a:b + 1]]
            for a, b in ranges if b > a]


def extract_clipped_ways(image: str, cuts: list, tag_filter: str,
                         out_path: Path, label: str,
                         grid, cell_x, cell_y, buffer_m: float,
                         keep_props: tuple) -> None:
    """tags-filter + export per country slice → stream-parse → clip each way
    to the GTFS stop buffers → concat with way-id dedup → atomic write.

    Same per-country pattern as the rail extraction (avoids `osmium merge`'s
    incomplete cross-border dedup), but exports GeoJSONSeq and streams the
    parse — the unclipped street network totals gigabytes across the slices,
    far too big for a single json.loads. Only `keep_props` properties are
    retained (they feed the walk's same-street rule in step 07)."""
    way_pbfs: list = []
    way_gjs: list = []
    print(f"Extracting {label} ways per country slice → {out_path.name}")
    for cut in cuts:
        stem = cut.stem.replace(".osm", "")
        w_pbf = OSM_DIR / f"{stem}.{label}.osm.pbf"
        w_gj = OSM_DIR / f"{stem}.{label}.geojson"
        docker_run(
            image, "osmium", "tags-filter",
            "--overwrite",
            "-o", f"/work/{relpath(w_pbf)}",
            f"/work/{relpath(cut)}",
            tag_filter,
        )
        docker_run(
            image, "osmium", "export",
            "--overwrite",
            "-f", "geojsonseq",
            "-o", f"/work/{relpath(w_gj)}",
            f"/work/{relpath(w_pbf)}",
        )
        way_pbfs.append(w_pbf)
        way_gjs.append(w_gj)

    seen_ids: set = set()
    features: list = []
    n_ways = n_kept = 0
    for gj in way_gjs:
        with open(gj, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("\x1e"):
                    line = line[1:]
                if not line:
                    continue
                try:
                    feat = json.loads(line)
                except json.JSONDecodeError:
                    continue
                geom = feat.get("geometry") or {}
                if geom.get("type") != "LineString":
                    continue
                fid = feat.get("id")
                if fid is not None:
                    if fid in seen_ids:
                        continue
                    seen_ids.add(fid)
                coords = geom.get("coordinates") or []
                if len(coords) < 2:
                    continue
                n_ways += 1
                runs = _clip_to_buffers(coords, grid, cell_x, cell_y, buffer_m)
                if not runs:
                    continue
                n_kept += 1
                props_in = feat.get("properties") or {}
                props = {k: props_in[k] for k in keep_props if props_in.get(k)}
                for run in runs:
                    features.append({
                        "type": "Feature",
                        "id": fid,
                        "properties": props,
                        "geometry": {"type": "LineString",
                                     "coordinates": run},
                    })

    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": features,
    }))
    tmp_path.replace(out_path)

    for p in way_pbfs + way_gjs:
        p.unlink(missing_ok=True)

    mb = out_path.stat().st_size / 1_000_000
    print(f"Done. {label} GeoJSON: {n_kept:,} of {n_ways:,} ways near stops "
          f"({len(features):,} clipped segments), {mb:.0f} MB → {out_path}")


def cut_bbox(image: str, bbox_str: str, inputs: list) -> list:
    cuts: list = []
    for pbf in inputs:
        cut = OSM_DIR / f"{pbf.stem.replace('.osm', '')}.bbox.osm.pbf"
        print(f"Cutting {pbf.name} → {cut.name} ({bbox_str})")
        docker_run(
            image, "osmium", "extract",
            "--overwrite",
            "-b", bbox_str,
            "-o", f"/work/{relpath(cut)}",
            f"/work/{relpath(pbf)}",
        )
        cuts.append(cut)
    return cuts


def main() -> None:
    force = "--force" in sys.argv

    inputs = [OSM_DIR / name for name in COUNTRY_PBFS]
    missing = [p for p in inputs if not p.exists()]
    if missing:
        names = ", ".join(p.name for p in missing)
        sys.exit(f"missing PBFs ({names}) — run 02_download_osm.py first")

    pbf_fresh = (not force) and newer_than(OUT_PBF, inputs + [CFG_PATH])
    rail_fresh = (
        (not force) and pbf_fresh
        and newer_than(OUT_RAIL_GEOJSON, [OUT_PBF])
        and is_valid_geojson(OUT_RAIL_GEOJSON)
    )
    # Tram / street extracts also depend on CFG_PATH (streets_stop_buffer_m).
    # Validity floors: CH tram networks total a few thousand clipped ways;
    # streets near stops run into the hundreds of thousands.
    tram_fresh = (
        (not force) and pbf_fresh
        and newer_than(OUT_TRAM_GEOJSON, [OUT_PBF, CFG_PATH])
        and is_valid_geojson(OUT_TRAM_GEOJSON, min_lines=200)
    )
    street_fresh = (
        (not force) and pbf_fresh
        and newer_than(OUT_STREET_GEOJSON, [OUT_PBF, CFG_PATH])
        and is_valid_geojson(OUT_STREET_GEOJSON, min_lines=20_000)
    )
    buildings_fresh = (
        (not force) and pbf_fresh
        and newer_than(OUT_BUILDINGS_GEOJSON, [OUT_PBF])
        and is_valid_buildings_geojson(OUT_BUILDINGS_GEOJSON)
    )
    builtup_fresh = (
        (not force) and pbf_fresh
        and newer_than(OUT_BUILTUP_GRID, [OUT_PBF])
        and is_valid_builtup_grid(OUT_BUILTUP_GRID)
    )
    # Platforms across the whole bbox run into the tens of thousands; a
    # min_lines floor of 5 000 catches crashed-mid-write files.
    platform_fresh = (
        (not force) and pbf_fresh
        and newer_than(OUT_PLATFORM_GEOJSON, [OUT_PBF])
        and is_valid_geojson(OUT_PLATFORM_GEOJSON, min_lines=5_000)
    )

    if (pbf_fresh and rail_fresh and tram_fresh and street_fresh
            and buildings_fresh and builtup_fresh and platform_fresh):
        size_mb = OUT_PBF.stat().st_size / 1_000_000
        gj_sizes = ", ".join(
            f"{p.name} ({p.stat().st_size / 1_000_000:.0f} MB)"
            for p in (OUT_RAIL_GEOJSON, OUT_TRAM_GEOJSON,
                      OUT_STREET_GEOJSON, OUT_PLATFORM_GEOJSON,
                      OUT_BUILDINGS_GEOJSON, OUT_BUILTUP_GRID))
        print(f"Up-to-date: {OUT_PBF} ({size_mb:.0f} MB), {gj_sizes}. "
              "Pass --force to rebuild.")
        return

    image = load_image()
    bbox = load_bbox()
    bbox_str = ",".join(f"{v:.4f}" for v in bbox)

    if pbf_fresh:
        # Merged pfaedle PBF is fresh — only stale extractions need to run.
        # Recreate the per-country bbox cuts since they're not kept on disk;
        # they're cheap relative to the merge.
        print(f"Reusing existing {OUT_PBF.name}; recreating bbox cuts for way extraction")
        cuts = cut_bbox(image, bbox_str, inputs)
    else:
        cuts = cut_bbox(image, bbox_str, inputs)
        print(f"Merging {len(cuts)} bbox slices → {relpath(OUT_PBF)}")
        docker_run(
            image, "osmium", "merge",
            "--overwrite",
            "-o", f"/work/{relpath(OUT_PBF)}",
            *[f"/work/{relpath(c)}" for c in cuts],
        )
        size_mb = OUT_PBF.stat().st_size / 1_000_000
        print(f"Merged PBF: {size_mb:.0f} MB → {OUT_PBF}")

    # Each extraction runs only when its own artifact is stale — a rerun
    # for the new tram / street extracts must not redo the (expensive)
    # buildings streaming pass. `cuts` stay on disk until the end of main().
    if not rail_fresh:
        extract_rail_ways(image, cuts)

    if not tram_fresh or not street_fresh:
        cfg = yaml.safe_load(CFG_PATH.read_text())
        buffer_m = float(cfg.get("streets_stop_buffer_m", 150))
        grid, cell_x, cell_y = load_stop_grid(buffer_m)
        if not tram_fresh:
            extract_clipped_ways(image, cuts, TRAM_TAG_FILTER,
                                 OUT_TRAM_GEOJSON, "tram",
                                 grid, cell_x, cell_y, buffer_m,
                                 keep_props=("railway", "name"))
        if not street_fresh:
            extract_clipped_ways(image, cuts, STREET_TAG_FILTER,
                                 OUT_STREET_GEOJSON, "street",
                                 grid, cell_x, cell_y, buffer_m,
                                 keep_props=("highway", "name"))

    if not buildings_fresh:
        extract_buildings(image, cuts)

    if not builtup_fresh:
        extract_builtup_grid(image, cuts)

    if not platform_fresh:
        extract_platform_ways(image, cuts)

    for cut in cuts:
        cut.unlink(missing_ok=True)


def extract_rail_ways(image: str, cuts: list) -> None:
    """Extract rail ways for step 07. Per-country slice → tags-filter →
    osmium export → JSON concat with way-id dedup. The per-country path
    avoids `osmium merge`'s incomplete dedup on cross-border duplicates,
    which `osmium export` rejects with "Node ID twice in input"."""
    rail_geojsons: list = []
    rail_pbfs: list = []
    print(f"Extracting rail ways per country slice → {OUT_RAIL_GEOJSON.name}")
    for cut in cuts:
        stem = cut.stem.replace(".osm", "")
        rail_pbf = OSM_DIR / f"{stem}.rail.osm.pbf"
        rail_gj = OSM_DIR / f"{stem}.rail.geojson"
        docker_run(
            image, "osmium", "tags-filter",
            "--overwrite",
            "-o", f"/work/{relpath(rail_pbf)}",
            f"/work/{relpath(cut)}",
            RAIL_TAG_FILTER,
        )
        docker_run(
            image, "osmium", "export",
            "--overwrite",
            "-f", "geojson",
            "-o", f"/work/{relpath(rail_gj)}",
            f"/work/{relpath(rail_pbf)}",
        )
        rail_pbfs.append(rail_pbf)
        rail_geojsons.append(rail_gj)

    seen_ids: set = set()
    features: list = []
    for gj in rail_geojsons:
        data = json.loads(gj.read_text())
        for feat in data.get("features", []):
            if (feat.get("geometry") or {}).get("type") != "LineString":
                continue
            fid = feat.get("id")
            if fid is not None:
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
            features.append(feat)

    # Atomic write — a crash mid-write must not leave a partial file behind
    # that satisfies the next run's mtime-based idempotency check.
    tmp_path = OUT_RAIL_GEOJSON.with_suffix(OUT_RAIL_GEOJSON.suffix + ".tmp")
    tmp_path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": features,
    }))
    tmp_path.replace(OUT_RAIL_GEOJSON)

    for p in rail_pbfs + rail_geojsons:
        p.unlink(missing_ok=True)

    rail_mb = OUT_RAIL_GEOJSON.stat().st_size / 1_000_000
    print(f"Done. Rail GeoJSON: {len(features):,} ways, "
          f"{rail_mb:.0f} MB → {OUT_RAIL_GEOJSON}")


def extract_platform_ways(image: str, cuts: list) -> None:
    """Extract public_transport=platform ways for step 07's search-index
    coord snap AND for `scripts/routing/preprocess_gtfs_for_motis.py`'s platform-code
    snap (transit-routing.md § Endpoint inputs / § Backend). Same
    per-country-slice pattern as extract_rail_ways: tags-filter → osmium
    export → JSON concat with way-id dedup. LineString geometries only;
    centroid calculation lives in the consumers. Keeps the tags the
    consumers need — `local_ref` and `uic_ref` disambiguate which specific
    OSM platform matches which GTFS `(parent_station, platform_code)` pair
    (nearest-in-space isn't enough — at Eigerplatz platform :C sits 3 m
    farther from the GTFS parent centroid than :D, so nearest-wins snaps
    to the wrong direction)."""
    plat_pbfs: list = []
    plat_geojsons: list = []
    print(f"Extracting platform ways per country slice → "
          f"{OUT_PLATFORM_GEOJSON.name}")
    for cut in cuts:
        stem = cut.stem.replace(".osm", "")
        p_pbf = OSM_DIR / f"{stem}.platform.osm.pbf"
        p_gj = OSM_DIR / f"{stem}.platform.geojson"
        docker_run(
            image, "osmium", "tags-filter",
            "--overwrite",
            "-o", f"/work/{relpath(p_pbf)}",
            f"/work/{relpath(cut)}",
            PLATFORM_TAG_FILTER,
        )
        docker_run(
            image, "osmium", "export",
            "--overwrite",
            "-f", "geojson",
            "-o", f"/work/{relpath(p_gj)}",
            f"/work/{relpath(p_pbf)}",
        )
        plat_pbfs.append(p_pbf)
        plat_geojsons.append(p_gj)

    seen_ids: set = set()
    features: list = []
    # Consumer needs: local_ref + uic_ref for platform-code / station-UIC
    # exact-match; public_transport / railway / highway for filtering; name
    # for debugging. Everything else stripped to keep the file small.
    KEEP_TAGS = ("local_ref", "uic_ref", "public_transport",
                 "railway", "highway", "name")
    for gj in plat_geojsons:
        data = json.loads(gj.read_text())
        for feat in data.get("features", []):
            if (feat.get("geometry") or {}).get("type") != "LineString":
                continue
            fid = feat.get("id")
            if fid is not None:
                if fid in seen_ids:
                    continue
                seen_ids.add(fid)
            props_in = feat.get("properties") or {}
            props = {k: props_in[k] for k in KEEP_TAGS if props_in.get(k)}
            features.append({
                "type": "Feature",
                "id": fid,
                "properties": props,
                "geometry": feat["geometry"],
            })

    tmp_path = OUT_PLATFORM_GEOJSON.with_suffix(
        OUT_PLATFORM_GEOJSON.suffix + ".tmp"
    )
    tmp_path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": features,
    }))
    tmp_path.replace(OUT_PLATFORM_GEOJSON)

    for p in plat_pbfs + plat_geojsons:
        p.unlink(missing_ok=True)

    mb = OUT_PLATFORM_GEOJSON.stat().st_size / 1_000_000
    print(f"Done. Platform GeoJSON: {len(features):,} ways, "
          f"{mb:.0f} MB → {OUT_PLATFORM_GEOJSON}")


def extract_buildings(image: str, cuts: list) -> None:
    """Extract building centroids for step 07's urbanness bracket. Same
    per-country-slice pattern as the rail extraction: tags-filter → export
    (with centroid-force) → Python dedup. Buildings cross borders far less
    than rail does, so dedup hits are rare, but the same key works."""
    building_pbfs: list = []
    building_geojsons: list = []
    print(f"Extracting building centroids per country slice → "
          f"{OUT_BUILDINGS_GEOJSON.name}")
    for cut in cuts:
        stem = cut.stem.replace(".osm", "")
        bldg_pbf = OSM_DIR / f"{stem}.bldg.osm.pbf"
        bldg_gj = OSM_DIR / f"{stem}.bldg.geojson"
        docker_run(
            image, "osmium", "tags-filter",
            "--overwrite",
            "-o", f"/work/{relpath(bldg_pbf)}",
            f"/work/{relpath(cut)}",
            BUILDING_TAG_FILTER,
        )
        # osmium export to GeoJSONSeq (newline-delimited GeoJSON) so step 03
        # can stream the parsing — building polygons total several GB across
        # CH + neighbours, way too big for a single json.loads. osmium has no
        # native centroid extraction, so we compute the centroid from the
        # polygon ring vertices in Python below.
        docker_run(
            image, "osmium", "export",
            "--overwrite",
            "-f", "geojsonseq",
            "-o", f"/work/{relpath(bldg_gj)}",
            f"/work/{relpath(bldg_pbf)}",
        )
        building_pbfs.append(bldg_pbf)
        building_geojsons.append(bldg_gj)

    # Stream per-feature centroid extraction from the line-delimited GeoJSON.
    # For polygons / multipolygons we use the bbox centre of the first ring as
    # a cheap centroid — fine for the urbanness use case (we just need a
    # representative point inside the building). Point geometries (the small
    # subset of buildings tagged on nodes) pass through directly.
    coords: list = []
    n_polys = n_points = n_skipped = 0

    def _flat_coords(coords_obj):
        """Yield flat (lon, lat) pairs from a polygon ring, multipolygon, or
        linestring. Used to compute the bbox centre below."""
        if not isinstance(coords_obj, list) or not coords_obj:
            return
        first = coords_obj[0]
        if isinstance(first, (int, float)):
            # Single Point
            yield (coords_obj[0], coords_obj[1])
            return
        if isinstance(first, list) and first and isinstance(first[0], (int, float)):
            # LineString / Polygon ring
            for pt in coords_obj:
                yield (pt[0], pt[1])
            return
        # Polygon / MultiLineString — take the first ring only.
        for sub in _flat_coords(first):
            yield sub

    def _bbox_centre(coords_obj):
        xs = []
        ys = []
        for x, y in _flat_coords(coords_obj):
            xs.append(x)
            ys.append(y)
        if not xs:
            return None
        return ((min(xs) + max(xs)) / 2.0,
                (min(ys) + max(ys)) / 2.0)

    seen_ids: set = set()
    for gj in building_geojsons:
        with open(gj, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                # RFC 7464 GeoJSONSeq prefixes each record with U+001E; strip
                # it before parsing.
                if line.startswith("\x1e"):
                    line = line[1:]
                if not line or line.startswith("#"):
                    continue
                try:
                    feat = json.loads(line)
                except json.JSONDecodeError:
                    n_skipped += 1
                    continue
                fid = feat.get("id")
                if fid is not None:
                    if fid in seen_ids:
                        continue
                    seen_ids.add(fid)
                geom = feat.get("geometry") or {}
                gtype = geom.get("type")
                g_coords = geom.get("coordinates")
                if gtype == "Point" and isinstance(g_coords, list) and len(g_coords) >= 2:
                    coords.append([round(float(g_coords[0]), 6),
                                   round(float(g_coords[1]), 6)])
                    n_points += 1
                    continue
                centre = _bbox_centre(g_coords)
                if centre is not None:
                    coords.append([round(centre[0], 6), round(centre[1], 6)])
                    n_polys += 1
                else:
                    n_skipped += 1
    print(f"  Building centroids: {n_polys:,} polygons, {n_points:,} points, "
          f"{n_skipped:,} skipped")

    tmp_path = OUT_BUILDINGS_GEOJSON.with_suffix(
        OUT_BUILDINGS_GEOJSON.suffix + ".tmp"
    )
    # Custom compact format: { "coords": [[lon, lat], ...] }. Not strict
    # GeoJSON; step 07 reads via json.loads. Stays a .geojson filename for
    # locality but the content is a flat coordinate list to keep the file
    # small (~50 MB instead of 200+ MB).
    tmp_path.write_text(json.dumps({"coords": coords}))
    tmp_path.replace(OUT_BUILDINGS_GEOJSON)

    for p in building_pbfs + building_geojsons:
        p.unlink(missing_ok=True)

    bldg_mb = OUT_BUILDINGS_GEOJSON.stat().st_size / 1_000_000
    print(f"Done. Building centroids: {len(coords):,} buildings, "
          f"{bldg_mb:.0f} MB → {OUT_BUILDINGS_GEOJSON}")


def extract_builtup_grid(image: str, cuts: list) -> None:
    """Extract built-up landuse polygons and rasterize them onto the 100 m
    grid consumed by step 06's city-bus promotion
    (citybus-landuse-promotion.md). Same per-country-slice pattern as the
    other extracts: tags-filter → export (GeoJSONSeq, streamed) → Python
    dedup by feature id → scanline rasterization (gtfs/citybus_promotion
    .py) → atomic write."""
    lu_pbfs: list = []
    lu_geojsons: list = []
    print(f"Extracting built-up landuse per country slice → "
          f"{OUT_BUILTUP_GRID.name}")
    for cut in cuts:
        stem = cut.stem.replace(".osm", "")
        lu_pbf = OSM_DIR / f"{stem}.lu.osm.pbf"
        lu_gj = OSM_DIR / f"{stem}.lu.geojson"
        docker_run(
            image, "osmium", "tags-filter",
            "--overwrite",
            "-o", f"/work/{relpath(lu_pbf)}",
            f"/work/{relpath(cut)}",
            LANDUSE_TAG_FILTER,
        )
        docker_run(
            image, "osmium", "export",
            "--overwrite",
            "-f", "geojsonseq",
            "-o", f"/work/{relpath(lu_gj)}",
            f"/work/{relpath(lu_pbf)}",
        )
        lu_pbfs.append(lu_pbf)
        lu_geojsons.append(lu_gj)

    cells: set = set()
    seen_ids: set = set()
    n_polys = n_dupes = 0
    for gj in lu_geojsons:
        with open(gj, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip().lstrip("\x1e")
                if not line:
                    continue
                try:
                    feat = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fid = feat.get("id")
                if fid is not None:
                    if fid in seen_ids:
                        n_dupes += 1
                        continue
                    seen_ids.add(fid)
                for rings in iter_polygons(feat.get("geometry") or {}):
                    rasterize_polygon(rings, cells)
                    n_polys += 1

    save_builtup_grid(cells)

    for p in lu_pbfs + lu_geojsons:
        p.unlink(missing_ok=True)

    grid_mb = OUT_BUILTUP_GRID.stat().st_size / 1_000_000
    print(f"Done. Built-up landuse: {n_polys:,} polygons "
          f"({n_dupes:,} cross-border dupes skipped), {len(cells):,} cells, "
          f"{grid_mb:.0f} MB → {OUT_BUILTUP_GRID}")


if __name__ == "__main__":
    main()
