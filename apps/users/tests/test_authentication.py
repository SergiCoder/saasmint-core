"""Tests for JWTAuthentication — all branches covered."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import jwt
import pytest
from django.conf import settings
from rest_framework.exceptions import AuthenticationFailed

from apps.users.authentication import (
    JWTAuthentication,
    create_access_token,
    create_refresh_token,
)
from apps.users.models import User

SECRET = settings.SECRET_KEY


def _make_token(
    user_id: str = "00000000-0000-0000-0000-000000000001",
    token_type: str = "access",  # noqa: S107
    exp_delta: timedelta | None = None,
    **extra,
) -> str:
    payload = {
        "sub": user_id,
        "type": token_type,
        "exp": datetime.now(UTC) + (exp_delta or timedelta(hours=1)),
        **extra,
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _make_request(token: str | None = None) -> MagicMock:
    request = MagicMock()
    if token:
        request.META = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    else:
        request.META = {}
    return request


class TestJWTAuthentication:
    auth = JWTAuthentication()

    def test_no_auth_header_returns_none(self):
        request = _make_request()
        assert self.auth.authenticate(request) is None

    def test_non_bearer_header_returns_none(self):
        request = MagicMock()
        request.META = {"HTTP_AUTHORIZATION": "Basic abc123"}
        assert self.auth.authenticate(request) is None

    def test_expired_token_raises(self):
        token = _make_token(exp_delta=timedelta(hours=-1))
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed, match="expired"):
            self.auth.authenticate(request)

    def test_invalid_token_raises(self):
        request = _make_request("not.a.valid.jwt")
        with pytest.raises(AuthenticationFailed, match="Invalid token"):
            self.auth.authenticate(request)

    def test_missing_sub_claim_raises(self):
        token = _make_token(user_id="")
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed, match="sub"):
            self.auth.authenticate(request)

    def test_refresh_token_rejected_for_api_auth(self):
        token = _make_token(token_type="refresh")  # noqa: S106
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed, match="Invalid token type"):
            self.auth.authenticate(request)

    @pytest.mark.django_db
    def test_existing_active_user_returned(self):
        user = User.objects.create_user(email="existing@example.com", full_name="Existing")
        token = _make_token(user_id=str(user.id))
        request = _make_request(token)

        result_user, result_token = self.auth.authenticate(request)
        assert result_user.pk == user.pk
        assert result_token == token

    @pytest.mark.django_db
    def test_user_cached_on_second_call(self):
        user = User.objects.create_user(email="cached@example.com", full_name="Cached")
        token = _make_token(user_id=str(user.id))

        self.auth.authenticate(_make_request(token))
        result_user, _ = self.auth.authenticate(_make_request(token))
        assert result_user.email == "cached@example.com"

    @pytest.mark.django_db
    def test_nonexistent_user_raises(self):
        token = _make_token(user_id="00000000-0000-0000-0000-000000000099")
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed, match="User not found"):
            self.auth.authenticate(request)

    @pytest.mark.django_db
    def test_soft_deleted_user_rejected(self):
        user = User.objects.create_user(email="deleted@example.com", full_name="Deleted")
        user.deleted_at = datetime.now(UTC)
        user.save()
        token = _make_token(user_id=str(user.id))
        request = _make_request(token)

        with pytest.raises(AuthenticationFailed, match="User not found"):
            self.auth.authenticate(request)

    @pytest.mark.django_db
    def test_inactive_user_rejected(self):
        user = User.objects.create_user(
            email="inactive@example.com",
            full_name="Inactive",
            is_active=False,
        )
        token = _make_token(user_id=str(user.id))
        request = _make_request(token)

        with pytest.raises(AuthenticationFailed, match="User not found"):
            self.auth.authenticate(request)

    @pytest.mark.django_db
    def test_scheduled_deletion_past_due_rejected(self):
        user = User.objects.create_user(email="scheduled@example.com", full_name="Scheduled")
        user.scheduled_deletion_at = datetime.now(UTC) - timedelta(hours=1)
        user.save()
        token = _make_token(user_id=str(user.id))
        request = _make_request(token)

        with pytest.raises(AuthenticationFailed) as exc_info:
            self.auth.authenticate(request)
        assert exc_info.value.detail["code"] == "account_deleted"

    @pytest.mark.django_db
    def test_scheduled_deletion_future_allowed(self):
        user = User.objects.create_user(email="future@example.com", full_name="Future")
        user.scheduled_deletion_at = datetime.now(UTC) + timedelta(days=10)
        user.save()
        token = _make_token(user_id=str(user.id))
        request = _make_request(token)

        result_user, _ = self.auth.authenticate(request)
        assert result_user.pk == user.pk

    def test_authenticate_header_returns_bearer(self):
        request = _make_request()
        assert self.auth.authenticate_header(request) == "Bearer"

    def test_expired_token_error_code(self):
        token = _make_token(exp_delta=timedelta(hours=-1))
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed) as exc_info:
            self.auth.authenticate(request)
        assert exc_info.value.detail["code"] == "token_expired"


class TestTokenCreation:
    @pytest.mark.django_db
    def test_create_access_token_is_valid(self):
        user = User.objects.create_user(email="token@example.com", full_name="Token User")
        token = create_access_token(user)
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        assert payload["sub"] == str(user.id)
        assert payload["type"] == "access"
        assert payload["email"] == user.email

    @pytest.mark.django_db
    def test_create_refresh_token_is_valid(self):
        user = User.objects.create_user(email="refresh@example.com", full_name="Refresh User")
        token = create_refresh_token(user)
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        assert payload["sub"] == str(user.id)
        assert payload["type"] == "refresh"
