# SaasMint Core

Django 6 SaaS backend. Python 3.12, uv, PostgreSQL (testcontainers), Celery + Redis.

## Architecture

- `core/saasmint_core/` — framework-agnostic domain layer (domain models, services, repository interfaces).
- `apps/` — Django apps (`users`, `billing`, `orgs`, `dashboard`, `admin_panel`, `marketing`). Each has models, views, serializers, urls, tests/.
- `config/` — Django settings (base/dev/test/prod), root urls, celery.
- `middleware/` — `security.py` (CSP / security headers), `exceptions.py` (DRF error-envelope normalisation).
- Django apps implement core's repository interfaces and wire them to DRF views/serializers.

## Billing model

- **Catalog**: USD-only. `PlanPrice`/`ProductPrice` store `amount` in cents — **the source of truth Stripe charges against**. Endpoints accept `?currency=` for display only; the response's `display_amount` comes from precomputed `LocalizedPrice` rows (one per `(price, currency)`, friendly-rounded by the daily `sync_localized_prices` task). Missing row → fall back to USD `amount`. **Stripe charges always use USD**, regardless of `?currency=`.
- **Plans**: `(context, tier, interval)` — `context` is `personal`|`team`, `tier` is `IntegerChoices` (`2=basic`, `3=pro`; `1=free` reserved for legacy, not seeded).
- **Subscription = pure Stripe mirror**. Every row has a `stripe_id`, synced via webhooks. Free tier = absence of a row. `GET /billing/subscriptions/me/` returns paginated `{count,next,previous,results}` with 0–2 rows (one personal, one team for concurrent billers).
- **Products**: one-time purchases (credit packs / Boost). `POST /billing/product-checkout-sessions/` (Stripe Checkout `mode=payment`). Webhook `_on_product_checkout_completed` grants credits via `CreditTransaction` + `CreditBalance`.
- **Credits**: `CreditBalance` (denormalized, XOR `user`/`org`) + `CreditTransaction` (immutable, unique on `stripe_session_id` for idempotency). `GET /billing/credits/me/` → `{balances:[...]}`.
- **Context selector**: subscription mutations and product checkout accept `?context=personal|team`. Default: `team` for org members, `personal` otherwise. `?context=team` requires `OrgMember.role=OWNER`. The `is_billing=True` gate only applies to team-context.
- **Org membership**: derived from `OrgMember.objects.filter(user_id=...).exists()`. The legacy `User.account_type` and the org-owner registration endpoint were removed — there is now exactly one register path: `POST /auth/register/`.
- **Team checkout**: mints a fresh org-scoped Stripe customer at init; webhook persists the `StripeCustomer` row inside `_create_org_with_owner`. Personal and team subs always live on distinct customers — the split is **for invoicing isolation** (separate tax IDs, addresses, receipts, payment methods per scope), not currency.
- **Owner uniqueness**: DB-enforced via partial unique index on `OrgMember(user) WHERE role='owner'` (`uniq_org_owner_per_user`). The view-layer `.exists()` check is a UX fast-path; the constraint is the authoritative TOCTOU guard.
- **Personal→team upgrade**: `keep_personal_subscription` field on `CheckoutRequestSerializer` (default `false`) controls whether the existing personal sub is scheduled to cancel at period end.
- **Stripe API**: pinned to `2026-03-25.dahlia`. `cancel_at_period_end=True` → `cancel_at="min_period_end"`; `current_period_start/end` live on subscription items. `Subscription.cancel_at` mirrors Stripe's scheduled-cutover (distinct from `canceled_at`). Cancel/resume mutations write the Stripe response back locally before returning so PATCH-then-GET sees new state without waiting for the webhook.
- **Deferred downgrades**: PATCH `/subscriptions/me/` with a `plan_price_id` whose `amount < current price unit_amount` creates a Stripe `SubscriptionSchedule` (current price → period end → new price) instead of switching immediately. Upgrades/same-amount switches still apply now. The `subscription_schedule.{created,updated}` webhooks mirror the pending switch onto `Subscription.scheduled_plan` + `scheduled_change_at`; `.{released,canceled,aborted}` clear them. `DELETE /subscriptions/me/scheduled-change/` releases an active schedule (user keeps current plan); like cancel/resume, it writes the cleared `scheduled_plan`/`scheduled_change_at` state locally before returning so PATCH-then-GET sees it without webhook lag. Cancel/cancel-now first releases any pinning schedule via `sub.schedule` lookup so Stripe doesn't reject the cancel or modify call.
- **Seeding**: `seed_catalog` (idempotent, USD-only) → `sync_stripe_catalog` (replaces placeholder `stripe_price_id`s with real ones, idempotent via `lookup_key`) → `sync_localized_prices` (recomputes `LocalizedPrice` rows from the FX feed). All three run from `infra/entrypoint.sh` after `migrate` on every deploy.

## Updating prices

The catalog has three layers; touch them in order. Each step is idempotent.

1. **Edit the USD amount in `apps/billing/management/commands/seed_catalog.py`.** USD cents are the source of truth Stripe charges against — every other amount derives from this. To change a price, change it here.
2. **Run `seed_catalog`** (`docker compose run --rm django uv run python manage.py seed_catalog`, or just redeploy — `infra/entrypoint.sh` runs it). Updates `PlanPrice.amount` / `ProductPrice.amount` in the DB.
3. **Run `sync_stripe_catalog`** to mint a new immutable Stripe `Price` and repoint `stripe_price_id` via `lookup_key`. Existing subscriptions stay on the old Stripe price until they renew or are migrated; new checkouts use the new one.
4. **Run `sync_localized_prices`** (or wait for the daily Celery beat tick) to regenerate `LocalizedPrice` rows for every `(price, currency)`. The task fetches USD→all rates from `open.er-api.com` and applies `format_amount` + `round_friendly` (charm-pricing for two-decimal currencies, nearest 10/100 for zero-decimal). Failure is non-fatal: existing rows are preserved so a flaky upstream never erases the catalog.

**Adding a new currency**: append the ISO code to `SUPPORTED_CURRENCIES` in `core/saasmint_core/services/currency.py` (and `ZERO_DECIMAL_CURRENCIES` if applicable), then run `sync_localized_prices`. No migration. The new currency is immediately accepted on `?currency=`; until `sync_localized_prices` finishes, the API falls back to the USD `amount` for that currency.

**What never changes**: Stripe charges in USD. The `display_amount` field is FE-only — the actual `Stripe.PaymentIntent`/`Subscription` is created against the USD `Price`, and Stripe's own FX kicks in at charge time on the customer's card. Display drift can never cause us to bill the wrong amount.

## Pre-push checklist

```bash
make lint        # ruff check
make typecheck   # mypy
make test        # pytest
```

Fix errors before pushing. Do not skip.

## Commands

```bash
make dev         # docker compose up (Django + Celery + Postgres + Redis)
make test        # pytest -v
make migrate     # run migrations (stack running)
docker compose exec django uv run python manage.py spectacular --file schema.yml  # regenerate OpenAPI
```

After modifying any endpoint, regenerate `schema.yml`.

## Code style

- Always use type hints.
- Don't hand-edit auto-generated migrations — regenerate.

## Bug investigation

For bugs touching infra, proxy, OAuth, or deploy:
- State which layer owns the bug (frontend / backend / proxy / infra) and the evidence before editing.
- Check proxy header trust (`SECURE_PROXY_SSL_HEADER`, `USE_X_FORWARDED_HOST`) before touching app logic for URL/scheme issues.
- Don't edit `config/settings/` for bugs whose evidence points at frontend or proxy.

## Security rules

- Webhooks: verify `livemode`/env, not just signature.
- Access checks belong in the queryset lookup, not just the serializer.
- Token-based actions: verify the caller owns the token's subject.
- All password inputs go through `validate_password()`.
- OAuth `email_verified=True` only from a provider-signed token. Microsoft: signature-valid OIDC `id_token` with `xms_edov: true` — Graph `/me.mail` is admin-mutable and doesn't prove ownership.
- Auto-linking OAuth onto an existing local account requires the provider on `apps.users.services.TRUSTED_FOR_AUTO_LINK` (`google`, `github`, `microsoft`). Otherwise raise `OAuthEmailUnverifiedCollisionError`.

## Settings

- Never set `ALLOWED_HOSTS=["*"]` when `USE_X_FORWARDED_HOST=True`.
- Separate env vars for secrets with different rotation lifecycles (`JWT_SIGNING_KEY` vs `SECRET_KEY`).
- CSP applied only to HTML responses. `/api/docs/` + `/api/redoc/` get the docs bucket; everything else (`/admin/`, `/hijack/`, `/dashboard/`, DRF browsable API) shares moderate `default-src 'self'` + `style-src 'self' 'unsafe-inline'` + `frame-ancestors 'self'`.

## CI/CD

- No `${{ github.* }}` interpolated into workflow shell — pass via `env:` and quote `"$VAR"`.

## Type-ignore / noqa suppressions

Intentional suppressions (django-stubs, drf-stubs, stripe stubs, celery, pydantic-settings, ruff) documented in `docs/type-ignores.md`. Don't remove them blindly.
