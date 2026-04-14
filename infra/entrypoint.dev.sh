#!/usr/bin/env bash
set -euo pipefail

if [ "${DJANGO_SETTINGS_MODULE:-}" = "config.settings.dev" ]; then
    echo "==> Running Django migrations..."
    uv run python manage.py migrate --no-input

    echo "==> Seeding dev data (superuser + fixtures)..."
    uv run python manage.py seed_dev_data

    echo "==> Seeding exchange rates (public API, non-Stripe)..."
    uv run python manage.py seed_exchange_rates || echo "  (non-fatal: exchange rate seed failed)"
fi

echo "==> Starting Django dev server..."
exec uv run uvicorn config.asgi:application \
    --host 0.0.0.0 \
    --port "${DJANGO_PORT:-8001}" \
    --log-config /app/infra/uvicorn-log-config.json \
    --reload
