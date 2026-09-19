#!/usr/bin/env python3
"""
Download OSM PBFs from Geofabrik.

Switzerland + Liechtenstein + every neighbouring country. The Swiss bbox in
config.yaml extends past Swiss soil (Domodossola, Konstanz, Annemasse,
Lörrach, Bregenz, ...) and step 03 needs OSM data for those areas to give pfaedle
a complete routing graph inside the bbox. Country extracts at Geofabrik are
country-clipped, so CH+FL alone leave the foreign sliver of the bbox empty.

Outputs to data/osm/:
  switzerland-latest.osm.pbf     ~450 MB
  liechtenstein-latest.osm.pbf   ~1 MB
  germany-latest.osm.pbf         ~4.5 GB
  france-latest.osm.pbf          ~4.5 GB
  italy-latest.osm.pbf           ~2.5 GB
  austria-latest.osm.pbf         ~0.8 GB

Geofabrik updates these daily. Total one-off download ≈ 12 GB; step 03 cuts them
to ~400 MB before pfaedle ever sees the data.

Integrity gate: every download is verified against Geofabrik's published
`<file>.md5` (fetched right before the transfer) and against the server's
announced length. A file that fails either check is deleted and the step
exits non-zero — a short file is otherwise indistinguishable from a good
one: on 2026-09-18 Geofabrik served a half-written Swiss extract (496 of
546 MB, all nodes, the modern half of the ways cut off, no relations) whose
transfer "completed" at 100 %, osmium read it without complaint, and pfaedle
drew every long-distance train as a straight line because the mainline
tunnels were missing from the graph. The md5 is re-fetched once on
mismatch, so a download that straddles Geofabrik's daily rotation is
retried rather than failed.
"""

import hashlib
import urllib.request
from pathlib import Path
import sys

SOURCES = [
    ("https://download.geofabrik.de/europe/switzerland-latest.osm.pbf",
     "switzerland-latest.osm.pbf"),
    ("https://download.geofabrik.de/europe/liechtenstein-latest.osm.pbf",
     "liechtenstein-latest.osm.pbf"),
    ("https://download.geofabrik.de/europe/germany-latest.osm.pbf",
     "germany-latest.osm.pbf"),
    ("https://download.geofabrik.de/europe/france-latest.osm.pbf",
     "france-latest.osm.pbf"),
    ("https://download.geofabrik.de/europe/italy-latest.osm.pbf",
     "italy-latest.osm.pbf"),
    ("https://download.geofabrik.de/europe/austria-latest.osm.pbf",
     "austria-latest.osm.pbf"),
]

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "osm"


def published_md5(url: str) -> str | None:
    """Geofabrik's `<file>.md5` ("<hex>  <name>"). None when unavailable —
    the size check below still applies, and the miss is printed."""
    try:
        with urllib.request.urlopen(url + ".md5", timeout=60) as resp:
            text = resp.read().decode("ascii", "replace").strip()
        digest = text.split()[0].lower()
        return digest if len(digest) == 32 else None
    except Exception as e:  # noqa: BLE001 — any failure means "no reference"
        print(f"  (no published md5 for {url}: {e})")
        return None


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: Path) -> int:
    """One transfer. Returns the server-announced length (0 if unknown)."""
    announced = 0

    def progress(block_count, block_size, total_size):
        nonlocal announced
        announced = max(total_size, 0)
        if total_size > 0:
            pct = block_count * block_size / total_size * 100
            mb = block_count * block_size / 1_000_000
            total_mb = total_size / 1_000_000
            print(f"\r  {pct:.1f}%  {mb:.0f}/{total_mb:.0f} MB", end="", flush=True)

    urllib.request.urlretrieve(url, dest, reporthook=progress)
    print()
    return announced


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url}")
    print(f"  → {dest}")

    # Reference first, transfer second: if Geofabrik rotates the daily file
    # in between, the md5 will not match and one retry re-fetches both.
    expected = published_md5(url)
    for attempt in (1, 2):
        announced = fetch(url, dest)
        size = dest.stat().st_size
        problems = []
        if announced and size != announced:
            problems.append(f"size {size:,} B differs from the announced {announced:,} B")
        actual = file_md5(dest)
        if expected and actual != expected:
            problems.append(f"md5 {actual} differs from the published {expected}")
        if not problems:
            print(f"Done. {size / 1_000_000:.0f} MB saved to {dest} (md5 verified)"
                  if expected else
                  f"Done. {size / 1_000_000:.0f} MB saved to {dest} (size verified, no md5 published)")
            return
        print(f"  integrity check failed ({'; '.join(problems)})")
        dest.unlink(missing_ok=True)
        if attempt == 1:
            print("  retrying once (daily rotation may have moved the file)")
            expected = published_md5(url)
    sys.exit(f"{dest.name}: download failed the integrity gate twice — "
             "file removed, refusing to hand a short extract to step 03")


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
