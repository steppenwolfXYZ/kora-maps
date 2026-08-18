#!/usr/bin/env bash
# Deploy the MOTIS routing backend to the VPS. Ships the prebuilt indexes
# (motis/data/, imported locally — see motis/docker-compose.yml), the
# config, the production compose file, and the locally-built forked
# MOTIS image (see motis/fork/), then restarts the container.
#
# Deliberately separate from the GitHub Actions app deploy, same model
# as scripts/deploy_map_assets.sh and scripts/deploy_valhalla.sh:
# re-imports happen locally and not every pipeline run produces a
# publishable routing dataset.
#
# One-time server prep (see .claude/rules/deployment.md): docker installed
# and enabled, ga_koramaps in the docker group, /var/www/koramaps.app/motis/
# owned by ga_koramaps, nginx location /routing/ proxying to 127.0.0.1:8080.
# The forked image has no registry, so it ships as a docker save tarball
# streamed over SSH — no GHCR / registry setup needed.
#
# Extra arguments are passed through to rsync, e.g.:
#   ./scripts/deploy_motis.sh --dry-run
set -euo pipefail

# SSH alias from ~/.ssh/config (ga_koramaps@91.99.74.183 + key).
REMOTE="koramaps"
REMOTE_PATH="/var/www/koramaps.app/motis/"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="koramaps/motis:footpath-matrix"

DRY_RUN=0
for a in "$@"; do
	{ [ "$a" = "--dry-run" ] || [ "$a" = "-n" ]; } && DRY_RUN=1 || true
done

# Sanity-check the image exists locally before touching the server —
# there's no upstream to pull it from.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
	echo "error: local image '$IMAGE' not found. Build the fork first:" >&2
	echo "  docker build -t $IMAGE -f motis/fork/Dockerfile motis/fork" >&2
	exit 1
fi

# MOTIS memory-maps its index files; replacing them under a running server
# can fault mid-query. Stop the container first, sync, restart. Skipped
# silently on the first-ever deploy (no compose file on the server yet).
if [ "$DRY_RUN" -eq 0 ]; then
	ssh "$REMOTE" "[ -f ${REMOTE_PATH}docker-compose.prod.yml ] && cd $REMOTE_PATH && docker compose -f docker-compose.prod.yml down" || true
fi

# --partial keeps half-transferred files so a dropped connection resumes
# mid-file on rerun instead of restarting the file from zero.
rsync -avz --partial --delete "$@" "$ROOT/motis/data/" "$REMOTE:${REMOTE_PATH}data/"
rsync -avz "$@" \
	"$ROOT/motis/config.yml" \
	"$ROOT/motis/docker-compose.prod.yml" \
	"$REMOTE:$REMOTE_PATH"

if [ "$DRY_RUN" -eq 0 ]; then
	# Stream the built image to the server. `docker save | ssh docker load`
	# is idempotent — repeat deploys skip the image transfer if the tag +
	# digest are unchanged (docker load short-circuits on identical layers).
	# Compressed size is ~250 MB — one-off, then only incremental on rebuild.
	echo "shipping $IMAGE to $REMOTE..."
	docker save "$IMAGE" | gzip | ssh "$REMOTE" "gunzip | docker load"
	ssh "$REMOTE" "cd $REMOTE_PATH && docker compose -f docker-compose.prod.yml up -d"
fi
