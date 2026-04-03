"""Django ORM implementation of the UserRepository protocol."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from saasmint_core.domain.user import AccountType, User

from apps.users.models import User as UserModel
from helpers import aget_or_none


class DjangoUserRepository:
    @staticmethod
    def _to_domain(obj: UserModel) -> User:
        return User(
            id=obj.id,
            supabase_uid=obj.supabase_uid,
            email=obj.email,
            full_name=obj.full_name,
            avatar_url=obj.avatar_url,
            account_type=AccountType(obj.account_type),
            preferred_locale=obj.preferred_locale,
            preferred_currency=obj.preferred_currency,
            is_verified=obj.is_verified,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
            deleted_at=obj.deleted_at,
        )

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await aget_or_none(UserModel, self._to_domain, id=user_id, deleted_at__isnull=True)

    async def get_by_email(self, email: str) -> User | None:
        return await aget_or_none(UserModel, self._to_domain, email=email, deleted_at__isnull=True)

    async def get_by_supabase_uid(self, supabase_uid: str) -> User | None:
        return await aget_or_none(
            UserModel, self._to_domain, supabase_uid=supabase_uid, deleted_at__isnull=True
        )

    async def save(self, user: User) -> User:
        await UserModel.objects.aupdate_or_create(
            id=user.id,
            defaults={
                "supabase_uid": user.supabase_uid,
                "email": str(user.email),
                "full_name": user.full_name,
                "avatar_url": user.avatar_url,
                "account_type": user.account_type.value,
                "preferred_locale": user.preferred_locale,
                "preferred_currency": user.preferred_currency,
                "is_verified": user.is_verified,
                "deleted_at": user.deleted_at,
            },
        )
        return user

    async def delete(self, user_id: UUID) -> None:
        obj = await UserModel.objects.filter(id=user_id, deleted_at__isnull=True).afirst()
        if obj is not None:
            obj.deleted_at = datetime.now(UTC)
            await obj.asave(update_fields=["deleted_at"])

    async def list_by_org(self, org_id: UUID, *, limit: int = 100, offset: int = 0) -> list[User]:
        from apps.orgs.models import OrgMember  # lazy import — avoids circular

        member_user_ids = OrgMember.objects.filter(org_id=org_id).values("user_id")
        return [
            self._to_domain(obj)
            async for obj in UserModel.objects.filter(
                id__in=member_user_ids, deleted_at__isnull=True
            )[offset : offset + limit]
        ]
