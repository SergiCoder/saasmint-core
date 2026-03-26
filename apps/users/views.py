"""User account API views."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from apps.billing.repositories import (
        DjangoStripeCustomerRepository,
        DjangoSubscriptionRepository,
    )

from asgiref.sync import async_to_sync
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from stripe_saas_core.services.gdpr import delete_user_data, export_user_data

from apps.users.repositories import DjangoUserRepository
from apps.users.serializers import UpdateUserSerializer, UserSerializer
from helpers import get_user

_user_repo = DjangoUserRepository()


def _billing_repos() -> tuple[DjangoStripeCustomerRepository, DjangoSubscriptionRepository]:
    """Lazy-import and instantiate billing repositories.

    Raises ``NotImplementedError`` if the billing app is not installed.
    """
    try:
        from apps.billing.repositories import (
            DjangoStripeCustomerRepository,
            DjangoSubscriptionRepository,
        )
    except ImportError:
        raise NotImplementedError(
            "Billing app is not installed. GDPR endpoints require apps.billing."
        ) from None

    return DjangoStripeCustomerRepository(), DjangoSubscriptionRepository()


class AccountView(APIView):
    """GET /api/v1/account — return the current user's profile."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "account"

    def get(self, request: Request) -> Response:
        return Response(UserSerializer(get_user(request)).data)

    def patch(self, request: Request) -> Response:
        """PATCH /api/v1/account — update profile fields."""
        user = get_user(request)
        ser = UpdateUserSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        if ser.validated_data:
            for field, value in ser.validated_data.items():
                setattr(user, field, value)
            user.save(update_fields=list(ser.validated_data.keys()))

        return Response(UserSerializer(user).data)

    def delete(self, request: Request) -> Response:
        """DELETE /api/v1/account — GDPR right to erasure."""
        customer_repo, subscription_repo = _billing_repos()
        user = get_user(request)
        async_to_sync(delete_user_data)(
            user_id=user.id,
            user_repo=_user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class AccountExportView(APIView):
    """GET /api/v1/account/export — GDPR right of access."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "account_export"

    def get(self, request: Request) -> Response:
        customer_repo, subscription_repo = _billing_repos()
        user = get_user(request)
        data = async_to_sync(export_user_data)(
            user_id=user.id,
            user_repo=_user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )
        return Response(data)
