#!/usr/bin/env python3
"""
Step 04a — Cut OSM PBF to the Switzerland bbox (with small buffer).

Merges the Switzerland and Liechtenstein Geofabrik PBFs from step 03 and
clips them to the bbox declared in scripts/transit/config.yaml. The output is
the routing graph fed to pfaedle in step 04c; nothing else consumes it.

Uses osmium-tool inside the pfaedle Docker container so the host needs only
Docker. Output:

    data/osm/ch_pfaedle.osm.pbf

Idempotent: skips if the output is newer than the inputs. Pass --force to rerun.
"""

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OSM_DIR = ROOT / "data" / "osm"
CFG_PATH = ROOT / "scripts" / "transit" / "config.yaml"

CH_PBF = OSM_DIR / "switzerland-latest.osm.pbf"
FL_PBF = OSM_DIR / "liechtenstein-latest.osm.pbf"
MERGED = OSM_DIR / "ch_fl_merged.osm.pbf"
OUT_PBF = OSM_DIR / "ch_pfaedle.osm.pbf"


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


def main() -> None:
    force = "--force" in sys.argv

    if not CH_PBF.exists():
        sys.exit(f"missing {CH_PBF} — run 03_download_osm.py first")
    if not FL_PBF.exists():
        sys.exit(f"missing {FL_PBF} — run 03_download_osm.py first")

    if not force and newer_than(OUT_PBF, [CH_PBF, FL_PBF, CFG_PATH]):
        size_mb = OUT_PBF.stat().st_size / 1_000_000
        print(f"Up-to-date: {OUT_PBF} ({size_mb:.0f} MB). Pass --force to rebuild.")
        return

    image = load_image()
    bbox = load_bbox()
    bbox_str = ",".join(f"{v:.4f}" for v in bbox)

    print(f"Merging Switzerland + Liechtenstein → {relpath(MERGED)}")
    docker_run(
        image, "osmium", "merge",
        "--overwrite",
        "-o", f"/work/{relpath(MERGED)}",
        f"/work/{relpath(CH_PBF)}",
        f"/work/{relpath(FL_PBF)}",
    )

    print(f"Cutting to bbox {bbox_str} → {relpath(OUT_PBF)}")
    docker_run(
        image, "osmium", "extract",
        "--overwrite",
        "-b", bbox_str,
        "-o", f"/work/{relpath(OUT_PBF)}",
        f"/work/{relpath(MERGED)}",
    )

    MERGED.unlink(missing_ok=True)

    size_mb = OUT_PBF.stat().st_size / 1_000_000
    print(f"Done. {size_mb:.0f} MB → {OUT_PBF}")


if __name__ == "__main__":
    main()
