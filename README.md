# stripe-django

A production-ready Django template for building SaaS applications with Stripe billing. Fork it, configure your Stripe keys, and start building.

## What you get

- **Stripe integration** — subscriptions, one-time payments, customer portal, and webhook handling
- **Django backend** — authentication, user management, and admin panel
- **Webhook processing** — idempotent event handling with database-backed deduplication
- **Multi-plan support** — free, pro, enterprise (or define your own)
- **CI/CD** — GitHub Actions for lint, typecheck, and tests out of the box

## Quick start

```bash
# 1. Fork and clone
gh repo fork SergiCoder/stripe-django --clone

# 2. Install dependencies
uv sync

# 3. Set up environment variables
cp .env.example .env
# Edit .env with your Stripe keys and database URL

# 4. Run migrations
uv run python manage.py migrate

# 5. Start the dev server
uv run python manage.py runserver
```

## Environment variables

| Variable | Description |
|---|---|
| `SECRET_KEY` | Django secret key |
| `DATABASE_URL` | PostgreSQL connection string |
| `STRIPE_SECRET_KEY` | Stripe API secret key |
| `STRIPE_PUBLISHABLE_KEY` | Stripe publishable key |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret |
| `STRIPE_PRICE_ID_PRO` | Stripe price ID for the pro plan |
| `STRIPE_PRICE_ID_ENTERPRISE` | Stripe price ID for the enterprise plan |
| `DEBUG` | Set to `False` in production |
| `ALLOWED_HOSTS` | Comma-separated list of allowed hosts |

## Project structure

```
stripe-django/
├── core/                # Framework-agnostic shared business logic (stripe-saas-core)
│   ├── stripe_saas_core/
│   │   ├── domain/      # Pydantic domain models (User, Org, Subscription, …)
│   │   ├── services/    # Business logic (billing, webhooks, GDPR, …)
│   │   ├── repositories/ # Repository protocols (async, framework-agnostic)
│   │   └── exceptions/  # Domain exceptions
│   └── tests/           # Core unit tests
├── config/              # Django settings, URLs, WSGI/ASGI
├── accounts/            # User auth and profile management
├── billing/             # Stripe integration and webhook handling
├── subscriptions/       # Plan management and access control
├── templates/           # HTML templates
├── static/              # Static assets
├── tests/               # Test suite
├── .github/             # CI workflows and PR template
└── manage.py
```

## Tech stack

- **Python 3.12+** with Django
- **PostgreSQL** as the database
- **Stripe** for payments and billing
- **uv** for dependency management
- **Ruff** for linting
- **mypy** for type checking (Django layer)
- **pyright** for type checking (core package)
- **pytest** for testing

## Development

```bash
# Run Django tests
uv run pytest -v

# Run core package tests
cd core && uv run pytest -v

# Lint
uv run ruff check .

# Typecheck (Django layer)
uv run mypy .

# Typecheck (core package)
cd core && uv run pyright

# Format
uv run ruff format .
```

## Stripe setup

1. Create a [Stripe account](https://dashboard.stripe.com/register)
2. Get your API keys from the [Stripe Dashboard](https://dashboard.stripe.com/apikeys)
3. Create your products and prices in Stripe
4. Set up a webhook endpoint pointing to `/billing/webhooks/` with these events:
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

## License

MIT
