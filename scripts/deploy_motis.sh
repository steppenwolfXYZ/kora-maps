#!/usr/bin/env bash
# Deploy the MOTIS routing backend to the VPS. Ships the prebuilt indexes
# (motis/data/, imported locally — see motis/docker-compose.yml), the config
# and the production compose file, then restarts the container.
#
# Deliberately separate from the GitHub Actions app deploy, same model as
# scripts/deploy_map_assets.sh: re-imports happen locally and not every
# pipeline run produces a publishable routing dataset.
#
# One-time server prep (see .claude/rules/deployment.md): docker installed
# and enabled, ga_koramaps in the docker group, /var/www/koramaps.app/motis/
# owned by ga_koramaps, nginx location /routing/ proxying to 127.0.0.1:8080.
#
# Extra arguments are passed through to rsync, e.g.:
#   ./scripts/deploy_motis.sh --dry-run
set -euo pipefail

# SSH alias from ~/.ssh/config (ga_koramaps@91.99.74.183 + key).
REMOTE="koramaps"
REMOTE_PATH="/var/www/koramaps.app/motis/"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

DRY_RUN=0
for a in "$@"; do
	{ [ "$a" = "--dry-run" ] || [ "$a" = "-n" ]; } && DRY_RUN=1 || true
done

# MOTIS memory-maps its index files; replacing them under a running server
# can fault mid-query. Stop the container first, sync, restart. The down is
# tolerated to fail so a first-ever deploy (no compose file on the server
# yet) still proceeds.
if [ "$DRY_RUN" -eq 0 ]; then
	ssh "$REMOTE" "cd $REMOTE_PATH 2>/dev/null && docker compose -f docker-compose.prod.yml down" || true
fi

rsync -avz --delete "$@" "$ROOT/motis/data/" "$REMOTE:${REMOTE_PATH}data/"
rsync -avz "$@" \
	"$ROOT/motis/config.yml" \
	"$ROOT/motis/docker-compose.prod.yml" \
	"$REMOTE:$REMOTE_PATH"

if [ "$DRY_RUN" -eq 0 ]; then
	ssh "$REMOTE" "cd $REMOTE_PATH && docker compose -f docker-compose.prod.yml pull && docker compose -f docker-compose.prod.yml up -d"
fi
