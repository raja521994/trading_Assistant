#!/usr/bin/env bash
# Runs on the EC2 host (as root, via SSM Run Command) for each release extracted by the pipeline.
# Usage: ec2-deploy.sh <release-dir>
set -euo pipefail

RELEASE_DIR="$1"
APP_DIR=/opt/trading
DATA_DIR="$APP_DIR/data"
PARAM_PREFIX="${PARAM_PREFIX:-/trading-dashboard}"
APP_UID=10001  # matches the "app" user in backend/Dockerfile

mkdir -p "$DATA_DIR/var"
if [ ! -f "$DATA_DIR/backend.env" ]; then
  cp "$RELEASE_DIR/deploy/backend.env.prod" "$DATA_DIR/backend.env"
fi
chown "$APP_UID" "$DATA_DIR/backend.env"
chmod 600 "$DATA_DIR/backend.env"
chown -R "$APP_UID" "$DATA_DIR/var"

BASIC_AUTH_USER=$(aws ssm get-parameter --name "$PARAM_PREFIX/basic-auth-user" --query Parameter.Value --output text)
BASIC_AUTH_PASSWORD=$(aws ssm get-parameter --name "$PARAM_PREFIX/basic-auth-password" --with-decryption \
  --query Parameter.Value --output text)
export DATA_DIR BASIC_AUTH_USER BASIC_AUTH_PASSWORD

COMPOSE=(docker compose -p trading -f "$RELEASE_DIR/deploy/docker-compose.prod.yml")
"${COMPOSE[@]}" build
"${COMPOSE[@]}" up -d --remove-orphans
ln -sfn "$RELEASE_DIR" "$APP_DIR/current"

echo "Waiting for the dashboard to become healthy..."
for _ in $(seq 1 60); do
  if [ "$(docker inspect -f '{{.State.Health.Status}}' trading-backend-1 2>/dev/null)" = "healthy" ] \
     && curl -fsS http://127.0.0.1/healthz >/dev/null; then
    echo "Deployed $(basename "$RELEASE_DIR")"
    ls -1dt "$APP_DIR"/releases/*/ | tail -n +6 | xargs -r rm -rf
    docker image prune -f >/dev/null
    exit 0
  fi
  sleep 5
done

echo "Dashboard did not become healthy in time" >&2
"${COMPOSE[@]}" ps >&2
"${COMPOSE[@]}" logs --tail 100 backend >&2
exit 1
