#!/usr/bin/env python3
"""
Download the Switzerland OSM PBF from Geofabrik.
Output: data/osm/switzerland-latest.osm.pbf  (~350 MB)
Updated daily by Geofabrik.
"""

import urllib.request
from pathlib import Path
import sys

OSM_URL = "https://download.geofabrik.de/europe/switzerland-latest.osm.pbf"

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "osm" / "switzerland-latest.osm.pbf"


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url}")
    print(f"  → {dest}")

    def progress(block_count, block_size, total_size):
        if total_size > 0:
            pct = block_count * block_size / total_size * 100
            mb = block_count * block_size / 1_000_000
            total_mb = total_size / 1_000_000
            print(f"\r  {pct:.1f}%  {mb:.0f}/{total_mb:.0f} MB", end="", flush=True)

    urllib.request.urlretrieve(url, dest, reporthook=progress)
    print()
    size_mb = dest.stat().st_size / 1_000_000
    print(f"Done. {size_mb:.0f} MB saved to {dest}")


if __name__ == "__main__":
    if OUT.exists() and "--force" not in sys.argv:
        size_mb = OUT.stat().st_size / 1_000_000
        print(f"Already downloaded ({size_mb:.0f} MB): {OUT}")
        print("Pass --force to re-download.")
    else:
        download(OSM_URL, OUT)
