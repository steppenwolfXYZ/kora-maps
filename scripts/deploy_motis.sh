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
# Roles (see the Mac/data-machine split): the dev Mac ships the arm64
# image when the fork changes; the data-refresh machine ships only fresh
# indexes with --data-only — its local fork image is amd64 (import only)
# and must never reach the arm64 server.
#
#   ./scripts/deploy_motis.sh              # image + data (dev Mac)
#   ./scripts/deploy_motis.sh --data-only  # data only (data machine)
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
DATA_ONLY=0
RSYNC_ARGS=()
for a in "$@"; do
	case "$a" in
		--data-only) DATA_ONLY=1 ;;
		--dry-run|-n) DRY_RUN=1; RSYNC_ARGS+=("$a") ;;
		*) RSYNC_ARGS+=("$a") ;;
	esac
done

# Sanity-check the image exists locally before touching the server —
# there's no upstream to pull it from. The server is arm64 (Hetzner
# CAX11): shipping an amd64 image would deploy a container that cannot
# start, so refuse it outright (that's what --data-only is for).
if [ "$DATA_ONLY" -eq 0 ]; then
	if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
		echo "error: local image '$IMAGE' not found. Build the fork first:" >&2
		echo "  docker build -t $IMAGE -f motis/fork/Dockerfile motis/fork" >&2
		echo "or pass --data-only to ship only the imported indexes." >&2
		exit 1
	fi
	ARCH="$(docker image inspect -f '{{.Architecture}}' "$IMAGE")"
	if [ "$ARCH" != "arm64" ]; then
		echo "error: local '$IMAGE' is $ARCH, but the server is arm64." >&2
		echo "This machine's image is for local imports only — deploy data" >&2
		echo "with --data-only, and ship the image from the machine that" >&2
		echo "builds it for arm64." >&2
		exit 1
	fi
fi

# MOTIS memory-maps its index files; replacing them under a running server
# can fault mid-query. Stop the container first, sync, restart. Skipped
# silently on the first-ever deploy (no compose file on the server yet).
if [ "$DRY_RUN" -eq 0 ]; then
	ssh "$REMOTE" "[ -f ${REMOTE_PATH}docker-compose.prod.yml ] && cd $REMOTE_PATH && docker compose -f docker-compose.prod.yml down" || true
fi

# --partial keeps half-transferred files so a dropped connection resumes
# mid-file on rerun instead of restarting the file from zero.
rsync -avz --partial --delete ${RSYNC_ARGS[@]+"${RSYNC_ARGS[@]}"} "$ROOT/motis/data/" "$REMOTE:${REMOTE_PATH}data/"
rsync -avz ${RSYNC_ARGS[@]+"${RSYNC_ARGS[@]}"} \
	"$ROOT/motis/config.yml" \
	"$ROOT/motis/docker-compose.prod.yml" \
	"$REMOTE:$REMOTE_PATH"

if [ "$DRY_RUN" -eq 0 ]; then
	if [ "$DATA_ONLY" -eq 0 ]; then
		# Stream the built image to the server. `docker save | ssh docker load`
		# is idempotent — repeat deploys skip the image transfer if the tag +
		# digest are unchanged (docker load short-circuits on identical layers).
		# Compressed size is ~250 MB — one-off, then only incremental on rebuild.
		echo "shipping $IMAGE to $REMOTE..."
		docker save "$IMAGE" | gzip | ssh "$REMOTE" "gunzip | docker load"
	fi
	ssh "$REMOTE" "cd $REMOTE_PATH && docker compose -f docker-compose.prod.yml up -d"
fi
