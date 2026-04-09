"""Tests for all domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from saasmint_core.domain.org import Org, OrgMember, OrgRole
from saasmint_core.domain.payment import Invoice, InvoiceStatus, Payment, PaymentStatus
from saasmint_core.domain.stripe_customer import StripeCustomer
from saasmint_core.domain.stripe_event import StripeEvent
from saasmint_core.domain.subscription import (
    Plan,
    PlanContext,
    PlanInterval,
    PlanPrice,
    PlanTier,
    Subscription,
    SubscriptionStatus,
)
from saasmint_core.domain.user import AccountType, User

NOW = datetime(2024, 1, 1, tzinfo=UTC)


# ── User ─────────────────────────────────────────────────────────────────────


def test_user_creation() -> None:
    user = User(
        id=uuid4(),
        email="alice@example.com",
        full_name="Alice Example",
        created_at=NOW,
    )
    assert user.email == "alice@example.com"
    assert user.account_type == AccountType.PERSONAL
    assert user.preferred_locale == "en"
    assert user.preferred_currency == "usd"
    assert user.is_verified is False
    assert user.full_name == "Alice Example"
    assert user.avatar_url is None
    assert user.deleted_at is None


def test_user_with_all_fields() -> None:
    uid = uuid4()
    user = User(
        id=uid,
        email="bob@example.com",
        full_name="Bob Smith",
        avatar_url="https://example.com/avatar.png",
        account_type=AccountType.ORG_MEMBER,
        preferred_locale="es",
        preferred_currency="eur",
        is_verified=True,
        created_at=NOW,
        deleted_at=NOW,
    )
    assert user.id == uid
    assert user.account_type == AccountType.ORG_MEMBER
    assert user.full_name == "Bob Smith"
    assert user.deleted_at == NOW


def test_user_is_frozen() -> None:
    user = User(id=uuid4(), email="a@b.com", full_name="Test", created_at=NOW)
    with pytest.raises(ValidationError):
        user.email = "other@b.com"  # type: ignore[misc]  # intentional: testing that frozen dataclass raises ValidationError on mutation


def test_account_type_values() -> None:
    assert AccountType.PERSONAL == "personal"
    assert AccountType.ORG_MEMBER == "org_member"


def test_user_invalid_email() -> None:
    with pytest.raises(ValidationError):
        User(id=uuid4(), email="not-an-email", full_name="Test", created_at=NOW)


# ── Org ───────────────────────────────────────────────────────────────────────


def test_org_creation() -> None:
    org = Org(
        id=uuid4(),
        name="Acme Corp",
        slug="acme-corp",
        created_by=uuid4(),
        created_at=NOW,
    )
    assert org.name == "Acme Corp"
    assert org.slug == "acme-corp"
    assert org.logo_url is None
    assert org.deleted_at is None


def test_org_with_optional_fields() -> None:
    org = Org(
        id=uuid4(),
        name="Acme",
        slug="acme",
        logo_url="https://example.com/logo.png",
        created_by=uuid4(),
        created_at=NOW,
        deleted_at=NOW,
    )
    assert org.logo_url == "https://example.com/logo.png"
    assert org.deleted_at == NOW


def test_org_is_frozen() -> None:
    org = Org(id=uuid4(), name="X", slug="x", created_by=uuid4(), created_at=NOW)
    with pytest.raises(ValidationError):
        org.name = "Y"  # type: ignore[misc]  # intentional: testing that frozen dataclass raises ValidationError on mutation


def test_org_role_values() -> None:
    assert OrgRole.OWNER == "owner"
    assert OrgRole.ADMIN == "admin"
    assert OrgRole.MEMBER == "member"


def test_org_member_creation() -> None:
    member = OrgMember(
        id=uuid4(),
        org_id=uuid4(),
        user_id=uuid4(),
        role=OrgRole.ADMIN,
        joined_at=NOW,
    )
    assert member.role == OrgRole.ADMIN
    assert member.is_billing is False


def test_org_member_billing_flag() -> None:
    member = OrgMember(
        id=uuid4(),
        org_id=uuid4(),
        user_id=uuid4(),
        role=OrgRole.OWNER,
        is_billing=True,
        joined_at=NOW,
    )
    assert member.is_billing is True


# ── StripeCustomer ────────────────────────────────────────────────────────────


def test_stripe_customer_with_user_id() -> None:
    cust = StripeCustomer(
        id=uuid4(),
        stripe_id="cus_abc",
        user_id=uuid4(),
        created_at=NOW,
    )
    assert cust.org_id is None
    assert cust.livemode is False


def test_stripe_customer_with_org_id() -> None:
    cust = StripeCustomer(
        id=uuid4(),
        stripe_id="cus_xyz",
        org_id=uuid4(),
        created_at=NOW,
    )
    assert cust.user_id is None


def test_stripe_customer_requires_exactly_one_owner_both_set() -> None:
    with pytest.raises(ValidationError):
        StripeCustomer(
            id=uuid4(),
            stripe_id="cus_bad",
            user_id=uuid4(),
            org_id=uuid4(),
            created_at=NOW,
        )


def test_stripe_customer_requires_exactly_one_owner_neither_set() -> None:
    with pytest.raises(ValidationError):
        StripeCustomer(
            id=uuid4(),
            stripe_id="cus_bad",
            created_at=NOW,
        )


def test_stripe_customer_livemode() -> None:
    cust = StripeCustomer(
        id=uuid4(),
        stripe_id="cus_live",
        user_id=uuid4(),
        livemode=True,
        created_at=NOW,
    )
    assert cust.livemode is True


# ── Subscription ──────────────────────────────────────────────────────────────


def test_subscription_status_values() -> None:
    assert SubscriptionStatus.ACTIVE == "active"
    assert SubscriptionStatus.TRIALING == "trialing"
    assert SubscriptionStatus.PAST_DUE == "past_due"
    assert SubscriptionStatus.CANCELED == "canceled"
    assert SubscriptionStatus.INCOMPLETE == "incomplete"
    assert SubscriptionStatus.INCOMPLETE_EXPIRED == "incomplete_expired"
    assert SubscriptionStatus.PAUSED == "paused"
    assert SubscriptionStatus.UNPAID == "unpaid"


def test_plan_interval_values() -> None:
    assert PlanInterval.MONTH == "month"
    assert PlanInterval.YEAR == "year"


def test_plan_context_values() -> None:
    assert PlanContext.PERSONAL == "personal"
    assert PlanContext.TEAM == "team"


def test_plan_tier_values() -> None:
    assert PlanTier.FREE == "free"
    assert PlanTier.BASIC == "basic"
    assert PlanTier.PRO == "pro"


def test_plan_default_tier_is_basic() -> None:
    plan = Plan(
        id=uuid4(),
        name="Starter",
        context=PlanContext.PERSONAL,
        interval=PlanInterval.MONTH,
    )
    assert plan.tier == PlanTier.BASIC


def test_plan_explicit_free_tier() -> None:
    plan = Plan(
        id=uuid4(),
        name="Personal Free",
        context=PlanContext.PERSONAL,
        tier=PlanTier.FREE,
        interval=PlanInterval.MONTH,
    )
    assert plan.tier == PlanTier.FREE


def test_plan_creation() -> None:
    plan = Plan(
        id=uuid4(),
        name="Starter",
        context=PlanContext.PERSONAL,
        interval=PlanInterval.YEAR,
    )
    assert plan.is_active is True


def test_plan_inactive() -> None:
    plan = Plan(
        id=uuid4(),
        name="Legacy",
        context=PlanContext.TEAM,
        interval=PlanInterval.MONTH,
        is_active=False,
    )
    assert plan.is_active is False


def test_plan_price_creation() -> None:
    price = PlanPrice(
        id=uuid4(),
        plan_id=uuid4(),
        stripe_price_id="price_abc",
        amount=999,
    )
    assert price.amount == 999


def test_subscription_creation() -> None:
    sub = Subscription(
        id=uuid4(),
        stripe_id="sub_abc",
        stripe_customer_id=uuid4(),
        status=SubscriptionStatus.TRIALING,
        plan_id=uuid4(),
        current_period_start=NOW,
        current_period_end=NOW,
        created_at=NOW,
    )
    assert sub.quantity == 1
    assert sub.promotion_code_id is None
    assert sub.discount_percent is None
    assert sub.trial_ends_at is None
    assert sub.canceled_at is None


def test_subscription_model_copy() -> None:
    sub = Subscription(
        id=uuid4(),
        stripe_id="sub_abc",
        stripe_customer_id=uuid4(),
        status=SubscriptionStatus.ACTIVE,
        plan_id=uuid4(),
        current_period_start=NOW,
        current_period_end=NOW,
        created_at=NOW,
    )
    canceled = sub.model_copy(update={"status": SubscriptionStatus.CANCELED, "canceled_at": NOW})
    assert canceled.status == SubscriptionStatus.CANCELED
    assert canceled.canceled_at == NOW
    assert sub.status == SubscriptionStatus.ACTIVE  # original unchanged


# ── Payment / Invoice ─────────────────────────────────────────────────────────


def test_payment_status_values() -> None:
    assert PaymentStatus.SUCCEEDED == "succeeded"
    assert PaymentStatus.PENDING == "pending"
    assert PaymentStatus.FAILED == "failed"
    assert PaymentStatus.CANCELED == "canceled"


def test_payment_creation() -> None:
    payment = Payment(
        id=uuid4(),
        stripe_id="pi_abc",
        stripe_customer_id=uuid4(),
        amount=5000,
        currency="usd",
        status=PaymentStatus.SUCCEEDED,
        created_at=NOW,
    )
    assert payment.description is None
    assert payment.metadata == {}


def test_payment_with_metadata() -> None:
    payment = Payment(
        id=uuid4(),
        stripe_id="pi_xyz",
        stripe_customer_id=uuid4(),
        amount=1000,
        currency="eur",
        status=PaymentStatus.PENDING,
        description="Test charge",
        metadata={"order_id": "ord_123"},
        created_at=NOW,
    )
    assert payment.metadata == {"order_id": "ord_123"}
    assert payment.description == "Test charge"


def test_invoice_status_values() -> None:
    assert InvoiceStatus.DRAFT == "draft"
    assert InvoiceStatus.OPEN == "open"
    assert InvoiceStatus.PAID == "paid"
    assert InvoiceStatus.VOID == "void"
    assert InvoiceStatus.UNCOLLECTIBLE == "uncollectible"


def test_invoice_creation() -> None:
    inv = Invoice(
        id=uuid4(),
        stripe_id="in_abc",
        stripe_customer_id=uuid4(),
        amount_due=2000,
        amount_paid=0,
        currency="usd",
        status=InvoiceStatus.OPEN,
        created_at=NOW,
    )
    assert inv.subscription_id is None
    assert inv.hosted_url is None
    assert inv.pdf_url is None
    assert inv.due_date is None


def test_invoice_with_all_fields() -> None:
    inv = Invoice(
        id=uuid4(),
        stripe_id="in_xyz",
        stripe_customer_id=uuid4(),
        subscription_id=uuid4(),
        amount_due=2000,
        amount_paid=2000,
        currency="eur",
        status=InvoiceStatus.PAID,
        hosted_url="https://invoice.stripe.com/i/xyz",
        pdf_url="https://invoice.stripe.com/i/xyz/pdf",
        due_date=NOW,
        created_at=NOW,
    )
    assert inv.status == InvoiceStatus.PAID
    assert inv.amount_paid == 2000


# ── StripeEvent ───────────────────────────────────────────────────────────────


def test_stripe_event_creation() -> None:
    event = StripeEvent(
        id=uuid4(),
        stripe_id="evt_abc",
        type="customer.subscription.created",
        livemode=False,
        payload={"id": "evt_abc", "type": "customer.subscription.created"},
        created_at=NOW,
    )
    assert event.processed_at is None
    assert event.error is None


def test_stripe_event_model_copy_processed() -> None:
    event = StripeEvent(
        id=uuid4(),
        stripe_id="evt_xyz",
        type="invoice.payment_succeeded",
        livemode=True,
        payload={},
        created_at=NOW,
    )
    processed = event.model_copy(update={"processed_at": NOW})
    assert processed.processed_at == NOW
    assert event.processed_at is None


# ── Subscription.is_free ────────────────────────────────────────────────────


def test_subscription_is_free_when_no_stripe_id() -> None:
    sub = Subscription(
        id=uuid4(),
        stripe_id=None,
        stripe_customer_id=None,
        user_id=uuid4(),
        status=SubscriptionStatus.ACTIVE,
        plan_id=uuid4(),
        current_period_start=NOW,
        current_period_end=NOW,
        created_at=NOW,
    )
    assert sub.is_free is True


def test_subscription_is_not_free_when_has_stripe_id() -> None:
    sub = Subscription(
        id=uuid4(),
        stripe_id="sub_paid",
        stripe_customer_id=uuid4(),
        status=SubscriptionStatus.ACTIVE,
        plan_id=uuid4(),
        current_period_start=NOW,
        current_period_end=NOW,
        created_at=NOW,
    )
    assert sub.is_free is False


# ── FREE_SUBSCRIPTION_PERIOD_END sentinel ───────────────────────────────────


def test_free_subscription_period_end_sentinel() -> None:
    from saasmint_core.domain.subscription import FREE_SUBSCRIPTION_PERIOD_END

    assert FREE_SUBSCRIPTION_PERIOD_END.year == 9999
    assert FREE_SUBSCRIPTION_PERIOD_END.tzinfo is not None


# ── Product / ProductPrice domain models ────────────────────────────────────


def test_product_creation() -> None:
    from saasmint_core.domain.product import Product, ProductType

    product = Product(
        id=uuid4(),
        name="100 Credits",
        type=ProductType.ONE_TIME,
        credits=100,
    )
    assert product.name == "100 Credits"
    assert product.type == ProductType.ONE_TIME
    assert product.credits == 100
    assert product.is_active is True


def test_product_inactive() -> None:
    from saasmint_core.domain.product import Product, ProductType

    product = Product(
        id=uuid4(),
        name="Retired Pack",
        type=ProductType.ONE_TIME,
        credits=50,
        is_active=False,
    )
    assert product.is_active is False


def test_product_is_frozen() -> None:
    from saasmint_core.domain.product import Product, ProductType

    product = Product(
        id=uuid4(),
        name="100 Credits",
        type=ProductType.ONE_TIME,
        credits=100,
    )
    with pytest.raises(ValidationError):
        product.name = "Changed"  # type: ignore[misc]


def test_product_price_creation() -> None:
    from saasmint_core.domain.product import ProductPrice

    price = ProductPrice(
        id=uuid4(),
        product_id=uuid4(),
        stripe_price_id="price_credits_100",
        amount=999,
    )
    assert price.amount == 999
    assert price.stripe_price_id == "price_credits_100"
