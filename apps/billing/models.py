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


class PlanTier(models.TextChoices):
    FREE = "free", "Free"
    BASIC = "basic", "Basic"
    PRO = "pro", "Pro"


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
    tier = models.CharField(max_length=10, choices=PlanTier.choices, default=PlanTier.BASIC)
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

    def __str__(self) -> str:
        return f"{self.name} ({self.interval})"

    @classmethod
    def free_plans(cls) -> models.QuerySet[Plan]:
        """Queryset of active personal plans on the free tier."""
        return cls.objects.filter(
            is_active=True, context=PlanContext.PERSONAL, tier=PlanTier.FREE
        ).select_related("price")


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
    quantity = models.IntegerField(default=1)
    promotion_code_id = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001  # nullable CharField intentional: NULL means no promo code applied (distinguishable from empty string)
    discount_percent = models.IntegerField(null=True, blank=True)
    discount_end_at = models.DateTimeField(null=True, blank=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
    canceled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "subscriptions"
        get_latest_by = "created_at"
        indexes = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            models.Index(fields=["stripe_customer", "status"], name="idx_sub_customer_status"),
            models.Index(fields=["user", "status"], name="idx_sub_user_status"),
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
