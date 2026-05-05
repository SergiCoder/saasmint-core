"""Django ORM models for Stripe billing entities."""

from __future__ import annotations

import uuid

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from saasmint_core.domain.subscription import (
    ACTIVE_SUBSCRIPTION_STATUSES as _CORE_ACTIVE_STATUSES,
)


class PlanContext(models.TextChoices):
    PERSONAL = "personal", "Personal"
    TEAM = "team", "Team"


class PlanInterval(models.TextChoices):
    MONTH = "month", "Monthly"
    YEAR = "year", "Yearly"


class PlanTier(models.IntegerChoices):
    FREE = 1, "Free"
    BASIC = 2, "Basic"
    PRO = 3, "Pro"


class SubscriptionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    TRIALING = "trialing", "Trialing"
    PAST_DUE = "past_due", "Past Due"
    CANCELED = "canceled", "Canceled"
    INCOMPLETE = "incomplete", "Incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired", "Incomplete Expired"
    PAUSED = "paused", "Paused"
    UNPAID = "unpaid", "Unpaid"


ACTIVE_SUBSCRIPTION_STATUSES = tuple(SubscriptionStatus(s.value) for s in _CORE_ACTIVE_STATUSES)


class Plan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    description = models.TextField(default="", blank=True)
    context = models.CharField(max_length=20, choices=PlanContext.choices)
    tier = models.IntegerField(choices=PlanTier.choices, default=PlanTier.BASIC)
    interval = models.CharField(max_length=10, choices=PlanInterval.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "plans"
        ordering = ("context", "tier", "interval")
        constraints = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            models.UniqueConstraint(
                fields=("context", "tier", "interval"),
                condition=models.Q(is_active=True),
                name="uniq_active_plan_per_context_tier_interval",
            ),
        ]
        indexes = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            # Hot path for PlanListView: filter by `context` among active plans.
            models.Index(
                fields=["context"],
                name="idx_plan_active_context",
                condition=models.Q(is_active=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.interval})"


class PlanPrice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.OneToOneField(Plan, on_delete=models.CASCADE, related_name="price")
    stripe_price_id = models.CharField(max_length=255, unique=True)
    amount = models.IntegerField(help_text="Amount in USD cents")

    class Meta:
        db_table = "plan_prices"

    def __str__(self) -> str:
        return f"{self.plan.name} — ${self.amount / 100:.2f}"


class StripeCustomer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stripe_id = models.CharField(max_length=255, unique=True)
    user = models.OneToOneField(
        "users.User",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="stripe_customer",
    )
    org = models.OneToOneField(
        "orgs.Org",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="stripe_customer",
    )
    livemode = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "stripe_customers"
        constraints = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            models.CheckConstraint(
                condition=(
                    models.Q(user_id__isnull=False, org_id__isnull=True)
                    | models.Q(user_id__isnull=True, org_id__isnull=False)
                ),
                name="stripecustomer_has_owner",
            ),
        ]

    def __str__(self) -> str:
        return self.stripe_id


class Subscription(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stripe_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    # CASCADE is intentional: subscriptions without a customer have no meaning,
    # and StripeCustomer is only purged when its owning user/org is deleted,
    # at which point the subscription history is no longer useful for audit.
    stripe_customer = models.ForeignKey(
        StripeCustomer,
        on_delete=models.CASCADE,
        related_name="subscriptions",
        null=True,
        blank=True,
    )
    user = models.ForeignKey(
        "users.User",
        on_delete=models.CASCADE,
        related_name="subscriptions",
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=30, choices=SubscriptionStatus.choices, default=SubscriptionStatus.INCOMPLETE
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    seat_limit = models.IntegerField(default=1)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
    canceled_at = models.DateTimeField(null=True, blank=True)
    # Mirror of Stripe's ``cancel_at`` — the scheduled cancellation timestamp.
    # NULL means no cancel is scheduled. Distinct from ``canceled_at`` (which
    # is when the cancellation *fired*): a sub with cancel_at set and status
    # still ``active`` is "scheduled to cancel"; once the cutover happens the
    # webhook flips status to ``canceled`` and sets ``canceled_at``.
    cancel_at = models.DateTimeField(null=True, blank=True)
    # Pending plan switch from an active Stripe SubscriptionSchedule. Written
    # by ``subscription_schedule.created/updated`` and cleared by
    # ``.released/.canceled/.aborted``. PROTECT keeps the FK from silently
    # dropping a schedule reference if a Plan is removed — that should be a
    # surfaced integrity error, not a hidden state divergence.
    scheduled_plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,
        related_name="scheduled_subscriptions",
        null=True,
        blank=True,
    )
    scheduled_change_at = models.DateTimeField(null=True, blank=True)
    # ISO 4217 lowercase. Mirrored from Stripe — Stripe pins the currency on a
    # subscription for life, so plan changes must resolve a Stripe Price in the
    # same currency. Default ``"usd"`` matches the historical behavior for any
    # row that pre-dates this column.
    currency = models.CharField(max_length=3, default="usd")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "subscriptions"
        get_latest_by = "created_at"
        indexes = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            models.Index(fields=["stripe_customer", "status"], name="idx_sub_customer_status"),
            models.Index(fields=["user", "status"], name="idx_sub_user_status"),
            # Hot path: "find the active subscription for this owner". Partial
            # index keeps the tree small by excluding terminal-state rows.
            models.Index(
                fields=["stripe_customer", "user"],
                name="idx_sub_active_owner",
                condition=models.Q(status__in=("active", "trialing", "past_due")),
            ),
        ]
        constraints = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            models.CheckConstraint(
                condition=(models.Q(user__isnull=False) | models.Q(stripe_customer__isnull=False)),
                name="subscription_has_owner",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.stripe_id} ({self.status})"


class ProductType(models.TextChoices):
    ONE_TIME = "one_time", "One-time"


class Product(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    type = models.CharField(max_length=30, choices=ProductType.choices)
    credits = models.IntegerField(help_text="Number of credits granted on purchase")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "products"
        indexes = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            # Hot path for ProductListView: fetching active products only.
            models.Index(
                fields=["is_active"],
                name="idx_product_active",
                condition=models.Q(is_active=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.credits} credits)"


class ProductPrice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.OneToOneField(Product, on_delete=models.CASCADE, related_name="price")
    stripe_price_id = models.CharField(max_length=255, unique=True)
    amount = models.IntegerField(help_text="Amount in USD cents")

    class Meta:
        db_table = "product_prices"

    def __str__(self) -> str:
        return f"{self.product.name} — ${self.amount / 100:.2f}"


class LocalizedPrice(models.Model):
    """Pre-computed display price for a (PlanPrice|ProductPrice, currency) pair.

    The catalog ``amount`` is USD cents (the source of truth Stripe charges
    against). ``LocalizedPrice`` is a derived projection: each row holds the
    USD amount times the FX rate, friendly-rounded, stored in the target currency's
    minor units. Recomputed daily by ``sync_localized_prices`` so users see
    a stable rounded price tag (€9.99) instead of a per-request flicker
    (€9.27 → €9.31).

    Exactly one of ``plan_price``/``product_price`` is set (XOR), mirroring
    the discriminator pattern used by :class:`StripeCustomer` and
    :class:`CreditBalance`. USD is never stored — clients fall back to the
    catalog ``amount`` directly when no row exists for the requested currency.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan_price = models.ForeignKey(
        PlanPrice,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="localized_prices",
    )
    product_price = models.ForeignKey(
        ProductPrice,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="localized_prices",
    )
    currency = models.CharField(max_length=3)
    amount_minor = models.IntegerField(
        help_text="Friendly-rounded display amount in target currency's minor units."
    )
    # Stripe Price ID for billable non-USD currencies (in BILLING_CURRENCIES).
    # NULL for display-only currencies — those rows exist purely so the API can
    # render a localized price tag without ever charging in that currency.
    # USD continues to live on PlanPrice/ProductPrice.stripe_price_id.
    # NULL (not "") because the partial-unique constraint
    # ``uniq_localized_stripe_price_id`` filters on ``stripe_price_id__isnull=False``.
    stripe_price_id = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001  # see comment above
    synced_at = models.DateTimeField()

    class Meta:
        db_table = "localized_prices"
        ordering = ("currency",)
        constraints = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            models.CheckConstraint(
                condition=(
                    models.Q(plan_price__isnull=False, product_price__isnull=True)
                    | models.Q(plan_price__isnull=True, product_price__isnull=False)
                ),
                name="localizedprice_has_owner",
            ),
            models.UniqueConstraint(
                fields=("plan_price", "currency"),
                condition=models.Q(plan_price__isnull=False),
                name="uniq_localized_plan_price_currency",
            ),
            models.UniqueConstraint(
                fields=("product_price", "currency"),
                condition=models.Q(product_price__isnull=False),
                name="uniq_localized_product_price_currency",
            ),
            # Stripe Price IDs are globally unique. Partial-unique because most
            # rows (display-only currencies) leave the column NULL.
            models.UniqueConstraint(
                fields=("stripe_price_id",),
                condition=models.Q(stripe_price_id__isnull=False),
                name="uniq_localized_stripe_price_id",
            ),
        ]

    def __str__(self) -> str:
        owner = self.plan_price if self.plan_price_id else self.product_price
        return f"{owner} → {self.currency.upper()} {self.amount_minor}"


class StripeEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stripe_id = models.CharField(max_length=255, unique=True)
    type = models.CharField(max_length=255)
    livemode = models.BooleanField()
    # DjangoJSONEncoder handles Decimal (Stripe sends `unit_amount_decimal`
    # and similar as Decimal after `to_dict()`), datetime, UUID, etc.
    payload = models.JSONField(encoder=DjangoJSONEncoder)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(null=True, blank=True)  # noqa: DJ001  # nullable TextField intentional: NULL means no error (distinguishable from empty string)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "stripe_events"
        indexes = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            models.Index(fields=["type"], name="idx_stripe_events_type"),
            models.Index(fields=["-created_at"], name="idx_stripe_events_created_at"),
        ]

    def __str__(self) -> str:
        return f"{self.stripe_id} ({self.type})"


class CreditBalance(models.Model):
    """Current credit balance for a user or an org.

    Exactly one of ``user``/``org`` is set — the XOR check mirrors
    :class:`StripeCustomer` so credit operations route the same way as billing.
    The row is a denormalised cache; :class:`CreditTransaction` is the audit log
    and the source of idempotency (unique ``stripe_session_id``).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        "users.User",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="credit_balance",
    )
    org = models.OneToOneField(
        "orgs.Org",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="credit_balance",
    )
    balance = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "credit_balances"
        constraints = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            models.CheckConstraint(
                condition=(
                    models.Q(user_id__isnull=False, org_id__isnull=True)
                    | models.Q(user_id__isnull=True, org_id__isnull=False)
                ),
                name="creditbalance_has_owner",
            ),
            models.CheckConstraint(
                condition=models.Q(balance__gte=0),
                name="creditbalance_non_negative",
            ),
        ]

    def __str__(self) -> str:
        owner = self.user if self.user_id else self.org
        return f"{owner}: {self.balance} credits"


class CreditTransaction(models.Model):
    """Ledger row for every credit grant or consume event.

    Unique on ``stripe_session_id`` (when set) so inserting twice for the same
    Stripe Checkout session is a noop — gives free idempotency when a
    ``checkout.session.completed`` webhook is retried.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "users.User",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="credit_transactions",
    )
    org = models.ForeignKey(
        "orgs.Org",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="credit_transactions",
    )
    amount = models.IntegerField(help_text="Positive = grant, negative = consume.")
    reason = models.CharField(max_length=64)
    stripe_session_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "credit_transactions"
        constraints = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            models.CheckConstraint(
                condition=(
                    models.Q(user_id__isnull=False, org_id__isnull=True)
                    | models.Q(user_id__isnull=True, org_id__isnull=False)
                ),
                name="credittransaction_has_owner",
            ),
            models.CheckConstraint(
                condition=~models.Q(amount=0),
                name="credittransaction_nonzero_amount",
            ),
        ]
        indexes = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            models.Index(fields=["user", "-created_at"], name="idx_credit_tx_user_created"),
            models.Index(fields=["org", "-created_at"], name="idx_credit_tx_org_created"),
        ]

    def __str__(self) -> str:
        owner = self.user if self.user_id else self.org
        return f"{owner}: {self.amount:+d} ({self.reason})"
