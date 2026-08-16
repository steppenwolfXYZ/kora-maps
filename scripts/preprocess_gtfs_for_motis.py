#!/usr/bin/env python3
"""Snap GTFS stop coords to OSM platforms — MOTIS-only sidecar.

Reads `data/gtfs_routed/` (the pfaedle output that the map pipeline also
consumes) and writes `data/gtfs_motis/` with a modified `stops.txt` in
which every stop whose `platform_code` matches an OSM
`public_transport=platform` way's `local_ref` at the same station is
snapped to that way's centroid. Every other file (trips.txt, shapes.txt,
calendar.txt, frequencies.txt, …) is hardlinked from `data/gtfs_routed/`
— no duplication, no re-pfaedle, only the modified `stops.txt` uses new
disk.

Why: MOTIS stores each GTFS stop at its coord and the OSR walk router
snaps that coord onto the nearest walkable OSM node. GTFS parent centroids
frequently land on the road / tram track (canonical case: Bern Eigerplatz
platform :C's GTFS coord sits ~2 m from the tram track). MOTIS then
computes the last-mile walk into that stop through short OSR edges tagged
`sidewalk=separate` on the primary road — each such edge costs +45 s in
the foot profile, stacking hundreds of seconds of penalty onto a
15 m real-world walk. Moving the stop onto its OSM platform (where a
passenger physically waits) puts MOTIS's snap target on a
`public_transport=platform` way, which the OSR foot profile whitelists
directly — the road-side last-mile disappears.

Scoped to the MOTIS-only sidecar directory so map rendering (which reads
`data/gtfs_routed/`) is untouched — the pipeline's stop-dot / pill-arrow
placement is unaffected. Only MOTIS sees the shift.

Match rule: exact (uic_ref, local_ref) equality between the GTFS stop's
`(parent-UIC, platform_code)` and an OSM platform way's tags. Nearest-in-
space isn't good enough — at Eigerplatz platform :C sits 3 m farther from
the GTFS centroid than :D, so nearest-wins snaps to the wrong direction.
Stops whose OSM match doesn't exist (no `platform_code`, unmatched code,
or no platform mapped in OSM) keep their raw GTFS coord — bounded
fallback that never makes routing worse.

Idempotent: re-running rebuilds `data/gtfs_motis/` from scratch. Cheap
(hardlinks + one small file). Run after any transit-pipeline rebuild that
updates `data/gtfs_routed/`, and before `docker compose up` for MOTIS.
"""

import csv
import json
import os
import sys
from collections import defaultdict
from math import cos, radians
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GTFS_IN = ROOT / "data" / "gtfs_routed"
GTFS_OUT = ROOT / "data" / "gtfs_motis"
PLATFORMS = ROOT / "data" / "osm" / "platform_ways.geojson"

# Sanity check on the exact-match snap: platforms whose centroid is farther
# than this from the GTFS parent centroid are treated as coincidental
# `local_ref` collisions (unrelated station with the same platform label)
# and skipped. 250 m accommodates long train platforms.
SANITY_RADIUS_M = 250.0


def _load_platforms():
    """Load OSM platform ways, indexed by (uic_ref, local_ref) with a
    spatial grid for radius-limited lookup. Returns (index, grid, cell_x,
    cell_y). index maps (uic, ref) → [(centroid_lon, centroid_lat), ...];
    grid maps cell → [(lon, lat, uic, ref), ...] for the fallback path if
    both uic_ref and local_ref happen to be missing on the OSM side."""
    if not PLATFORMS.exists():
        sys.exit(f"missing {PLATFORMS} — run 03_bbox_osm.py first")
    data = json.loads(PLATFORMS.read_text())
    index: dict = defaultdict(list)
    for feat in data.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        props = feat.get("properties") or {}
        uic = (props.get("uic_ref") or "").strip()
        ref = (props.get("local_ref") or "").strip()
        if not uic or not ref:
            continue
        n = len(coords)
        cx = sum(c[0] for c in coords) / n
        cy = sum(c[1] for c in coords) / n
        index[(uic, ref)].append((cx, cy))
    return index


def _snap_stop(index, uic: str, ref: str, anchor_lon: float, anchor_lat: float):
    """Return (lon, lat) of the OSM platform matching (uic, ref) within
    SANITY_RADIUS_M of the anchor, or None if no match. When multiple OSM
    ways share the same (uic, ref) — rare — pick the nearest."""
    candidates = index.get((uic, ref))
    if not candidates:
        return None
    cos_lat = cos(radians(anchor_lat))
    best_d_sq = (SANITY_RADIUS_M ** 2)
    best_pt = None
    for plon, plat in candidates:
        dx = (anchor_lon - plon) * 111320.0 * cos_lat
        dy = (anchor_lat - plat) * 111320.0
        d_sq = dx * dx + dy * dy
        if d_sq < best_d_sq:
            best_d_sq = d_sq
            best_pt = (plon, plat)
    return best_pt


def _parent_uic(parent_station_id: str) -> str:
    """Extract the bare UIC digits from a GTFS parent_station id like
    "Parent8571393" → "8571393"."""
    digits = "".join(c for c in parent_station_id if c.isdigit())
    return digits


def main() -> None:
    if not GTFS_IN.exists():
        sys.exit(f"missing {GTFS_IN} — run pipeline steps 1–5 first")
    index = _load_platforms()

    # Read stops.txt into memory, indexed by stop_id for parent lookup.
    stops_path = GTFS_IN / "stops.txt"
    if not stops_path.exists():
        sys.exit(f"missing {stops_path}")
    with open(stops_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    parent_coord: dict = {}
    for row in rows:
        if row.get("location_type") == "1":
            try:
                parent_coord[row["stop_id"]] = (
                    float(row["stop_lon"]),
                    float(row["stop_lat"]),
                )
            except (KeyError, TypeError, ValueError):
                continue

    n_snapped = 0
    n_no_code = 0
    n_no_match = 0
    max_shift_m = 0.0
    for row in rows:
        code = (row.get("platform_code") or "").strip()
        parent_id = (row.get("parent_station") or "").strip()
        if not code or not parent_id:
            n_no_code += 1
            continue
        uic = _parent_uic(parent_id)
        if not uic:
            n_no_code += 1
            continue
        # Anchor snap search from the parent centroid when available (child
        # coord may already be off, e.g. on the road); fall back to child.
        anchor = parent_coord.get(parent_id)
        if anchor is None:
            try:
                anchor = (float(row["stop_lon"]), float(row["stop_lat"]))
            except (KeyError, TypeError, ValueError):
                continue
        snap = _snap_stop(index, uic, code, anchor[0], anchor[1])
        if snap is None:
            n_no_match += 1
            continue
        # Measure shift for the log line before overwriting.
        try:
            old_lon = float(row["stop_lon"])
            old_lat = float(row["stop_lat"])
            cos_lat = cos(radians(old_lat))
            dx = (old_lon - snap[0]) * 111320.0 * cos_lat
            dy = (old_lat - snap[1]) * 111320.0
            shift = (dx * dx + dy * dy) ** 0.5
            if shift > max_shift_m:
                max_shift_m = shift
        except (KeyError, TypeError, ValueError):
            pass
        row["stop_lon"] = f"{snap[0]:.6f}"
        row["stop_lat"] = f"{snap[1]:.6f}"
        n_snapped += 1

    # Rebuild the output dir from scratch — cheap because everything except
    # stops.txt is hardlinked. Nuke-and-rebuild avoids stale files if
    # data/gtfs_routed/ dropped one between runs.
    if GTFS_OUT.exists():
        for f in GTFS_OUT.iterdir():
            if f.is_file() or f.is_symlink():
                f.unlink()
    GTFS_OUT.mkdir(parents=True, exist_ok=True)

    # Write modified stops.txt atomically.
    tmp = GTFS_OUT / "stops.txt.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fout:
        writer = csv.DictWriter(fout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(GTFS_OUT / "stops.txt")

    # Hardlink everything else. Symlinks would work on native Linux but
    # Docker Desktop on macOS may not follow them through the bind mount.
    n_hardlinked = 0
    for src in GTFS_IN.iterdir():
        if not src.is_file() or src.name == "stops.txt":
            continue
        os.link(src, GTFS_OUT / src.name)
        n_hardlinked += 1

    print(
        f"stops.txt: {n_snapped} snapped to OSM platforms, "
        f"{n_no_match} skipped (platform_code set but no OSM match), "
        f"{n_no_code} skipped (no platform_code / not a child stop). "
        f"Max shift {max_shift_m:.0f} m."
    )
    print(f"Hardlinked {n_hardlinked} other GTFS files. → {GTFS_OUT}")


if __name__ == "__main__":
    main()
