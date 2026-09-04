# Deployment

Four deliberately separate deploy channels:

1. **App** (SvelteKit build) — automatic, GitHub Actions on every push to `main`.
2. **Map assets** (pmtiles, style.json, indexes, glyph fonts) — manual, `scripts/deploy/deploy_map_assets.sh`, run only when a pipeline result is worth publishing. Not integrated into the pipeline on purpose: not every rebuild produces a publishable outcome.
3. **MOTIS routing backend** — manual, `scripts/deploy/deploy_motis.sh`, run only when the routing data should be (re)published. The GTFS/OSM import runs locally (Mac is aarch64, portable to the server's arm64 image); the server only serves prebuilt indexes, never imports. Ships the Kora fork of MOTIS as a locally-built docker image (see `motis/fork/`).
4. **Valhalla router** — manual, `scripts/deploy/deploy_valhalla.sh`. Ships the Kora fork of Valhalla (`valhalla/fork/`: upstream pinned at `VALHALLA_REF` plus the Kora bicycle costing, see `bicycle-costing-fork.md`) as a locally-built docker image, and — on request — the tile set. Tile + elevation build runs locally; the server only serves prebuilt tiles and never builds.

`scripts/update_map.sh` is the data-refresh machine's whole routine, scheduled as a DAG rather than a line: GTFS ∥ OSM downloads → OSM extracts ∥ GTFS preprocess → pfaedle (sharded over `PFAEDLE_JOBS` containers) ∥ routing prep (`setup_routing.sh --steps 1,2,3,4`: network, image, OSM patch, station walk network + quay anchors, Valhalla tiles — with a tile wipe first when the OSM extract is newer than the tiles) → footpath matrix ∥ map emission (`rebuild_transit.sh --only 6,7,8`) → MOTIS import + local smoke test → the three deploys (`deploy_motis.sh --data-only` and `deploy_valhalla.sh --data-only`: indexes / tiles only, never this machine's amd64 images) → production smoke test. Any failing stage aborts before the deploy phase, so a broken local import never reaches the server; per-stage wall times print at the end. Sizing env: `PFAEDLE_JOBS`, `TIPPECANOE_JOBS`, `VALHALLA_THREADS`, `MATRIX_WORKERS`. The app deploy stays separate (git push from the dev Mac).

The build's shape is set by two independent axes. **Which branch:** `--only-pipeline` (transit pipeline + map emission; skips routing prep, matrix, import) or `--only-routing` (routing prep, matrix, import, smoke test; skips map emission *and* the whole GTFS chain, building on the routed feed already on disk — not re-shaping it is the point of the flag). Neither flag = both branches. **What to refresh:** `--osm` re-downloads the country PBFs, `--skip-gtfs` skips the GTFS/atlas download entirely. **Where the pipeline starts:** `--pipeline-from N` skips every pipeline step below N and reuses what it produced last time — `4` gtfs_prep onward, `5` pfaedle onward, `6` emit (6,7,8), `7` emit (7,8), `8` pmtiles only. It is not the same as `--skip-gtfs`, which skips only the download and still runs steps 2-4, and it is rejected with `--only-routing` rather than ignored. Emit-only is the common iteration: ~11 min against ~30 for the whole pipeline branch. Deploy scope follows the branch automatically — `--only-pipeline` ships map assets, `--only-routing` ships Valhalla + MOTIS data — so deploy targets are never named by hand, and the production smoke test checks whatever was actually shipped. Preconditions a pruned stage graph cannot satisfy itself are caught in preflight, in seconds, and are named by missing artifact rather than by flag. The guards are scoped to the actual consumer, not to "something later runs" — step 8 reads none of the OSM or GTFS artifacts, so a pmtiles-only rebuild is not blocked by their absence.

Because that routing-prep branch runs *alongside* pfaedle, anything in it that needs GTFS reads `data/gtfs_filtered/` (final since the previous phase) and never `data/gtfs_routed/`, which pfaedle is rewriting at that moment — see `station-walk-network.md` § Quay source.

Separate from all four, the Mac drives builds on the data machine remotely and pulls the result back: `scripts/remote_build.sh` on the Mac launches, watches, fetches and post-syncs in one command — see § Remote build and fetch.

The split exists because map assets are large generated artifacts (~470 MB, gitignored) while the app is small committed code. It also maps cleanly onto the future setup where a dedicated pipeline server runs nightly rebuilds and pushes assets itself — the GitHub Actions side never changes.

## Production environment

- **Server:** shared Hetzner VPS (Debian), `91.99.74.183` / `2a01:4f8:c0c:cbf0::1`, hosts other low-traffic sites too. Nothing heavy may run there — the transit pipeline never runs on this machine.
- **Domain:** `koramaps.app` + `www.koramaps.app` (A + AAAA on both).
- **Deploy user:** `ga_koramaps`, owns `/var/www/koramaps.app/`. GitHub Actions authenticates with the repo secret `SSH_PRIVATE_KEY`; the local machine uses the `~/.ssh/config` alias `koramaps` (same user, personal key).
- **Directory layout** under `/var/www/koramaps.app/`:
  - `app/` — live app (build + node_modules + ecosystem.config.cjs + .env). Overwritten by every app deploy.
  - `build-environment/` — staging dir the workflow rsyncs into; removed after each finalize.
  - `map-assets/` — map data, written only by `deploy_map_assets.sh`.
  - `motis/` — routing backend (config.yml + docker-compose.prod.yml + data/ with the prebuilt indexes), written only by `deploy_motis.sh`.
  - `valhalla/` — pedestrian router (docker-compose.prod.yml + data/ with the prebuilt Valhalla tiles + elevation + admins), written only by `deploy_valhalla.sh`.
- **Process manager:** pm2, app name `koramaps`, defined in `ecosystem.config.cjs` (repo root, deployed with the artifact). It runs `build/index.js` (adapter-node) with `node --env-file=.env` — requires node ≥ 20.6 on the server. All runtime config (`PORT=3012`) lives in `.env`, which the workflow writes from the `ENV_VARS` repo secret. No ORIGIN var: the planned login system is JSON/REST, which bypasses SvelteKit's form-action CSRF path.
- **nginx:** site file `/etc/nginx/sites-available/koramaps.app`. `location /map-assets/` is an alias to the map-assets dir — nginx serves pmtiles directly (range requests, 1 h cache header); the node app never sees those requests. `location /` proxies to `localhost:3012`. `location /valhalla/` proxies to `http://127.0.0.1:8002/` (trailing slash strips the prefix, so Valhalla sees its native `/route`, `/sources_to_targets`, …). TLS via certbot (`--nginx -d koramaps.app -d www.koramaps.app`); the port-80 server block is required (https redirect + ACME renewals) — do not "clean it up".

## App deploy (`.github/workflows/deploy.yml`)

Mirrors the user's standard workflow used across their projects (same shape as ogoy.app; keep them consistent): build on the GA runner (never on the VPS), `npm prune --omit=dev`, assemble `deploy_artifact/` (build, node_modules, package.json, ecosystem.config.cjs, .env), rsync to the server staging dir, then a server-side finalize: rsync staging → `app/`, delete staging, `pm2 startOrRestart ecosystem.config.cjs`. Repo secrets: `ENV_VARS` (content of .env) and `SSH_PRIVATE_KEY`.

adapter-node does not bundle production dependencies — that is why node_modules ships in the artifact.

## Map assets deploy (`scripts/deploy/deploy_map_assets.sh`)

Rsyncs `static/map-assets/` → `map-assets/` on the server over the `koramaps` SSH alias. Allowlist: `*.json` (style, stop-search index, line index), `fonts/` (MapLibre glyph PBFs), `tl_*.pmtiles`; excludes `tl_debug_*` and anything else (stale/legacy files never leave the machine). `--delete` inside the target dir. Extra args pass through to rsync (`--dry-run`). Fonts transfer once; later runs skip them as unchanged.

Run it before the first app deploy on a fresh server — without assets the app serves but the map cannot load.

## MOTIS deploy (`scripts/deploy/deploy_motis.sh`)

Ships the MOTIS **software** by default — the locally-built Kora fork image plus `motis/config.yml` and `motis/docker-compose.prod.yml` → `motis/` on the server over the `koramaps` SSH alias, then restarts the container. `motis/data/` (the prebuilt nigiri/OSR/shapes indexes, ~2.6 GB, imported locally) ships only on explicit request: `--with-data` adds it to the software deploy (exception case from the dev Mac), `--data-only` ships data + config without the image (the data machine's mode, used by `update_map.sh` — its amd64 image must never reach the arm64 server). The default skips data because the dev Mac's indexes are usually older than the data machine's last deploy, and the data rsync runs with `--delete`. The image transfer uses `docker save | ssh docker load` (no registry) — repeat deploys skip the transfer when layers are unchanged. The prod compose is serve-only: no `motis-import` service, no GTFS/OSM bind mounts (the server never imports — the Mac's aarch64 indexes run on the arm64 image; `/motis server` ignores the import-only config paths at serve time, verified locally), loopback-bound port (`127.0.0.1:8080`), `mem_limit: 2g` (CAX11 has 4 GB total), capped json-file logs (10 MB × 3). When data ships, the script stops the container before rsync because MOTIS memory-maps its index files — replacing them under a running server can fault mid-query (an image-only deploy skips the stop; `up -d` recreates the container from the new image). `--delete` on `data/`; `--dry-run` passes through to rsync and skips the stop/start.

The MOTIS binary is the Kora fork (`motis/fork/`, image tag `koramaps/motis:footpath-matrix`) — Valhalla is the sole walking authority end to end (see `valhalla-pedestrian-router.md` and `motis/fork/README.md`): the import-time transfer table loads the precomputed Valhalla matrix (`KORA_FOOTPATH_MATRIX_PATH` → `/data/data/valhalla_footpath_matrix.csv`, abort if missing), floored per quay pair by the feed's own minimum transfer times read from `transfers.txt` (`KORA_GTFS_TRANSFERS_PATH` → `/data/gtfs/transfers.txt`, abort if missing — see `transfer-point-optimization.md` § Minimum transfer time), and at query time the fork calls Valhalla live for WALK offsets (RAPTOR boarding-stop selection) and WALK legs (`KORA_VALHALLA_URL`, default `http://kora-valhalla:8002`). No OSR walking fallback anywhere; the fork's server exits at startup while Valhalla is unreachable and docker's restart policy retries until it is. MOTIS and Valhalla containers share the external docker network `koramaps` (one-time, per machine: `docker network create koramaps`). Build the image locally once: `docker build -t koramaps/motis:footpath-matrix -f motis/fork/Dockerfile motis/fork`. The upstream MOTIS commit is pinned via `MOTIS_REF` in the Dockerfile — bump procedure in `motis/fork/README.md`.

The client reaches MOTIS same-origin at `/routing/` (env var `PUBLIC_MOTIS_URL`, `$env/static/public`, baked at build time: `http://localhost:8080` in `.env`, `/routing` in `.env.production` / the `ENV_VARS` secret). nginx proxies `location /routing/` → `http://127.0.0.1:8080/` (trailing slash strips the prefix, so MOTIS sees its native `/api/v1/…`); `/api/` stays free for a future koramaps API and is already partially used by the app's own geocode endpoints. Docker (not pm2) supervises the container (`restart: unless-stopped` + enabled `docker.service`).

One-time server prep: install docker + compose plugin, `systemctl enable --now docker`, add `ga_koramaps` to the `docker` group, create `/var/www/koramaps.app/motis/` (owned by `ga_koramaps`), add the nginx location, keep 8080 closed in the cloud firewall, confirm ~5 GB free disk. Because the transfer table is built at import, both the two-tier split and the minimum-transfer-time floor only exist in indexes produced by an image that carries them — an index imported by an older image silently lacks them (the `koraFullTransfers` profile then degrades to the capped table). After bumping the fork, re-import before judging routing behaviour.

Re-import cycle (all local): `python3 scripts/routing/build_station_walk_network.py` → `python3 scripts/routing/preprocess_gtfs_for_motis.py` → `python3 scripts/routing/check_gtfs_motis_consistency.py` (aborts on a mixed-vintage sidecar; `setup_routing.sh` step 7 runs it for you) → **`python3 scripts/routing/build_valhalla_footpath_matrix.py`** (writes `motis/data/valhalla_footpath_matrix.csv` — Valhalla must be running locally, see below) → `docker compose --profile import up motis-import` (in `motis/`) → `./scripts/deploy/deploy_motis.sh --with-data` (the fresh import must ship, so the data flag is required here).

## Valhalla deploy (`scripts/deploy/deploy_valhalla.sh`)

Same software / data split as the MOTIS deploy. Default ships the **software**: the locally-built Kora fork image `koramaps/valhalla:bicycle-costing` (`valhalla/fork/`, built by `setup_routing.sh` step 2b or `docker build -t koramaps/valhalla:bicycle-costing -f valhalla/fork/Dockerfile valhalla/fork`) via `docker save | ssh docker load` plus `valhalla/docker-compose.prod.yml` → `valhalla/` on the server, then `up -d` recreates the container. `valhalla/data/` (Valhalla tiles + SRTM elevation + admin polygons, ~500-800 MB depending on the OSM extract) ships only on explicit request: `--with-data` adds it (dev-Mac exception), `--data-only` ships data + compose without the image (the data machine's mode, used by `update_map.sh` — its amd64 image must never reach the arm64 server; the script refuses a non-arm64 image outright). When data ships the script stops the container before rsync because Valhalla memory-maps its tiles. Prod compose is serve-only (`use_tiles_ignore_pbf=True`, no PBF mounted, no elevation download), loopback-bound (`127.0.0.1:8002`), `mem_limit: 1g`, capped json-file logs.

The fork image is a drop-in for the pinned upstream `valhalla-scripted` image (same entrypoint and environment interface), so switching to it needs no tile rebuild — costing is query-time. The upstream version pin lives in `valhalla/fork/Dockerfile` (`ARG VALHALLA_REF`, currently 3.8.3) and is the one version string for image, tiles and footpath matrix: `setup_routing.sh` stamps the tiles with the version that built them (`valhalla/data/.tiles_valhalla_version`) and wipes them only when the pin moves — a costing-only fork iteration leaves them alone. Bumping the pin is a deliberate act (tile + matrix rebuild + re-applying the overlay; checklist in `valhalla/fork/README.md`). Tuning loop and benchmark set: `valhalla/fork/README.md` § Iterating, `scripts/bicycle_benchmark.py`.

The **transit tab** does NOT call Valhalla — its walking is computed server-side inside the MOTIS fork, so the browser makes exactly one request per transit query (to `/routing/`). The **cycling / walking tabs** (pedestrian-bicycle-routing.md) call Valhalla's `/route` directly from the client via `PUBLIC_VALHALLA_URL` (`$env/static/public`, baked at build time — same pattern as `PUBLIC_MOTIS_URL`: `http://localhost:8002` in `.env`, `/valhalla` in `.env.production` / the `ENV_VARS` repo secret). The nginx `location /valhalla/` → `http://127.0.0.1:8002/` proxy is therefore a **required, supported endpoint** in production (previously optional debug-only). Restrict it to what the feature needs — only `POST/GET /valhalla/route` must pass; other Valhalla actions (`/sources_to_targets`, `/expansion`, …) should not be exposed publicly, e.g. `location = /valhalla/route { proxy_pass http://127.0.0.1:8002/route; }` instead of the blanket prefix location. MOTIS still reaches Valhalla over the internal `koramaps` docker network regardless of the proxy.

One-time server prep: create `/var/www/koramaps.app/valhalla/` (owned by `ga_koramaps`), `docker network create koramaps`, optionally add the nginx debug location, keep 8002 closed in the cloud firewall, confirm ~1 GB free disk. The tiles are built from `ch_pfaedle_walkable.osm.pbf`, which carries the synthetic station walk network merged in by `preprocess_osm_for_motis.py --valhalla` (see `station-walk-network.md`) — changing that overlay means rebuilding tiles *and* the footpath matrix, since both describe the same walking. Local tile build (one-off, ~20-40 min): `cd valhalla && docker compose up -d valhalla` — the fork image (upstream's scripted entrypoint) downloads SRTM elevation, builds admins, then routing tiles. First-time bring-up order: Valhalla tiles → matrix build → MOTIS import → deploy both (Valhalla first — the forked MOTIS refuses to serve without it).

## Remote build and fetch

Sideways, not a deploy channel. The **Mac** drives a build on the **data
machine** ("Kranich", Linux amd64) and pulls the finished artifacts back, so
the Mac's local map and routing stack are current without re-running the
pipeline there. It never touches production, and it only ever flows
Kranich → Mac.

Three scripts, and one of them is the only one you normally run:

| Script | Runs on | Job |
|---|---|---|
| `scripts/remote_build.sh` | Mac | the entry point: launch → watch → fetch → post-sync |
| `scripts/run_build_detached.sh` | Kranich | starts the build in tmux, writes the log and the exit-code stamp |
| `scripts/fetch_build.sh` | Mac | pulls the five artifact groups back |

**Reachability is Tailscale.** Both machines are on the tailnet, so Kranich
has a stable name from anywhere — no DynDNS, no port forwarding, no SSH
exposed to the internet. Kranich needs power only; it never needs a graphical
login. Overrides: `KRANICH_REMOTE` (SSH alias, default `kranich`) and
`KRANICH_PATH` (repo path over there, default `~/Prog/kora-maps`).

**The build outlives the connection.** `run_build_detached.sh` starts it in a
named tmux session (`kora-build`) on Kranich and returns immediately, so a
closed lid or a WiFi switch costs the view and nothing else. It writes
`build.log` live and — only on completion — `build.status` containing the
build's real exit code, captured through `PIPESTATUS` so `tee` cannot mask it.
The stamp is deleted before each run, so a previous run's result can never be
mistaken for this one's.

**The stamp is the sole success signal.** `remote_build.sh` streams the log
for the human and polls the stamp for the decision; those are separate. A
dropped stream reconnects and is never itself a failure. On a non-zero stamp
the whole log is copied down to
`data/transit/logs/remote-build-failed.log` and the fetch does not run.

**Phases are separately enterable**, so an interrupted run resumes instead of
restarting: `--watch-only` attaches to a build already running,
`--fetch-only` skips to the transfer, `--no-fetch` / `--no-post-sync` stop
early, `--dry-run` prints the decisions. Every unrecognised argument is
forwarded verbatim to `update_map.sh` — the build's flag surface lives on the
build script, and the transport wrapper deliberately does not interpret it.

**The transfer pulls, it does not push.** The direction flipped when the Mac
became the machine that drives builds. Three reasons: the roaming machine
should be the client, so the link that fails is the one you are already
watching (a push needs a second, Kranich → Mac connection); Kranich then needs
no credentials for and no way to reach the Mac; and `--delete` acts on the
machine you are sitting at, gated by checks made from there. What did *not*
change is everything below — the groups, filters, `--delete` set, compression
choices and sentinels are direction-agnostic and encode past incidents.

**Why it exists.** The Mac is where code is written, so every pipeline or fork
change lands there long before the data machine runs. What the Mac lacks is
fresh *data* — and it cannot practically build the footpath matrix. Re-running
the whole pipeline on the Mac just to catch up costs hours for artifacts the
data machine already produced.

**Two-machine roles.** Kranich imports MOTIS and builds Valhalla tiles + the
matrix; the Mac develops the app and the fork and triggers the builds. This
supersedes the assumption in deploy channel 3 above that the Mac is the
importing machine — `update_map.sh` on Kranich now does that, and
`deploy_motis.sh --data-only` ships its indexes to the VPS.

**Groups** of `fetch_build.sh` (all run by default; `--only a,b` selects,
`--no-routed` drops the big one). Nothing relevant is opt-in: the script's job
is to leave the Mac able to run *and* debug everything, and a flag you have to
remember is a flag that gets forgotten — which is exactly how the Mac ended up
importing a feed it had never been sent.

| Group | Source | Size | Contents |
|---|---|---|---|
| `assets` | `static/map-assets/` | ~470 MB | pmtiles, style.json, search/line/color indexes, glyph fonts |
| `motis` | `motis/data/` | ~6.3 GB | prebuilt nigiri / OSR / shapes indexes + the footpath matrix CSV |
| `valhalla` | `valhalla/data/` | ~1.0 GB | `valhalla_tiles.tar` + admins |
| `lookup` | `data/` (raw feed + derived) | ~400 MB | the whole GTFS feed, diagnostics, identity, OSM way extracts |
| `routed` | `data/gtfs_routed/` + `data/gtfs_motis/stops.txt` | ~6.2 GB | pfaedle's feed — input to `--start 6` and to a Mac re-import |

**The matrix ships with the indexes.** It used to be opt-in
(`--with-matrix`), on the theory that the Mac never re-imports because MOTIS
indexes are architecture-portable. That failed in practice: the Mac re-imports
whenever the fork's *import* path changes, and the `valhalla` group meanwhile
replaces its tiles — leaving a fresh tile set beside a months-old matrix. The
two describe the same walking, so the mismatch produces transfers the tiles
cannot draw (cancelled walk legs, no geometry) and transfers priced against a
walk surface that no longer exists. Nothing warns you: the import only reports
the unresolvable stop ids as a count. The flag is now accepted and ignored.
The CSV is still `--exclude`d from the index push and sent in a second,
`-z` push of its own — the indexes are incompressible binaries, the CSV is
text that compresses ~8.5× — and that exclude also keeps `--delete` from
removing the Mac's copy between the two pushes.

**Feed directories travel whole or not at all.** This is the rule the sync
broke for months. `lookup` used to send six small tables out of `data/gtfs/`
(`stops`, `routes`, `agency`, `calendar`, `frequencies`, `feed_info`) and the
sidecar's lone `data/gtfs_motis/stops.txt`, holding back `stop_times.txt` /
`trips.txt` / `calendar_dates.txt` as "pipeline fuel". The result was a Mac
whose `data/gtfs/feed_info.txt` announced the new release while its big tables
were the previous one — a directory that lies about its vintage, which is
worse to debug against than one that is simply absent — and, far worse, a
`data/gtfs_motis/` carrying this machine's new `stops.txt` over the Mac's own
old, hardlinked `stop_times.txt`. SBB renumbers quays between releases (Bern
platform 8 went `ch:1:sloid:7000:0:229097` → `ch:1:sloid:7000:4:8`), so those
stop references dangle. **MOTIS imports that without complaining**: nigiri
drops the unresolvable stop, keeps the trip, and reports only a count — the
IC1 then ran Fribourg → Zürich without ever calling at Bern, invisible to any
query from Bern while still listed in `/stoptimes` elsewhere.

So: `data/gtfs/` now ships in full (`--delete`, only `gtfs_complete.zip`
excluded as a duplicate of what was just sent). It is ~3.6 GB on disk but
overwrites the Mac's existing copy in place, so the disk delta is ~zero, and
`-z` puts ~240 MB on the wire. That also restores the two lookups you actually
need — `trips.txt` (trip_id → route / service / headsign, the join for every
trip id in a MOTIS response) and `calendar_dates.txt` (whether a service runs
on a given date; in this feed `calendar.txt` alone is a coarse weekday row that
~50 exception rows then override). `data/gtfs_motis/stops.txt` moved to the
`routed` group, so the sidecar is never half-updated.

The rest of `lookup` is unchanged: `data/transit/**/*.json`
(`gtfs_groups_full.json` above all, now including the `diagnostics/`
subdirectory), `stop_identity.json`, and the OSM extracts (`rail_ways`,
`tram_ways`, `platform_ways`, `builtup_grid_100m`, `quay_anchors`).
`--street-ways` adds `street_ways.geojson` (152 MB). The country PBFs
(12.7 GB, unreadable without osmium) still stay here.

**Re-importing MOTIS on the Mac is normally unnecessary.** The `motis` group
already delivers this machine's finished, self-consistent indexes; a local
re-import throws them away and rebuilds the same thing from
`data/gtfs_motis/`. Do it only to test a change to the fork's *import* path.
A default sync makes that safe — the `routed` group delivers the feed, and
`post_sync.sh` rebuilds the sidecar from it — but after `--no-routed` the
Mac's feed is stale, so don't re-import; just restart MOTIS on the synced
indexes. `setup_routing.sh` step 7 runs `scripts/routing/check_gtfs_motis_consistency.py`
before the importer and refuses a mixed feed, but "old yet internally
consistent" passes that check by design.

**Safety rails.** Two, both learned the hard way:

- The fetch **refuses to run while a build is alive on Kranich** (a remote
  `pgrep`). Mid-run, artifacts are being rewritten — pfaedle is producing a
  partial feed, and the Valhalla tile wipe that precedes a rebuild leaves
  `valhalla/data/` with no tiles at all. Fetching that with `--delete` would
  replace good data on the Mac with an empty directory. `--force` overrides.
- Every `--delete` group is gated on a sentinel that exists only once that
  group's build finished (`style.json`, `tt.bin`, `valhalla_tiles.tar`,
  `shapes.txt`). A missing sentinel skips the group with a warning instead of
  mirroring an interrupted run. Within `lookup`, `--delete` is used only on
  `data/gtfs/`, which the data machine owns end to end (gated on
  `stop_times.txt`, and a table the new release dropped must not linger); the
  other transfers in that group land inside the Mac's own 40+ GB `data/` tree
  and must never delete. Sentinels are now checked **on Kranich** over SSH,
  since that is where the artifacts are.

**Transfer details.** `-z` is applied per group: on for the text payloads
(JSON / CSV / GeoJSON compress 9–20×), off for pmtiles and the binary indexes,
where it only burns CPU on an already-fast link. `--partial` is kept
throughout because the Mac is on WiFi — a dropped connection resumes mid-file
instead of restarting a 1.4 GB index. Added with the pull flip: `--timeout=120`
so a stalled link fails in two minutes instead of hanging, and a five-attempt
retry loop per group (30 s apart) that resumes rather than restarts.

**GNU rsync is required on the Mac.** Under the old push, GNU rsync on the
data machine drove the filter chain and the Mac was a passive receiver, so the
bundled openrsync (protocol 29) was fine. Pulling makes the Mac the client
that interprets `--include` / `--exclude` / `--delete`, and openrsync's
filter-rule support is not up to it — the failure mode is silently
transferring the wrong set, not an error. `fetch_build.sh` therefore prefers a
Homebrew rsync, and **aborts** if all it can find is openrsync. Install with
`brew install rsync`, or point `RSYNC_BIN` at one.

**Target.** SSH alias `kranich` from the Mac's `~/.ssh/config` (the tailnet
name), repo path `~/Prog/kora-maps`. Override with `KRANICH_REMOTE` /
`KRANICH_PATH`. Preflight prints the **Mac's** free disk, which is worth
watching — its data volume runs near full and a default fetch is ~12 GB,
almost all of it overwriting in place. One caveat on that: the sidecar's
hardlinks pin the superseded routed feed until `post_sync.sh` rebuilds them,
so the Mac transiently holds two copies of it.

**Making it serve is `scripts/post_sync.sh`.** The fetch copies files; it does
not restart anything and does not know whether what it delivered still
matches. This script closes that gap. `remote_build.sh` runs it as the final
phase, so success means "the Mac serves the new data", not "files arrived";
run it by hand after a bare `fetch_build.sh`. It reports the feed version and artifact ages, then:
checks the sidecar and rebuilds it when it no longer matches the routed feed
(see below), decides whether the index needs re-importing by comparing
`motis/data/tt.bin` against `data/gtfs_routed/shapes.txt` and the matrix
(rsync `-a` preserves mtimes, so the data machine's "index newer than feed"
ordering survives the trip; the sidecar's own `stops.txt` is deliberately not
a trigger, since the rebuild would then demand an import of a feed the synced
index already describes), stops both services, imports only if needed, brings
Valhalla up before
MOTIS, and finishes with a Bern→Chur query — a station-to-station one, because
a walk-only smoke test cannot tell you whether a quay has matrix rows at all.
`--dry-run` prints the decisions; `--force-import` after a fork import-path
change; `--no-import` to restart on what is already there.

**The sidecar cannot be transferred — only rebuilt.** `data/gtfs_motis/` is a
hardlink farm over `data/gtfs_routed/` plus its own `stops.txt`. rsync mirrors
the routed feed by writing *new inodes*, so the sidecar's links keep pointing
at the feed the Mac held before; fetching `gtfs_routed` therefore never
refreshes it, and the superseded feed stays on disk held alive by those links.
Only `preprocess_gtfs_for_motis.py` re-creates the farm. So a failed
consistency check right after a fetch is the normal case, not an alarm, and
`post_sync.sh` repairs it — all of that script's inputs (`quay_anchors.json`,
`platform_ways.geojson`, `stop_identity.json`) come along in the same fetch,
so the result matches what Kranich produced. If it is *still* inconsistent
afterwards, `data/gtfs_routed/` itself is mixed: the script then blocks a
local import, keeps serving the fetched index, and points at Kranich. The rebuild cannot mask a stale feed either — the import
decision is made from the routed feed's own timestamp, so a stale feed simply
yields "no import".

## Which machine rebuilds what

The deciding question is how long the rebuild takes, not what it touches.

**Short rebuilds (under ~20-30 min): the Mac does them.** Staying on one
machine avoids a round trip, and the Mac is where the code already is.

**Long rebuilds: Kranich does them**, driven from the Mac with one command:

```
./scripts/remote_build.sh [--osm] [--only-pipeline | --only-routing] [--skip-gtfs]
```

That launches the build detached on Kranich, streams its log here, waits for
the exit-code stamp, fetches the artifacts, and runs `post_sync.sh`. Kranich
still deploys to production itself at the end of the build. The Mac may sleep
during the build but must stay awake for the fetch and post-sync legs.

If you run the pieces by hand instead — `run_build_detached.sh` on Kranich,
then `fetch_build.sh` on the Mac — **`post_sync.sh` is not optional**. The
fetch only copies files; it cannot restart anything and does not know whether
what it delivered is self-consistent. `post_sync.sh` rebuilds the
`data/gtfs_motis` hardlink farm (rsync writes new inodes, so the links still
point at the Mac's previous feed), decides whether a re-import is needed,
restarts both services in the required order — **Valhalla first, then
MOTIS** — and runs a smoke query. Skipping it leaves the Mac serving
pre-fetch mmaps of post-fetch files, which looks like corrupt routing data
rather than a stale process.

**Wake-on-LAN is not set up.** Kranich has sleep disabled and is assumed to be
powered on; a build cannot currently be triggered on a machine that is off.
Waking it would need an always-on device inside the LAN, since magic packets
must originate there.

**Fresh clone:** `./scripts/rebuild_transit.sh` with no arguments (the
no-argument form additionally runs the glyph bootstrap, step 0), then
`./scripts/routing/setup_routing.sh`.

Run `remote_build.sh`, `fetch_build.sh` or `post_sync.sh` with `--dry-run` first when in doubt.

## SSR constraints (deployment-driven)

Map assets never exist inside the server build, so the app must not touch them during SSR:

- `style.json` is fetched client-side in `+page.svelte` (`onMount`), never in a load function — a server-side fetch would 404 in production. The map route's `<svelte:head>` carries a `<link rel="preload">` so the download still starts with the document. (Previously in `app.html`, moved out so non-map routes like `/about` don't trigger an unused-preload warning.)
- The style object is held in `$state.raw` — Map.svelte's init effect mutates `style.layers` in place, and a deeply reactive proxy would make that effect re-trigger itself in an endless map-recreate loop.
- All URL writes go through SvelteKit's `replaceState` / `pushState` (`$app/navigation`), never raw `history.replaceState`. MapLibre's `hash: true` is NOT used — Map.svelte has its own position-hash sync (same `#zoom/lat/lng` format, written on `moveend`). Opening the line detail view is the one write that pushes rather than replaces, so browser back closes it; its close button correspondingly calls `history.back()` to consume that entry (see `line-detail-view.md` § Deep link). The selection rides along in SvelteKit's `page.state` (typed in `src/app.d.ts`), so the position-hash writer must preserve that state instead of overwriting it with an empty object.

## Stats page (`/stats`)

Server-rendered usage dashboard: per-day hits / routing queries / unique IPs (bots excluded, counted separately) plus the most-requested route pairs. Data comes from parsing the per-site nginx access log (`/var/log/nginx/koramaps/access.log` + rotated `.gz` siblings; path overridable via `STATS_ACCESS_LOG`). Guarded by basic auth in `src/hooks.server.ts` — credentials `STATS_USER` / `STATS_PASS` from `.env` (prod: the `ENV_VARS` repo secret); with either unset the route 404s. Route-pair place tokens (`ch_Parentch:1:sloid:<n>` → `p:` token, legacy `ch_Parent<uic>` → `u:`, coords → `c:`) are resolved to station names and deep-link UICs client-side via `stop_search_index.json` (its `p` field bridges SLOID parent ids to UICs) — never server-side (SSR constraint below). One-time server prep (root, done 2026-08): `access_log /var/log/nginx/koramaps/access.log;` inside the koramaps.app TLS server block; log dir `chown www-data:ga_koramaps` + `chmod 750` (deliberately NOT the `adm` group — that would let the deploy user read all system logs); dedicated logrotate rule `/etc/logrotate.d/koramaps` (daily, `rotate 30`, `create 0640 www-data ga_koramaps`) — the subdirectory keeps it out of the shared `/var/log/nginx/*.log` wildcard rule.

## UI fonts (self-hosted, no Google CDN)

`static/fonts/` (committed): `saira-vf-latin.woff2` + `saira-vf-latin-ext.woff2` (variable, weights 100–900, covers UI + splash) and `material-symbols-subset.woff2` (icon font subsetted to every Material Symbols glyph used across the app — mode icons in StopSearch and EndpointInput plus the routing/endpoint pill icons, time selector, popups, etc.). `@font-face` rules live inline in `app.html`. These are separate from `map-assets/fonts/` (MapLibre SDF glyph PBFs for tile labels) — both are needed; MapLibre cannot use web fonts.

If a new icon is used anywhere in the app (any `<span class="material-symbols-outlined">…</span>`), regenerate the subset: fetch `https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20,400,0..1,0&icon_names=<comma-separated, alphabetical, incl. new icon>&display=block` with a browser user agent, download the woff2 URL it contains, replace `material-symbols-subset.woff2`. The FILL axis is variable (0..1); `.material-symbols-outlined` in `app.html` bakes in `font-variation-settings: 'FILL' 1` so every icon renders filled by default — see `.claude/rules/project.md` § UI icons. The canonical icon list is the sorted comment inside the `@font-face` block in `app.html` — keep it in sync when you add or drop an icon.

## Ops notes

- First-line diagnostics: `ssh koramaps`, then `pm2 list`, `pm2 logs koramaps`.
- MOTIS diagnostics: `docker logs kora-motis --tail 50`, `docker stats kora-motis --no-stream`. External smoke test: `curl 'https://koramaps.app/routing/api/v1/plan?fromPlace=47.378,8.540&toPlace=47.424,8.508&arriveBy=false&numItineraries=1&directModes=WALK'` should return JSON.
- Valhalla diagnostics: `docker logs kora-valhalla --tail 50`, `docker stats kora-valhalla --no-stream`. External smoke test: `curl -X POST 'https://koramaps.app/valhalla/route' -H 'Content-Type: application/json' -d '{"costing":"pedestrian","locations":[{"lat":47.378,"lon":8.540},{"lat":47.380,"lon":8.542}]}'` should return a JSON trip with a `legs[0].shape` polyline.
- After a first-ever pm2 start: `pm2 save` so the app survives server reboots.
- Cert renewal is automatic (certbot timer). Never run bare `certbot --nginx` (interactive all-domains checklist) or `certbot delete` on this shared server; always scope with explicit `-d`.
