"""Tests for SupabaseJWTAuthentication — all branches covered."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from django.conf import settings
from rest_framework.exceptions import AuthenticationFailed

from apps.billing.models import Plan, PlanPrice, Subscription
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
    def test_auto_create_assigns_free_plan(self):
        """New users get a free-plan subscription automatically."""
        plan = Plan.objects.create(
            name="Personal Free",
            context="personal",
            tier="free",
            interval="month",
            is_active=True,
        )
        PlanPrice.objects.create(plan=plan, stripe_price_id="price_free_usd", amount=0)

        token = _make_token(sub="sup_free_plan", email="free@example.com")
        result_user, _ = self.auth.authenticate(_make_request(token))

        sub = Subscription.objects.get(user=result_user)
        assert sub.status == "active"
        assert sub.plan == plan
        assert sub.stripe_id is None
        assert sub.stripe_customer is None

    @pytest.mark.django_db
    def test_missing_email_claim_when_user_not_found_raises(self):
        token = _make_token(sub="sup_no_email", email="")
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed, match="email"):
            self.auth.authenticate(request)

    @pytest.mark.django_db
    def test_admin_deactivated_user_raises(self):
        """An admin-deactivated user (is_active=False) cannot re-authenticate."""
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
    def test_self_deleted_user_rejected(self):
        """A soft-deleted user (deleted_at set, is_active=True) is always rejected."""
        user = User.objects.create_user(
            email="deleted@example.com",
            supabase_uid="sup_deleted",
        )
        user.deleted_at = datetime.now(UTC)
        user.save()
        token = _make_token(sub="sup_deleted", email="deleted@example.com")
        request = _make_request(token)

        with pytest.raises(AuthenticationFailed) as exc_info:
            self.auth.authenticate(request)
        assert exc_info.value.detail["code"] == "account_deleted"

    @pytest.mark.django_db
    def test_scheduled_deletion_past_due_rejected(self):
        """A user whose scheduled_deletion_at has passed is rejected."""
        user = User.objects.create_user(
            email="scheduled@example.com",
            supabase_uid="sup_scheduled",
        )
        user.scheduled_deletion_at = datetime.now(UTC) - timedelta(hours=1)
        user.save()
        token = _make_token(sub="sup_scheduled", email="scheduled@example.com")
        request = _make_request(token)

        with pytest.raises(AuthenticationFailed) as exc_info:
            self.auth.authenticate(request)
        assert exc_info.value.detail["code"] == "account_deleted"

    @pytest.mark.django_db
    def test_scheduled_deletion_future_allowed(self):
        """A user whose scheduled_deletion_at is in the future can still authenticate."""
        user = User.objects.create_user(
            email="future@example.com",
            supabase_uid="sup_future",
        )
        user.scheduled_deletion_at = datetime.now(UTC) + timedelta(days=10)
        user.save()
        token = _make_token(sub="sup_future", email="future@example.com")
        request = _make_request(token)

        result_user, _ = self.auth.authenticate(request)
        assert result_user.pk == user.pk

    @pytest.mark.django_db
    def test_auto_create_persists_full_name_from_metadata(self):
        """When auto-creating a user, full_name from user_metadata is persisted."""
        payload = {
            "sub": "sup_fullname",
            "email": "fname@example.com",
            "user_metadata": {"email_verified": True, "full_name": "  Jane Doe  "},
            "aud": "authenticated",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        }
        token = jwt.encode(payload, SECRET, algorithm="HS256")
        request = _make_request(token)

        result_user, _ = self.auth.authenticate(request)
        assert result_user.full_name == "Jane Doe"

    @pytest.mark.django_db
    def test_auto_create_persists_pronouns_from_metadata(self):
        """When auto-creating a user, pronouns from user_metadata is persisted."""
        payload = {
            "sub": "sup_pronouns",
            "email": "pronouns@example.com",
            "user_metadata": {"email_verified": True, "pronouns": " they/them "},
            "aud": "authenticated",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        }
        token = jwt.encode(payload, SECRET, algorithm="HS256")
        request = _make_request(token)

        result_user, _ = self.auth.authenticate(request)
        assert result_user.pronouns == "they/them"
        # Verify persisted in DB
        db_user = User.objects.get(supabase_uid="sup_pronouns")
        assert db_user.pronouns == "they/them"

    @pytest.mark.django_db
    def test_auto_create_no_pronouns_in_metadata(self):
        """When user_metadata has no pronouns, pronouns should be None."""
        token = _make_token(sub="sup_no_pronouns", email="nopronouns@example.com")
        request = _make_request(token)

        result_user, _ = self.auth.authenticate(request)
        assert result_user.pronouns is None

    @pytest.mark.django_db
    def test_auto_create_empty_pronouns_treated_as_none(self):
        """When user_metadata has empty string pronouns, it should be treated as None."""
        payload = {
            "sub": "sup_empty_pronouns",
            "email": "emptypro@example.com",
            "user_metadata": {"email_verified": True, "pronouns": ""},
            "aud": "authenticated",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        }
        token = jwt.encode(payload, SECRET, algorithm="HS256")
        request = _make_request(token)

        result_user, _ = self.auth.authenticate(request)
        assert result_user.pronouns is None

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

    def test_non_dict_user_metadata_rejected(self):
        """When user_metadata is present but not a dict, email_verified defaults to False."""
        payload = {
            "sub": "sup_test123",
            "email": "test@example.com",
            "user_metadata": "not-a-dict",
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

    @pytest.mark.django_db
    def test_auto_create_missing_full_name_defaults_to_unknown(self):
        """When user_metadata has no full_name, it defaults to 'Unknown'."""
        payload = {
            "sub": "sup_no_name",
            "email": "noname@example.com",
            "user_metadata": {"email_verified": True},
            "aud": "authenticated",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        }
        token = jwt.encode(payload, SECRET, algorithm="HS256")
        request = _make_request(token)

        result_user, _ = self.auth.authenticate(request)
        assert result_user.full_name == "Unknown"

    @pytest.mark.django_db
    def test_auto_create_empty_full_name_defaults_to_unknown(self):
        """When user_metadata has empty/whitespace full_name, it defaults to 'Unknown'."""
        payload = {
            "sub": "sup_empty_name",
            "email": "emptyname@example.com",
            "user_metadata": {"email_verified": True, "full_name": "   "},
            "aud": "authenticated",
            "exp": datetime.now(UTC) + timedelta(hours=1),
        }
        token = jwt.encode(payload, SECRET, algorithm="HS256")
        request = _make_request(token)

        result_user, _ = self.auth.authenticate(request)
        assert result_user.full_name == "Unknown"

    @pytest.mark.django_db
    def test_scheduled_deletion_exactly_now_is_rejected(self):
        """A user whose scheduled_deletion_at is in the recent past is rejected."""
        user = User.objects.create_user(
            email="exact@example.com",
            supabase_uid="sup_exact",
        )
        user.scheduled_deletion_at = datetime.now(UTC) - timedelta(seconds=1)
        user.save()
        token = _make_token(sub="sup_exact", email="exact@example.com")
        request = _make_request(token)

        with pytest.raises(AuthenticationFailed) as exc_info:
            self.auth.authenticate(request)
        assert exc_info.value.detail["code"] == "account_deleted"

    def test_expired_token_error_code(self):
        """Expired token should return structured error with token_expired code."""
        token = _make_token(exp_delta=timedelta(hours=-1))
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed) as exc_info:
            self.auth.authenticate(request)
        assert exc_info.value.detail["code"] == "token_expired"

    def test_email_not_verified_error_code(self):
        """Unverified email should return structured error with email_not_verified code."""
        token = _make_token(email_verified=False)
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed) as exc_info:
            self.auth.authenticate(request)
        assert exc_info.value.detail["code"] == "email_not_verified"

    def test_missing_sub_error_code(self):
        """Missing sub claim should return structured error with invalid_token code."""
        token = _make_token(sub="")
        request = _make_request(token)
        with pytest.raises(AuthenticationFailed) as exc_info:
            self.auth.authenticate(request)
        assert exc_info.value.detail["code"] == "invalid_token"


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
