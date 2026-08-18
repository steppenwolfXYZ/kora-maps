# Kora Maps fork of MOTIS: rebuilds the upstream binary with a single
# file replaced (src/compute_footpaths.cc — reads a Valhalla-precomputed
# footpath matrix instead of computing footpaths from OSR at import
# time; see .claude/concepts/valhalla-pedestrian-router.md).
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
#   2. COPY fork/src/compute_footpaths.cc, then re-run cmake --build.
#      Ninja detects only that one .cc changed and rebuilds just it +
#      the link (~2-5 min per iteration on the fork).
#
# Docker Desktop needs ≥6 GB memory allocated or the link step OOMs.
#
# Pin MOTIS to a known-good ref by overriding MOTIS_REF. When bumping
# it, verify the upstream compute_footpaths.cc signature still matches
# motis/fork/src/compute_footpaths.cc — if the parameter list changed
# upstream, the fork's function definition must follow.
ARG MOTIS_REF=master

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
RUN cmake -G Ninja -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_COMPILER=gcc \
        -DCMAKE_CXX_COMPILER=g++ \
        -DCMAKE_EXE_LINKER_FLAGS="-fuse-ld=mold" \
        -DMOTIS_MIMALLOC=ON && \
    cmake --build build --target motis

# --- Fork overlay (fast per iteration) ---------------------------------
# Replace the single file, touch its mtime forward so ninja definitely
# sees it as newer than the cached .o, then re-run the build. Ninja
# compiles just compute_footpaths.cc and re-links motis (~2-5 min).
COPY src/compute_footpaths.cc /src/src/compute_footpaths.cc
RUN touch -c /src/src/compute_footpaths.cc && \
    cmake --build build --target motis

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
