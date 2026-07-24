#!/usr/bin/env bash
# Deploy map assets (pmtiles, style.json, fonts, search/line indexes) to the
# VPS. Deliberately separate from the GitHub Actions app deploy: pipeline
# runs happen locally and not every run produces a publishable result — run
# this manually once the current data looks good.
#
# Extra arguments are passed through to rsync, e.g.:
#   ./scripts/deploy_map_assets.sh --dry-run
set -euo pipefail

# SSH alias from ~/.ssh/config (ga_koramaps@91.99.74.183 + key).
REMOTE="koramaps"
REMOTE_PATH="/var/www/koramaps.app/map-assets/"
SRC="$(cd "$(dirname "$0")/.." && pwd)/static/map-assets/"

# Allowlist: style + indexes, glyph fonts, and the tile bundles the style
# references. Debug bundles and legacy/stray files never leave the machine.
rsync -avz --delete \
	--exclude 'tl_debug_*' \
	--include '*.json' \
	--include 'fonts/***' \
	--include 'tl_*.pmtiles' \
	--exclude '*' \
	"$@" \
	"$SRC" "$REMOTE:$REMOTE_PATH"
