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
        full_name="Repo User",
    )


def test_get_by_id(repo, orm_user):
    domain_user = async_to_sync(repo.get_by_id)(orm_user.id)
    assert domain_user is not None
    assert domain_user.email == "repo@example.com"


def test_get_by_id_not_found(repo):
    result = async_to_sync(repo.get_by_id)(uuid4())
    assert result is None


def test_save_creates_new(repo):
    from saasmint_core.domain.user import User as DomainUser

    user_id = uuid4()
    domain_user = DomainUser(
        id=user_id,
        email="save_new@example.com",
        full_name="Save New",
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


def test_hard_delete_removes_row(repo, orm_user):
    async_to_sync(repo.hard_delete)(orm_user.id)
    assert not User.objects.filter(id=orm_user.id).exists()


def test_hard_delete_nonexistent_user_is_noop(repo):
    async_to_sync(repo.hard_delete)(uuid4())


def test_to_domain_maps_pronouns(repo, orm_user):
    orm_user.pronouns = "they/them"
    orm_user.save(update_fields=["pronouns"])
    domain_user = async_to_sync(repo.get_by_id)(orm_user.id)
    assert domain_user is not None
    assert domain_user.pronouns == "they/them"
