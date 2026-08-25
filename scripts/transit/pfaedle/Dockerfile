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

# Kora patch: drop `psv=yes` from the bus profile's fast class (osm_filter_lvl2
# in [bus, coach]). A bare psv=yes only records that public-service vehicles are
# *permitted* — on pedestrian zones it would otherwise outrank
# highway=pedestrian and turn them into 55 km/h shortcuts (canonical case:
# Langenthal Marktgasse, buses 52/64 drawn through the pedestrian zone instead
# of the real Farbgasse route). Dedicated bus infrastructure keeps its
# psv=designated / bus=yes fast-class entries. The line-count check makes an
# upstream config reshuffle fail the build instead of silently losing the patch.
RUN cfg=/usr/local/etc/pfaedle/pfaedle.cfg \
    && awk 'BEGIN{sec="";blk=""} /^\[/{sec=$0} /^[a-z0-9_]+:/{blk=$0} !(sec=="[bus, coach]" && blk ~ /^osm_filter_lvl2:/ && $0 ~ /^[ \t]*psv=yes[ \t]*$/)' "$cfg" > "$cfg.patched" \
    && [ "$(wc -l < "$cfg.patched")" -eq "$(( $(wc -l < "$cfg") - 1 ))" ] \
    && mv "$cfg.patched" "$cfg"

WORKDIR /data

# Default entrypoint is a no-op shell; caller picks the tool (pfaedle / osmium).
ENTRYPOINT []
CMD ["/bin/sh", "-c", "echo 'carfree-pfaedle: provide a command (pfaedle / osmium / ...)'"]
