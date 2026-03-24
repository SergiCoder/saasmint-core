"""Tests for SupabaseJWTAuthentication — all branches covered."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import jwt
import pytest
from django.conf import settings
from django.core.cache import cache
from rest_framework.exceptions import AuthenticationFailed

from apps.users.authentication import SupabaseJWTAuthentication
from apps.users.models import User

SECRET = settings.SUPABASE_JWT_SECRET


def _make_token(
    sub: str = "sup_test123",
    email: str = "test@example.com",
    email_verified: bool = True,
    exp_delta: timedelta | None = None,
    **extra,
) -> str:
    payload = {
        "sub": sub,
        "email": email,
        "email_verified": email_verified,
        "aud": "authenticated",
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


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


class TestSupabaseJWTAuthentication:
    auth = SupabaseJWTAuthentication()

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
        token = _make_token(sub="")
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed, match="sub"):
            self.auth.authenticate(request)

    def test_email_not_verified_raises(self):
        token = _make_token(email_verified=False)
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed, match="Email not verified"):
            self.auth.authenticate(request)

    @pytest.mark.django_db
    def test_existing_active_user_returned(self):
        user = User.objects.create_user(email="existing@example.com", supabase_uid="sup_existing")
        token = _make_token(sub="sup_existing", email="existing@example.com")
        request = _make_request(token)

        result_user, result_token = self.auth.authenticate(request)
        assert result_user.pk == user.pk
        assert result_token == token

    @pytest.mark.django_db
    def test_user_cached_on_second_call(self):
        User.objects.create_user(email="cached@example.com", supabase_uid="sup_cached")
        token = _make_token(sub="sup_cached", email="cached@example.com")

        # First call: hits DB
        self.auth.authenticate(_make_request(token))
        # Second call: should use cache (no DB needed)
        result_user, _ = self.auth.authenticate(_make_request(token))
        assert result_user.email == "cached@example.com"

    @pytest.mark.django_db
    def test_auto_creates_user_when_not_found(self):
        token = _make_token(sub="sup_new_user", email="new@example.com")
        request = _make_request(token)

        result_user, _ = self.auth.authenticate(request)
        assert result_user.supabase_uid == "sup_new_user"
        assert result_user.email == "new@example.com"
        assert result_user.is_verified is True
        # Verify it was persisted
        assert User.objects.filter(supabase_uid="sup_new_user").exists()

    @pytest.mark.django_db
    def test_missing_email_claim_when_user_not_found_raises(self):
        token = _make_token(sub="sup_no_email", email="")
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed, match="email"):
            self.auth.authenticate(request)

    @pytest.mark.django_db
    def test_deactivated_user_raises(self):
        """A soft-deleted user cannot re-authenticate."""
        User.objects.create_user(
            email="deactivated@example.com",
            supabase_uid="sup_deactivated",
            is_active=False,
        )
        token = _make_token(sub="sup_deactivated", email="deactivated@example.com")
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed, match="deactivated"):
            self.auth.authenticate(request)

    @pytest.mark.django_db
    def test_soft_deleted_user_raises(self):
        """A user with deleted_at set cannot re-authenticate."""
        user = User.objects.create_user(
            email="deleted@example.com",
            supabase_uid="sup_deleted",
        )
        user.deleted_at = datetime.now(UTC)
        user.save()
        token = _make_token(sub="sup_deleted", email="deleted@example.com")
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed, match="deactivated"):
            self.auth.authenticate(request)

    def test_authenticate_header_returns_bearer(self):
        request = _make_request()
        assert self.auth.authenticate_header(request) == "Bearer"
