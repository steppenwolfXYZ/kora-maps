#!/usr/bin/env python3
"""
Step 03 — Cut OSM PBF to the Switzerland bbox + extract rail ways for pill walking.

Cuts each Geofabrik country PBF from step 02 to the bbox declared in
scripts/transit/config.yaml, then merges the bbox-sized slices into a single
file fed to pfaedle in step 05. After the merge, runs `osmium tags-filter`
+ `osmium export` to produce a GeoJSON of rail ways used by step 07's
terminal-pill OSM walk (see pill-rendering concept § "Missing-range fill
(rail only)"). Step 07 consumes the GeoJSON only; it does not parse the PBF.

The bbox covers Switzerland + a 1–2 km margin past CH's outermost tips and
intentionally captures a foreign sliver (Domodossola, Konstanz, Annemasse,
Lörrach, Bregenz, ...). Step 02 downloads every neighbouring country so that
sliver is non-empty here.

Uses osmium-tool inside the pfaedle Docker container so the host needs only
Docker. Outputs:

    data/osm/ch_pfaedle.osm.pbf
    data/osm/rail_ways.geojson

Idempotent: skips if both outputs are newer than every input. Pass --force to rerun.
"""

import json
import subprocess
import sys
from pathlib import Path

import yaml

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
# Railway tags whose ways step 07 walks at terminal train stops. Subway/tram/
# funicular are excluded — they aren't used by train-bucket lines. See
# pill-rendering concept § "Missing-range fill (rail only)".
RAIL_TAG_FILTER = "w/railway=rail,light_rail,narrow_gauge"


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


def is_valid_geojson(path: Path) -> bool:
    """True if path parses as a GeoJSON FeatureCollection with a plausible
    number of rail LineStrings. Detects partial files left behind by a crashed
    osmium-export run — osmium streams features incrementally and may write
    thousands of point features (level crossings etc.) before hitting the
    duplicate-node error, so a structural type-check alone is fooled."""
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
    # CH+neighbours within the bbox has tens of thousands of rail ways;
    # require well above any crashed-mid-write count.
    n_lines = sum(
        1 for f in features
        if (f.get("geometry") or {}).get("type") == "LineString"
    )
    return n_lines >= 1000


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

    if pbf_fresh and rail_fresh:
        size_mb = OUT_PBF.stat().st_size / 1_000_000
        rail_mb = OUT_RAIL_GEOJSON.stat().st_size / 1_000_000
        print(f"Up-to-date: {OUT_PBF} ({size_mb:.0f} MB), "
              f"{OUT_RAIL_GEOJSON.name} ({rail_mb:.0f} MB). Pass --force to rebuild.")
        return

    image = load_image()
    bbox = load_bbox()
    bbox_str = ",".join(f"{v:.4f}" for v in bbox)

    if pbf_fresh:
        # Merged pfaedle PBF is fresh — only rail extraction needs to run.
        # Recreate the per-country bbox cuts since they're not kept on disk;
        # they're cheap relative to the merge.
        print(f"Reusing existing {OUT_PBF.name}; recreating bbox cuts for rail extraction")
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

    # Extract rail ways for step 07. Per-country slice → tags-filter →
    # osmium export → JSON concat with way-id dedup. The per-country path
    # avoids `osmium merge`'s incomplete dedup on cross-border duplicates,
    # which `osmium export` rejects with "Node ID twice in input".
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
    for cut in cuts:
        cut.unlink(missing_ok=True)

    rail_mb = OUT_RAIL_GEOJSON.stat().st_size / 1_000_000
    print(f"Done. Rail GeoJSON: {len(features):,} ways, "
          f"{rail_mb:.0f} MB → {OUT_RAIL_GEOJSON}")


if __name__ == "__main__":
    main()
