#!/usr/bin/env python3
"""
Download the latest Swiss public-transport master data:

  • GTFS feed                 → data/gtfs/gtfs_complete.zip (+ extracted)
      Source: https://data.opentransportdata.swiss/dataset/timetable-2026-gtfs2020
      Carries the `original_stop_id` (SLOID) column and full sector-range
      platform codes — required by the gtfs-source-switch concept.

  • Atlas traffic-point CSV   → data/atlas/actual-date-world-traffic-point.csv
      Source: https://data.opentransportdata.swiss/dataset/traffic-point-v2
      Per-platform attributes (length, compassDirection) keyed by SLOID —
      consumed by the prm-platform-positions concept.

Flags:
  --force         re-download GTFS and atlas
  --force-gtfs    re-download GTFS only
  --force-atlas   re-download atlas only

Without any flag, each download skips when the target file is already present.
"""

import re
import sys
import urllib.request
import zipfile
from pathlib import Path

GTFS_DATASET_URL  = "https://data.opentransportdata.swiss/dataset/timetable-2026-gtfs2020"
ATLAS_DATASET_URL = "https://data.opentransportdata.swiss/dataset/traffic-point-v2"

ROOT = Path(__file__).resolve().parents[2]
GTFS_DIR    = ROOT / "data" / "gtfs"
GTFS_ZIP    = GTFS_DIR / "gtfs_complete.zip"
ATLAS_DIR   = ROOT / "data" / "atlas"
ATLAS_CSV   = ATLAS_DIR / "actual-date-world-traffic-point.csv"

UA = {"User-Agent": "newmap-pipeline/1.0"}


def _fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def discover_latest_gtfs_url() -> str:
    """Scrape the OTD timetable dataset page for the most recent gtfs_fp*.zip."""
    print(f"Discovering latest GTFS resource from {GTFS_DATASET_URL}")
    html = _fetch_html(GTFS_DATASET_URL)
    dates = re.findall(
        r'https://data\.opentransportdata\.swiss/dataset/[0-9a-f-]+/resource/[0-9a-f-]+/download/gtfs_fp\d{4}_(\d{8})\.zip',
        html,
    )
    urls = re.findall(
        r'https://data\.opentransportdata\.swiss/dataset/[0-9a-f-]+/resource/[0-9a-f-]+/download/gtfs_fp\d{4}_\d{8}\.zip',
        html,
    )
    if not dates:
        raise RuntimeError(
            f"No gtfs_fp*.zip resource found on {GTFS_DATASET_URL}. "
            "The page layout may have changed."
        )
    latest_idx = max(range(len(dates)), key=lambda i: dates[i])
    latest = urls[latest_idx]
    print(f"  latest: {latest.rsplit('/', 1)[1]}")
    return latest


def discover_atlas_url() -> str:
    """Scrape the atlas traffic-point dataset page for the actual-date CSV."""
    print(f"Discovering atlas resource from {ATLAS_DATASET_URL}")
    html = _fetch_html(ATLAS_DATASET_URL)
    matches = re.findall(
        r'https://data\.opentransportdata\.swiss/dataset/[0-9a-f-]+/resource/[0-9a-f-]+/download/actual-date-world-traffic-point\.csv',
        html,
    )
    if not matches:
        raise RuntimeError(
            f"No actual-date-world-traffic-point.csv on {ATLAS_DATASET_URL}. "
            "The page layout may have changed."
        )
    url = matches[0]
    print(f"  resource: {url.rsplit('/', 1)[1]}")
    return url


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url}")
    print(f"  → {dest}")
    req = urllib.request.Request(url, headers=UA)
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


def fetch_gtfs(force: bool) -> None:
    if GTFS_ZIP.exists() and not force:
        print(f"GTFS already downloaded: {GTFS_ZIP}  (pass --force-gtfs or --force to re-download)")
    else:
        url = discover_latest_gtfs_url()
        download(url, GTFS_ZIP)
    extract(GTFS_ZIP, GTFS_DIR)


def fetch_atlas(force: bool) -> None:
    if ATLAS_CSV.exists() and not force:
        print(f"Atlas already downloaded: {ATLAS_CSV}  (pass --force-atlas or --force to re-download)")
        return
    url = discover_atlas_url()
    download(url, ATLAS_CSV)


if __name__ == "__main__":
    args = set(sys.argv[1:])
    force_all   = "--force"        in args
    force_gtfs  = "--force-gtfs"   in args or force_all
    force_atlas = "--force-atlas"  in args or force_all
    fetch_gtfs(force_gtfs)
    fetch_atlas(force_atlas)
