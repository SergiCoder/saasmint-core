"""User-related service functions (business logic independent of HTTP)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from django.db import IntegrityError, transaction

from apps.users.models import SocialAccount, User
from apps.users.oauth import OAuthEmailNotVerifiedError, OAuthUserInfo

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
TRUSTED_FOR_AUTO_LINK: frozenset[str] = frozenset({"google", "github", "microsoft"})


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
    try:
        social = SocialAccount.objects.select_related("user").get(
            provider=provider,
            provider_user_id=user_info.provider_user_id,
        )
        return OAuthResolution(kind="user", user=social.user)
    except SocialAccount.DoesNotExist:
        pass

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


def _link_or_request(
    provider: str, user_info: OAuthUserInfo, existing: User
) -> OAuthResolution:
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
