# Kora Maps fork of MOTIS: Valhalla is the sole walking authority (see
# .claude/concepts/valhalla-pedestrian-router.md). The fork overlays:
#   src/compute_footpaths.cc       — import-time transfer table from the
#                                    precomputed Valhalla matrix CSV,
#                                    floored per pair by the feed's own
#                                    minimum transfer times
#                                    (transfer-point-optimization.md
#                                    § Minimum transfer time)
#   src/kora_valhalla.cc
#   include/motis/kora_valhalla.h  — query-time Valhalla HTTP client
#   src/endpoints/routing.cc       — WALK offsets (RAPTOR boarding-stop
#                                    selection) via Valhalla one-to-many;
#                                    koraMinWalkFrom/To response fields
#                                    (comfort-walk-baseline.md)
#   src/osr/street_routing.cc      — WALK legs (direct, pre/post,
#                                    transfer) via Valhalla /route;
#                                    leg.duration is Valhalla's walking
#                                    time, not the leg's time span
#                                    (routing-options.md § Connection
#                                    safety)
#   include/motis/osr/parameters.h — kora_walk_factor_: query-scoped
#                                    walking-speed scaling of the
#                                    Valhalla surfaces (routing-options.md)
#   openapi.yaml                   — Leg.elevationUp / Leg.elevationDown:
#                                    walk ascent / descent sampled from
#                                    Valhalla's elevation profile
#                                    (transit-routing.md § Walk elevation);
#                                    plan koraMinWalkFrom / koraMinWalkTo
#                                    (comfort-walk-baseline.md)
#   src/server.cc                  — startup probe: no Valhalla, no serve
#   deps/nigiri/src/routing/raptor/optimize_footpaths.cc
#                                  — same-stop re-boarding as a transfer-
#                                    optimizer candidate (see
#                                    transfer-point-optimization.md)
#   deps/nigiri/include/nigiri/routing/{query.h,journey.h,search.h,
#                                       kora_alternatives.h}
#   deps/nigiri/src/routing/raptor/{reconstruct.cc,pong.cc}
#                                  — ε-alternates: near-optimal egress/
#                                    access variants read out of the
#                                    finished RAPTOR/PONG state (see
#                                    near-optimal-endpoint-alternatives.md)
#   deps/nigiri/include/nigiri/routing/kora_walk_points.h (new)
#   deps/nigiri/src/routing/raptor_search.cc
#   deps/nigiri/src/routing/raptor/raptor_state.cc
#                                  — kMaxVias raised from 2 to 3
#                                    (via-stops.md). Every site that
#                                    switches on the via count carries a
#                                    static_assert on kMaxVias, so all
#                                    four dispatchers (these two plus
#                                    reconstruct.cc and pong.cc) are
#                                    overlaid to add the third case.
#   deps/nigiri/include/nigiri/routing/limits.h
#   deps/nigiri/include/nigiri/routing/raptor/raptor.h
#                                  — walk-weighted transfer points:
#                                    RAPTOR's discrete criterion counts
#                                    points (boarding cost scales with
#                                    the preceding walk's class) instead
#                                    of plain boardings (see
#                                    fix-long-transfer-walks.md). Touches
#                                    limits.h, so a fork iteration after
#                                    this change rebuilds most of nigiri
#                                    (~15-25 min instead of ~5-10).
#   deps/nigiri/include/nigiri/types.h
#                                  — kKoraFullTransferProfile: dedicated
#                                    profile slot for the full 2-h
#                                    transfer table (kNProfiles 5 -> 6).
#                                    The bike slot it previously borrowed
#                                    loses lb-graph transit edges for
#                                    routes without bikes_allowed, which
#                                    made RAPTOR overprune. types.h is
#                                    included by everything, so a fork
#                                    iteration after this change is a
#                                    near-full rebuild (~60-90 min).
#   include/motis/hashes.h         — nigiri_bin_ver 37 -> 1037: the
#                                    kNProfiles bump changes the
#                                    timetable layout; the version bump
#                                    makes the import re-run tt +
#                                    osr_footpath instead of serving
#                                    pre-bump indexes.
# No OSR walking fallback anywhere; OSR remains for non-foot profiles.
#
# Build (both steps; scripts/routing/setup_routing.sh step 2 does this):
#   1. the BASELINE image — toolchain + upstream MOTIS at MOTIS_REF + all
#      dependency clones + the full unmodified compile (60-90 min), once
#      per machine and per pin, from Dockerfile.baseline:
#        docker build --build-arg MOTIS_REF=<sha> -t koramaps/motis-baseline:<sha> \
#            -f motis/fork/Dockerfile.baseline motis/fork
#   2. this file — FROM that image, COPY the fork's overlays, re-run
#      cmake --build; ninja recompiles only the overlaid files + the link
#      (~5-10 min per iteration on the fork):
#        docker build --build-arg MOTIS_REF=<sha> -t koramaps/motis:footpath-matrix \
#            -f motis/fork/Dockerfile motis/fork
# The baseline is a tagged IMAGE, not a cached layer, so BuildKit's cache
# GC can never evict it (that eviction once re-fired all 30 dependency
# clones on every retry until GitHub rate-limited the machine). It is
# removed only by an explicit `docker image rm` / `prune -a`.
#
# Native arm64 build under Ubuntu 24.04 (see Dockerfile.baseline).
# Docker Desktop needs ≥6 GB memory allocated or the link step OOMs.
#
# MOTIS_REF is PINNED to the commit the fork's full-file overlays are
# based on (src/endpoints/routing.cc, src/osr/street_routing.cc,
# src/server.cc are patched COPIES of upstream files — building against
# a moved master would silently revert upstream changes in those files).
# To bump: diff upstream's versions of every overlaid file against the
# pin, re-apply the kora patches (marked "kora fork:" comments), update
# this ARG. See README.md for the checklist.
ARG MOTIS_REF=061857d38a375248ae62b4ce722753368a75e474

# The baseline image carries the toolchain, the clone at MOTIS_REF, the
# dependency checkouts and the compiled /src/build (Dockerfile.baseline).
# Its tag IS the pin, so a MOTIS_REF bump can never build on a stale
# baseline. WORKDIR /src is inherited from it.
FROM koramaps/motis-baseline:${MOTIS_REF} AS build

# --- Fork overlay (fast per iteration) ---------------------------------
# Overlay the fork's files (new ones land next to upstream's; patched
# ones replace their upstream copies), bump mtimes so ninja definitely
# sees them as newer than the cached .o files, then re-run the build.
# The CMake file glob (src/*.cc) picks up new sources automatically;
# `Re-checking globbed directories` in the log confirms it.
COPY include/ /src/include/
COPY src/ /src/src/
# The API schema drives codegen (openapi_generate -> motis-api). It is
# deliberately ABSENT from the touch list below: re-touching it on every
# iteration would regenerate motis-api.h and recompile nearly every TU
# (~full rebuild each time). COPY preserves the working tree's mtime,
# which is newer than the baseline layer's generated header, so a real
# edit still triggers regeneration. Caveat for a MOTIS_REF bump: re-diff
# and re-save this file so its mtime lands after the new baseline layer.
COPY openapi.yaml /src/openapi.yaml
# deps/ overlays land INSIDE dependency checkouts that the baseline
# configure step populated (e.g. deps/nigiri/) — nigiri is pinned
# transitively via MOTIS's .pkg, so these are full-file copies of that
# pinned commit's files, same rules as the MOTIS overlays above.
COPY deps/ /src/deps/
# Touch exactly the overlaid files (a blanket find-touch would mark every
# upstream source dirty and re-trigger the full 90-min build). ADD NEW
# OVERLAY FILES HERE when the fork grows.
# The explicit `cmake -S . -B build` re-configure is REQUIRED: upstream's
# `file(GLOB_RECURSE motislib-files src/*.cc)` has no CONFIGURE_DEPENDS,
# so the source list is frozen at configure time — without the re-run,
# NEW fork files (kora_valhalla.cc) never enter the build and the link
# fails with undefined kora_valhalla symbols. Cached CMake vars make the
# re-configure fast (~1-2 min).
RUN touch -c \
        /src/include/motis/kora_valhalla.h \
        /src/include/motis/hashes.h \
        /src/include/motis/osr/parameters.h \
        /src/src/compute_footpaths.cc \
        /src/src/kora_valhalla.cc \
        /src/src/server.cc \
        /src/src/endpoints/routing.cc \
        /src/src/osr/street_routing.cc \
        /src/deps/nigiri/src/routing/raptor_search.cc \
        /src/deps/nigiri/src/routing/raptor/optimize_footpaths.cc \
        /src/deps/nigiri/src/routing/raptor/raptor_state.cc \
        /src/deps/nigiri/src/routing/raptor/reconstruct.cc \
        /src/deps/nigiri/src/routing/raptor/pong.cc \
        /src/deps/nigiri/include/nigiri/routing/query.h \
        /src/deps/nigiri/include/nigiri/routing/journey.h \
        /src/deps/nigiri/include/nigiri/routing/search.h \
        /src/deps/nigiri/include/nigiri/routing/kora_alternatives.h \
        /src/deps/nigiri/include/nigiri/routing/kora_walk_points.h \
        /src/deps/nigiri/include/nigiri/routing/transfer_time_settings.h \
        /src/deps/nigiri/include/nigiri/routing/limits.h \
        /src/deps/nigiri/include/nigiri/routing/raptor/raptor.h \
        /src/deps/nigiri/include/nigiri/types.h && \
    cmake -S . -B build && \
    cmake --build build --target motis -- -j4

# Runtime image — Ubuntu 24.04 arm64 so the glibc-linked binary works.
# (Upstream MOTIS ships alpine + musl; that requires the musl cross-
# toolchain we skipped, so we mirror the build's glibc here.)
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        libatomic1 \
    && rm -rf /var/lib/apt/lists/* && \
    groupadd -r motis && useradd -r -g motis -d /data motis && \
    mkdir /data && chown motis:motis /data

COPY --from=build /src/build/motis /motis

EXPOSE 8080
VOLUME ["/data"]
USER motis
CMD ["/motis", "server", "/data"]
