#!/usr/bin/env python3
"""
Step 04c — Route the filtered GTFS feed through pfaedle.

Inputs:
  data/gtfs_filtered/  — written by 04b
  data/osm/ch_pfaedle.osm.pbf — written by 04a

Output:
  data/gtfs_routed/  — same GTFS schema plus shapes.txt and shape_dist_traveled
                       populated on stop_times.txt

Pfaedle runs in the carfree-pfaedle Docker image. The host needs only Docker.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GTFS_IN = ROOT / "data" / "gtfs_filtered"
GTFS_OUT = ROOT / "data" / "gtfs_routed"
OSM_PBF = ROOT / "data" / "osm" / "ch_pfaedle.osm.pbf"
CFG_PATH = ROOT / "scripts" / "transit" / "config.yaml"


def load_cfg() -> dict:
    return yaml.safe_load(CFG_PATH.read_text())


def relpath(p: Path) -> str:
    return str(p.relative_to(ROOT))


def main() -> None:
    if not GTFS_IN.exists():
        sys.exit(f"missing {GTFS_IN} — run 04b_preprocess_gtfs.py first")
    if not OSM_PBF.exists():
        sys.exit(f"missing {OSM_PBF} — run 04a_bbox_osm.py first")

    cfg = load_cfg().get("pfaedle", {})
    image = cfg.get("image", "carfree-pfaedle:latest")
    modes = cfg.get("modes", ["all"])

    # Stage input: pfaedle writes alongside its input by default, so we copy
    # the filtered feed into the output dir and route it in place.
    if GTFS_OUT.exists():
        shutil.rmtree(GTFS_OUT)
    shutil.copytree(GTFS_IN, GTFS_OUT)

    # Pfaedle expects a single comma-separated -m value; repeated flags
    # silently overwrite (only the last mode would be routed).
    modes_str = ",".join(modes) if isinstance(modes, list) else str(modes)

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{ROOT}:/work",
        "-w", "/work",
        image,
        "pfaedle",
        "-D",                                       # drop existing shapes
        "-x", f"/work/{relpath(OSM_PBF)}",
        "-m", modes_str,
        "--inplace",
        f"/work/{relpath(GTFS_OUT)}",
    ]

    print("Running pfaedle:")
    print("  $", " ".join(cmd))
    res = subprocess.run(cmd)
    if res.returncode != 0:
        sys.exit(f"pfaedle exited with status {res.returncode}")

    shapes = GTFS_OUT / "shapes.txt"
    if not shapes.exists():
        sys.exit("pfaedle did not produce shapes.txt — check stderr above")
    size_mb = shapes.stat().st_size / 1_000_000
    print(f"\nDone. shapes.txt = {size_mb:.1f} MB → {GTFS_OUT}")


if __name__ == "__main__":
    main()
