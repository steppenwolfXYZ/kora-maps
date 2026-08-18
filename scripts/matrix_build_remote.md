# Building the Valhalla footpath matrix on a remote machine

The [Valhalla footpath matrix](build_valhalla_footpath_matrix.py) is the
one-off input MOTIS's fork loads at import time (see
`.claude/concepts/valhalla-pedestrian-router.md`). On the Mac it takes
~10+ hours; the CH extract is small enough that a beefier CPU + more
Valhalla threads cuts that to ~1-2 h. Everything else (MOTIS fork build,
MOTIS import, deploy) stays on the primary dev machine.

## Prerequisites on the remote

- Docker + compose plugin (`sudo dnf install docker docker-compose-plugin` on Nobara/Fedora).
- Python 3.10+.
- SSH access back to the primary dev machine — set up an alias `mac`
  in `~/.ssh/config`, or substitute `user@ip` in the rsync commands below.

## Steps

Clone the repo and check out the branch that carries the fork:

    git clone <repo> newmap && cd newmap
    git checkout feature/routing

Rsync the two data files needed for the matrix build (~1.5 GB total).
`gtfs_motis/` supplies stop coords + IDs; the PBF is the wide-bbox
walkable-patched OSM extract that Valhalla ingests.

    mkdir -p data/gtfs_motis data/osm motis/data
    rsync -avz mac:'~/Documents/prog/newmap/data/gtfs_motis/' data/gtfs_motis/
    rsync -avz mac:'~/Documents/prog/newmap/data/osm/ch_pfaedle_walkable.osm.pbf' data/osm/

Bump Valhalla's thread count to match the remote CPU (16 is a good
default for 24 physical cores; the same knob is at
`valhalla/docker-compose.yml`, env `server_threads`):

    sed -i 's/server_threads=8/server_threads=16/' valhalla/docker-compose.yml

Bring Valhalla up and wait for tile build + serve (~30-45 min on a
24-core box; watches the log until the HTTP port responds):

    cd valhalla && docker compose up -d valhalla
    until curl -sf http://localhost:8002/status >/dev/null; do sleep 5; done
    cd ..

Prescan first to confirm the setup is sane. Expected: ~2,000 stops
dropped (all outside the routing bbox — TGV to Paris, ICE to Berlin,
etc.); zero inside-bbox stops unroutable.

    python3 scripts/build_valhalla_footpath_matrix.py --prescan-only

If that looks right, run the full matrix build. `MATRIX_WORKERS`
controls Python-side HTTP concurrency; match or slightly exceed
Valhalla's `server_threads`.

    MATRIX_WORKERS=16 python3 scripts/build_valhalla_footpath_matrix.py

Progress prints once per source batch with an ETA. Resumable via
`motis/data/valhalla_footpath_matrix.checkpoint` — killing and
restarting picks up from the last completed source.

## Shipping the result back

    rsync -avz motis/data/valhalla_footpath_matrix.csv \
        mac:'~/Documents/prog/newmap/motis/data/'

On the primary dev machine, the MOTIS fork picks it up at the next
import (`cd motis && docker compose --profile import up motis-import`).
The env var `KORA_FOOTPATH_MATRIX_PATH` (wired in the compose files)
points the fork at `/data/data/valhalla_footpath_matrix.csv`; if the
file is missing at import time the fork aborts — no silent fallback to
MOTIS's OSR walker.

## Tuning knobs

- **`MATRIX_WORKERS=N`** (env var): Python-side concurrent HTTP
  requests. Default 8. On a 24-core box, 16-20 fits comfortably.
- **`server_threads=N`** in `valhalla/docker-compose.yml`: Valhalla's
  own worker pool. Match or slightly exceed `MATRIX_WORKERS`.
- **`BATCH_SOURCES` / `BATCH_TARGETS`** in the Python script (both 50):
  Valhalla's default `max_matrix_locations` is 2500 = 50×50. Raising
  either would need editing `valhalla.json` to lift that limit + a
  container restart — usually not worth it for a one-off build.
- **`RADIUS_M`** in the Python script (11000 m): straight-line radius
  around each source for candidate target selection. Should stay above
  `MAX_FOOTPATH_SEC` × `WALK_SPEED_KMH` / 3.6 (currently ~10.2 km).
