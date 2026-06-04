#!/usr/bin/env bash
set -euo pipefail

echo "==> Running Django migrations..."
uv run python manage.py migrate --no-input

echo "==> Seeding catalog (plans, products)..."
uv run python manage.py seed_catalog

# sync_localized_prices runs before sync_stripe_catalog because the latter
# reads LocalizedPrice.amount_minor when minting Stripe Prices for non-USD
# billing currencies. If the FX feed is unreachable, sync_stripe_catalog has
# its own bootstrap path (calls sync_localized_prices inline once) and skips
# any currencies that still have no localized row.
echo "==> Syncing localized prices from FX feed..."
uv run python manage.py sync_localized_prices || echo "  (non-fatal: localized price sync failed, Beat will retry daily)"

echo "==> Syncing Stripe catalog (idempotent)..."
uv run python manage.py sync_stripe_catalog

echo "==> Collecting static files..."
uv run python manage.py collectstatic --no-input

echo "==> Starting uvicorn..."
exec uv run uvicorn config.asgi:application \
    --host 0.0.0.0 \
    --port "${DJANGO_PORT:-8001}" \
    --log-config /app/infra/uvicorn-log-config.json \
    --workers 4
