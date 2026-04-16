"""User account API views."""

from __future__ import annotations

import io
import uuid
from typing import TYPE_CHECKING, ClassVar

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from drf_spectacular.utils import extend_schema, inline_serializer
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import serializers, status
from rest_framework.parsers import MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from saasmint_core.services.gdpr import (
    delete_account,
    export_user_data,
)

from apps.base_views import AccountScopedView
from apps.billing.repositories import get_billing_repos
from apps.users.repositories import DjangoUserRepository
from apps.users.serializers import UpdateUserSerializer, UserSerializer
from helpers import get_user

if TYPE_CHECKING:
    from apps.billing.repositories import (
        DjangoStripeCustomerRepository,
        DjangoSubscriptionRepository,
    )


def _get_account_repos() -> tuple[
    DjangoUserRepository,
    DjangoStripeCustomerRepository,
    DjangoSubscriptionRepository,
]:
    """Assemble the repo tuple consumed by GDPR helpers (delete/export).

    Exposed as a factory (mirroring ``get_billing_repos`` / ``get_webhook_repos``)
    so tests can swap one call target rather than patching three module-level
    singletons in every consumer module.
    """
    billing = get_billing_repos()
    return DjangoUserRepository(), billing.customers, billing.subscriptions


class AccountView(AccountScopedView):
    """GET /api/v1/account — return the current user's profile."""

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

        user = get_user(request)

        async def _pre_delete(user_id: uuid.UUID) -> None:
            # If owner: delete owned orgs (cascades member account deletion)
            await sync_to_async(delete_orgs_created_by_user)(user_id)
            # If non-owner member: remove from every org and decrement each
            # seat count. `.afirst()` missed the multi-membership case.
            memberships = OrgMember.objects.filter(user_id=user_id)
            org_ids = [m.org_id async for m in memberships]
            if org_ids:
                await memberships.adelete()
                for org_id in org_ids:
                    await sync_to_async(decrement_subscription_seats)(org_id)

        user_repo, customer_repo, subscription_repo = _get_account_repos()
        async_to_sync(delete_account)(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
            pre_delete_hook=_pre_delete,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class AccountExportView(AccountScopedView):
    """GET /api/v1/account/export — GDPR right of access."""

    throttle_scope = "account_export"

    @extend_schema(
        responses={
            200: inline_serializer(
                "AccountExportResponse",
                {
                    "user": serializers.DictField(),
                    "stripe_customer": serializers.DictField(required=False),
                    "subscriptions": serializers.ListField(child=serializers.DictField()),
                },
            )
        },
        tags=["account"],
    )
    def get(self, request: Request) -> Response:
        user = get_user(request)
        user_repo, customer_repo, subscription_repo = _get_account_repos()
        data = async_to_sync(export_user_data)(
            user_id=user.id,
            user_repo=user_repo,
            customer_repo=customer_repo,
            subscription_repo=subscription_repo,
        )
        return Response(data)


_MAX_AVATAR_SIZE = 5 * 1024 * 1024  # 5 MB upload cap
_AVATAR_DIM = 128  # final square dimension
_AVATAR_WEBP_QUALITY = 80
_ALLOWED_AVATAR_INPUT_FORMATS: frozenset[str] = frozenset({"JPEG", "PNG", "WEBP", "GIF"})


def _delete_local_avatar(avatar_url: str | None) -> None:
    """Remove a locally-stored avatar file if it exists."""
    if avatar_url and avatar_url.startswith(settings.MEDIA_URL):
        old_path = avatar_url.removeprefix(settings.MEDIA_URL)
        if default_storage.exists(old_path):
            default_storage.delete(old_path)


class _AvatarUploadSerializer(serializers.Serializer["_AvatarUploadSerializer"]):
    avatar = serializers.ImageField(max_length=255)

    def validate_avatar(self, value: object) -> object:
        size = getattr(value, "size", None)
        if size is not None and size > _MAX_AVATAR_SIZE:
            raise serializers.ValidationError("File too large (max 5 MB).")
        return value


class AvatarView(AccountScopedView):
    """POST/DELETE /api/v1/account/avatar/ — upload or delete avatar."""

    parser_classes: ClassVar[list[type[MultiPartParser]]] = [MultiPartParser]  # type: ignore[misc]

    @extend_schema(
        request=_AvatarUploadSerializer,
        responses={
            200: inline_serializer("AvatarResponse", {"avatar_url": serializers.URLField()})
        },
        tags=["account"],
    )
    def post(self, request: Request) -> Response:
        """Upload avatar (multipart), return { avatar_url }.

        The uploaded image is decoded with Pillow, re-encoded as a 128x128 WebP,
        and stored. The original bytes and client-supplied filename/content_type
        are never written to storage — this blocks stored-XSS from polyglot or
        mis-typed uploads (e.g. ``foo.svg``/``foo.html`` claiming to be images).
        """
        user = get_user(request)

        ser = _AvatarUploadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        file = ser.validated_data["avatar"]

        file.seek(0)
        try:
            with Image.open(file) as opened:
                fmt = (opened.format or "").upper()
                if fmt not in _ALLOWED_AVATAR_INPUT_FORMATS:
                    raise serializers.ValidationError(
                        {"avatar": ["Unsupported image type."]},
                    )
                img: Image.Image = ImageOps.exif_transpose(opened) or opened
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
                img.thumbnail((_AVATAR_DIM, _AVATAR_DIM), Image.Resampling.LANCZOS)

                buffer = io.BytesIO()
                img.save(
                    buffer,
                    format="WEBP",
                    quality=_AVATAR_WEBP_QUALITY,
                    method=6,
                )
        except (UnidentifiedImageError, OSError) as exc:
            raise serializers.ValidationError(
                {"avatar": ["Invalid image file."]},
            ) from exc

        _delete_local_avatar(user.avatar_url)

        path = f"avatars/{user.id}/{uuid.uuid4().hex}.webp"
        saved_path = default_storage.save(path, ContentFile(buffer.getvalue()))
        avatar_url = request.build_absolute_uri(f"{settings.MEDIA_URL}{saved_path}")

        user.avatar_url = avatar_url
        user.save(update_fields=["avatar_url", "updated_at"])

        return Response(
            {"avatar_url": avatar_url},
            status=status.HTTP_200_OK,
        )

    @extend_schema(responses={204: None}, tags=["account"])
    def delete(self, request: Request) -> Response:
        """Delete avatar."""
        user = get_user(request)

        _delete_local_avatar(user.avatar_url)

        user.avatar_url = None
        user.save(update_fields=["avatar_url", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
