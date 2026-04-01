"""Tests for SupabaseJWTAuthentication — all branches covered."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from django.conf import settings
from rest_framework.exceptions import AuthenticationFailed

from apps.users.authentication import SupabaseJWTAuthentication, _get_jwks_client
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
        "user_metadata": {"email_verified": email_verified},
        "aud": "authenticated",
        "exp": datetime.now(UTC) + (exp_delta or timedelta(hours=1)),
        **extra,
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


def _make_asymmetric_token(
    private_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey,
    algorithm: str,
    sub: str = "sup_test123",
    email: str = "test@example.com",
    exp_delta: timedelta | None = None,
) -> str:
    payload = {
        "sub": sub,
        "email": email,
        "user_metadata": {"email_verified": True},
        "aud": "authenticated",
        "exp": datetime.now(UTC) + (exp_delta or timedelta(hours=1)),
    }
    return jwt.encode(payload, private_key, algorithm=algorithm)


def _make_request(token: str | None = None) -> MagicMock:
    request = MagicMock()
    if token:
        request.META = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    else:
        request.META = {}
    return request


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

    @pytest.mark.django_db
    def test_integrity_error_on_duplicate_email(self):
        """When get_or_create races and hits an IntegrityError (e.g. duplicate email),
        the authentication should raise a clear error."""
        # Pre-create a user with the same email but a different supabase_uid
        User.objects.create_user(email="conflict@example.com", supabase_uid="sup_other_uid")
        token = _make_token(sub="sup_brand_new", email="conflict@example.com")
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed, match="already associated"):
            self.auth.authenticate(request)

    def test_authenticate_header_returns_bearer(self):
        request = _make_request()
        assert self.auth.authenticate_header(request) == "Bearer"

    def test_unverified_email_rejected(self):
        """Tokens with email_verified=False should be rejected."""
        token = _make_token(email_verified=False)
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed, match="Email not verified"):
            self.auth.authenticate(request)

    def test_missing_email_verified_claim_rejected(self):
        """Tokens without email_verified claim default to rejected."""
        payload = {
            "sub": "sup_test123",
            "email": "test@example.com",
            "aud": "authenticated",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        }
        token = jwt.encode(payload, SECRET, algorithm="HS256")
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed, match="Email not verified"):
            self.auth.authenticate(request)

    def test_unsupported_algorithm_raises(self):
        """A token with an unsupported algorithm should raise AuthenticationFailed."""
        payload = {
            "sub": "sup_test123",
            "email": "test@example.com",
            "aud": "authenticated",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        }
        token = jwt.encode(payload, SECRET, algorithm="HS256")
        request = _make_request(token)
        with patch(
            "apps.users.authentication.jwt.get_unverified_header", return_value={"alg": "PS256"}
        ):
            with pytest.raises(AuthenticationFailed, match="Unsupported token algorithm"):
                self.auth.authenticate(request)


class TestAsymmetricJWTAuthentication:
    """Tests for RS256 and ES256 (JWKS-based) authentication paths."""

    auth = SupabaseJWTAuthentication()

    @pytest.mark.django_db
    def test_rs256_token_verified_with_jwks(self):
        """RS256 tokens should be verified via the JWKS client."""
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key()

        token = _make_asymmetric_token(
            private_key, "RS256", sub="sup_rs256", email="rs@example.com"
        )
        request = _make_request(token)

        mock_signing_key = MagicMock()
        mock_signing_key.key = public_key

        mock_jwks_client = MagicMock()
        mock_jwks_client.get_signing_key_from_jwt.return_value = mock_signing_key

        with patch("apps.users.authentication._get_jwks_client", return_value=mock_jwks_client):
            result_user, result_token = self.auth.authenticate(request)

        assert result_user.supabase_uid == "sup_rs256"
        assert result_token == token
        mock_jwks_client.get_signing_key_from_jwt.assert_called_once_with(token)

    @pytest.mark.django_db
    def test_es256_token_verified_with_jwks(self):
        """ES256 tokens should be verified via the JWKS client."""
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key = private_key.public_key()

        token = _make_asymmetric_token(
            private_key, "ES256", sub="sup_es256", email="es@example.com"
        )
        request = _make_request(token)

        mock_signing_key = MagicMock()
        mock_signing_key.key = public_key

        mock_jwks_client = MagicMock()
        mock_jwks_client.get_signing_key_from_jwt.return_value = mock_signing_key

        with patch("apps.users.authentication._get_jwks_client", return_value=mock_jwks_client):
            result_user, result_token = self.auth.authenticate(request)

        assert result_user.supabase_uid == "sup_es256"
        assert result_token == token

    @pytest.mark.django_db
    def test_rs256_expired_token_raises(self):
        """An expired RS256 token should raise AuthenticationFailed."""
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key()

        token = _make_asymmetric_token(
            private_key, "RS256", sub="sup_expired_rs", exp_delta=timedelta(hours=-1)
        )
        request = _make_request(token)

        mock_signing_key = MagicMock()
        mock_signing_key.key = public_key

        mock_jwks_client = MagicMock()
        mock_jwks_client.get_signing_key_from_jwt.return_value = mock_signing_key

        with patch("apps.users.authentication._get_jwks_client", return_value=mock_jwks_client):
            with pytest.raises(AuthenticationFailed, match="expired"):
                self.auth.authenticate(request)

    def test_rs256_invalid_signature_raises(self):
        """An RS256 token signed with a wrong key should raise AuthenticationFailed."""
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        wrong_public_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        ).public_key()

        token = _make_asymmetric_token(private_key, "RS256", sub="sup_bad_sig")
        request = _make_request(token)

        mock_signing_key = MagicMock()
        mock_signing_key.key = wrong_public_key

        mock_jwks_client = MagicMock()
        mock_jwks_client.get_signing_key_from_jwt.return_value = mock_signing_key

        with patch("apps.users.authentication._get_jwks_client", return_value=mock_jwks_client):
            with pytest.raises(AuthenticationFailed, match="Invalid token"):
                self.auth.authenticate(request)

    def test_jwks_client_failure_raises(self):
        """If the JWKS client fails, AuthenticationFailed should be raised."""
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = _make_asymmetric_token(private_key, "RS256", sub="sup_jwks_fail")
        request = _make_request(token)

        mock_jwks_client = MagicMock()
        mock_jwks_client.get_signing_key_from_jwt.side_effect = jwt.PyJWKClientError(
            "JWKS unreachable"
        )

        with patch("apps.users.authentication._get_jwks_client", return_value=mock_jwks_client):
            with pytest.raises(AuthenticationFailed, match="Invalid token"):
                self.auth.authenticate(request)

    def test_jwks_connection_error_raises(self):
        """A network ConnectionError during JWKS fetch should raise AuthenticationFailed."""
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = _make_asymmetric_token(private_key, "RS256", sub="sup_conn_fail")
        request = _make_request(token)

        mock_jwks_client = MagicMock()
        mock_jwks_client.get_signing_key_from_jwt.side_effect = ConnectionError(
            "Network unreachable"
        )

        with patch("apps.users.authentication._get_jwks_client", return_value=mock_jwks_client):
            with pytest.raises(AuthenticationFailed, match="Invalid token"):
                self.auth.authenticate(request)


class TestGetJWKSClient:
    def test_returns_pyjwk_client(self):
        import apps.users.authentication as auth_mod

        auth_mod._jwks_client = None  # reset singleton for test isolation
        try:
            client = _get_jwks_client()
            assert isinstance(client, jwt.PyJWKClient)
        finally:
            auth_mod._jwks_client = None
