"""Tests for helpers.py — get_user, aget_or_none, aget_latest_or_none."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from asgiref.sync import async_to_sync

from apps.users.models import User
from helpers import aget_latest_or_none, aget_or_none, get_user


class TestGetUser:
    def test_returns_request_user(self):
        mock_user = MagicMock(spec=User)
        request = MagicMock()
        request.user = mock_user
        assert get_user(request) is mock_user


@pytest.mark.django_db
class TestAgetOrNone:
    def test_returns_converted_object(self):
        user = User.objects.create(
            email="helper@example.com",
        )

        def to_dict(obj):
            return {"email": obj.email}

        result = async_to_sync(aget_or_none)(User, to_dict, pk=user.pk)
        assert result == {"email": "helper@example.com"}

    def test_returns_none_when_not_found(self):
        result = async_to_sync(aget_or_none)(User, lambda obj: obj, pk=uuid.uuid4())
        assert result is None

    def test_raises_on_multiple_objects_returned(self):
        """aget_or_none should propagate MultipleObjectsReturned (data integrity bug)."""
        User.objects.create(
            email="dup1@example.com",
            full_name="Duplicate",
        )
        User.objects.create(
            email="dup2@example.com",
            full_name="Duplicate",
        )
        with pytest.raises(User.MultipleObjectsReturned):
            async_to_sync(aget_or_none)(User, lambda obj: obj, full_name="Duplicate")


@pytest.mark.django_db
class TestAgetLatestOrNone:
    def test_aget_latest_or_none_returns_none_for_empty_queryset(self):
        result = async_to_sync(aget_latest_or_none)(
            User.objects.filter(email="missing@example.com"),
            lambda obj: obj,
        )
        assert result is None

    def test_aget_latest_or_none_returns_latest_by_created_at(self):
        # The User model has ``created_at`` (auto_now_add). Insert three rows
        # in order; ``aget_latest_or_none`` must return the most recent one.
        a = User.objects.create(email="a@example.com", full_name="A")
        b = User.objects.create(email="b@example.com", full_name="B")
        c = User.objects.create(email="c@example.com", full_name="C")
        # Sanity: created_at strictly increases in insertion order.
        assert a.created_at < b.created_at < c.created_at

        result = async_to_sync(aget_latest_or_none)(
            User.objects.all(),
            lambda obj: obj.email,
        )
        assert result == "c@example.com"

    def test_aget_latest_or_none_uses_custom_field_name(self):
        # User has both ``created_at`` and ``updated_at``; bump the earliest
        # row's ``updated_at`` so latest-by-updated-at is the first-inserted
        # row, but latest-by-created-at is still the last-inserted row.
        from django.utils import timezone

        a = User.objects.create(email="a2@example.com", full_name="A")
        User.objects.create(email="b2@example.com", full_name="B")
        c = User.objects.create(email="c2@example.com", full_name="C")
        # Force ``a`` to have the latest updated_at.
        future = timezone.now() + __import__("datetime").timedelta(days=1)
        User.objects.filter(pk=a.pk).update(updated_at=future)

        latest_by_updated = async_to_sync(aget_latest_or_none)(
            User.objects.all(),
            lambda obj: obj.email,
            field_name="updated_at",
        )
        assert latest_by_updated == "a2@example.com"

        # Without override → default created_at — last-inserted row wins.
        latest_by_created = async_to_sync(aget_latest_or_none)(
            User.objects.all(),
            lambda obj: obj.email,
        )
        assert latest_by_created == c.email
