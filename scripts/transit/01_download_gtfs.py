#!/usr/bin/env python3
"""
Download the latest Swiss GTFS feed from gtfs.geops.ch (public mirror of
the official Swiss schedule, updated daily, no auth required).
Output: data/gtfs/gtfs_complete.zip  (and extracted files in data/gtfs/)

Source: https://gtfs.geops.ch/ — sourced from official Swiss HAFAS data.
"""

import urllib.request
import zipfile
import sys
from pathlib import Path

# Public mirror — updated daily, all operators, all modes
GTFS_URL = "https://gtfs.geops.ch/dl/gtfs_complete.zip"

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data" / "gtfs"
ZIP_PATH = DATA_DIR / "gtfs_complete.zip"


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


def extract(zip_path: Path, out_dir: Path) -> None:
    print(f"Extracting to {out_dir}")
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        for name in names:
            print(f"  {name}")
        zf.extractall(out_dir)
    print("Done.")


if __name__ == "__main__":
    if ZIP_PATH.exists() and "--force" not in sys.argv:
        print(f"Already downloaded: {ZIP_PATH}  (pass --force to re-download)")
    else:
        download(GTFS_URL, ZIP_PATH)

    extract(ZIP_PATH, DATA_DIR)
