# SaasMint Core

Django 6 SaaS backend. Python 3.12, uv, PostgreSQL (testcontainers), Celery + Redis.

## Architecture

- `core/saasmint_core/` — framework-agnostic domain layer (domain models, services, repositories interfaces).
- `apps/` — Django apps (`users`, `billing`, `orgs`, `dashboard`, `admin_panel`). Each has models, views, serializers, urls, tests/.
- `config/` — Django settings (base/dev/test/prod), root urls, celery.
- `middleware/` — custom middleware (security, etc).
- Django apps implement repository interfaces from core and wire them to DRF views/serializers.

## Billing model

- Single-currency (USD) catalog. `PlanPrice` / `ProductPrice` store `amount` in cents, no `currency` column.
- `Plan` has `(context, tier, interval)` — `context` is `personal` or `team`, `tier` is `free`/`basic`/`pro`. Active rows are unique on that triple.
- `Subscription` covers two shapes:
  - Paid: `stripe_id` + `stripe_customer_id` set, lifecycle synced via webhooks.
  - Free: `stripe_id IS NULL`, `user_id` set directly. Created on signup (`apps.billing.services.assign_free_plan`) and on personal subscription cancellation (auto-fallback in `_on_subscription_deleted`). `Subscription.is_free` distinguishes them.
- `current_period_end` for free subs is the sentinel `FREE_SUBSCRIPTION_PERIOD_END` (year 9999) — they never renew.
- `Product` / `ProductPrice` are one-time purchases (credit packs / Boost), separate from subscription plans.
- Stripe API version is pinned to `2026-03-25.dahlia`. Notable: `cancel_at_period_end=True` is replaced by `cancel_at="min_period_end"` (clear with `cancel_at=""`); the singular `subscription.discount` is replaced by `discounts[]`; `current_period_start/end` live on subscription items, not the subscription itself.
- `make sync-stripe` (runs `manage.py sync_stripe_catalog`) is the source of truth for pushing local Plans/Products into Stripe. It should run after every `migrate` in deploy pipelines so Stripe matches the DB; it is idempotent via Stripe `lookup_key`s.

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

## Accepted type: ignore / noqa suppressions

The following suppressions are intentional and should not be removed. They stem from upstream library limitations or deliberate design choices.

### django-stubs / drf-stubs

- `# type: ignore[type-arg]` on `admin.ModelAdmin`, `BaseUserAdmin`, `forms.ModelForm` — these are generic in django-stubs but **not subscriptable at runtime**. Django autodiscovers admin modules at import time, so `ModelAdmin[Model]` causes `TypeError`.
- `# type: ignore[misc]` on `permission_classes`, `throttle_classes`, `parser_classes` — DRF stubs type these as instance vars; using `ClassVar` (required by RUF012) triggers mypy `misc`. A conflict between drf-stubs and mypy.
- `# type: ignore[misc]` on `super().get_queryset()` in admin — django-stubs returns `QuerySet[Any]`; narrowing to `QuerySet[Model]` triggers `misc`.
- `# type: ignore[no-untyped-call]` on drf-spectacular `OpenApiAuthenticationExtension` — missing stubs.

### Stripe stubs

- `# type: ignore[no-untyped-call]` — `Webhook.construct_event`, `SignatureVerificationError` missing return annotations.
- `# type: ignore[arg-type]` — stub overloads don't match actual API signatures (`locale`, `**params`).
- `# type: ignore[return-value]` — `session.url` typed as `str | None` but always `str` for hosted checkout.
- `# type: ignore[attr-defined]` — `promotion_code.coupon` missing from stubs.

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
