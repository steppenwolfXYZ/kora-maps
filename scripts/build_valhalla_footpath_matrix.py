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
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STOPS_TXT = ROOT / "data" / "gtfs_motis" / "stops.txt"
OUT_CSV = ROOT / "motis" / "data" / "valhalla_footpath_matrix.csv"
CHECKPOINT = ROOT / "motis" / "data" / "valhalla_footpath_matrix.checkpoint"
UNROUTABLE_CSV = ROOT / "motis" / "data" / "valhalla_unroutable_stops.csv"
FAILED_PAIRS_CSV = ROOT / "motis" / "data" / "valhalla_failed_pairs.csv"

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

# Smallest block bisection will try to rescue when Valhalla refuses a
# request. Below this it skips the block instead of splitting further —
# see _matrix_call_resilient for why isolating single pairs is not worth
# the sequential round trips. Trades stall time against discarded pairs:
# on a 50x50 chunk with scattered failures, 1 costs ~999 calls and loses
# 3% of cells, 16 costs ~463 and loses 31%, 64 costs ~191 and loses the
# lot (every block ends up holding a bad pair).
BISECT_FLOOR_PAIRS = 16

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


def _hilbert_d(x: int, y: int, order: int) -> int:
    """Distance along a Hilbert curve of 2**order cells per axis.

    Standard xy2d walk: at each halving of the square, work out which
    quadrant (rx, ry) the point sits in, add that quadrant's share of
    the curve, then rotate the frame so the next level is expressed in
    the sub-square's own orientation.
    """
    d = 0
    s = 1 << (order - 1)
    while s > 0:
        rx = 1 if (x & s) > 0 else 0
        ry = 1 if (y & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        s >>= 1
    return d


def _sort_spatially(
    stops: list[tuple[str, float, float, str]],
) -> list[tuple[str, float, float, str]]:
    """Order stops along a Hilbert curve so that consecutive stops are
    geographic neighbours.

    `stops.txt` arrives in feed order, which is effectively random in
    space — a 50-stop window out of it can span the whole feed area.
    That matters because a source batch sends Valhalla the union of its
    sources' candidate targets, and Valhalla validates *every*
    source-target pair in the request against `max_matrix_distance`
    (200 km): one wide batch aborts the run with error 154. The wasted
    work is just as bad — pairs hundreds of km apart get routed only to
    be discarded by the MAX_FOOTPATH_SEC filter afterwards.

    A Hilbert ordering keeps both axes local (unlike sorting by lat then
    lon, which leaves batches stretched along a whole latitude band), so
    a batch's candidate union collapses to the surrounding
    neighbourhood.
    """
    if not stops:
        return stops
    lats = [lat for _sid, lat, _lon, _name in stops]
    lons = [lon for _sid, _lat, lon, _name in stops]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    span_lat = (max_lat - min_lat) or 1.0
    span_lon = (max_lon - min_lon) or 1.0

    order = 16
    scale = (1 << order) - 1

    def key(stop: tuple[str, float, float, str]) -> int:
        _sid, lat, lon, _name = stop
        x = int((lon - min_lon) / span_lon * scale)
        y = int((lat - min_lat) / span_lat * scale)
        return _hilbert_d(x, y, order)

    return sorted(stops, key=key)


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


_failed_pairs: list[tuple[str, str, str]] = []
_failed_lock = threading.Lock()
_bisect_notified = False


def _record_failed(rows: list[tuple[str, str, str]]) -> None:
    """Append skipped pairs to FAILED_PAIRS_CSV immediately.

    Written as they happen, not at the end: the checkpoint marks a
    source complete as soon as its batch lands, so a run killed later
    would otherwise leave those pairs both absent from the matrix and
    unrecorded — invisible data loss that `--repair` could never find.
    """
    if not rows:
        return
    with _failed_lock:
        _failed_pairs.extend(rows)
        new = not FAILED_PAIRS_CSV.exists()
        FAILED_PAIRS_CSV.parent.mkdir(parents=True, exist_ok=True)
        with open(FAILED_PAIRS_CSV, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["from_stop_id", "to_stop_id", "error"])
            w.writerows(rows)


def _shape(
    matrix: list[list[dict | None]],
    n_rows: int,
    n_cols: int,
) -> list[list[dict | None]]:
    """Force `matrix` to exactly n_rows x n_cols, padding with None.

    Bisection concatenates sub-matrices, so a short row or a missing row
    from Valhalla would silently shift every later column onto the wrong
    target. Padding keeps the index-to-stop mapping honest.
    """
    out: list[list[dict | None]] = []
    for r in range(n_rows):
        row = list(matrix[r]) if r < len(matrix) else []
        row = row[:n_cols] + [None] * max(0, n_cols - len(row))
        out.append(row)
    return out


def _matrix_call_resilient(
    sources: list[tuple[float, float]],
    targets: list[tuple[float, float]],
    src_labels: list[str],
    tgt_labels: list[str],
) -> list[list[dict | None]]:
    """`_matrix_call` that isolates and skips pairs Valhalla refuses.

    Valhalla occasionally fails a whole matrix request over a single bad
    pair — error 499 ("Could not find candidate edge used for label") is
    an internal thor failure, not a limit being exceeded, and it kills a
    request that is 99.99% computable. Treating that as fatal throws away
    the entire run.

    On failure the request is halved along its longer axis and each half
    retried, recursively, until either a half succeeds or the block is
    down to BISECT_FLOOR_PAIRS. A still-failing block at the floor is
    retried once (some Valhalla errors are races in its threaded matrix
    code) and then skipped wholesale, every pair in it recorded in
    FAILED_PAIRS_CSV, so the run continues.

    The floor is what keeps this bounded. Drilling to single pairs costs
    ~2N calls for an N-pair chunk — a fully poisoned 50x50 chunk is
    ~7,500 sequential calls, and since the recursion runs inside one
    pool worker (the other workers idle), that stalls a batch for the
    better part of an hour. The floor caps the same chunk at ~770 calls.
    The price is discarding up to BISECT_FLOOR_PAIRS computable pairs
    alongside the bad ones; they are logged, and this only ever applies
    to blocks Valhalla has already refused twice.

    Parallelising the two halves would remove the stall outright and
    allow a much lower floor, but recursive submission into the batch's
    own thread pool deadlocks, so it needs a separate executor. Not done.

    Bisection also rescues requests that trip a service limit, since the
    halves are geographically narrower than the whole.
    """
    global _bisect_notified
    n_pairs = len(sources) * len(targets)
    try:
        return _shape(_matrix_call(sources, targets), len(sources), len(targets))
    except RuntimeError as exc:
        # Bisection is sequential inside one pool worker, so it stalls
        # the batch's progress line. Say so once, or it reads as a hang.
        if not _bisect_notified:
            with _failed_lock:
                if not _bisect_notified:
                    _bisect_notified = True
                    print(
                        f"  note: Valhalla refused a matrix request "
                        f"({exc}) — bisecting to isolate; progress pauses "
                        f"until the batch completes.",
                        flush=True,
                    )
        if n_pairs <= BISECT_FLOOR_PAIRS:
            try:
                return _shape(
                    _matrix_call(sources, targets), len(sources), len(targets)
                )
            except RuntimeError as exc2:
                _record_failed(
                    [
                        (s_label, t_label, str(exc2))
                        for s_label in src_labels
                        for t_label in tgt_labels
                    ]
                )
                return [[None] * len(targets) for _ in sources]

        if len(sources) >= len(targets):
            mid = len(sources) // 2
            top = _matrix_call_resilient(
                sources[:mid], targets, src_labels[:mid], tgt_labels
            )
            bottom = _matrix_call_resilient(
                sources[mid:], targets, src_labels[mid:], tgt_labels
            )
            return top + bottom

        mid = len(targets) // 2
        left = _matrix_call_resilient(
            sources, targets[:mid], src_labels, tgt_labels[:mid]
        )
        right = _matrix_call_resilient(
            sources, targets[mid:], src_labels, tgt_labels[mid:]
        )
        return [lrow + rrow for lrow, rrow in zip(left, right)]


def _repair(stops: list[tuple[str, float, float, str]]) -> None:
    """Recover pairs the main run skipped, and rewrite the skip list.

    The main run trades data for time: BISECT_FLOOR_PAIRS stops the
    bisection early, so a block that fails gets discarded whole, taking
    computable pairs down with the bad ones. This pass re-runs exactly
    those pairs with the floor at 1, so only pairs Valhalla genuinely
    refuses stay out of the matrix.

    Cheap because it only revisits recorded failures, and it parallelises
    across sources rather than bisecting one chunk in a single thread —
    the reason the floor had to exist during the main run.
    """
    global BISECT_FLOOR_PAIRS

    if not FAILED_PAIRS_CSV.exists():
        print("No failed-pairs file — nothing to repair.")
        return

    with open(FAILED_PAIRS_CSV, encoding="utf-8", newline="") as f:
        skipped = [(r["from_stop_id"], r["to_stop_id"]) for r in csv.DictReader(f)]
    if not skipped:
        print("Failed-pairs file is empty — nothing to repair.")
        return

    coords = {sid: (lat, lon) for sid, lat, lon, _name in stops}
    by_source: dict[str, list[str]] = {}
    for from_sid, to_sid in skipped:
        if from_sid in coords and to_sid in coords:
            by_source.setdefault(from_sid, []).append(to_sid)

    print(
        f"Repairing {len(skipped):,} skipped pair(s) across "
        f"{len(by_source):,} source stop(s)."
    )

    # Full isolation this time — cost is bounded by the skip list, not
    # by the whole matrix.
    BISECT_FLOOR_PAIRS = 1
    _failed_pairs.clear()

    recovered = 0
    still_bad: list[tuple[str, str, str]] = []
    lock = threading.Lock()

    def repair_source(from_sid: str, to_sids: list[str]) -> list[list[str | int]]:
        rows: list[list[str | int]] = []
        src = [coords[from_sid]]
        for start in range(0, len(to_sids), BATCH_TARGETS):
            chunk = to_sids[start : start + BATCH_TARGETS]
            matrix = _matrix_call_resilient(
                src, [coords[t] for t in chunk], [from_sid], chunk
            )
            for col_idx, cell in enumerate(matrix[0]):
                if cell is None:
                    continue
                secs = cell.get("time")
                if secs is None or secs > MAX_FOOTPATH_SEC:
                    continue
                if chunk[col_idx] == from_sid:
                    continue
                rows.append([from_sid, chunk[col_idx], int(round(secs))])
        return rows

    with open(OUT_CSV, "a", encoding="utf-8", newline="") as out_f:
        writer = csv.writer(out_f)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=MATRIX_WORKERS)
        try:
            futures = {
                pool.submit(repair_source, sid, tgts): sid
                for sid, tgts in by_source.items()
            }
            done_n = 0
            for fut in concurrent.futures.as_completed(futures):
                rows = fut.result()
                with lock:
                    writer.writerows(rows)
                    recovered += len(rows)
                    done_n += 1
                    if done_n % 100 == 0:
                        out_f.flush()
                        print(
                            f"  repaired {done_n:,}/{len(futures):,} sources  "
                            f"recovered {recovered:,} pairs",
                            flush=True,
                        )
        finally:
            pool.shutdown(wait=True)

    # _matrix_call_resilient appended this run's genuine failures to the
    # same file; rewrite it so it holds only those, not the wide blocks
    # the main run skipped.
    still_bad = list(_failed_pairs)
    with open(FAILED_PAIRS_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["from_stop_id", "to_stop_id", "error"])
        w.writerows(still_bad)

    print(
        f"Repair done: recovered {recovered:,} pairs; "
        f"{len(still_bad):,} pair(s) remain genuinely unroutable "
        f"(see {FAILED_PAIRS_CSV})."
    )


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
    ap.add_argument(
        "--repair",
        action="store_true",
        help="Re-run only the pairs listed in valhalla_failed_pairs.csv "
             "with full isolation, appending whatever is recoverable to "
             "the matrix. Run after a completed build to undo the "
             "block-level skipping BISECT_FLOOR_PAIRS causes.",
    )
    args = ap.parse_args()

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    if args.restart:
        OUT_CSV.unlink(missing_ok=True)
        CHECKPOINT.unlink(missing_ok=True)

    stops = _load_stops()
    print(f"Loaded {len(stops):,} platform-level stops from {STOPS_TXT.name}.")

    # Batching is positional, so the input order decides how wide each
    # source batch reaches. See _sort_spatially for why feed order is
    # unusable here.
    stops = _sort_spatially(stops)

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

    if args.repair:
        _repair(stops)
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
    # Rate is measured over this run only. Counting resumed sources
    # against this run's clock reports a nonsense rate on startup
    # (15,800 sources "done" in 3 seconds) and an ETA to match.
    n_sources_this_run = 0
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
            src_labels = [sid for _i, sid in batch_pending]

            chunk_specs = []
            for t_start in range(0, len(candidates), BATCH_TARGETS):
                tgt_indices = candidates[t_start : t_start + BATCH_TARGETS]
                tgt_coords = [(stops[i][1], stops[i][2]) for i in tgt_indices]
                tgt_labels = [stops[i][0] for i in tgt_indices]
                chunk_specs.append((tgt_indices, tgt_coords, tgt_labels))

            futures = [
                pool.submit(
                    _matrix_call_resilient,
                    src_coords,
                    tgt_coords,
                    src_labels,
                    tgt_labels,
                )
                for _tgt_indices, tgt_coords, tgt_labels in chunk_specs
            ]

            for (tgt_indices, _tgt_coords, _tgt_labels), fut in zip(chunk_specs, futures):
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
                n_sources_this_run += 1

            dt = time.monotonic() - t0
            rate = n_sources_this_run / dt if dt > 0 else 0
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

    # Skipped pairs were already appended to FAILED_PAIRS_CSV as they
    # happened; this is just the closing summary. Run --repair to try to
    # win them back before shipping the matrix.
    if _failed_pairs:
        print(
            f"{len(_failed_pairs):,} pair(s) skipped — see "
            f"{FAILED_PAIRS_CSV}. Run with --repair to recover the "
            f"computable ones."
        )


if __name__ == "__main__":
    main()
