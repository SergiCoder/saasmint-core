"""Tests for helpers.py — get_user and aget_or_none."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from asgiref.sync import async_to_sync

from apps.users.models import User
from helpers import aget_or_none, get_user


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
            supabase_uid="sup_helper",
        )

        def to_dict(obj):
            return {"email": obj.email}

        result = async_to_sync(aget_or_none)(User, to_dict, pk=user.pk)
        assert result == {"email": "helper@example.com"}

    def test_returns_none_when_not_found(self):
        result = async_to_sync(aget_or_none)(User, lambda obj: obj, pk=uuid.uuid4())
        assert result is None
