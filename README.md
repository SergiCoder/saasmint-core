# SaasMint Core

A production-ready Django backend for building SaaS applications with Stripe billing. Fork it, configure your Stripe keys, and start building.

## What you get

- **Stripe integration** — subscriptions, one-time payments, customer portal, and webhook handling
- **Django backend** — authentication, user management, and admin panel
- **Admin dashboard** — extended Django admin with subscription status, Stripe event log, and user impersonation via django-hijack
- **Webhook processing** — idempotent event handling with database-backed deduplication
- **Organisations** — multi-tenant orgs with role-based membership (owner, admin, member)
- **Multi-plan support** — personal and team plans (free, basic, pro) with seat-based team pricing, or define your own
- **One-time products** — credit packs (Boost) for non-subscription purchases via Stripe Checkout
- **Dev seed data** — one command to populate the database with realistic test users, orgs, and subscriptions
- **CI/CD** — GitHub Actions for lint, typecheck, and tests out of the box

## Quick start

```bash
# 1. Fork and clone
gh repo fork SergiCoder/saasmint-core --clone

# 2. Install dependencies
uv sync

# 3. Set up environment variables
cp .env.base .env.local
# Edit .env.local with your Stripe keys, Supabase JWT secret, and database URL

# 4. Start the Docker stack (PostgreSQL, Redis, Django, Celery)
make dev

# 5. In a separate terminal, run migrations
make migrate

# 6. (Optional) Seed dev data with test users and orgs
make seed
```

## Local HTTPS

The dev stack includes a [Caddy](https://caddyserver.com/) reverse proxy that terminates TLS at `https://localhost:8443` and forwards to Django. This requires a one-time [mkcert](https://github.com/FiloSottile/mkcert) setup per machine.

**Install mkcert (once per machine):**

| Platform | Command |
|---|---|
| macOS | `brew install mkcert` |
| Ubuntu | `sudo apt install mkcert` |
| Windows | `winget install FiloSottile.mkcert` or `choco install mkcert` |

**Generate locally-trusted certs:**

```bash
mkdir -p infra/certs
mkcert -install
mkcert -key-file infra/certs/localhost-key.pem -cert-file infra/certs/localhost.pem localhost
```

After that, `make dev` serves Django at both:
- `http://localhost:8001` — direct (no TLS)
- `https://localhost:8443` — via Caddy (TLS, green padlock)

The `infra/certs/` directory is gitignored. Certs are never committed.

> Run `make https-setup` at any time to see these instructions again.

## API documentation

When `DEBUG=True`, interactive API docs are served at:

- `/api/docs/` — Swagger UI
- `/api/redoc/` — ReDoc
- `/api/schema/` — raw OpenAPI 3 JSON schema

Links to Swagger and ReDoc also appear in the Django admin header (debug only).

## Environment variables

| Variable | Description |
|---|---|
| `ENVIRONMENT` | Environment name (`local`, `development`, `production`) — selects which env file to load |
| `DJANGO_SECRET_KEY` | Django secret key |
| `DATABASE_URL` | PostgreSQL connection string |
| `STRIPE_SECRET_KEY` | Stripe API secret key |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_ANON_KEY` | Supabase anonymous/public key |
| `SUPABASE_JWT_SECRET` | Supabase JWT signing secret (used for auth) |
| `REDIS_URL` | Redis connection string (defaults to `redis://localhost:6379/0`) |
| `DEBUG` | Set to `True` for local development |
| `ALLOWED_HOSTS` | JSON array of allowed hosts (e.g. `["localhost","127.0.0.1"]`) |
| `CORS_ALLOWED_ORIGINS` | JSON array of allowed CORS origins |
| `CORS_ALLOW_ALL_ORIGINS` | Set to `True` to allow all CORS origins (dev only) |
| `CSRF_TRUSTED_ORIGINS` | JSON array of trusted origins for CSRF (e.g. `["https://localhost:8443"]`) |
| `DJANGO_SETTINGS_MODULE` | Python dotted path to the Django settings module (e.g. `config.settings.dev`) |
| `ENABLE_SESSION_AUTH` | Set to `True` to enable DRF browsable API session auth (dev only) |

## Project structure

```
saasmint-core/
├── core/                # Framework-agnostic shared business logic (saasmint-core-lib)
│   ├── saasmint_core/
│   │   ├── domain/      # Pydantic domain models (User, Org, Subscription, …)
│   │   ├── services/    # Business logic (billing, webhooks, GDPR, …)
│   │   ├── repositories/ # Repository protocols (async, framework-agnostic)
│   │   └── exceptions/  # Domain exceptions
│   └── tests/           # Core unit tests
├── config/              # Django settings, URLs, WSGI/ASGI
├── apps/                # Django apps
│   ├── admin_panel/     # Extended Django admin (subscription status column, site_url → /dashboard/)
│   ├── billing/         # Stripe billing, subscriptions, and webhook processing
│   ├── dashboard/       # Server-rendered dashboard, hijack impersonation landing views
│   ├── orgs/            # Organisation management and membership
│   └── users/           # User auth, Supabase JWT authentication, and profile management
├── middleware/           # Django middleware (exception handling, security headers)
├── infra/               # Docker, Caddy TLS proxy, and dev entrypoint
├── templates/           # Shared HTML templates (admin overrides, DRF browsable API, topbar)
├── scripts/             # CI helper scripts (dependency parser)
├── .github/             # CI workflows and PR template
├── helpers.py           # Shared Django helpers (aget_or_none, get_user)
└── manage.py
```

## Tech stack

- **Python 3.12+** with Django
- **PostgreSQL** as the database
- **Stripe** for payments and billing
- **django-hijack** for admin user impersonation
- **drf-spectacular** for OpenAPI schema, Swagger UI, and ReDoc
- **Caddy** as local TLS reverse proxy
- **uv** for dependency management
- **Ruff** for linting
- **mypy** for type checking
- **pytest** for testing

## Development

```bash
# Run Django tests
make test

# Run core package tests
make test-core

# Lint
make lint

# Typecheck (django + core)
make typecheck

# Format
make format

# Seed dev data (requires Docker stack running and DEBUG=True)
make seed
```

## Stripe setup

1. Create a [Stripe account](https://dashboard.stripe.com/register)
2. Get your API keys from the [Stripe Dashboard](https://dashboard.stripe.com/apikeys) and put them in `.env.local`
3. Define your catalog locally — edit `apps/billing/migrations/0005_seed_boost_products.py` and `0007_update_plans_and_prices.py` (or add new migrations) to set the plans/products you want, then run `make migrate`
4. Push the local catalog to Stripe with `make sync-stripe` — this creates Stripe Products/Prices via `python manage.py sync_stripe_catalog` and writes the resulting `stripe_price_id` back onto `PlanPrice` / `ProductPrice`. The command is idempotent (uses Stripe `lookup_key`s) and should also be run after every deploy.
5. Webhook forwarding for local development is handled automatically by the bundled `stripe-cli` service in `docker-compose.yml`. Run `stripe login` once on the host (its config is mounted into the container), then `make dev` will start the forwarder alongside Django. Tail it with `make stripe-logs`.
6. In production, set up a webhook endpoint pointing to `/api/v1/webhooks/stripe` with these events:
   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.payment_succeeded`
   - `invoice.payment_failed`

## Deploying

This template works with any platform that supports Django:

- **Railway** — `railway up`
- **Render** — connect your repo and deploy
- **Fly.io** — `fly launch`
- **VPS** — Gunicorn + Nginx + PostgreSQL

Make sure to set all environment variables and run migrations in production.

## Plugins

- [Prism](https://github.com/SergiCoder/prism) — Claude Code plugin for multi-profile code review, conventional commits, branching, and PR workflows

## License

[MIT](https://github.com/SergiCoder/saasmint-core/blob/main/LICENSE)
