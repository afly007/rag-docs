#!/usr/bin/env bash
# Run on the server to pull the latest images and restart services.
# Triggered automatically by the release workflow, or run manually:
#   ssh user@server '~/rag-docs/scripts/deploy.sh'

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Pulling latest code"
cd "$REPO_DIR"
git pull origin main

echo "==> Pulling latest images"
docker compose pull

echo "==> Restarting services"
docker compose up -d

echo "==> Cleaning up old images"
docker image prune -f

echo "==> Done"
docker compose ps
