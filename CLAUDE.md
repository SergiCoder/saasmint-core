# SaasMint Core

Django 6 SaaS backend. Python 3.12, uv, PostgreSQL (testcontainers), Celery + Redis.

## Architecture

- `core/saasmint_core/` — framework-agnostic domain layer (domain models, services, repositories interfaces).
- `apps/` — Django apps (`users`, `billing`, `orgs`, `dashboard`, `admin_panel`). Each has models, views, serializers, urls, tests/.
- `config/` — Django settings (base/dev/test/prod), root urls, celery.
- `middleware/` — custom middleware: `security.py` (CSP / security headers) and `exceptions.py` (DRF error-envelope normalisation).
- Django apps implement repository interfaces from core and wire them to DRF views/serializers.

## Billing model

- Single-currency (USD) catalog. `PlanPrice` / `ProductPrice` store `amount` in cents, no `currency` column. Pricing endpoints accept an optional `?currency=` query param; amounts are converted for display using `ExchangeRate`. In production, rates are synced daily from Stripe by the `sync_exchange_rates` Celery beat task (runs once at deploy from `infra/entrypoint.sh`, then on schedule). In dev, `infra/entrypoint.dev.sh` instead seeds rates once via `seed_exchange_rates` (a public-API seeder that doesn't touch Stripe). The catalog and Stripe charges remain in USD.
- `Plan` has `(context, tier, interval)` — `context` is `personal` or `team`, `tier` is an `IntegerChoices` enum (`2=basic`, `3=pro`; `1=free` is reserved for legacy data and not seeded). Active rows are unique on that triple.
- `Subscription` is a pure Stripe mirror — every row has a `stripe_id` and is synced via webhooks. The free tier is the *absence* of a Subscription, not a row with `stripe_id IS NULL`. Signup creates only the `User`; `_on_subscription_deleted` flips status to `CANCELED` and does not create a fallback row.
- `Product` / `ProductPrice` are one-time purchases (credit packs / Boost), separate from subscription plans. Purchases go through `POST /api/v1/billing/product-checkout-sessions/` (Stripe Checkout `mode=payment`); on `checkout.session.completed` the webhook routes to `_on_product_checkout_completed` which grants credits via `CreditTransaction` + `CreditBalance`. ORG_MEMBER owners buy for the org; PERSONAL users buy for themselves. Admins/members get 403.
- Credit ledger: `CreditBalance` (denormalised per-user-or-org current balance, XOR `user`/`org`) + `CreditTransaction` (immutable audit log, unique on `stripe_session_id` for webhook-replay idempotency). Read via `GET /api/v1/billing/credits/me/`; any active org member can read the org balance.
- Subscription mutations (`PATCH` / `DELETE /api/v1/billing/subscriptions/me/`) on team subs require `is_billing=True` on the caller's active org membership — non-billing members get 403. On `cancel_at_period_end` flips, `send_subscription_cancel_notice_task` emails every `is_billing=True` member so a rogue billing contact's action is visible.
- Team checkout writes a user-scoped `StripeCustomer` at checkout-init time (the org doesn't exist yet). The webhook handler rebinds that same row to the new org inside `_create_org_with_owner` — duplicate webhook deliveries are idempotent (existing org + membership returned unchanged).
- Stripe API version is pinned to `2026-03-25.dahlia`. Notable: `cancel_at_period_end=True` is replaced by `cancel_at="min_period_end"` (clear with `cancel_at=""`); `current_period_start/end` live on subscription items, not the subscription itself.
- `manage.py seed_catalog` is the idempotent, USD-only seeder for Plans, PlanPrices, and Boost Products. It updates existing `PlanPrice.amount` / `ProductPrice.amount` in place when the spec changes (so re-seeding adjusts prices without dropping rows). It runs automatically from `infra/entrypoint.sh` after `migrate` on every deploy, using placeholder `stripe_price_id` values — followed immediately by `sync_stripe_catalog`, which replaces those placeholders with real Stripe price IDs. The dev entrypoint (`infra/entrypoint.dev.sh`) also runs `sync_stripe_catalog` (via `seed_dev_data --sync-stripe`).
- `make sync-stripe` (runs `manage.py sync_stripe_catalog`) is the source of truth for pushing local Plans/Products into Stripe. The deploy entrypoint already runs it after `migrate` + `seed_catalog`, so Stripe matches the DB on every boot; it is idempotent via Stripe `lookup_key`s.

## Prism commands

Use Prism for all workflow operations — it is the source of truth for branching, reviews, and shipping:

- `/prism:create-branch` — create feature/fix/hotfix branches from the correct base
- `/prism:ship` — pre-ship checks, conventional commit, and push
- `/prism:review-and-fix` — run code review and fix all findings
- `/prism:review-and-report-only` — run review without fixing
- `/prism:address-pr-review` — address inline PR review comments
- `/prism:open-pr` — sync base, run tests, open PR
- `/prism:release` — open release PR from dev to main

## Pre-push checklist

Before pushing or opening a PR, always run:

```bash
make lint        # ruff check .
make typecheck   # mypy . (django + core)
make test        # pytest
```

Fix any errors before pushing. Do not skip this.

## Commands

```bash
make dev         # docker compose up (Django + Celery + Postgres + Redis)
make test        # pytest -v
make lint        # ruff check
make format      # ruff format
make typecheck   # mypy
make migrate     # run migrations (stack running)
docker compose exec django uv run python manage.py spectacular --file schema.yml  # regenerate OpenAPI schema
```

After modifying any endpoint (views, serializers, URL routes), regenerate `schema.yml` so the OpenAPI spec stays in sync.

## Code style

- Always use type hints in Python.

## Bug investigation

For bugs touching infra, proxy (Caddy/Nginx), OAuth, or deploy:
- Before editing, state which layer owns the bug (frontend / backend / proxy / infra) and the specific evidence.
- Check proxy header trust (`SECURE_PROXY_SSL_HEADER`, `USE_X_FORWARDED_HOST`) before touching app logic for URL/scheme issues.
- Do not edit `config/settings/` for bugs whose evidence points at the frontend or proxy layer.

## Project rules

**Security**
- Webhooks: verify `livemode`/env, not just signature.
- Access checks belong in the queryset lookup, not just the serializer.
- Token-based actions (decline, accept, unsubscribe): verify the caller owns the token's subject.
- All password inputs go through `validate_password()`.

**Settings & secrets**
- Never set `ALLOWED_HOSTS=["*"]` when `USE_X_FORWARDED_HOST=True` — enumerate hosts.
- Use separate env vars for secrets with different rotation lifecycles (e.g. `JWT_SIGNING_KEY` vs `SECRET_KEY`).
- CSP is applied only to HTML responses (JSON API responses never get a CSP header). HTML on `/api/docs/` + `/api/redoc/` gets the docs bucket (CDN allowances); every other HTML surface — `/admin/`, `/hijack/`, `/dashboard/`, and DRF's browsable API on `/api/…` — shares a moderate `default-src 'self'` + `style-src 'self' 'unsafe-inline'` + `frame-ancestors 'self'` policy.

**CI/CD**
- No `${{ github.* }}` interpolated into workflow shell — pass via `env:` and quote `"$VAR"`.

**Django**
- Don't hand-edit auto-generated migrations beyond formatting — regenerate instead.

## Accepted type: ignore / noqa suppressions

The following suppressions are intentional and should not be removed. They stem from upstream library limitations or deliberate design choices.

### django-stubs / drf-stubs

- `# type: ignore[type-arg]` on `admin.ModelAdmin`, `BaseUserAdmin`, `forms.ModelForm` — these are generic in django-stubs but **not subscriptable at runtime**. Django autodiscovers admin modules at import time, so `ModelAdmin[Model]` causes `TypeError`.
- `# type: ignore[misc]` on `permission_classes`, `throttle_classes`, `parser_classes` — DRF stubs type these as instance vars; using `ClassVar` (required by RUF012) triggers mypy `misc`. A conflict between drf-stubs and mypy.
- `# type: ignore[misc]` on `super().get_queryset()` in admin — django-stubs returns `QuerySet[Any]`; narrowing to `QuerySet[Model]` triggers `misc`.
- `# type: ignore[no-untyped-call]` on drf-spectacular `OpenApiAuthenticationExtension` — missing stubs.

### Stripe stubs

- `# type: ignore[no-untyped-call]` — `Webhook.construct_event`, `SignatureVerificationError` missing return annotations.
- `# type: ignore[arg-type]` — stub overloads don't match actual API signatures (`locale`, `**params`, `ExchangeRate.rates`).
- `# type: ignore[return-value]` — `session.url` typed as `str | None` but always `str` for hosted checkout.
- `# type: ignore[attr-defined]` — `ExchangeRate.rates` missing from stubs.

### Celery

- `# type: ignore[untyped-decorator]` on `@app.task` — celery has no type stubs.
- `# type: ignore[attr-defined]` on `self.retry` / `self.request` in bound tasks — injected by Celery at runtime.

### pydantic-settings

- `# type: ignore[call-arg]` on `_Env()` — fields read from env vars; mypy sees no positional args.

### Ruff / design-correct

- `# noqa: DJ001` — nullable `CharField`/`TextField` where `NULL` has semantic meaning (e.g. no avatar vs empty string).
- `# noqa: RUF012` — `Meta.constraints` / `Meta.indexes` must be mutable lists; `ClassVar` doesn't apply in Django `Meta`.
- `# noqa: ANN401` — `*args`/`**kwargs` forwarded to parent methods; `Any` is appropriate.
- `# noqa: F403` / `F405` / `E402` — star imports in settings files; standard Django inheritance pattern.
- `# noqa: S106` / `S107` — hardcoded passwords in test fixtures.
- `# noqa: F401` — side-effect import to register drf-spectacular auth extension.

### Test-only

- `# type: ignore[misc]` on frozen dataclass field mutation — intentional: testing that frozen models raise on mutation.
