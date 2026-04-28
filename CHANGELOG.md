# Changelog

All notable user-facing and contract changes to `saasmint-core`. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
the project adheres to [Semantic Versioning](https://semver.org/).

From `v0.7.0` onward, `saasmint-core` (root), `saasmint-core-lib` (`core/`),
and the frontend `saasmint-app` ship in lockstep — a `v<X.Y.Z>` tag is
only valid if all three repos already match `<X.Y.Z>` on `main`.

## [0.8.4] - 2026-04-28

### Added

- **Personal→team upgrade flow.** A user with `account_type=personal`
  (with or without an active personal subscription) can now POST a team-
  context checkout to `POST /api/v1/billing/checkout-sessions/`. The 409
  guard previously gated on `account_type == ORG_MEMBER`; it now gates on
  "user already owns an org" (rule 8 — one owned org per user). The
  legacy `/auth/register/org-owner/` path still works unchanged.
- **`keep_personal_subscription` field on `CheckoutRequestSerializer`**
  (boolean, default `false`). On the team-context
  `checkout.session.completed` webhook, when the flag is `false`
  (default), the user's existing personal subscription — if any — is
  scheduled to cancel at period end via Stripe
  (`Subscription.modify(cancel_at="min_period_end")`). When `true`, the
  personal sub is left running concurrently with the new team sub
  (rule 5b). Idempotent: Stripe accepts re-modifying an
  already-scheduled cancellation with no error, and the helper no-ops
  cleanly when the user has no personal customer or no active personal
  subscription.

### Changed

- **`account_type` flips atomically with org creation.**
  `apps.orgs.services._create_org_with_owner` now flips
  `user.account_type` from `personal` to `org_member` inside the same
  transaction that creates the org and owner membership. The flip is
  one-way — the function never demotes `org_member` back to `personal`,
  and is a no-op for users who registered via the legacy
  `/auth/register/org-owner/` path.
- **Team checkout creates a fresh org-scoped Stripe customer.**
  `CheckoutSessionView` no longer reuses the user-scoped
  `StripeCustomer` for team-context checkouts. A new helper
  `saasmint_core.services.billing.create_team_stripe_customer` mints a
  Stripe customer at checkout-init time without persisting a DB row;
  `_create_org_with_owner` then creates the org-scoped `StripeCustomer`
  in the webhook. This keeps personal and team subs on distinct Stripe
  customers (rule 3) so each retains its own locked currency and
  payment method, instead of rebinding the user-scoped customer to the
  org and orphaning the personal subscription's billing linkage.
- **`OnTeamCheckoutCompleted` callback signature** gains a trailing
  `keep_personal_subscription: bool` argument. The webhook decodes the
  Stripe metadata string `"true"`/`"false"` to a Python bool and forwards
  it to the callback.

### Removed

- **User-scoped `StripeCustomer` rebind branch in
  `_create_org_with_owner`.** With the team-checkout customer now
  created fresh and org-scoped from the webhook, the rebind path
  (`existing_customer.user = None; existing_customer.org = org`) is
  unreachable and has been deleted. The duplicate-webhook short-circuit
  on an already-org-bound customer is preserved.

## [0.8.3] - 2026-04-27

### Changed

- **OAuth + existing-password-account collision now returns a specific
  error code.** When an OAuth-provided email matches an existing local
  account but the provider is either unverified or not on the auto-link
  trust allowlist, `OAuthCallbackView` now redirects to
  `/auth/error?error=oauth_email_unverified_collision` (previously the
  generic `email_not_verified`). The frontend uses this to guide the
  user to log in with their password and link the provider explicitly,
  rather than showing the generic verification error.
- **Auto-link onto an existing local account now requires the OAuth
  provider to be on `apps.users.services.TRUSTED_FOR_AUTO_LINK`.** The
  current allowlist is `{"google", "github", "microsoft"}` — providers
  whose `email_verified=True` reflects the user's own mailbox-
  verification act (Google + GitHub) or whose `xms_edov` claim attests
  to tenant-domain ownership (Microsoft, gated upstream in
  `apps.users.oauth.exchange_code`). The allowlist is defense-in-depth
  for any future provider added without explicit trust review — fresh
  signups still work, but auto-linking onto an existing local account
  requires explicit trust certification.

### Added

- `OAuthEmailUnverifiedCollisionError` exception in
  `apps.users.oauth`. Raised by `apps.users.services.resolve_oauth_user`
  when the OAuth-provided email matches an existing user but auto-link
  is unsafe (provider untrusted or `email_verified` false).

## [0.8.2] - 2026-04-27

### Changed

- **Team-subscription cancellation now hard-deletes the org.** When the
  `customer.subscription.deleted` Stripe webhook fires for a team sub,
  the org and everything cascading from it (`OrgMember` rows, pending
  `Invitation` rows, single-org-member `User` accounts) are now hard-
  deleted. Previously the org was left in a `is_active=False` zombie
  state, which produced a confusing post-cancellation experience and
  was inconsistent with the no-soft-delete philosophy. Owners cannot
  recover the org after cancellation; they must subscribe again to a
  fresh org. The cascade itself runs in a Celery task
  (`apps.orgs.tasks.delete_org_on_subscription_cancel_task`) so the
  webhook returns within Stripe's retry window even for orgs with
  many members.
- **The cascade is unconditional.** Voluntary (owner-initiated) and
  involuntary (failed-payment retries exhausted, fraud, Stripe-side
  termination) cancellation collapse to the same code path. The
  webhook handler does NOT branch on `cancellation_details.reason`.

### Removed

- **`Org.is_active` column.** The flag was only ever written by the
  pre-PR-2 `deactivate_org` soft-state path; with hard-delete on
  cancel, no production code sets it. Migration
  `apps/orgs/migrations/0010_remove_org_is_active.py` drops the
  column. All `is_active=True` filters in `apps.orgs.views`,
  `apps.orgs.services`, and `apps.billing.views` are gone, as is the
  field on `core.saasmint_core.domain.org.Org`.
- **`apps.orgs.services.deactivate_org`** — replaced by
  `delete_org_on_subscription_cancel`. Same webhook trigger, hard-
  delete cascade instead of soft `is_active=False` flip.
- **`apps.orgs.services.cancel_pending_invitations_for_org`** — the
  helper had no production callers post-rewrite (the cascade goes
  through `_delete_org_db_only`, which inlines the same UPDATE).
- **`_InvitationOrgGone` exception class and the `if not org.is_active`
  guard in `InvitationAcceptView`.** Unreachable now that
  `Invitation.org` is `on_delete=CASCADE`: a deleted org has no
  invitations, so an `Invitation` row implies a live `Org`.

## [0.8.1] - 2026-04-27

### Changed

- **Stripe subscription cancellations now pass `prorate=False`.** Org
  deletion (`apps.orgs.tasks.cancel_stripe_subs_task`,
  `apps.orgs.services._cancel_team_subscription`) and GDPR account
  deletion (`saasmint_core.services.gdpr.delete_account`) are terminal
  actions; the unused billing period is not refunded. Previously Stripe
  applied default proration, which could issue an unwanted credit/refund
  on the customer.

### Fixed

- **Org-deletion sub-cancel task is now idempotent and per-item
  fault-isolated.** `cancel_stripe_subs_task` swallows Stripe
  `resource_missing` (`InvalidRequestError`) so a DELETE-then-webhook
  race or a Celery retry after partial success no longer raises. A
  transient Stripe error on one `sub_id` (e.g. `APIConnectionError`,
  or a non-`resource_missing` `InvalidRequestError`) no longer
  short-circuits the loop — every sub in the batch is still attempted,
  then the first failure is re-raised at end-of-loop so Celery records
  it. Without this, with no `autoretry_for` on the task, subs
  positioned after the failing one would have leaked: never cancelled,
  never reattempted.
- **`deactivate_org` is a no-op when the org row is already gone.**
  Covers the DELETE-then-webhook race where
  `customer.subscription.deleted` fires after the org has been
  hard-deleted.

### Removed

- **`Org.deleted_at` column and partial unique index.** The org table
  no longer carries a soft-delete marker — hard delete is the only
  termination path. Migration `apps/orgs/migrations/0009_remove_org_deleted_at.py`
  drops the column and replaces the partial `UniqueConstraint(slug,
  where deleted_at IS NULL)` with an unconditional `UniqueConstraint(slug)`.
  All `org__deleted_at__isnull=True` filters in `apps.orgs.views`,
  `apps.orgs.services`, and `apps.billing.views` are gone, as is the
  field on `core.saasmint_core.domain.org.Org`.

## [0.8.0] - 2026-04-26

### Added

- **Marketing inquiries endpoint.** New `POST /api/v1/marketing/inquiries/`
  (unauthenticated) accepts landing-page CTA and Contact-form submissions
  from the frontend and forwards them as a plain-text email to the inbox
  configured by `MARKETING_INQUIRIES_TO`. Returns `204 No Content` on
  acceptance and on honeypot-triggered silent drops, `400` for validation
  failures, `429` when the per-IP rate limit is exceeded, and `500` if
  the inbox env var is missing. Email payloads are dispatched via the
  existing Resend transport on a Celery task; logs include the source
  and a redacted sender (`j***@example.com`) but never the message body.
- **Dedicated throttle scope `marketing_inquiries` at `3/10minute`.** The
  endpoint does *not* share the `auth` scope — failure modes differ
  (admin inbox flood vs outbound spam), traffic shape differs (one
  submission per visitor vs bursty auth retries), and tuning the
  contact-form rate would otherwise also throttle login / OAuth /
  invitation accept. A small custom throttle class
  (`apps.marketing.throttling.MarketingInquiryThrottle`) extends DRF's
  rate parser to support multi-unit periods (`N/<count><unit>`).
- New required env var `MARKETING_INQUIRIES_TO` (documented in `.env.base`
  and `README.md`).

## [0.7.2] - 2026-04-25

### Fixed

- **Microsoft OAuth login signs verified-tenant users in directly.**
  The Microsoft callback now parses and validates the OIDC `id_token`
  returned alongside the access token (signature verified against
  Microsoft's JWKS at `login.microsoftonline.com/common/discovery/v2.0/keys`,
  audience pinned to `OAUTH_MICROSOFT_CLIENT_ID`, issuer prefix-checked
  against `https://login.microsoftonline.com/{tid}/v2.0`). When the
  token's `xms_edov` claim is `true` — Microsoft's attestation that the
  email's domain belongs to the user's tenant — the user is signed in
  with `is_verified=True`, mirroring the Google/GitHub UX. Otherwise
  (no `id_token`, signature/audience failure, or `xms_edov` absent /
  false) the flow falls back to the existing unverified path and the
  user is bounced to `/auth/error?error=email_not_verified`.

### Security

- **Microsoft Graph `/me` is no longer treated as proof of email
  ownership.** A tenant admin can set a user's `mail` attribute to any
  string (including a third-party domain) without verifying the
  destination mailbox; combined with the email-match auto-link in
  `resolve_oauth_user`, naïvely trusting `/me.mail` would have enabled
  account takeover of an existing password-registered user. Trust now
  flows from the signed `id_token`'s `xms_edov` claim, not Graph.

## [0.7.1] - 2026-04-25

### Fixed

- **Hijack release no longer 405s on the admin re-login bounce.**
  `HijackReleaseView` is no longer wrapped in `staff_member_required`
  (during impersonation `request.user` is the impersonated non-staff
  user, so the gate would 302 the release POST to the admin login page
  with a stale `next=/hijack/release/`, which then returned 405 on the
  follow-up GET). The parent `UserPassesTestMixin.test_func` already
  gates POST on `session["hijack_history"]`, which is the correct
  invariant. GETs to `/hijack/release/` now 302 to the admin home as a
  no-op instead of 405. Stop-impersonating from the admin toolbar now
  lands on `/admin/` (admin index) rather than the users changelist.

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
