"""User account API views."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from apps.billing.repositories import (
        DjangoStripeCustomerRepository,
        DjangoSubscriptionRepository,
    )

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.files.storage import default_storage
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from saasmint_core.services.gdpr import (
    delete_account,
    export_user_data,
)

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

    @extend_schema(responses=UserSerializer, tags=["account"])
    def get(self, request: Request) -> Response:
        return Response(UserSerializer(get_user(request)).data)

    @extend_schema(request=UpdateUserSerializer, responses=UserSerializer, tags=["account"])
    def patch(self, request: Request) -> Response:
        """PATCH /api/v1/account — update profile fields."""
        user = get_user(request)
        ser = UpdateUserSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        if ser.validated_data:
            for field, value in ser.validated_data.items():
                setattr(user, field, value)
            user.save(update_fields=[*ser.validated_data.keys(), "updated_at"])

        return Response(UserSerializer(user).data)

    @extend_schema(
        request=None,
        responses={204: None},
        tags=["account"],
    )
    def delete(self, request: Request) -> Response:
        """DELETE /api/v1/account — GDPR right to erasure.

        Immediately hard-deletes the user and all associated data.
        """
        from asgiref.sync import sync_to_async

        from apps.orgs.models import OrgMember
        from apps.orgs.services import decrement_subscription_seats, delete_orgs_created_by_user

        customer_repo, subscription_repo = _billing_repos()
        user = get_user(request)

        async def _pre_delete(user_id: uuid.UUID) -> None:
            # If owner: delete owned orgs (cascades member account deletion)
            await sync_to_async(delete_orgs_created_by_user)(user_id)
            # If non-owner member: remove from org + decrement seats
            membership = await OrgMember.objects.filter(user_id=user_id).afirst()
            if membership:
                org_id = membership.org_id
                await membership.adelete()
                await sync_to_async(decrement_subscription_seats)(org_id)

        async_to_sync(delete_account)(
            user_id=user.id,
            user_repo=_user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
            pre_delete_hook=_pre_delete,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class AccountExportView(APIView):
    """GET /api/v1/account/export — GDPR right of access."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "account_export"

    @extend_schema(responses={200: dict}, tags=["account"])
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


_MAX_AVATAR_SIZE = 5 * 1024 * 1024  # 5 MB
_ALLOWED_AVATAR_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _delete_local_avatar(avatar_url: str | None) -> None:
    """Remove a locally-stored avatar file if it exists."""
    if avatar_url and avatar_url.startswith(settings.MEDIA_URL):
        old_path = avatar_url.removeprefix(settings.MEDIA_URL)
        if default_storage.exists(old_path):
            default_storage.delete(old_path)


class _AvatarUploadSerializer(serializers.Serializer["_AvatarUploadSerializer"]):
    avatar = serializers.ImageField()


class AvatarView(APIView):
    """POST/DELETE /api/v1/account/avatar/ — upload or delete avatar."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "account"
    parser_classes: ClassVar[list[type[MultiPartParser]]] = [MultiPartParser]  # type: ignore[misc]

    @extend_schema(
        request=_AvatarUploadSerializer,
        responses={201: dict},
        tags=["account"],
    )
    def post(self, request: Request) -> Response:
        """Upload avatar (multipart), return { avatar_url }."""
        user = get_user(request)

        file = request.FILES.get("avatar")
        if file is None:
            return Response(
                {"detail": "No file provided.", "code": "missing_file"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if file.content_type not in _ALLOWED_AVATAR_TYPES:
            return Response(
                {"detail": "Unsupported image type.", "code": "invalid_type"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if file.size is not None and file.size > _MAX_AVATAR_SIZE:
            return Response(
                {"detail": "File too large (max 5 MB).", "code": "file_too_large"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        _delete_local_avatar(user.avatar_url)

        ext = file.name.rsplit(".", 1)[-1] if "." in file.name else "jpg"
        path = f"avatars/{user.id}/{uuid.uuid4().hex}.{ext}"
        saved_path = default_storage.save(path, file)
        avatar_url = request.build_absolute_uri(f"{settings.MEDIA_URL}{saved_path}")

        user.avatar_url = avatar_url
        user.save(update_fields=["avatar_url", "updated_at"])

        return Response(
            {"avatar_url": avatar_url},
            status=status.HTTP_201_CREATED,
            headers={"Location": avatar_url},
        )

    @extend_schema(responses={204: None}, tags=["account"])
    def delete(self, request: Request) -> Response:
        """Delete avatar."""
        user = get_user(request)

        _delete_local_avatar(user.avatar_url)

        user.avatar_url = None
        user.save(update_fields=["avatar_url", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
