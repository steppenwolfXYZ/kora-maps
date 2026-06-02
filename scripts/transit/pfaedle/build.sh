#!/usr/bin/env bash
# Build the pfaedle Docker image used by the transit pipeline.
# Run from the project root: ./scripts/transit/pfaedle/build.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="carfree-pfaedle:latest"

echo "Building $IMAGE from $HERE/Dockerfile ..."
docker build -t "$IMAGE" "$HERE"
echo "Done. Image: $IMAGE"
