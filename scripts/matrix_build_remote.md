# Building the Valhalla footpath matrix on a remote machine

The [Valhalla footpath matrix](build_valhalla_footpath_matrix.py) is the
one-off input MOTIS's fork loads at import time (see
`.claude/concepts/valhalla-pedestrian-router.md`). On the Mac it takes
~10+ hours; a beefier CPU cuts that to 2-4 h. Everything else (MOTIS
fork build, MOTIS import, deploy) stays on the primary dev machine.

## Prerequisites on the remote

- Docker + compose plugin (`sudo dnf install docker docker-compose-plugin` on Nobara/Fedora).
- Python 3.10+ (stdlib only — the matrix builder pulls in no packages).
- SSH access back to the primary dev machine — set up an alias `mac`
  in `~/.ssh/config`, or substitute `user@ip` in the rsync commands below.

Setting up that SSH access to a Mac hits three things: Remote Login is
off by default (System Settings → Sharing, and "Allow access for" must
include your account); the account's short name is not the display name
(`whoami` on the Mac gives it); and `~/Documents` is TCC-protected, so
an SSH session can stat files there but not read them — grant Full Disk
Access to `/usr/libexec/sshd-keygen-wrapper`. Symptoms are misleading in
all three cases: a connection closed before any password prompt, and
`Operation not permitted` on directories whose POSIX permissions are
fine.

macOS also ships **openrsync**, not GNU rsync. It rejects
`--skip-compress` and other GNU options, reporting only "connection
unexpectedly closed". Stick to plain `-a --partial`.

## Steps

Clone the repo and check out the branch that carries the fork:

    git clone <repo> newmap && cd newmap
    git checkout feature/routing

Rsync the two data files needed (~915 MB). `stops.txt` supplies stop
coords + IDs; the PBF is the wide-bbox walkable-patched OSM extract
Valhalla ingests.

    mkdir -p data/gtfs_motis data/osm motis/data
    rsync -a --partial mac:'~/Documents/prog/newmap/data/gtfs_motis/stops.txt' data/gtfs_motis/
    rsync -a --partial mac:'~/Documents/prog/newmap/data/osm/ch_pfaedle_walkable.osm.pbf' data/osm/

Copy **only `stops.txt`**, not all of `gtfs_motis/` — the builder reads
nothing else from `data/`, and the rest is ~4.4 GB of timetable bulk.

Set Valhalla's serving thread count (`valhalla/docker-compose.yml`, env
`server_threads`). **16 is the ceiling** — see troubleshooting.

    sed -i 's/server_threads=8/server_threads=16/' valhalla/docker-compose.yml

Bring Valhalla up and wait for the tile build (~7 min on a 13700K; the
OSM parse stages are single-threaded and look like a hung process):

    cd valhalla && docker compose up -d valhalla
    until curl -sf http://localhost:8002/status >/dev/null; do sleep 5; done
    cd ..

Verify before trusting it — a route must return a trip, and uphill must
differ from downhill (if they match, elevation never made it into the
graph and `use_hills` is doing nothing):

    curl -s -X POST http://localhost:8002/route \
      -d '{"locations":[{"lat":46.9480,"lon":7.4474},{"lat":46.9520,"lon":7.4390}],"costing":"pedestrian"}'

Prescan next. Expected: ~2,000 stops dropped, all outside the routing
bbox (TGV to Paris, ICE to Berlin); zero inside-bbox stops unroutable.
Takes ~15 s. Check the dropped list against the bbox rather than just
counting — the right count does not prove the right stops were dropped.

    python3 scripts/build_valhalla_footpath_matrix.py --prescan-only

Then the full build. `MATRIX_WORKERS` is Python-side HTTP concurrency;
match or slightly exceed `server_threads`.

    MATRIX_WORKERS=20 python3 scripts/build_valhalla_footpath_matrix.py

Reckon on 2-4 h for ~65k stops (34.8M pairs for the CH feed). The rate
swings from ~25 stops/s in rural areas to ~3.5 across the Zurich
agglomeration — that is candidate density (~113 stops within the 11 km
radius rural, ~2,124 urban), not a fault. Valhalla should sit near 1500%
CPU with the Python side near 1%; sampling between batches catches an
idle trough and reads far lower.

A `note: Valhalla refused a matrix request … bisecting` line means
progress has paused while a chunk is split down to isolate what failed.
Expect up to ~8 min. It is not a hang — see troubleshooting.

Finally, recover whatever the bisection floor discarded:

    MATRIX_WORKERS=20 python3 scripts/build_valhalla_footpath_matrix.py --repair

This re-runs only the pairs in `valhalla_failed_pairs.csv` with full
isolation, parallelised across sources, and rewrites that file with the
pairs that genuinely failed. Cheap, and it makes the main run's
time-for-data trade-off temporary rather than permanent. On the CH feed
it recovered 0, because every skipped pair turned out to be a
cross-batch artifact (see error 154 below) rather than a real footpath.

### Deduplicate before shipping

Rows are written before the checkpoint is updated, so a run killed
mid-batch re-does those sources on resume and duplicates their rows.
After any run that was interrupted:

    awk '!seen[$0]++' motis/data/valhalla_footpath_matrix.csv > matrix.dedup.csv
    mv matrix.dedup.csv motis/data/valhalla_footpath_matrix.csv

Duplicates are byte-identical (same pair, same duration — 8,675 of
34.8M on the CH build), so this cannot change a value. Costs ~5 GB of
RAM for the seen-set.

Sanity-check the result: no self-pairs, no duration above
`MAX_FOOTPATH_SEC`, three columns throughout.

## Outputs

All under `motis/data/`:

- **`valhalla_footpath_matrix.csv`** — the deliverable.
  `from_stop_id,to_stop_id,duration_sec`, one row per ordered pair under
  `MAX_FOOTPATH_SEC` (7200 s). ~1 GB for the CH feed. Stop IDs are the
  platform-level IDs from `stops.txt`, so they match what the MOTIS
  importer uses.
- **`valhalla_footpath_matrix.checkpoint`** — completed source stop IDs.
  The run resumes from here, so killing it costs only the current batch.
  Delete both this and the CSV (or pass `--restart`) to force a rebuild.
- **`valhalla_unroutable_stops.csv`** — stops with no walkable OSM edge
  nearby, excluded from the matrix. Dominated by stops outside the OSM
  extract's bbox.
- **`valhalla_failed_pairs.csv`** — pairs Valhalla refused, appended as
  they happen (not at the end: the checkpoint marks a source complete
  as soon as its batch lands, so writing this only on exit would let a
  killed run lose the record while the pairs stay out of the matrix).
  After `--repair` it holds only the genuinely unroutable ones.

Both diagnostic CSVs describe stops that are silently *absent* from the
matrix — worth checking before blaming MOTIS for a missing transfer.
Judge `valhalla_failed_pairs.csv` by distance, not count: pairs further
apart than ~10.2 km could never have been footpaths anyway, so a file
full of 200 km pairs means nothing was lost.

## Shipping the result back

    rsync -avz motis/data/valhalla_footpath_matrix.csv \
        mac:'~/Documents/prog/newmap/motis/data/'

On the primary dev machine, the MOTIS fork picks it up at the next
import (`cd motis && docker compose --profile import up motis-import`).
The env var `KORA_FOOTPATH_MATRIX_PATH` (wired in the compose files)
points the fork at `/data/data/valhalla_footpath_matrix.csv`; if the
file is missing at import time the fork aborts — no silent fallback to
MOTIS's OSR walker.

Re-run the matrix whenever the GTFS stop set changes materially: stop
IDs absent from the matrix get no Valhalla-corrected transfers.

## Troubleshooting

**Every location returns "No data found for location".** The tile build
was interrupted. It leaves a plausible-looking tile directory — right
level-2 count, gigabytes on disk — with no hierarchy, and the entrypoint
skips straight to serving it on the next start. A complete build has
levels 0, 1 *and* 2 (4 / 18 / 180 tiles for CH+neighbours) and no `*.bin`
parse intermediates left:

    ls valhalla/data/valhalla_tiles/*.bin 2>/dev/null | wc -l   # 0 when complete

To recover, delete `valhalla_tiles/` and `valhalla_tiles.tar` and start
again — keep `elevation_data/` and `admin_data/`.

**Valhalla crash-loops at startup with `Too many open files`
(`ipc_listener.cpp`).** `server_threads` is above 16. The entrypoint
drops privileges via `sudo`, which resets the file-descriptor soft limit
to 1024 whatever Docker was told, so a `ulimits:` block does not help.
The restart policy hides this as plain "connection refused".

**Progress stops for minutes at a time, Valhalla pinned at ~100% CPU
(one core) instead of ~1500%.** A chunk is being bisected: the
recursion is sequential inside a single pool worker while the other
workers idle. `BISECT_FLOOR_PAIRS` bounds it — at 16 the worst case is
~770 calls (~8 min) per poisoned chunk, versus ~7,500 (~80 min) if it
drilled down to single pairs. Lowering the floor recovers more pairs
per run but lengthens the stall; `--repair` afterwards makes that
trade-off moot. Parallelising the halves would remove the stall
entirely, but needs a separate executor — recursing into the batch's
own pool deadlocks. Not done.

**Error 154, "Path distance exceeds the max distance limit".** The one
that actually bites, and the cause of nearly every skipped pair on the
CH feed. Valhalla validates every source-target pair in a request
against `max_matrix_distance` (200 km). Batches send the *union* of
their sources' candidate targets, so a stop 11 km from source A travels
in the same request as source B 200 km away, and the whole request is
rejected on that widest pair. The Hilbert ordering keeps batches local
enough that this is rare, but the curve's longest jumps still exceed the
limit — expect it near the end of a run. Bisection resolves it: the
halves are geographically narrower, so the real pairs get computed and
only the cross-batch artifacts are dropped. Confirm that is all you lost
by checking the skipped pairs' distances.

**Error 499, "Could not find candidate edge used for label".** An
internal thor failure on a pair, deterministic — resuming hits the same
batch again. Handled by the same bisection path.

## Tuning knobs

- **`MATRIX_WORKERS=N`** (env var): Python-side concurrent HTTP
  requests. Default 8. On a 24-thread box, 16-20 fits comfortably.
- **`server_threads=N`** in `valhalla/docker-compose.yml`: Valhalla's
  own worker pool, and the actual ceiling on throughput. Capped at 16
  by the container's file-descriptor limit.
- **`BATCH_SOURCES` / `BATCH_TARGETS`** in the Python script (both 50):
  Valhalla's default `max_matrix_locations` is 2500 = 50×50. Raising
  either would need editing `valhalla.json` to lift that limit + a
  container restart — usually not worth it for a one-off build.
- **`RADIUS_M`** in the Python script (11000 m): straight-line radius
  around each source for candidate target selection. Should stay above
  `MAX_FOOTPATH_SEC` × `WALK_SPEED_KMH` / 3.6 (currently ~10.2 km).
- **`BISECT_FLOOR_PAIRS`** in the Python script (16): smallest block
  bisection tries to rescue before skipping it whole. Trades stall time
  against pairs discarded per failure — 1 loses least but stalls
  longest, 64 is fastest but discards a whole block on a single bad
  pair. `--repair` recovers whatever it discarded, so favour speed here.
