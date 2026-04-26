#!/usr/bin/env bash
# Build the custom-erpnext image with the latest DMS code, restart the
# stack, and migrate the site. Idempotent: safe to re-run after every
# code change.
set -euo pipefail

cd "$(dirname "$0")"

# Load .env so SITE_NAME / DMS_BRANCH / etc. are available.
if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

SITE_NAME="${SITE_NAME:-dms.localhost}"
CUSTOM_IMAGE="${CUSTOM_IMAGE:-custom-erpnext}"
CUSTOM_TAG="${CUSTOM_TAG:-latest}"

echo "╔══════════════════════════════════════════════════════╗"
echo "║           DMS Update Script — Espad Pharmed          ║"
echo "╚══════════════════════════════════════════════════════╝"

echo ""
echo "▶ [1/4] Building image ${CUSTOM_IMAGE}:${CUSTOM_TAG} from apps.json..."
DOCKER_BUILDKIT=1 docker build \
    --secret id=apps_json,src=apps.json \
    --build-arg CACHEBUST="$(date +%s)" \
    --tag "${CUSTOM_IMAGE}:${CUSTOM_TAG}" \
    --file Dockerfile .
echo "✔ Image built."

echo ""
echo "▶ [2/4] Bringing up the stack..."
docker compose up -d --remove-orphans
echo "✔ Stack is up."

echo ""
echo "▶ [3/4] Waiting for backend to be ready..."
for i in $(seq 1 60); do
    if docker compose exec -T backend bench --version >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
echo "✔ Backend is ready."

echo ""
echo "▶ [4/4] Running migrate and clearing cache on ${SITE_NAME}..."
if docker compose exec -T backend bash -c "ls sites/${SITE_NAME} >/dev/null 2>&1"; then
    docker compose exec -T backend bench --site "${SITE_NAME}" migrate
    docker compose exec -T backend bench --site "${SITE_NAME}" clear-cache
    echo "✔ Migration complete."
else
    echo "⚠ Site ${SITE_NAME} does not exist yet."
    echo "  Run ./create-site.sh to provision it."
fi

echo ""
echo "✔ Done. Site: http://${SITE_NAME}:${HTTP_PUBLISH_PORT:-8080}"
