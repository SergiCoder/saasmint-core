"""Django ORM implementation of the UserRepository protocol."""

from __future__ import annotations

from uuid import UUID

from saasmint_core.domain.user import User

from apps.users.models import User as UserModel
from helpers import aget_or_none


class DjangoUserRepository:
    @staticmethod
    def _to_domain(obj: UserModel) -> User:
        return User(
            id=obj.id,
            email=obj.email,
            full_name=obj.full_name,
            avatar_url=obj.avatar_url,
            preferred_locale=obj.preferred_locale,
            preferred_currency=obj.preferred_currency,
            pronouns=obj.pronouns,
            is_verified=obj.is_verified,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
        )

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await aget_or_none(UserModel, self._to_domain, id=user_id)

    async def get_by_email(self, email: str) -> User | None:
        return await aget_or_none(UserModel, self._to_domain, email=email)

    async def save(self, user: User) -> User:
        await UserModel.objects.aupdate_or_create(
            id=user.id,
            defaults={
                "email": str(user.email),
                "full_name": user.full_name,
                "avatar_url": user.avatar_url,
                "preferred_locale": user.preferred_locale,
                "preferred_currency": user.preferred_currency,
                "pronouns": user.pronouns,
                "is_verified": user.is_verified,
            },
        )
        return user

    async def hard_delete(self, user_id: UUID) -> None:
        await UserModel.objects.filter(id=user_id).adelete()

    async def list_by_org(self, org_id: UUID, *, limit: int = 100, offset: int = 0) -> list[User]:
        from apps.orgs.models import OrgMember  # lazy import — avoids circular

        member_user_ids = OrgMember.objects.filter(org_id=org_id).values("user_id")
        # Explicit ordering: slicing without order_by is non-deterministic and
        # can skip/repeat rows across paginated calls.
        return [
            self._to_domain(obj)
            async for obj in UserModel.objects.filter(id__in=member_user_ids).order_by("id")[
                offset : offset + limit
            ]
        ]
