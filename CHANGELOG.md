# Changelog

All notable user-facing and contract changes to `saasmint-core`. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
the project adheres to [Semantic Versioning](https://semver.org/).

From `v0.7.0` onward, `saasmint-core` (root), `saasmint-core-lib` (`core/`),
and the frontend `saasmint-app` ship in lockstep — a `v<X.Y.Z>` tag is
only valid if all three repos already match `<X.Y.Z>` on `main`.

## [0.7.0] - 2026-04-25

### Changed (breaking)

- **`Subscription` is now a pure Stripe mirror.** Every persisted row
  has a `stripe_id`; the free tier is the *absence* of a Subscription
  row. The dual-shape `stripe_id IS NULL` placeholder is gone. (#46)
- **`GET /api/v1/billing/subscriptions/me/`** returns **404** for users
  on the free tier (previously a synthetic 200 with `stripe_id: null`,
  `current_period_end: 9999-12-31`, `plan.tier: "free"`). The 404 is
  now declared in `schema.yml`. (#46)
- **`GET /api/v1/billing/plans/`** no longer includes the `Personal
  Free` plan row. (#46)
- **Signup paths** (`/auth/register/`, `/auth/register/org-owner/`,
  OAuth callback) no longer create any Subscription. The user only
  gets one once they pay. (#46)
- The `customer.subscription.deleted` webhook flips status to `CANCELED`
  and **no longer creates a fallback free Subscription** for personal
  users. Subsequent reads observe the absence as 404. (#46)

### Removed

- `Subscription.is_free`, `FREE_SUBSCRIPTION_PERIOD_END` sentinel,
  `assign_free_plan`, `_lock_user`, `Plan.free_plans()`,
  `delete_free_for_user`, `get_free_plan`. (#46)
- The "Personal Free" entry from `seed_catalog`. The `PlanTier.FREE = 1`
  enum value is preserved for legacy data but is no longer seeded. (#46)
- The `uniq_free_subscription_per_user` partial unique constraint on
  `Subscription` (no rows match the predicate any more). (#46)

### Performance

- Migration `0014` deletes free Subscriptions in 1000-row batches to
  keep transaction size bounded — the migration runs on every deploy
  via the entrypoint and the table can be the largest in the schema.
  (#46)
- One fewer query per `customer.subscription.created` / `.updated`
  webhook (the prune-free-sub block is gone). (#46)
- One fewer SELECT FOR UPDATE per signup (the `_lock_user` row lock
  that guarded `assign_free_plan` race is gone). (#46)

### Documentation

- CLAUDE.md "Versioning" rule added: every PR bumps both
  `pyproject.toml` files together; `saasmint-core` and `saasmint-app`
  ship in lockstep. (#46)
- README plan list dropped the unseeded "free" tier. (#46)

### Maintenance

- `uv.lock` re-resolved so the editable entries match the bumped
  `pyproject.toml` versions. (#47)

## Deprecated tags (pre-lockstep)

These tags shipped with divergent versions across `saasmint-core` and
`saasmint-core-lib`. Kept for deploy-forensics; do not use as a basis
for new work. From `v0.7.0` onward all packages ship in lockstep.

- `v0.5.0` — `saasmint-core` 0.5.0, `saasmint-core-lib` 0.4.0.
- `v0.5.1` — `saasmint-core` 0.5.1, `saasmint-core-lib` 0.4.0.
- `v0.6.0` — `saasmint-core` 0.6.0, `saasmint-core-lib` 0.5.0.
