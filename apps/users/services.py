"""User-related service functions (business logic independent of HTTP)."""

from __future__ import annotations

from django.db import IntegrityError, transaction

from apps.billing.services import assign_free_plan
from apps.users.models import SocialAccount, User
from apps.users.oauth import OAuthEmailNotVerifiedError, OAuthUserInfo


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
            with transaction.atomic():
                user = User.objects.create_user(
                    email=user_info.email,
                    full_name=user_info.full_name,
                    avatar_url=user_info.avatar_url,
                    is_verified=True,
                    registration_method=provider,
                )
        except IntegrityError:
            # Race: another request created the user between our get and create
            user = User.objects.get(email=user_info.email)
        else:
            assign_free_plan(user)

    # Auto-link provider for steps 2 and 3
    SocialAccount.objects.get_or_create(
        provider=provider,
        provider_user_id=user_info.provider_user_id,
        defaults={"user": user},
    )
    return user
