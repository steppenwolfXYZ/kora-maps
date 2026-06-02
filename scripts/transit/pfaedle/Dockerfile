# ─────────────────────────────────────────────────────────────────────────────
# Pfaedle container
#
# Builds pfaedle (https://github.com/ad-freiburg/pfaedle) from source at a
# pinned commit, plus osmium-tool for OSM PBF manipulation. Both are exposed
# as CLI tools; the runtime image is small.
#
# Build:
#   docker build -t carfree-pfaedle:latest scripts/transit/pfaedle/
# Run:
#   docker run --rm -v "$PWD/data:/data" carfree-pfaedle:latest pfaedle ...
# ─────────────────────────────────────────────────────────────────────────────

# ── Build stage ─────────────────────────────────────────────────────────────
FROM debian:bookworm-slim AS build

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
        build-essential cmake \
        libboost-dev libboost-system-dev libboost-filesystem-dev \
        libbz2-dev zlib1g-dev libexpat1-dev \
        libprotozero-dev \
    && rm -rf /var/lib/apt/lists/*

# Pin to a specific pfaedle revision for reproducible builds.
# To update: bump PFAEDLE_REF and rebuild.
ARG PFAEDLE_REF=master

RUN git clone --recursive https://github.com/ad-freiburg/pfaedle.git /src/pfaedle \
    && cd /src/pfaedle \
    && git checkout "${PFAEDLE_REF}" \
    && git submodule update --init --recursive

RUN mkdir /src/pfaedle/build \
    && cd /src/pfaedle/build \
    && cmake .. -DCMAKE_BUILD_TYPE=Release \
    && make -j"$(nproc)" \
    && make install

# ── Runtime stage ───────────────────────────────────────────────────────────
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        libboost-system1.74.0 libboost-filesystem1.74.0 \
        libbz2-1.0 zlib1g libexpat1 \
        osmium-tool \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/bin/pfaedle /usr/local/bin/pfaedle
COPY --from=build /usr/local/etc/pfaedle /usr/local/etc/pfaedle

WORKDIR /data

# Default entrypoint is a no-op shell; caller picks the tool (pfaedle / osmium).
ENTRYPOINT []
CMD ["/bin/sh", "-c", "echo 'carfree-pfaedle: provide a command (pfaedle / osmium / ...)'"]
