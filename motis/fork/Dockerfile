# Kora Maps fork of MOTIS: Valhalla is the sole walking authority (see
# .claude/concepts/valhalla-pedestrian-router.md). The fork overlays:
#   src/compute_footpaths.cc       — import-time transfer table from the
#                                    precomputed Valhalla matrix CSV
#   src/kora_valhalla.cc
#   include/motis/kora_valhalla.h  — query-time Valhalla HTTP client
#   src/endpoints/routing.cc       — WALK offsets (RAPTOR boarding-stop
#                                    selection) via Valhalla one-to-many
#   src/osr/street_routing.cc      — WALK legs (direct, pre/post,
#                                    transfer) via Valhalla /route
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
# No OSR walking fallback anywhere; OSR remains for non-foot profiles.
#
# Build:
#   docker build -t koramaps/motis:footpath-matrix -f motis/fork/Dockerfile motis/fork
#
# Native arm64 build under Ubuntu 24.04. Runs natively on Apple Silicon
# and on the CAX11 (both linux/arm64). Upstream's own `linux-arm64-release`
# preset cross-compiles from x86_64 inside a specialised docker-cpp-build
# image; running that under Rosetta segfaults inside GCC. Building natively
# with the distro toolchain is slower to set up but reliable, and the
# output binary is the same shape (linux/arm64 ELF).
#
# Two-stage build pattern for fast fork iterations:
#   1. Layer A compiles the UNMODIFIED upstream MOTIS (60-90 min once,
#      Docker caches it — invalidated only when MOTIS_REF changes).
#   2. COPY the fork's src/ + include/ overlays, re-run cmake --build.
#      Ninja recompiles only the overlaid files + the link
#      (~5-10 min per iteration on the fork).
#
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

FROM ubuntu:24.04 AS build
ARG MOTIS_REF

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc-14 g++-14 \
        cmake \
        ninja-build \
        mold \
        git \
        curl \
        ca-certificates \
        pkg-config \
        python3 \
        libatomic1 \
    && rm -rf /var/lib/apt/lists/*

RUN update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-14 100 && \
    update-alternatives --install /usr/bin/g++ g++ /usr/bin/g++-14 100

RUN git config --global --add safe.directory '*'

WORKDIR /
RUN git clone https://github.com/motis-project/motis.git src && \
    cd src && git checkout "$MOTIS_REF"

WORKDIR /src

# --- Baseline build (cached across fork iterations) --------------------
# Configure + fully compile the UNMODIFIED motis. This layer is expensive
# (~60-90 min) but only invalidates when MOTIS_REF or the base toolchain
# changes. The intermediate .o files stay in /src/build/ so the next
# layer's ninja incremental logic reuses them.
# -j4: ninja's default (-j nproc) OOM-kills cc1plus — several of the
# template-heavy TUs (osr route.cc, routing.cc) peak at 2-3 GB each,
# and 6 in parallel exceed Docker Desktop's memory allowance.
RUN cmake -G Ninja -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_COMPILER=gcc \
        -DCMAKE_CXX_COMPILER=g++ \
        -DCMAKE_EXE_LINKER_FLAGS="-fuse-ld=mold" \
        -DMOTIS_MIMALLOC=ON && \
    cmake --build build --target motis -- -j4

# --- Fork overlay (fast per iteration) ---------------------------------
# Overlay the fork's files (new ones land next to upstream's; patched
# ones replace their upstream copies), bump mtimes so ninja definitely
# sees them as newer than the cached .o files, then re-run the build.
# The CMake file glob (src/*.cc) picks up new sources automatically;
# `Re-checking globbed directories` in the log confirms it.
COPY include/ /src/include/
COPY src/ /src/src/
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
        /src/src/compute_footpaths.cc \
        /src/src/kora_valhalla.cc \
        /src/src/server.cc \
        /src/src/endpoints/routing.cc \
        /src/src/osr/street_routing.cc \
        /src/deps/nigiri/src/routing/raptor/optimize_footpaths.cc \
        /src/deps/nigiri/src/routing/raptor/reconstruct.cc \
        /src/deps/nigiri/src/routing/raptor/pong.cc \
        /src/deps/nigiri/include/nigiri/routing/query.h \
        /src/deps/nigiri/include/nigiri/routing/journey.h \
        /src/deps/nigiri/include/nigiri/routing/search.h \
        /src/deps/nigiri/include/nigiri/routing/kora_alternatives.h && \
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
