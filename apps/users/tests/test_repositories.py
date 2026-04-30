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


def test_get_by_email(repo, orm_user):
    domain_user = async_to_sync(repo.get_by_email)("repo@example.com")
    assert domain_user is not None
    assert domain_user.email == "repo@example.com"


def test_get_by_email_not_found(repo):
    result = async_to_sync(repo.get_by_email)("nobody@example.com")
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


class TestListByOrg:
    @pytest.fixture
    def org(self, orm_user):
        from apps.orgs.models import Org

        return Org.objects.create(name="Test Org", slug="test-org", created_by=orm_user)

    @pytest.fixture
    def members(self, org, orm_user):
        from apps.orgs.models import OrgMember, OrgRole

        OrgMember.objects.create(org=org, user=orm_user, role=OrgRole.OWNER)
        extras = []
        for i in range(3):
            u = User.objects.create_user(
                email=f"member{i}@example.com",
                full_name=f"Member {i}",
            )
            OrgMember.objects.create(org=org, user=u, role=OrgRole.MEMBER)
            extras.append(u)
        return [orm_user, *extras]

    def test_returns_org_members(self, repo, org, members):
        result = async_to_sync(repo.list_by_org)(org.id)
        assert len(result) == 4
        returned_emails = {u.email for u in result}
        assert all(m.email in returned_emails for m in members)

    def test_empty_org(self, repo, org):
        result = async_to_sync(repo.list_by_org)(org.id)
        assert result == []

    def test_limit_and_offset(self, repo, org, members):
        result = async_to_sync(repo.list_by_org)(org.id, limit=2, offset=0)
        assert len(result) == 2

        result_offset = async_to_sync(repo.list_by_org)(org.id, limit=2, offset=2)
        assert len(result_offset) == 2
