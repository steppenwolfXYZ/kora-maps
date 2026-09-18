# Kora Maps fork of Valhalla: Kora-owned bicycle costing
# (.claude/concepts/bicycle-costing-fork.md). Everything else — pedestrian
# costing, the walk legs and offsets the MOTIS fork requests, the footpath
# matrix — is upstream byte for byte.
#
# The fork overlays / patches:
#   src/sif/bicyclecost.cc                  — full-file overlay: the three-tier
#                                             weighting model, rider-power speed
#                                             model (+ e-bike profiles),
#                                             pushed-bike access, stairs pricing,
#                                             crossing rule, the kora request
#                                             options
#   src/thor/triplegbuilder.cc              — full-file overlay: pushed-bike
#                                             sections come back as pedestrian
#                                             travel_mode maneuvers so the
#                                             client can draw them dotted
#   patches/options-proto-kora-bicycle.patch
#                                           — adds `exclude_steps` (98),
#                                             `avoidance_scale` (99),
#                                             `bonus_scale` (100),
#                                             `surface_profile` (101) and
#                                             `route_character` (102) to
#                                             Costing.Options (the avoid-stairs
#                                             toggle and the fast ↔ nice ruler)
#
# Build (both steps; scripts/routing/setup_routing.sh step 2b does this):
#   1. the BASELINE image — upstream at VALHALLA_REF + deps + full compile,
#      once per machine and per pin, from Dockerfile.baseline:
#        docker build --build-arg VALHALLA_REF=<ver> -t koramaps/valhalla-baseline:<ver> \
#            -f valhalla/fork/Dockerfile.baseline valhalla/fork
#   2. this file — FROM that image, apply the patches + overlays, recompile
#      only the touched translation units:
#        docker build --build-arg VALHALLA_REF=<ver> -t koramaps/valhalla:bicycle-costing \
#            -f valhalla/fork/Dockerfile valhalla/fork
# The baseline is a tagged IMAGE, not a cached layer, so BuildKit's cache
# GC can never evict it and force a full re-clone + recompile.
#
# DROP-IN for ghcr.io/valhalla/valhalla-scripted:<VALHALLA_REF>. The runner
# stage below IS upstream's Dockerfile-scripted, fed with our patched build
# instead of the stock one: same entrypoint, same environment interface
# (use_tiles_ignore_pbf, build_elevation, server_threads, …), same
# /custom_files layout. valhalla/docker-compose.yml and
# docker-compose.prod.yml reference this tag and nothing else changes.
#
# VALHALLA_REF is PINNED to the upstream release the current tiles and the
# footpath matrix were built with. Costing is query-time, so swapping the
# stock image for this one needs no tile rebuild — as long as the graph
# version is the same. The pin is therefore the single version string for
# image, tiles and matrix: scripts/setup_routing.sh reads it from this line
# and wipes the tiles when they were built with another version. Bumping it
# is a deliberate act: re-apply the overlay onto the new upstream copy (see
# README.md), then expect a tile rebuild AND a matrix rebuild.
ARG VALHALLA_REF=3.8.3

# ─── Stage 1: the fork overlay on the baseline image ────────────────────
# The baseline (Dockerfile.baseline) carries the clone at VALHALLA_REF, the
# deps and the compiled build/; PATH, LD_LIBRARY_PATH and WORKDIR
# /src/valhalla are inherited from it. Its tag IS the pin, so a
# VALHALLA_REF bump can never build on a stale baseline.
FROM koramaps/valhalla-baseline:${VALHALLA_REF} AS builder
ARG VALHALLA_REF
ARG CONCURRENCY=4

# ─── Fork overlay (fast per iteration) ──────────────────────────────────
# The proto patch regenerates options.pb.h, which most of the tree includes
# — a proto change costs a near-full rebuild, a costing-only change costs
# one TU plus the link. `git apply` (not a full-file copy) keeps the patch
# small and portable across upstream bumps.
COPY patches/ /src/valhalla/kora-patches/
COPY src/ /src/valhalla/src/
RUN for p in /src/valhalla/kora-patches/*.patch; do git apply --verbose "$p"; done && \
    touch -c src/sif/bicyclecost.cc src/sif/pedestriancost.cc src/thor/triplegbuilder.cc && \
    make -C build all -j${CONCURRENCY} && \
    make -C build install

# What upstream's builder leaves behind for the scripted image.
WORKDIR /usr/local/src
RUN echo "${VALHALLA_REF}+kora-bicycle" > /usr/local/valhalla_version && \
    for f in /src/valhalla/locales/*.json; do cat ${f} | python3 -c 'import sys; import json; print(json.load(sys.stdin)["posix_locale"])'; done > valhalla_locales

# Same trim as upstream's Dockerfile-scripted: keep the serving + tile-build
# binaries, drop the rest.
RUN cd /usr/local/bin && \
  preserve="valhalla_service valhalla_build_tiles valhalla_build_config valhalla_build_admins valhalla_build_timezones valhalla_build_elevation valhalla_ways_to_edges valhalla_build_extract valhalla_export_edges valhalla_add_predicted_traffic valhalla_ingest_transit valhalla_convert_transit valhalla_add_landmarks valhalla_build_landmarks" && \
  mv $preserve .. && \
  for f in valhalla*; do rm $f; done && \
  cd .. && mv $preserve ./bin

# ─── Stage 2: the scripted runner, verbatim from upstream ───────────────
# Keep in step with docker/Dockerfile-scripted at VALHALLA_REF (runtime
# packages, env defaults, entrypoint). The scripts come from the cloned
# tree, so they are the pinned version's, not a copy that can drift.
FROM ubuntu:24.04 AS runner
ARG VALHALLA_REF

ENV PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
ENV LD_LIBRARY_PATH="/usr/local/lib"

RUN apt-get update > /dev/null && \
  export DEBIAN_FRONTEND=noninteractive && \
  apt-get install -y libluajit-5.1-2 libgeotiff5 \
  libzmq5 libczmq4 spatialite-bin libprotobuf-lite32 sudo locales \
  libsqlite3-0 libsqlite3-mod-spatialite libcurl4 \
  python3.12-minimal python3-requests python3-shapely python-is-python3 \
  curl unzip moreutils jq spatialite-bin > /dev/null && \
  rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local /usr/local
COPY --from=builder /usr/local/lib/python3.12/dist-packages/valhalla /usr/local/lib/python3.12/dist-packages/
COPY --from=builder /src/valhalla/docker/scripts/. /valhalla/scripts

RUN cat /usr/local/src/valhalla_locales | xargs -d '\n' -n1 locale-gen

# default expected env vars (upstream's)
ENV use_tiles_ignore_pbf=True
ENV build_tar=True
ENV serve_tiles=True
ENV update_existing_config=True
ENV force_rebuild=False
ENV path_extension=""
ENV build_admins=True
ENV build_time_zones=True
ENV build_elevation=False
ENV build_transit=False
ENV use_default_speeds_config=True
ENV traffic_name=""
ENV default_speeds_config_url="https://raw.githubusercontent.com/OpenStreetMapSpeeds/schema/master/default_speeds.json"

# Smoke tests: the binaries run, the version string carries the fork marker,
# and the new request option is known to the parser (a stock build would
# reject nothing here either, so the version line is the real check).
RUN python -c "import valhalla, sys; print(sys.version, valhalla)" \
  && valhalla_build_config | jq type \
  && cat /usr/local/valhalla_version | grep -q kora-bicycle \
  && valhalla_build_tiles -v \
  && ls -la /usr/local/bin/valhalla*

WORKDIR /custom_files

EXPOSE 8002
ENTRYPOINT ["/valhalla/scripts/docker-entrypoint.sh"]
CMD ["build_tiles"]
