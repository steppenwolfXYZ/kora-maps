#!/usr/bin/env python3
"""
Download OSM PBFs from Geofabrik.
Outputs:
  data/osm/switzerland-latest.osm.pbf  (~350 MB)
  data/osm/liechtenstein-latest.osm.pbf  (~1 MB)
Updated daily by Geofabrik.
"""

import urllib.request
from pathlib import Path
import sys

SOURCES = [
    ("https://download.geofabrik.de/europe/switzerland-latest.osm.pbf",
     "switzerland-latest.osm.pbf"),
    ("https://download.geofabrik.de/europe/liechtenstein-latest.osm.pbf",
     "liechtenstein-latest.osm.pbf"),
]

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "osm"


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
    force = "--force" in sys.argv
    for url, filename in SOURCES:
        dest = OUT_DIR / filename
        if dest.exists() and not force:
            size_mb = dest.stat().st_size / 1_000_000
            print(f"Already downloaded ({size_mb:.0f} MB): {dest}")
            print("Pass --force to re-download.")
        else:
            download(url, dest)
