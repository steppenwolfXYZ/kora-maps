#!/usr/bin/env python3
"""
Download the latest Swiss GTFS feed from opentransportdata.swiss.
Output: data/gtfs/gtfs_complete.zip  (and extracted files in data/gtfs/)

Source: https://data.opentransportdata.swiss/dataset/timetable-2026-gtfs2020
The official Swiss timetable export, refreshed twice weekly. Carries the
`original_stop_id` (SLOID) column and full sector-range platform codes —
both required by the gtfs-source-switch concept.
"""

import re
import sys
import urllib.request
import zipfile
from pathlib import Path

DATASET_URL = "https://data.opentransportdata.swiss/dataset/timetable-2026-gtfs2020"
DATASET_PAGE = DATASET_URL

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data" / "gtfs"
ZIP_PATH = DATA_DIR / "gtfs_complete.zip"


def discover_latest_url() -> str:
    """Scrape the OTD dataset page for the most recent gtfs_fp*.zip resource."""
    print(f"Discovering latest resource from {DATASET_PAGE}")
    req = urllib.request.Request(
        DATASET_PAGE, headers={"User-Agent": "newmap-pipeline/1.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    matches = re.findall(
        r'https://data\.opentransportdata\.swiss/dataset/[0-9a-f-]+/resource/[0-9a-f-]+/download/gtfs_fp\d{4}_(\d{8})\.zip',
        html,
    )
    if not matches:
        raise RuntimeError(
            f"No gtfs_fp*.zip resource found on {DATASET_PAGE}. "
            "The page layout may have changed."
        )
    full_matches = re.findall(
        r'https://data\.opentransportdata\.swiss/dataset/[0-9a-f-]+/resource/[0-9a-f-]+/download/gtfs_fp\d{4}_\d{8}\.zip',
        html,
    )
    latest_idx = max(range(len(matches)), key=lambda i: matches[i])
    latest = full_matches[latest_idx]
    print(f"  latest: {latest.rsplit('/', 1)[1]}")
    return latest


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url}")
    print(f"  → {dest}")
    req = urllib.request.Request(url, headers={"User-Agent": "newmap-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total_size = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        chunk = 1 << 16
        with open(dest, "wb") as out:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                out.write(buf)
                downloaded += len(buf)
                if total_size:
                    pct = downloaded / total_size * 100
                    mb = downloaded / 1_000_000
                    total_mb = total_size / 1_000_000
                    print(f"\r  {pct:.1f}%  {mb:.0f}/{total_mb:.0f} MB",
                          end="", flush=True)
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
        url = discover_latest_url()
        download(url, ZIP_PATH)

    extract(ZIP_PATH, DATA_DIR)
