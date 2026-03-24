"""Tests for DjangoUserRepository."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from asgiref.sync import async_to_sync

from apps.users.models import User
from apps.users.repositories import DjangoUserRepository

pytestmark = pytest.mark.django_db


@pytest.fixture
def repo():
    return DjangoUserRepository()


@pytest.fixture
def orm_user(db):
    return User.objects.create_user(
        email="repo@example.com",
        supabase_uid="sup_repo",
        full_name="Repo User",
    )


def test_get_by_id(repo, orm_user):
    domain_user = async_to_sync(repo.get_by_id)(orm_user.id)
    assert domain_user is not None
    assert domain_user.email == "repo@example.com"


def test_get_by_id_not_found(repo):
    result = async_to_sync(repo.get_by_id)(uuid4())
    assert result is None


def test_get_by_id_excludes_soft_deleted(repo, orm_user):
    orm_user.deleted_at = datetime.now(UTC)
    orm_user.save()
    result = async_to_sync(repo.get_by_id)(orm_user.id)
    assert result is None


def test_get_by_email(repo, orm_user):
    domain_user = async_to_sync(repo.get_by_email)("repo@example.com")
    assert domain_user is not None
    assert domain_user.supabase_uid == "sup_repo"


def test_get_by_email_not_found(repo):
    result = async_to_sync(repo.get_by_email)("nobody@example.com")
    assert result is None


def test_get_by_supabase_uid(repo, orm_user):
    domain_user = async_to_sync(repo.get_by_supabase_uid)("sup_repo")
    assert domain_user is not None
    assert domain_user.email == "repo@example.com"


def test_get_by_supabase_uid_not_found(repo):
    result = async_to_sync(repo.get_by_supabase_uid)("nonexistent")
    assert result is None


def test_save_creates_new(repo):
    from stripe_saas_core.domain.user import AccountType
    from stripe_saas_core.domain.user import User as DomainUser

    user_id = uuid4()
    domain_user = DomainUser(
        id=user_id,
        supabase_uid="sup_save_new",
        email="save_new@example.com",
        full_name="Save New",
        account_type=AccountType.PERSONAL,
        preferred_locale="en",
        preferred_currency="usd",
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    saved = async_to_sync(repo.save)(domain_user)
    assert saved.id == user_id
    assert User.objects.filter(id=user_id).exists()


def test_save_updates_existing(repo, orm_user):
    domain_user = async_to_sync(repo.get_by_id)(orm_user.id)
    assert domain_user is not None
    updated = domain_user.model_copy(update={"full_name": "Updated Via Repo"})
    async_to_sync(repo.save)(updated)
    refreshed = async_to_sync(repo.get_by_id)(orm_user.id)
    assert refreshed is not None
    assert refreshed.full_name == "Updated Via Repo"


def test_delete_soft_deletes(repo, orm_user):
    async_to_sync(repo.delete)(orm_user.id)
    result = async_to_sync(repo.get_by_id)(orm_user.id)
    assert result is None
    # ORM record should still exist with deleted_at set
    obj = User.objects.get(id=orm_user.id)
    assert obj.deleted_at is not None
