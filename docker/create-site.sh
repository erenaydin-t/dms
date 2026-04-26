#!/usr/bin/env bash
# Provision a fresh site with ERPNext + HRMS + DMS installed.
# Run once after the very first `update-dms.sh`. Re-running on an
# existing site will fail; use `./update-dms.sh` for subsequent updates.
set -euo pipefail

cd "$(dirname "$0")"

if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

SITE_NAME="${SITE_NAME:-dms.localhost}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD:-admin}"

echo "▶ Creating site ${SITE_NAME} ..."
docker compose exec -T backend bench new-site "${SITE_NAME}" \
    --no-mariadb-socket \
    --admin-password "${ADMIN_PASSWORD}" \
    --db-root-password "${DB_ROOT_PASSWORD}" \
    --install-app erpnext \
    --install-app hrms \
    --install-app dms

docker compose exec -T backend bench use "${SITE_NAME}"

echo ""
echo "✔ Site ${SITE_NAME} created."
echo ""
echo "  URL:      http://${SITE_NAME}:${HTTP_PUBLISH_PORT:-8080}"
echo "  Username: Administrator"
echo "  Password: ${ADMIN_PASSWORD}"
echo ""
echo "Add to /etc/hosts so the browser resolves it:"
echo "  127.0.0.1 ${SITE_NAME}"
