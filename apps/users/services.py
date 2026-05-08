"""User-related service functions (business logic independent of HTTP)."""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO, Literal
from urllib.parse import urlparse

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import serializers as drf_serializers

from apps.users.models import SocialAccount, User
from apps.users.oauth import OAuthEmailNotVerifiedError, OAuthUserInfo, Provider

if TYPE_CHECKING:
    from rest_framework.request import Request

# OAuth providers whose ``email_verified=True`` we trust to auto-link onto an
# existing local account. A provider qualifies when its email-ownership
# confirmation is the user's own act (clicking a verification link the
# provider sent), not an admin-mutable attribute.
#
# - Google: ``email_verified`` reflects the user's own mailbox verification.
# - GitHub: ``verified`` from ``/user/emails`` primary reflects the user's
#   own mailbox verification. Comparable strength to Google.
# - Microsoft: only when the signed id_token carries ``xms_edov=true`` (which
#   ``apps.users.oauth.exchange_code`` already gates on — without it,
#   ``email_verified`` is False, so this branch isn't reached).
#
# A provider missing from this set still works for fresh signups but cannot
# silently auto-link onto an existing local account — the user proves
# mailbox control via the email-confirm flow (see ``OAuthConfirmLinkView``).
#
# Explicit set rather than ``frozenset(p.value for p in Provider)`` so adding
# a new provider to the enum is NOT enough to grant auto-link trust — that
# decision needs to live next to evidence that the provider's email-verified
# claim is signed by the user's own act (mailbox click), not an admin-mutable
# attribute. New entries go here only after review.
TRUSTED_FOR_AUTO_LINK: frozenset[str] = frozenset(
    {Provider.GOOGLE.value, Provider.GITHUB.value, Provider.MICROSOFT.value}
)


@dataclass(frozen=True)
class OAuthResolution:
    """Outcome of ``resolve_oauth_user``.

    Two cases:

    - ``kind="user"``: caller should sign ``user`` in.
    - ``kind="collision"``: caller should mint a SocialLinkRequest for
      ``existing_user`` and email it to the user. ``existing_user`` may be
      None when the matching account is inactive — the callback should
      silently drop in that case (no email sent), preserving the same
      response shape as the active-user path to avoid enumeration leaks.
    """

    kind: Literal["user", "collision"]
    user: User | None = None
    existing_user: User | None = None


def email_is_registered(email: str) -> bool:
    """Return True if any user is already registered with this email.

    Case-insensitive to match the manager's normalize-on-save behavior — callers
    that only filter by ``email=`` miss differently-cased duplicates.
    """
    return User.objects.filter(email__iexact=email).exists()


def resolve_oauth_user(provider: str, user_info: OAuthUserInfo) -> OAuthResolution:
    """Resolve an OAuth callback into either a signed-in user or a link request.

    Three-step lookup:

    1. By SocialAccount (returning OAuth user — bypasses all checks below).
    2. By email — silently auto-link when the provider confirmed email
       ownership AND is on :data:`TRUSTED_FOR_AUTO_LINK`. Otherwise return
       a ``collision`` resolution; the caller mints an email-confirm token
       for ``existing_user`` so the user can prove mailbox control.
    3. Brand new user — only when the provider has confirmed email
       ownership.
    """
    social = (
        SocialAccount.objects.select_related("user")
        .filter(
            provider=provider,
            provider_user_id=user_info.provider_user_id,
        )
        .first()
    )
    if social is not None:
        return OAuthResolution(kind="user", user=social.user)

    # Case-insensitive: provider returning "Alice@Example.com" must match
    # a stored "alice@example.com" — otherwise we'd create a duplicate user
    # alongside the password-registered one.
    existing = User.objects.filter(email__iexact=user_info.email).first()
    if existing is not None:
        return _link_or_request(provider, user_info, existing)

    if not user_info.email_verified:
        raise OAuthEmailNotVerifiedError(f"Provider {provider} did not confirm email ownership.")

    # Atomic covers create_user + SocialAccount link so a partial failure
    # can't leave a user without the provider linked (retry would then hit
    # the email collision and follow the existing-user path).
    try:
        with transaction.atomic():
            user = User.objects.create_user(
                email=user_info.email,
                full_name=user_info.full_name,
                avatar_url=user_info.avatar_url,
                is_verified=True,
                registration_method=provider,
            )
            SocialAccount.objects.get_or_create(
                provider=provider,
                provider_user_id=user_info.provider_user_id,
                defaults={"user": user},
            )
        return OAuthResolution(kind="user", user=user)
    except IntegrityError:
        # Race: another request created the user between our get and create.
        # Reapply the trust check on the now-existing row.
        existing = User.objects.get(email__iexact=user_info.email)
        return _link_or_request(provider, user_info, existing)


def _link_or_request(provider: str, user_info: OAuthUserInfo, existing: User) -> OAuthResolution:
    """Auto-link if ``email_verified`` and the provider is trusted; otherwise return a collision."""
    if user_info.email_verified and provider in TRUSTED_FOR_AUTO_LINK:
        SocialAccount.objects.get_or_create(
            provider=provider,
            provider_user_id=user_info.provider_user_id,
            defaults={"user": existing},
        )
        return OAuthResolution(kind="user", user=existing)

    # Anti-enumeration: inactive accounts collapse to the same response
    # shape as active ones (caller treats existing_user=None as "do not
    # send the link email, but redirect identically").
    if not existing.is_active:
        return OAuthResolution(kind="collision", existing_user=None)
    return OAuthResolution(kind="collision", existing_user=existing)


# ---------------------------------------------------------------------------
# Avatar upload pipeline
# ---------------------------------------------------------------------------

_AVATAR_DIM = 128  # final square dimension
_AVATAR_WEBP_QUALITY = 80
_ALLOWED_AVATAR_INPUT_FORMATS: frozenset[str] = frozenset({"JPEG", "PNG", "WEBP", "GIF"})


def _delete_local_avatar(avatar_url: str | None) -> None:
    """Remove a locally-stored avatar file if it exists.

    Used by the avatar upload (replace previous file) and the avatar delete
    endpoints. The path-prefix check ensures we never try to delete an
    externally-hosted avatar (e.g. an OAuth provider's CDN URL): we parse
    out the URL path so the absolute form (``http://host/media/…``) the
    upload endpoint persists matches the same ``MEDIA_URL`` prefix as a
    relative form would.
    """
    if not avatar_url:
        return
    path = urlparse(avatar_url).path
    if path.startswith(settings.MEDIA_URL):
        old_path = path.removeprefix(settings.MEDIA_URL)
        if default_storage.exists(old_path):
            default_storage.delete(old_path)


def process_and_save_avatar(file: BinaryIO, user: User, request: Request) -> str:
    """Decode, normalise, encode, store and persist a user's avatar.

    Pillow round-trip strips metadata and re-encodes as a 128x128 WebP, so
    the original bytes and client-supplied filename/content_type never
    reach storage. Raises :class:`drf_serializers.ValidationError` on
    unsupported formats or undecodable input — callers (the view) propagate
    those as 400 responses with the field-shaped error envelope.
    """
    file.seek(0)
    try:
        with Image.open(file) as opened:
            fmt = (opened.format or "").upper()
            if fmt not in _ALLOWED_AVATAR_INPUT_FORMATS:
                raise drf_serializers.ValidationError(
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
        raise drf_serializers.ValidationError(
            {"avatar": ["Invalid image file."]},
        ) from exc

    _delete_local_avatar(user.avatar_url)

    path = f"avatars/{user.id}/{uuid.uuid4().hex}.webp"
    saved_path = default_storage.save(path, ContentFile(buffer.getvalue()))
    avatar_url = request.build_absolute_uri(f"{settings.MEDIA_URL}{saved_path}")

    user.avatar_url = avatar_url
    user.save(update_fields=["avatar_url", "updated_at"])
    return avatar_url
