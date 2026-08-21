#!/usr/bin/env python3
"""
Step 05 — Route the filtered GTFS feed through pfaedle.

Inputs:
  data/gtfs_filtered/  — written by 04
  data/osm/ch_pfaedle.osm.pbf — written by 03

Output:
  data/gtfs_routed/  — same GTFS schema plus shapes.txt and shape_dist_traveled
                       populated on stop_times.txt

Pfaedle runs in the carfree-pfaedle Docker image. The host needs only Docker.

Parallelism: pfaedle itself is single-threaded and has no thread option.
With PFAEDLE_JOBS=N (env, default 1 = the classic single run) the feed is
sharded by route into N subsets, each routed by its own container
concurrently, and the results merged back into one feed. Shape ids are
prefixed per shard so they cannot collide. Each container loads the OSM
graph on its own (several GB of RAM per job), so size N to memory, not
just to cores — the default stays 1 so laptops keep today's behaviour.
"""

import csv
import os
import shutil
import subprocess
import sys
import time
import zlib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GTFS_IN = ROOT / "data" / "gtfs_filtered"
GTFS_OUT = ROOT / "data" / "gtfs_routed"
SHARD_ROOT = ROOT / "data" / "gtfs_routed_shards"
OSM_PBF = ROOT / "data" / "osm" / "ch_pfaedle.osm.pbf"
CFG_PATH = ROOT / "scripts" / "transit" / "config.yaml"

# Files that carry per-trip rows and therefore get split across shards.
# Everything else is copied whole into every shard and taken back from
# shard 0's pfaedle output (pfaedle normalises column sets on the way).
TRIP_FILES = ("trips.txt", "stop_times.txt", "frequencies.txt")
SHAPE_FILE = "shapes.txt"


def load_cfg() -> dict:
    return yaml.safe_load(CFG_PATH.read_text())


def relpath(p: Path) -> str:
    return str(p.relative_to(ROOT))


def pfaedle_cmd(cfg: dict, image: str, feed_dir: Path) -> list:
    modes = cfg.get("modes", ["all"])
    # Pfaedle expects a single comma-separated -m value; repeated flags
    # silently overwrite (only the last mode would be routed).
    modes_str = ",".join(modes) if isinstance(modes, list) else str(modes)
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{ROOT}:/work",
        "-w", "/work",
        image,
        "pfaedle",
        "-D",                                       # drop existing shapes
        "-x", f"/work/{relpath(OSM_PBF)}",
        "-m", modes_str,
    ]
    spf = cfg.get("station_move_penalty_fac")
    if spf is not None:
        # The override must be scoped to a mode section — without one, pfaedle
        # parses the value but never applies it during routing. [bus] is the
        # section that hosts the default routing_station_move_penalty_fac in
        # pfaedle.cfg, so a plain key under [bus] replaces it.
        cmd.extend(["-P", f"[bus]\nrouting_station_move_penalty_fac: {spf}"])
    cmd.extend(["--inplace", f"/work/{relpath(feed_dir)}"])
    return cmd


# ── Single run (PFAEDLE_JOBS=1) ──────────────────────────────────────────────

def run_single(cfg: dict, image: str) -> None:
    # Stage input: pfaedle writes alongside its input by default, so we copy
    # the filtered feed into the output dir and route it in place.
    if GTFS_OUT.exists():
        shutil.rmtree(GTFS_OUT)
    shutil.copytree(GTFS_IN, GTFS_OUT)
    cmd = pfaedle_cmd(cfg, image, GTFS_OUT)
    print("Running pfaedle:")
    print("  $", " ".join(cmd))
    res = subprocess.run(cmd)
    if res.returncode != 0:
        sys.exit(f"pfaedle exited with status {res.returncode}")


# ── Sharded run (PFAEDLE_JOBS=N) ─────────────────────────────────────────────

def _shard_of(route_id: str, n: int) -> int:
    # Stable hash (Python's str hash is salted per process). Sharding by
    # route keeps a route's trips — and thus its repeated stop patterns,
    # which pfaedle routes once and reuses — inside one shard.
    return zlib.crc32(route_id.encode("utf-8")) % n


def make_shards(n: int) -> list:
    """Split the filtered feed into n shard dirs by route. Returns the
    shard dirs. Streams stop_times.txt once (1.6 GB) rather than loading
    it; trip→shard is a dict over ~2 M trip ids, which fits easily."""
    if SHARD_ROOT.exists():
        shutil.rmtree(SHARD_ROOT)
    shard_dirs = [SHARD_ROOT / f"{k}" for k in range(n)]
    for d in shard_dirs:
        d.mkdir(parents=True)

    # Non-trip files: copied whole into every shard.
    for f in GTFS_IN.iterdir():
        if f.is_file() and f.name not in TRIP_FILES:
            for d in shard_dirs:
                shutil.copy2(f, d / f.name)

    # trips.txt → shard by route, remember each trip's shard.
    trip_shard: dict = {}
    with open(GTFS_IN / "trips.txt", encoding="utf-8-sig", newline="") as fin:
        reader = csv.DictReader(fin)
        fields = reader.fieldnames or []
        writers = []
        handles = []
        for d in shard_dirs:
            fh = open(d / "trips.txt", "w", encoding="utf-8", newline="")
            w = csv.DictWriter(fh, fieldnames=fields, quoting=csv.QUOTE_ALL)
            w.writeheader()
            writers.append(w)
            handles.append(fh)
        counts = [0] * n
        for row in reader:
            k = _shard_of(row["route_id"], n)
            trip_shard[row["trip_id"]] = k
            writers[k].writerow(row)
            counts[k] += 1
        for fh in handles:
            fh.close()
    print("  trips per shard: " + ", ".join(f"{c:,}" for c in counts))

    # stop_times.txt / frequencies.txt → follow the trip. Line-based
    # streaming: the header is copied verbatim, rows routed on trip_id.
    for name in ("stop_times.txt", "frequencies.txt"):
        src = GTFS_IN / name
        if not src.exists():
            continue
        with open(src, encoding="utf-8-sig", newline="") as fin:
            header = fin.readline()
            cols = next(csv.reader([header]))
            tid_idx = cols.index("trip_id")
            outs = [open(d / name, "w", encoding="utf-8", newline="") for d in shard_dirs]
            for fh in outs:
                fh.write(header)
            for line in fin:
                # Cheap trip_id extraction: the column is unquoted in
                # this feed, but fall back to the csv parser if needed.
                parts = line.split(",", tid_idx + 1)
                tid = parts[tid_idx] if len(parts) > tid_idx else ""
                if tid.startswith('"') or tid not in trip_shard:
                    try:
                        tid = next(csv.reader([line]))[tid_idx]
                    except (StopIteration, IndexError):
                        continue
                k = trip_shard.get(tid)
                if k is None:
                    continue
                outs[k].write(line)
            for fh in outs:
                fh.close()
    return shard_dirs


def run_shards(cfg: dict, image: str, shard_dirs: list) -> None:
    """Launch one pfaedle container per shard concurrently; stream each
    one's output to a per-shard log and fail loudly if any exits
    non-zero."""
    procs = []
    logs = []
    t0 = time.monotonic()
    # A shard can end up with no trips on tiny feeds (route-hash
    # imbalance); pfaedle has nothing to do there, so don't launch it.
    shard_dirs = [d for d in shard_dirs
                  if sum(1 for _ in open(d / "trips.txt", encoding="utf-8-sig")) > 1]
    for k, d in enumerate(shard_dirs):
        log_path = SHARD_ROOT / f"pfaedle_{k}.log"
        fh = open(log_path, "w")
        cmd = pfaedle_cmd(cfg, image, d)
        if k == 0:
            print("Running pfaedle per shard:")
            print("  $", " ".join(cmd).replace(str(d), "<shard>"))
        procs.append(subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT))
        logs.append((log_path, fh))
    print(f"  {len(procs)} containers running — logs in {relpath(SHARD_ROOT)}/pfaedle_*.log")

    failed = []
    remaining = set(range(len(procs)))
    while remaining:
        time.sleep(10)
        for k in sorted(remaining):
            rc = procs[k].poll()
            if rc is None:
                continue
            remaining.discard(k)
            logs[k][1].close()
            mins = (time.monotonic() - t0) / 60
            if rc == 0:
                print(f"  shard {k} done after {mins:.0f} min")
            else:
                failed.append(k)
                print(f"  shard {k} FAILED (exit {rc}) after {mins:.0f} min")
    if failed:
        for k in failed:
            print(f"\n--- tail of shard {k} log ---")
            try:
                lines = logs[k][0].read_text().splitlines()
                print("\n".join(lines[-25:]))
            except OSError:
                pass
        sys.exit(f"pfaedle failed in shard(s) {failed}")


def merge_shards(shard_dirs: list) -> None:
    """Assemble data/gtfs_routed/ from the per-shard pfaedle outputs.

    shapes.txt: concatenated, shape_id prefixed `s{k}_` per shard (pfaedle
    numbers shapes per run, so two shards can both emit `shp_1000_1`).
    trips.txt: concatenated, shape_id rewritten with the same prefix.
    stop_times.txt / frequencies.txt: concatenated (trips stay contiguous
    — every consumer that streams stop_times relies on that).
    Everything else: shard 0's copy.
    """
    if GTFS_OUT.exists():
        shutil.rmtree(GTFS_OUT)
    GTFS_OUT.mkdir(parents=True)

    for f in shard_dirs[0].iterdir():
        if f.is_file() and f.name not in TRIP_FILES and f.name != SHAPE_FILE:
            shutil.copy2(f, GTFS_OUT / f.name)

    # shapes.txt
    n_shapes = 0
    with open(GTFS_OUT / SHAPE_FILE, "w", encoding="utf-8", newline="") as fout:
        wrote_header = False
        for k, d in enumerate(shard_dirs):
            src = d / SHAPE_FILE
            if not src.exists():
                continue
            with open(src, encoding="utf-8-sig", newline="") as fin:
                header = fin.readline()
                if not wrote_header:
                    fout.write(header)
                    wrote_header = True
                prefix = f"s{k}_"
                for line in fin:
                    fout.write(prefix + line)
                    n_shapes += 1

    # trips.txt — rewrite shape_id through the csv module (quoting-safe).
    n_trips = 0
    with open(GTFS_OUT / "trips.txt", "w", encoding="utf-8", newline="") as fout:
        writer = None
        for k, d in enumerate(shard_dirs):
            with open(d / "trips.txt", encoding="utf-8-sig", newline="") as fin:
                reader = csv.DictReader(fin)
                if writer is None:
                    writer = csv.DictWriter(fout, fieldnames=reader.fieldnames or [])
                    writer.writeheader()
                prefix = f"s{k}_"
                for row in reader:
                    sid = (row.get("shape_id") or "").strip()
                    if sid:
                        row["shape_id"] = prefix + sid
                    writer.writerow(row)
                    n_trips += 1

    # stop_times.txt / frequencies.txt — plain concatenation.
    for name in ("stop_times.txt", "frequencies.txt"):
        parts = [d / name for d in shard_dirs if (d / name).exists()]
        if not parts:
            continue
        with open(GTFS_OUT / name, "w", encoding="utf-8", newline="") as fout:
            for i, src in enumerate(parts):
                with open(src, encoding="utf-8-sig", newline="") as fin:
                    header = fin.readline()
                    if i == 0:
                        fout.write(header)
                    shutil.copyfileobj(fin, fout)

    print(f"  merged {len(shard_dirs)} shards: {n_trips:,} trips, "
          f"{n_shapes:,} shape points")
    shutil.rmtree(SHARD_ROOT)


def main() -> None:
    if not GTFS_IN.exists():
        sys.exit(f"missing {GTFS_IN} — run 04_preprocess_gtfs.py first")
    if not OSM_PBF.exists():
        sys.exit(f"missing {OSM_PBF} — run 03_bbox_osm.py first")

    cfg = load_cfg().get("pfaedle", {})
    image = cfg.get("image", "carfree-pfaedle:latest")

    try:
        jobs = max(1, int(os.environ.get("PFAEDLE_JOBS", "1")))
    except ValueError:
        jobs = 1

    if jobs == 1:
        run_single(cfg, image)
    else:
        print(f"Sharding the feed into {jobs} route-hashed subsets (PFAEDLE_JOBS={jobs})…")
        shard_dirs = make_shards(jobs)
        run_shards(cfg, image, shard_dirs)
        print("Merging shard outputs…")
        merge_shards([d for d in shard_dirs if (d / "trips.txt").exists()])

    shapes = GTFS_OUT / "shapes.txt"
    if not shapes.exists():
        sys.exit("pfaedle did not produce shapes.txt — check stderr above")
    size_mb = shapes.stat().st_size / 1_000_000
    print(f"\nDone. shapes.txt = {size_mb:.1f} MB → {GTFS_OUT}")


if __name__ == "__main__":
    main()
