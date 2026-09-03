#!/usr/bin/env bash
# Deploy the Valhalla router to the VPS. Ships the locally-built Kora fork
# image (see valhalla/fork/ — Kora bicycle costing on pinned upstream), the
# production compose file, and — on request — the prebuilt tiles + elevation
# + admin polygons under valhalla/data/, then restarts the container.
#
# Deliberately separate from the app deploy and from scripts/deploy_motis.sh,
# same model as scripts/deploy_map_assets.sh: tile rebuilds happen locally
# and are only worth publishing when the OSM extract changed.
#
# Roles (the same software / data split as deploy_motis.sh): the dev Mac
# ships the arm64 image when the fork changes; the data-refresh machine
# ships only fresh tiles with --data-only — its local fork image is amd64
# and must never reach the arm64 server. The default deliberately skips
# valhalla/data/: the dev Mac's tiles are usually older than what the data
# machine last deployed, and the data rsync runs with --delete.
#
#   ./scripts/deploy_valhalla.sh              # image + compose, NO data (dev Mac)
#   ./scripts/deploy_valhalla.sh --with-data  # also ship valhalla/data/ (exception)
#   ./scripts/deploy_valhalla.sh --data-only  # data only, no image (data machine)
#
# One-time server prep (see .claude/rules/deployment.md § Valhalla):
#   docker installed and enabled, ga_koramaps in the docker group,
#   /var/www/koramaps.app/valhalla/ owned by ga_koramaps, nginx
#   location /valhalla/ proxying to 127.0.0.1:8002.
#
# Extra arguments are passed through to rsync, e.g.:
#   ./scripts/deploy_valhalla.sh --dry-run
set -euo pipefail

# SSH alias from ~/.ssh/config (ga_koramaps@91.99.74.183 + key).
REMOTE="koramaps"
REMOTE_PATH="/var/www/koramaps.app/valhalla/"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="koramaps/valhalla:bicycle-costing"

DRY_RUN=0
DATA_ONLY=0
WITH_DATA=0
RSYNC_ARGS=()
for a in "$@"; do
	case "$a" in
		--data-only) DATA_ONLY=1 ;;
		--with-data) WITH_DATA=1 ;;
		--dry-run|-n) DRY_RUN=1; RSYNC_ARGS+=("$a") ;;
		*) RSYNC_ARGS+=("$a") ;;
	esac
done

if [ "$DATA_ONLY" -eq 1 ] && [ "$WITH_DATA" -eq 1 ]; then
	echo "error: --data-only and --with-data are mutually exclusive." >&2
	exit 2
fi

# SHIP_DATA: only on explicit request (--with-data / --data-only).
SHIP_DATA=$(( DATA_ONLY || WITH_DATA ))

# Sanity-check the image exists locally before touching the server —
# there's no registry to pull it from. The server is arm64 (Hetzner
# CAX11): shipping an amd64 image would deploy a container that cannot
# start, so refuse it outright (that's what --data-only is for).
if [ "$DATA_ONLY" -eq 0 ]; then
	if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
		echo "error: local image '$IMAGE' not found. Build the fork first:" >&2
		echo "  docker build -t $IMAGE -f valhalla/fork/Dockerfile valhalla/fork" >&2
		echo "or pass --data-only to ship only the tiles." >&2
		exit 1
	fi
	ARCH="$(docker image inspect -f '{{.Architecture}}' "$IMAGE")"
	if [ "$ARCH" != "arm64" ]; then
		echo "error: local '$IMAGE' is $ARCH, but the server is arm64." >&2
		echo "This machine's image is for local use only — deploy data" >&2
		echo "with --data-only, and ship the image from the machine that" >&2
		echo "builds it for arm64." >&2
		exit 1
	fi
fi

# Valhalla memory-maps its tiles; replacing them under a running server
# can fault mid-query. Stop the container first, sync, restart. Skipped
# silently on the first-ever deploy (no compose file on the server yet).
# Only needed when data ships — an image-only deploy just docker-loads
# the new image and `up -d` recreates the container from it.
if [ "$DRY_RUN" -eq 0 ] && [ "$SHIP_DATA" -eq 1 ]; then
	ssh "$REMOTE" "[ -f ${REMOTE_PATH}docker-compose.prod.yml ] && cd $REMOTE_PATH && docker compose -f docker-compose.prod.yml down" || true
fi

# --partial keeps half-transferred files so a dropped connection resumes
# mid-file on rerun instead of restarting the file from zero. --exclude
# keeps the local PBF (used only for tile building) off the server.
if [ "$SHIP_DATA" -eq 1 ]; then
	rsync -avz --partial --delete --exclude='*.pbf' ${RSYNC_ARGS[@]+"${RSYNC_ARGS[@]}"} \
		"$ROOT/valhalla/data/" "$REMOTE:${REMOTE_PATH}data/"
fi
rsync -avz ${RSYNC_ARGS[@]+"${RSYNC_ARGS[@]}"} \
	"$ROOT/valhalla/docker-compose.prod.yml" \
	"$REMOTE:$REMOTE_PATH"

if [ "$DRY_RUN" -eq 0 ]; then
	if [ "$DATA_ONLY" -eq 0 ]; then
		# Stream the built image to the server. `docker save | ssh docker load`
		# is idempotent — repeat deploys skip the image transfer if the tag +
		# digest are unchanged (docker load short-circuits on identical layers).
		echo "shipping $IMAGE to $REMOTE..."
		docker save "$IMAGE" | gzip | ssh "$REMOTE" "gunzip | docker load"
	fi
	# No `pull`: the image is local-only, a pull would fail against the
	# missing registry entry.
	ssh "$REMOTE" "cd $REMOTE_PATH && docker compose -f docker-compose.prod.yml up -d"
fi
