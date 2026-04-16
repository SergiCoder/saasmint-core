"""User-related service functions (business logic independent of HTTP)."""

from __future__ import annotations

from django.db import IntegrityError, transaction

from apps.billing.services import assign_free_plan
from apps.users.models import SocialAccount, User
from apps.users.oauth import OAuthEmailNotVerifiedError, OAuthUserInfo


def email_is_registered(email: str) -> bool:
    """Return True if any user is already registered with this email.

    Case-insensitive to match the manager's normalize-on-save behavior — callers
    that only filter by ``email=`` miss differently-cased duplicates.
    """
    return User.objects.filter(email__iexact=email).exists()


def resolve_oauth_user(provider: str, user_info: OAuthUserInfo) -> User:
    """Find or create a user from OAuth provider info, linking the social account.

    Three-step lookup:
    1. By SocialAccount (returning OAuth user).
    2. By email (existing user, first OAuth login — auto-link), only when the
       provider has confirmed email ownership.
    3. Brand new user, only when the provider has confirmed email ownership.
    """
    try:
        social = SocialAccount.objects.select_related("user").get(
            provider=provider,
            provider_user_id=user_info.provider_user_id,
        )
        return social.user
    except SocialAccount.DoesNotExist:
        pass

    if not user_info.email_verified:
        raise OAuthEmailNotVerifiedError(f"Provider {provider} did not confirm email ownership.")

    try:
        user = User.objects.get(email=user_info.email)
    except User.DoesNotExist:
        try:
            # Atomic covers create_user + assign_free_plan + SocialAccount link
            # so a partial failure can't leave a user without a free sub or
            # without the provider linked (retry would then hit the email
            # collision and follow the existing-user path).
            with transaction.atomic():
                user = User.objects.create_user(
                    email=user_info.email,
                    full_name=user_info.full_name,
                    avatar_url=user_info.avatar_url,
                    is_verified=True,
                    registration_method=provider,
                )
                assign_free_plan(user)
                SocialAccount.objects.get_or_create(
                    provider=provider,
                    provider_user_id=user_info.provider_user_id,
                    defaults={"user": user},
                )
            return user
        except IntegrityError:
            # Race: another request created the user between our get and create
            user = User.objects.get(email=user_info.email)

    # Auto-link provider for the existing-user path
    SocialAccount.objects.get_or_create(
        provider=provider,
        provider_user_id=user_info.provider_user_id,
        defaults={"user": user},
    )
    return user
