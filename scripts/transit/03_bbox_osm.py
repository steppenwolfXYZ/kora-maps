#!/usr/bin/env python3
"""
Step 03 — Cut OSM PBF to the Switzerland bbox.

Cuts each Geofabrik country PBF from step 02 to the bbox declared in
scripts/transit/config.yaml, then merges the bbox-sized slices into a single
file. The output is the routing graph fed to pfaedle in step 05; nothing
else consumes it.

The bbox covers Switzerland + a 1–2 km margin past CH's outermost tips and
intentionally captures a foreign sliver (Domodossola, Konstanz, Annemasse,
Lörrach, Bregenz, ...). Step 02 downloads every neighbouring country so that
sliver is non-empty here.

Uses osmium-tool inside the pfaedle Docker container so the host needs only
Docker. Output:

    data/osm/ch_pfaedle.osm.pbf

Idempotent: skips if the output is newer than every input. Pass --force to rerun.
"""

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

    inputs = [OSM_DIR / name for name in COUNTRY_PBFS]
    missing = [p for p in inputs if not p.exists()]
    if missing:
        names = ", ".join(p.name for p in missing)
        sys.exit(f"missing PBFs ({names}) — run 02_download_osm.py first")

    if not force and newer_than(OUT_PBF, inputs + [CFG_PATH]):
        size_mb = OUT_PBF.stat().st_size / 1_000_000
        print(f"Up-to-date: {OUT_PBF} ({size_mb:.0f} MB). Pass --force to rebuild.")
        return

    image = load_image()
    bbox = load_bbox()
    bbox_str = ",".join(f"{v:.4f}" for v in bbox)

    # Cut each country PBF to the bbox first. Merging 12+ GB of country
    # extracts before cutting would need a huge intermediate file and burn
    # disk for no benefit — each cut is only the bbox slice (~tens of MB),
    # and merging those small slices is cheap.
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

    print(f"Merging {len(cuts)} bbox slices → {relpath(OUT_PBF)}")
    docker_run(
        image, "osmium", "merge",
        "--overwrite",
        "-o", f"/work/{relpath(OUT_PBF)}",
        *[f"/work/{relpath(c)}" for c in cuts],
    )

    for cut in cuts:
        cut.unlink(missing_ok=True)

    size_mb = OUT_PBF.stat().st_size / 1_000_000
    print(f"Done. {size_mb:.0f} MB → {OUT_PBF}")


if __name__ == "__main__":
    main()
