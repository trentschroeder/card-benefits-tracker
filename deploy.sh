#!/usr/bin/env bash
# Pull-based deploy. Invoked by card-benefits-deploy.timer every couple of
# minutes. Fetches origin/main and — only when HEAD would actually change —
# fast-forwards the working tree, installs deps, and restarts the service.
# A no-op (exit 0, no restart) when there's nothing new.
#
# This replaces the GitHub Actions SSH push, which can't reach the box:
# the provider's edge silently drops inbound :22 from datacenter (Azure)
# ranges, so runners time out. Here the VPS dials *out* to GitHub over 443,
# the path we confirmed works, so the filter is irrelevant.
set -euo pipefail

REPO=/var/www/credit-card-benefits
BRANCH=main
SERVICE=credit-card-benefits

cd "$REPO"

git fetch --quiet origin "$BRANCH"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

echo "Deploying $LOCAL -> $REMOTE"
git reset --hard "origin/$BRANCH"
./venv/bin/pip install --quiet -r requirements.txt
systemctl restart "$SERVICE"
systemctl is-active --quiet "$SERVICE"
echo "Deploy OK $(date -u +%FT%TZ) — $REMOTE"
