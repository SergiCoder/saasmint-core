"""Tests for JWTAuthentication and token management — all branches covered."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import jwt
import pytest
from django.conf import settings
from rest_framework.exceptions import AuthenticationFailed

from apps.users.authentication import (
    JWTAuthentication,
    _hash_token,
    create_access_token,
    create_email_verification_token,
    create_password_reset_token,
    create_refresh_token,
    create_social_link_token,
    revoke_all_refresh_tokens,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_email_token,
    verify_password_reset_token,
    verify_social_link_token,
)
from apps.users.models import RefreshToken, SocialLinkRequest, User

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
    def test_create_refresh_token_returns_opaque_string(self):
        user = User.objects.create_user(email="refresh@example.com", full_name="Refresh User")
        raw = create_refresh_token(user)
        assert isinstance(raw, str)
        assert len(raw) > 20
        # Stored as hash in DB
        assert RefreshToken.objects.filter(token_hash=_hash_token(raw)).exists()

    @pytest.mark.django_db
    def test_create_refresh_token_creates_db_record(self):
        user = User.objects.create_user(email="rt_db@example.com", full_name="RT DB")
        raw = create_refresh_token(user)
        rt = RefreshToken.objects.get(token_hash=_hash_token(raw))
        assert rt.user_id == user.id
        assert rt.revoked_at is None
        assert rt.expires_at > datetime.now(UTC)


@pytest.mark.django_db
class TestRefreshTokenRotation:
    def test_rotate_returns_new_token_and_user(self):
        user = User.objects.create_user(email="rotate@example.com", full_name="Rotate")
        raw = create_refresh_token(user)

        returned_user, new_raw = rotate_refresh_token(raw)
        assert returned_user.pk == user.pk
        assert new_raw != raw
        # Old token is revoked
        old_rt = RefreshToken.objects.get(token_hash=_hash_token(raw))
        assert old_rt.revoked_at is not None
        # New token exists
        assert RefreshToken.objects.filter(token_hash=_hash_token(new_raw)).exists()

    def test_rotate_revoked_token_revokes_all(self):
        user = User.objects.create_user(email="reuse@example.com", full_name="Reuse")
        raw1 = create_refresh_token(user)
        raw2 = create_refresh_token(user)

        # Revoke raw1 first
        revoke_refresh_token(raw1)
        # Attempt reuse of revoked token should revoke all
        with pytest.raises(AuthenticationFailed, match="revoked"):
            rotate_refresh_token(raw1)
        # raw2 should also be revoked now
        rt2 = RefreshToken.objects.get(token_hash=_hash_token(raw2))
        assert rt2.revoked_at is not None

    def test_rotate_expired_token_raises(self):
        user = User.objects.create_user(email="expired_rt@example.com", full_name="Expired RT")
        raw = create_refresh_token(user)
        # Force expire
        rt = RefreshToken.objects.get(token_hash=_hash_token(raw))
        rt.expires_at = datetime.now(UTC) - timedelta(hours=1)
        rt.save(update_fields=["expires_at"])

        with pytest.raises(AuthenticationFailed, match="expired"):
            rotate_refresh_token(raw)

    def test_rotate_nonexistent_token_raises(self):
        with pytest.raises(AuthenticationFailed, match="Invalid"):
            rotate_refresh_token("nonexistent-token")

    def test_rotate_inactive_user_raises(self):
        user = User.objects.create_user(
            email="inactive_rt@example.com", full_name="Inactive RT", is_active=False
        )
        raw = create_refresh_token(user)
        with pytest.raises(AuthenticationFailed, match="User not found"):
            rotate_refresh_token(raw)


@pytest.mark.django_db
class TestRevokeRefreshToken:
    def test_revoke_single_token(self):
        user = User.objects.create_user(email="revoke1@example.com", full_name="Revoke1")
        raw = create_refresh_token(user)
        revoke_refresh_token(raw)
        rt = RefreshToken.objects.get(token_hash=_hash_token(raw))
        assert rt.revoked_at is not None

    def test_revoke_nonexistent_is_noop(self):
        revoke_refresh_token("does-not-exist")

    def test_revoke_all_for_user(self):
        user = User.objects.create_user(email="revokeall@example.com", full_name="RevokeAll")
        raw1 = create_refresh_token(user)
        raw2 = create_refresh_token(user)
        revoke_all_refresh_tokens(user)
        assert RefreshToken.objects.filter(user=user, revoked_at__isnull=True).count() == 0
        assert (
            RefreshToken.objects.filter(
                token_hash__in=[_hash_token(raw1), _hash_token(raw2)],
                revoked_at__isnull=False,
            ).count()
            == 2
        )


@pytest.mark.django_db
class TestEmailVerificationToken:
    def test_create_and_verify(self):
        user = User.objects.create_user(email="verify@example.com", full_name="Verify")
        raw = create_email_verification_token(user)
        returned_user = verify_email_token(raw)
        assert returned_user.pk == user.pk

    def test_verify_invalid_token_raises(self):
        with pytest.raises(AuthenticationFailed, match="Invalid"):
            verify_email_token("bad-token")

    def test_verify_used_token_raises(self):
        user = User.objects.create_user(email="used_vt@example.com", full_name="UsedVT")
        raw = create_email_verification_token(user)
        verify_email_token(raw)  # consume it
        with pytest.raises(AuthenticationFailed, match="already been used"):
            verify_email_token(raw)

    def test_verify_expired_token_raises(self):
        from apps.users.models import EmailVerificationToken

        user = User.objects.create_user(email="exp_vt@example.com", full_name="ExpVT")
        raw = create_email_verification_token(user)
        evt = EmailVerificationToken.objects.get(token_hash=_hash_token(raw))
        evt.expires_at = datetime.now(UTC) - timedelta(hours=1)
        evt.save(update_fields=["expires_at"])
        with pytest.raises(AuthenticationFailed, match="expired"):
            verify_email_token(raw)


@pytest.mark.django_db
class TestPasswordResetToken:
    def test_create_and_verify(self):
        user = User.objects.create_user(email="reset@example.com", full_name="Reset")
        raw = create_password_reset_token(user)
        returned_user = verify_password_reset_token(raw)
        assert returned_user.pk == user.pk

    def test_verify_invalid_token_raises(self):
        with pytest.raises(AuthenticationFailed, match="Invalid"):
            verify_password_reset_token("bad-token")

    def test_verify_used_token_raises(self):
        user = User.objects.create_user(email="used_prt@example.com", full_name="UsedPRT")
        raw = create_password_reset_token(user)
        verify_password_reset_token(raw)
        with pytest.raises(AuthenticationFailed, match="already been used"):
            verify_password_reset_token(raw)

    def test_verify_expired_token_raises(self):
        from apps.users.models import PasswordResetToken

        user = User.objects.create_user(email="exp_prt@example.com", full_name="ExpPRT")
        raw = create_password_reset_token(user)
        prt = PasswordResetToken.objects.get(token_hash=_hash_token(raw))
        prt.expires_at = datetime.now(UTC) - timedelta(hours=1)
        prt.save(update_fields=["expires_at"])
        with pytest.raises(AuthenticationFailed, match="expired"):
            verify_password_reset_token(raw)


@pytest.mark.django_db
class TestSocialLinkToken:
    def test_create_persists_triple_binding(self):
        user = User.objects.create_user(email="link@example.com", full_name="Link")
        raw = create_social_link_token(
            user,
            provider="microsoft",
            provider_user_id="ms-abc",
            full_name="Link User",
            avatar_url="https://example.com/a.png",
        )
        row = SocialLinkRequest.objects.get(token_hash=_hash_token(raw))
        assert row.user_id == user.id
        assert row.provider == "microsoft"
        assert row.provider_user_id == "ms-abc"
        assert row.full_name == "Link User"
        assert row.avatar_url == "https://example.com/a.png"
        assert row.used_at is None

    def test_verify_returns_row_and_marks_used(self):
        user = User.objects.create_user(email="link2@example.com", full_name="Link2")
        raw = create_social_link_token(
            user,
            provider="microsoft",
            provider_user_id="ms-2",
            full_name="L2",
            avatar_url=None,
        )
        row = verify_social_link_token(raw)
        assert row.user_id == user.id
        assert row.provider == "microsoft"
        assert row.used_at is not None

    def test_verify_invalid_token_raises_invalid_token(self):
        with pytest.raises(AuthenticationFailed) as excinfo:
            verify_social_link_token("not-a-real-token")
        assert excinfo.value.detail.get("code") == "invalid_token"  # type: ignore[union-attr]

    def test_verify_used_token_raises_token_used(self):
        user = User.objects.create_user(email="used_slt@example.com", full_name="UsedSLT")
        raw = create_social_link_token(
            user,
            provider="microsoft",
            provider_user_id="ms-3",
            full_name="",
            avatar_url=None,
        )
        verify_social_link_token(raw)
        with pytest.raises(AuthenticationFailed) as excinfo:
            verify_social_link_token(raw)
        assert excinfo.value.detail.get("code") == "token_used"  # type: ignore[union-attr]

    def test_verify_expired_token_raises_token_expired(self):
        user = User.objects.create_user(email="exp_slt@example.com", full_name="ExpSLT")
        raw = create_social_link_token(
            user,
            provider="microsoft",
            provider_user_id="ms-4",
            full_name="",
            avatar_url=None,
        )
        SocialLinkRequest.objects.filter(token_hash=_hash_token(raw)).update(
            expires_at=datetime.now(UTC) - timedelta(minutes=1)
        )
        with pytest.raises(AuthenticationFailed) as excinfo:
            verify_social_link_token(raw)
        assert excinfo.value.detail.get("code") == "token_expired"  # type: ignore[union-attr]

    def test_verify_inactive_user_raises_user_not_found(self):
        user = User.objects.create_user(email="inact_slt@example.com", full_name="Inact")
        raw = create_social_link_token(
            user,
            provider="microsoft",
            provider_user_id="ms-5",
            full_name="",
            avatar_url=None,
        )
        user.is_active = False
        user.save(update_fields=["is_active"])
        with pytest.raises(AuthenticationFailed) as excinfo:
            verify_social_link_token(raw)
        assert excinfo.value.detail.get("code") == "user_not_found"  # type: ignore[union-attr]
