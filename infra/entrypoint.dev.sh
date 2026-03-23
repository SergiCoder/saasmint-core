#!/usr/bin/env bash
set -euo pipefail

if [ "${DJANGO_SETTINGS_MODULE:-}" = "config.settings.dev" ]; then
    echo "==> Running Django migrations..."
    uv run python manage.py migrate --no-input

    echo "==> Seeding dev data (superuser + fixtures)..."
    uv run python manage.py seed_dev_data
fi

echo "==> Starting Django dev server..."
exec uv run uvicorn config.asgi:application \
    --host 0.0.0.0 \
    --port "${DJANGO_PORT:-8001}" \
    --reload
