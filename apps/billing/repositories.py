"""Django ORM implementations of billing repository protocols."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.db.models import Q
from saasmint_core.domain.product import Product, ProductPrice, ProductType
from saasmint_core.domain.stripe_customer import StripeCustomer
from saasmint_core.domain.stripe_event import StripeEvent
from saasmint_core.domain.subscription import (
    ACTIVE_SUBSCRIPTION_STATUSES,
    Plan,
    PlanContext,
    PlanInterval,
    PlanPrice,
    PlanTier,
    Subscription,
    SubscriptionStatus,
)

from apps.billing.models import Plan as PlanModel
from apps.billing.models import PlanPrice as PlanPriceModel
from apps.billing.models import Product as ProductModel
from apps.billing.models import ProductPrice as ProductPriceModel
from apps.billing.models import StripeCustomer as StripeCustomerModel
from apps.billing.models import StripeEvent as StripeEventModel
from apps.billing.models import Subscription as SubscriptionModel
from helpers import aget_or_none

if TYPE_CHECKING:
    from saasmint_core.services.webhooks import WebhookRepos

logger = logging.getLogger(__name__)


class DjangoStripeCustomerRepository:
    @staticmethod
    def _to_domain(obj: StripeCustomerModel) -> StripeCustomer:
        return StripeCustomer(
            id=obj.id,
            stripe_id=obj.stripe_id,
            user_id=obj.user_id,
            org_id=obj.org_id,
            livemode=obj.livemode,
            created_at=obj.created_at,
        )

    async def get_by_id(self, customer_id: UUID) -> StripeCustomer | None:
        return await aget_or_none(StripeCustomerModel, self._to_domain, id=customer_id)

    async def get_by_stripe_id(self, stripe_id: str) -> StripeCustomer | None:
        return await aget_or_none(StripeCustomerModel, self._to_domain, stripe_id=stripe_id)

    async def get_by_user_id(self, user_id: UUID) -> StripeCustomer | None:
        return await aget_or_none(StripeCustomerModel, self._to_domain, user_id=user_id)

    async def get_by_org_id(self, org_id: UUID) -> StripeCustomer | None:
        return await aget_or_none(StripeCustomerModel, self._to_domain, org_id=org_id)

    async def save(self, customer: StripeCustomer) -> StripeCustomer:
        lookup: dict[str, Any] = {}
        if customer.user_id:
            lookup["user_id"] = customer.user_id
        elif customer.org_id:
            lookup["org_id"] = customer.org_id
        else:
            lookup["id"] = customer.id

        defaults: dict[str, object] = {
            "stripe_id": customer.stripe_id,
            "user_id": customer.user_id,
            "org_id": customer.org_id,
            "livemode": customer.livemode,
        }

        await StripeCustomerModel.objects.aupdate_or_create(
            **lookup,
            defaults=defaults,
        )
        return customer

    async def delete(self, customer_id: UUID) -> None:
        await StripeCustomerModel.objects.filter(id=customer_id).adelete()


class DjangoSubscriptionRepository:
    @staticmethod
    def _to_domain(obj: SubscriptionModel) -> Subscription:
        return Subscription(
            id=obj.id,
            stripe_id=obj.stripe_id,
            stripe_customer_id=obj.stripe_customer_id,
            user_id=obj.user_id,
            status=SubscriptionStatus(obj.status),
            plan_id=obj.plan_id,
            quantity=obj.quantity,
            trial_ends_at=obj.trial_ends_at,
            current_period_start=obj.current_period_start,
            current_period_end=obj.current_period_end,
            canceled_at=obj.canceled_at,
            created_at=obj.created_at,
        )

    async def get_by_id(self, subscription_id: UUID) -> Subscription | None:
        return await aget_or_none(SubscriptionModel, self._to_domain, id=subscription_id)

    async def get_by_stripe_id(self, stripe_id: str) -> Subscription | None:
        return await aget_or_none(SubscriptionModel, self._to_domain, stripe_id=stripe_id)

    async def _get_latest_active(self, **filter_kwargs: object) -> Subscription | None:
        try:
            obj = await SubscriptionModel.objects.filter(
                status__in=ACTIVE_SUBSCRIPTION_STATUSES,
                **filter_kwargs,
            ).alatest("created_at")
            return self._to_domain(obj)
        except SubscriptionModel.DoesNotExist:
            return None

    async def get_active_for_user(self, user_id: UUID) -> Subscription | None:
        try:
            obj = await SubscriptionModel.objects.filter(
                Q(user_id=user_id) | Q(stripe_customer__user_id=user_id),
                status__in=ACTIVE_SUBSCRIPTION_STATUSES,
            ).alatest("created_at")
            return self._to_domain(obj)
        except SubscriptionModel.DoesNotExist:
            return None

    async def get_active_for_customer(self, stripe_customer_id: UUID) -> Subscription | None:
        try:
            obj = await SubscriptionModel.objects.aget(
                stripe_customer_id=stripe_customer_id,
                status__in=ACTIVE_SUBSCRIPTION_STATUSES,
            )
            return self._to_domain(obj)
        except SubscriptionModel.DoesNotExist:
            return None
        except SubscriptionModel.MultipleObjectsReturned:
            logger.error(
                "Multiple active subscriptions for customer %s — returning latest",
                stripe_customer_id,
            )
            return await self._get_latest_active(stripe_customer_id=stripe_customer_id)

    async def save(self, subscription: Subscription) -> Subscription:
        await SubscriptionModel.objects.aupdate_or_create(
            id=subscription.id,
            defaults={
                "stripe_id": subscription.stripe_id,
                "stripe_customer_id": subscription.stripe_customer_id,
                "user_id": subscription.user_id,
                "status": subscription.status.value,
                "plan_id": subscription.plan_id,
                "quantity": subscription.quantity,
                "trial_ends_at": subscription.trial_ends_at,
                "current_period_start": subscription.current_period_start,
                "current_period_end": subscription.current_period_end,
                "canceled_at": subscription.canceled_at,
            },
        )
        return subscription

    async def delete(self, subscription_id: UUID) -> None:
        await SubscriptionModel.objects.filter(id=subscription_id).adelete()

    async def delete_free_for_user(self, user_id: UUID) -> int:
        """Delete any free (stripe_id IS NULL) subscriptions belonging to *user_id*.

        Used when a user upgrades from free → paid: the placeholder free row is
        superseded by the new Stripe-backed subscription created via webhook.
        Returns the number of rows deleted.
        """
        deleted, _ = await SubscriptionModel.objects.filter(
            user_id=user_id, stripe_id__isnull=True
        ).adelete()
        return deleted


class DjangoPlanRepository:
    @staticmethod
    def _plan_to_domain(obj: PlanModel) -> Plan:
        return Plan(
            id=obj.id,
            name=obj.name,
            description=obj.description,
            context=PlanContext(obj.context),
            tier=PlanTier(obj.tier),
            interval=PlanInterval(obj.interval),
            is_active=obj.is_active,
        )

    @staticmethod
    def _price_to_domain(obj: PlanPriceModel) -> PlanPrice:
        return PlanPrice(
            id=obj.id,
            plan_id=obj.plan_id,
            stripe_price_id=obj.stripe_price_id,
            amount=obj.amount,
        )

    async def get_by_id(self, plan_id: UUID) -> Plan | None:
        return await aget_or_none(PlanModel, self._plan_to_domain, id=plan_id)

    async def list_active(self) -> list[Plan]:
        return [self._plan_to_domain(obj) async for obj in PlanModel.objects.filter(is_active=True)]

    async def list_active_by_context(self, context: PlanContext) -> list[Plan]:
        return [
            self._plan_to_domain(obj)
            async for obj in PlanModel.objects.filter(is_active=True, context=context)
        ]

    async def get_free_plan(self) -> Plan | None:
        obj = await PlanModel.free_plans().afirst()
        return self._plan_to_domain(obj) if obj is not None else None

    async def get_price(self, plan_id: UUID) -> PlanPrice | None:
        return await aget_or_none(PlanPriceModel, self._price_to_domain, plan_id=plan_id)

    async def get_price_by_stripe_id(self, stripe_price_id: str) -> PlanPrice | None:
        return await aget_or_none(
            PlanPriceModel, self._price_to_domain, stripe_price_id=stripe_price_id
        )


class DjangoProductRepository:
    @staticmethod
    def _product_to_domain(obj: ProductModel) -> Product:
        return Product(
            id=obj.id,
            name=obj.name,
            type=ProductType(obj.type),
            credits=obj.credits,
            is_active=obj.is_active,
        )

    @staticmethod
    def _price_to_domain(obj: ProductPriceModel) -> ProductPrice:
        return ProductPrice(
            id=obj.id,
            product_id=obj.product_id,
            stripe_price_id=obj.stripe_price_id,
            amount=obj.amount,
        )

    async def get_by_id(self, product_id: UUID) -> Product | None:
        return await aget_or_none(ProductModel, self._product_to_domain, id=product_id)

    async def list_active(self) -> list[Product]:
        return [
            self._product_to_domain(obj)
            async for obj in ProductModel.objects.filter(is_active=True)
        ]

    async def get_price(self, product_id: UUID) -> ProductPrice | None:
        return await aget_or_none(ProductPriceModel, self._price_to_domain, product_id=product_id)

    async def get_price_by_stripe_id(self, stripe_price_id: str) -> ProductPrice | None:
        return await aget_or_none(
            ProductPriceModel, self._price_to_domain, stripe_price_id=stripe_price_id
        )


class DjangoStripeEventRepository:
    @staticmethod
    def _to_domain(obj: StripeEventModel) -> StripeEvent:
        return StripeEvent(
            id=obj.id,
            stripe_id=obj.stripe_id,
            type=obj.type,
            livemode=obj.livemode,
            payload=obj.payload,
            processed_at=obj.processed_at,
            error=obj.error,
            created_at=obj.created_at,
        )

    async def exists(self, stripe_id: str) -> bool:
        return await StripeEventModel.objects.filter(stripe_id=stripe_id).aexists()

    async def save(self, event: StripeEvent) -> StripeEvent:
        await StripeEventModel.objects.aupdate_or_create(
            id=event.id,
            defaults={
                "stripe_id": event.stripe_id,
                "type": event.type,
                "livemode": event.livemode,
                "payload": event.payload,
                "processed_at": event.processed_at,
                "error": event.error,
            },
        )
        return event

    async def save_if_new(self, event: StripeEvent) -> bool:
        _, created = await StripeEventModel.objects.aget_or_create(
            stripe_id=event.stripe_id,
            defaults={
                "id": event.id,
                "type": event.type,
                "livemode": event.livemode,
                "payload": event.payload,
            },
        )
        return created

    async def mark_processed(self, stripe_id: str) -> None:
        await StripeEventModel.objects.filter(stripe_id=stripe_id).aupdate(
            processed_at=datetime.now(UTC),
            error=None,
        )

    async def mark_failed(self, stripe_id: str, error: str) -> None:
        await StripeEventModel.objects.filter(stripe_id=stripe_id).aupdate(error=error)

    async def list_recent(self, limit: int = 50) -> list[StripeEvent]:
        capped = min(limit, 100)
        qs = StripeEventModel.objects.order_by("-created_at")[:capped]
        return [self._to_domain(obj) async for obj in qs]


def get_webhook_repos() -> WebhookRepos:
    """Build the WebhookRepos used by webhook processing (view + Celery task)."""
    from saasmint_core.services.webhooks import WebhookRepos

    from apps.orgs.services import deactivate_org, on_team_checkout_completed

    return WebhookRepos(
        events=DjangoStripeEventRepository(),
        subscriptions=DjangoSubscriptionRepository(),
        customers=DjangoStripeCustomerRepository(),
        plans=DjangoPlanRepository(),
        on_team_checkout_completed=on_team_checkout_completed,
        on_org_subscription_canceled=deactivate_org,
    )
