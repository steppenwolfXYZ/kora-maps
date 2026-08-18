#!/usr/bin/env python3
"""Precompute the Valhalla stop-to-stop walking matrix consumed by MOTIS.

Reads `data/gtfs_motis/stops.txt` (the platform-snapped stops the MOTIS
importer uses), queries the local Valhalla instance for stop pairs
within a max radius, and writes `motis/data/valhalla_footpath_matrix.csv`
— the file the MOTIS fork loads at import time in place of its own OSR-
based transfer table (see valhalla-pedestrian-router.md).

Output format:

    from_stop_id,to_stop_id,duration_sec

One row per ordered pair whose walking duration is under
`MAX_FOOTPATH_SEC`. Same threshold as MOTIS's own
`timetable.max_footpath_length` (currently 120 min = 7200 s) so RAPTOR
sees the same set of transfer opportunities, only with corrected times.

Prerequisite:
  * `valhalla` docker service running (see valhalla/docker-compose.yml).
  * `data/gtfs_motis/` up to date (`scripts/preprocess_gtfs_for_motis.py`).

Idempotent: the run is resumable if killed — completed source stops are
recorded in a checkpoint file and skipped on restart. Delete the CSV +
checkpoint to force a full rebuild.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STOPS_TXT = ROOT / "data" / "gtfs_motis" / "stops.txt"
OUT_CSV = ROOT / "motis" / "data" / "valhalla_footpath_matrix.csv"
CHECKPOINT = ROOT / "motis" / "data" / "valhalla_footpath_matrix.checkpoint"
UNROUTABLE_CSV = ROOT / "motis" / "data" / "valhalla_unroutable_stops.csv"

# Straight-line search radius for candidate targets. Any pair further
# apart than this in metres is not queried at all. Must exceed the
# walking distance covered in MAX_FOOTPATH_SEC at the chosen speed; a
# 5.1 km/h walker covers 10.2 km in 120 min, and real OSM routes never
# beat the straight line, so 11 km leaves safe headroom for detours.
RADIUS_M = 11_000.0

# Walking-time cap for what makes it into the matrix. Same value as
# MOTIS's config `timetable.max_footpath_length` (currently 120 min);
# the concept requires the matrix to at least match MOTIS's transfer
# reach.
MAX_FOOTPATH_SEC = 7200

# Base walking speed handed to Valhalla. 5.1 km/h is Valhalla's own
# default and matches Naismith's flat rate ("normal-brisk" pedestrian).
WALK_SPEED_KMH = 5.1

# Valhalla matrix batch: sources × targets per request. Valhalla's
# default `max_matrix_locations` is 2500 (product of sources × targets);
# a 50×50 = 2500-cell batch sits at that ceiling. Raising it would need
# a config change + container restart; the default is fine for a
# ~1-2 h one-off build.
BATCH_SOURCES = 50
BATCH_TARGETS = 50

# Concurrent Valhalla requests. Each Python worker owns one in-flight
# matrix call; Valhalla's own `server_threads` (see valhalla/docker-
# compose.yml) sets how many it can execute in parallel. Match them or
# set Python slightly higher so Valhalla's queue never idles. Overriding
# via `MATRIX_WORKERS=16 python3 …` avoids editing the file when tuning.
MATRIX_WORKERS = int(os.environ.get("MATRIX_WORKERS", "8"))

VALHALLA_URL = os.environ.get("VALHALLA_URL", "http://localhost:8002")

COSTING_JSON = {
    "costing": "pedestrian",
    "costing_options": {
        "pedestrian": {
            "walking_speed": WALK_SPEED_KMH,
            "use_hills": 1.0,
            "use_lit": 0.0,
        },
    },
}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _load_stops() -> list[tuple[str, float, float, str]]:
    """Return [(stop_id, lat, lon, name), ...] over platform-level stops
    (location_type == 0 or empty). Parents (location_type == 1) are
    skipped — MOTIS's transfer table is stop-level. `name` is kept only
    for the unroutable-stops diagnostic file (never sent to Valhalla)."""
    if not STOPS_TXT.exists():
        sys.exit(f"missing {STOPS_TXT} — run scripts/preprocess_gtfs_for_motis.py")
    stops: list[tuple[str, float, float, str]] = []
    with open(STOPS_TXT, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            lt = (row.get("location_type") or "").strip()
            if lt not in ("", "0"):
                continue
            try:
                lat = float(row["stop_lat"])
                lon = float(row["stop_lon"])
            except (KeyError, TypeError, ValueError):
                continue
            name = (row.get("stop_name") or "").strip()
            stops.append((row["stop_id"], lat, lon, name))
    return stops


def _candidates(
    src_lat: float,
    src_lon: float,
    all_stops: list[tuple[str, float, float, str]],
    self_idx: int,
) -> list[int]:
    """Return indices of stops within RADIUS_M of the source. Self is
    excluded — a stop's transfer to itself is not a footpath."""
    out: list[int] = []
    for i, (_sid, lat, lon, _name) in enumerate(all_stops):
        if i == self_idx:
            continue
        if _haversine_m(src_lat, src_lon, lat, lon) <= RADIUS_M:
            out.append(i)
    return out


def _matrix_call(
    sources: list[tuple[float, float]],
    targets: list[tuple[float, float]],
) -> list[list[dict | None]]:
    """POST to Valhalla /sources_to_targets. Returns the raw
    `sources_to_targets` matrix (rows = sources, cols = targets)."""
    body = {
        **COSTING_JSON,
        "sources": [{"lat": lat, "lon": lon} for lat, lon in sources],
        "targets": [{"lat": lat, "lon": lon} for lat, lon in targets],
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{VALHALLA_URL}/sources_to_targets",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read())
                return payload.get("sources_to_targets", [])
        except urllib.error.HTTPError as e:
            # Surface Valhalla's own error body — the default HTTPError
            # str() only shows the status line, hiding the actual reason
            # (e.g. "Exceeded max locations: 2500"). Fatal on the first
            # attempt: HTTP 4xx will not fix itself on retry.
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Valhalla {e.code}: {body}") from e
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    return []


def _prescan_routable(
    stops: list[tuple[str, float, float, str]],
    batch: int = 100,
) -> set[int]:
    """Return the set of stop indices Valhalla's pedestrian graph can snap
    to. Cross-border stops falling outside the OSM extract, or stops on
    islands with no walkable connectivity, would otherwise poison every
    matrix batch they appear in with error 171 ("no suitable edges near
    location"). One /locate call per batch of `batch` stops covers them
    all in ~1-2 min."""
    routable: set[int] = set()
    n = len(stops)
    t0 = time.monotonic()
    for start in range(0, n, batch):
        chunk = stops[start : start + batch]
        body = {
            "costing": "pedestrian",
            "locations": [{"lat": lat, "lon": lon} for _sid, lat, lon, _name in chunk],
            "verbose": False,
        }
        req = urllib.request.Request(
            f"{VALHALLA_URL}/locate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # Whole-batch failure — split in half and recurse. Base case
            # is a single stop, which we then mark as unroutable.
            if len(chunk) == 1:
                continue
            mid = len(chunk) // 2
            left = _prescan_routable(chunk[:mid], batch=batch)
            right = _prescan_routable(chunk[mid:], batch=batch)
            for i in left:
                routable.add(start + i)
            for i in right:
                routable.add(start + mid + i)
            continue
        for offset, entry in enumerate(payload):
            edges = entry.get("edges") or []
            nodes = entry.get("nodes") or []
            if edges or nodes:
                routable.add(start + offset)
        if start % (batch * 20) == 0:
            dt = time.monotonic() - t0
            print(f"  prescan {start:>6,}/{n:,}  routable so far {len(routable):,}  {dt:.0f}s", flush=True)
    dt = time.monotonic() - t0
    print(f"Prescan done: {len(routable):,}/{n:,} stops routable ({dt:.0f}s).")
    return routable


def _load_checkpoint() -> set[str]:
    if not CHECKPOINT.exists():
        return set()
    return set(CHECKPOINT.read_text().splitlines())


def _append_checkpoint(stop_id: str) -> None:
    with open(CHECKPOINT, "a", encoding="utf-8") as f:
        f.write(stop_id + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--restart",
        action="store_true",
        help="Delete the CSV + checkpoint and rebuild from scratch.",
    )
    ap.add_argument(
        "--prescan-only",
        action="store_true",
        help="Run the Valhalla /locate pre-scan and write the unroutable-"
             "stops diagnostic CSV, then exit. Useful for auditing which "
             "stops the OSM extract does not cover.",
    )
    args = ap.parse_args()

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    if args.restart:
        OUT_CSV.unlink(missing_ok=True)
        CHECKPOINT.unlink(missing_ok=True)

    stops = _load_stops()
    print(f"Loaded {len(stops):,} platform-level stops from {STOPS_TXT.name}.")

    # Filter out stops Valhalla cannot snap to a walkable edge. One bad
    # coord (cross-border stop outside the OSM extract, unreachable
    # island, GTFS error) would otherwise error every batch it lands in.
    routable_indices = _prescan_routable(stops)
    unroutable_stops = [s for i, s in enumerate(stops) if i not in routable_indices]
    if unroutable_stops:
        print(f"Dropping {len(unroutable_stops):,} unroutable stop(s) — no walkable OSM edge within Valhalla's search radius.")
        # Write a diagnostic CSV so the unroutable list can be inspected
        # (usually dominated by cross-border stops the OSM extract does
        # not cover — Basel Badischer, Konstanz, Domodossola, etc.).
        UNROUTABLE_CSV.parent.mkdir(parents=True, exist_ok=True)
        with open(UNROUTABLE_CSV, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["stop_id", "stop_name", "lat", "lon"])
            for sid, lat, lon, name in unroutable_stops:
                w.writerow([sid, name, f"{lat:.6f}", f"{lon:.6f}"])
        print(f"Diagnostic list written to {UNROUTABLE_CSV}.")
    stops = [s for i, s in enumerate(stops) if i in routable_indices]

    if args.prescan_only:
        print("prescan-only mode — skipping matrix build.")
        return

    done = _load_checkpoint()
    if done:
        print(f"Resuming: {len(done):,} source stops already complete.")

    write_header = not OUT_CSV.exists()
    out_f = open(OUT_CSV, "a", encoding="utf-8", newline="")
    writer = csv.writer(out_f)
    if write_header:
        writer.writerow(["from_stop_id", "to_stop_id", "duration_sec"])

    n_pairs = 0
    n_sources_done = len(done)
    t0 = time.monotonic()

    print(f"Using {MATRIX_WORKERS} concurrent Valhalla workers.")
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=MATRIX_WORKERS)

    try:
        for s_start in range(0, len(stops), BATCH_SOURCES):
            batch = stops[s_start : s_start + BATCH_SOURCES]
            batch_pending = [(i, sid) for i, (sid, _, _, _) in enumerate(batch, start=s_start) if sid not in done]
            if not batch_pending:
                continue

            # Union of candidates across the batch's sources — one
            # matrix call per target chunk covers all their neighbours.
            cand_set: set[int] = set()
            for global_idx, _sid in batch_pending:
                _sid2, slat, slon, _name = stops[global_idx]
                for j in _candidates(slat, slon, stops, global_idx):
                    cand_set.add(j)
            candidates = sorted(cand_set)

            src_coords = [(stops[i][1], stops[i][2]) for i, _ in batch_pending]

            # Fire all target chunks concurrently — Valhalla's server
            # threads process them in parallel, chunk order preserved
            # via list index so we know which tgt_indices each result
            # maps to.
            chunk_specs = []
            for t_start in range(0, len(candidates), BATCH_TARGETS):
                tgt_indices = candidates[t_start : t_start + BATCH_TARGETS]
                tgt_coords = [(stops[i][1], stops[i][2]) for i in tgt_indices]
                chunk_specs.append((tgt_indices, tgt_coords))

            futures = [
                pool.submit(_matrix_call, src_coords, tgt_coords)
                for _tgt_indices, tgt_coords in chunk_specs
            ]

            for (tgt_indices, _tgt_coords), fut in zip(chunk_specs, futures):
                matrix = fut.result()
                for row_idx, (global_idx, from_sid) in enumerate(batch_pending):
                    if row_idx >= len(matrix):
                        continue
                    row = matrix[row_idx]
                    for col_idx, cell in enumerate(row):
                        if cell is None:
                            continue
                        secs = cell.get("time")
                        if secs is None:
                            continue
                        if secs > MAX_FOOTPATH_SEC:
                            continue
                        tgt_global = tgt_indices[col_idx]
                        if tgt_global == global_idx:
                            continue
                        to_sid = stops[tgt_global][0]
                        writer.writerow([from_sid, to_sid, int(round(secs))])
                        n_pairs += 1

            out_f.flush()
            for _global_idx, sid in batch_pending:
                _append_checkpoint(sid)
                n_sources_done += 1

            dt = time.monotonic() - t0
            rate = n_sources_done / dt if dt > 0 else 0
            eta = (len(stops) - n_sources_done) / rate if rate > 0 else float("inf")
            print(
                f"  sources {n_sources_done:>6,}/{len(stops):,}  "
                f"pairs {n_pairs:>9,}  "
                f"{rate:5.1f} stops/s  "
                f"eta {eta / 60:5.1f} min",
                flush=True,
            )
    finally:
        pool.shutdown(wait=True)
        out_f.close()

    print(f"Wrote {n_pairs:,} footpath pairs to {OUT_CSV}.")


if __name__ == "__main__":
    main()
