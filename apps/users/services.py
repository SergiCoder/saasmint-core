"""User-related service functions (business logic independent of HTTP)."""

from __future__ import annotations

from apps.billing.services import assign_free_plan
from apps.users.models import SocialAccount, User
from apps.users.oauth import OAuthUserInfo


def resolve_oauth_user(provider: str, user_info: OAuthUserInfo) -> User:
    """Find or create a user from OAuth provider info, linking the social account.

    Three-step lookup:
    1. By SocialAccount (returning OAuth user).
    2. By email (existing user, first OAuth login — auto-link).
    3. Brand new user.
    """
    try:
        social = SocialAccount.objects.select_related("user").get(
            provider=provider,
            provider_user_id=user_info.provider_user_id,
        )
        return social.user
    except SocialAccount.DoesNotExist:
        pass

    try:
        user = User.objects.get(email=user_info.email)
    except User.DoesNotExist:
        user = User.objects.create_user(
            email=user_info.email,
            full_name=user_info.full_name,
            avatar_url=user_info.avatar_url,
            is_verified=True,
            registration_method=provider,
        )
        assign_free_plan(user)

    # Auto-link provider for steps 2 and 3
    SocialAccount.objects.get_or_create(
        provider=provider,
        provider_user_id=user_info.provider_user_id,
        defaults={"user": user},
    )
    return user
