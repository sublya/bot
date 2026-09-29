#!/usr/bin/env bash
# Builds the bot image locally and ships it to the server over ssh: the server has 1 vCPU
# and doesn't build anything itself. The server's .env is created once by hand, from
# .env.example, and never overwritten.
#
# Usage: deploy/deploy.sh
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
host="${SUBLYA_HOST:-root@skator.ru}"
remote="${SUBLYA_REMOTE_DIR:-/opt/sublya/bot}"
image="sublya-bot:latest"

# the server is amd64, while the build usually runs on an arm64 Mac
docker build --platform linux/amd64 -t "$image" "$root"
docker save "$image" | gzip | ssh "$host" 'gunzip | docker load'

ssh "$host" "mkdir -p $remote/data"
scp "$root/deploy/docker-compose.yml" "$host:$remote/"
if ! ssh "$host" "test -f $remote/.env"; then
	echo "no $remote/.env on the server: fill one in from .env.example" >&2
	exit 1
fi
ssh "$host" "cd $remote && docker compose up -d --remove-orphans && docker image prune -f"

sleep 5
ssh "$host" "cd $remote && docker compose ps && docker compose logs --tail 5 bot"
