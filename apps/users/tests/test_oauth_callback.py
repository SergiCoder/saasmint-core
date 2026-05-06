"""Tests for OAuthCallbackView — user lookup, auto-linking, and SocialAccount creation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from apps.users.models import SocialAccount, User
from apps.users.oauth import OAuthError, OAuthUserInfo

_TEST_DRF = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {"auth": "1000/hour"},
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}


@pytest.fixture(autouse=True)
def _disable_throttle(settings):
    settings.REST_FRAMEWORK = _TEST_DRF


@pytest.fixture
def _oauth_state(client):
    """Set a valid OAuth state in the session so the callback can verify it."""
    session = client.session
    session["oauth_state"] = "test-state"
    session.save()


def _mock_exchange(
    email: str = "oauth@example.com",
    provider_user_id: str = "12345",
    email_verified: bool = True,
):
    return OAuthUserInfo(
        email=email,
        full_name="OAuth User",
        provider_user_id=provider_user_id,
        avatar_url="https://example.com/avatar.png",
        email_verified=email_verified,
    )


_PATCH_TARGET = "apps.users.auth_views.exchange_code"


def _patch_exchange(
    *,
    return_value: OAuthUserInfo | None = None,
    side_effect: BaseException | type[BaseException] | None = None,
):
    """Patch the now-async exchange_code with an AsyncMock."""
    kwargs: dict[str, object] = {"new_callable": AsyncMock}
    if return_value is not None:
        kwargs["return_value"] = return_value
    if side_effect is not None:
        kwargs["side_effect"] = side_effect
    return patch(_PATCH_TARGET, **kwargs)


@pytest.mark.django_db
class TestOAuthCallbackNewUser:
    def test_creates_user_and_social_account(self, client, _oauth_state):
        with _patch_exchange(return_value=_mock_exchange()):
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "#code=" in resp["Location"]

        user = User.objects.get(email="oauth@example.com")
        assert user.registration_method == "google"
        assert user.is_verified is True
        assert user.has_usable_password() is False

        social = SocialAccount.objects.get(user=user, provider="google")
        assert social.provider_user_id == "12345"

    def test_microsoft_verified_email_creates_user_and_logs_in(self, client, _oauth_state):
        # Happy path: Microsoft id_token carried `xms_edov: true` (Microsoft
        # has verified the email's domain belongs to the user's tenant). The
        # callback should sign the user in directly, mirroring Google/GitHub.
        info = _mock_exchange(
            email="alice@verified-tenant.com",
            provider_user_id="ms-oid-1",
            email_verified=True,
        )
        with _patch_exchange(return_value=info):
            resp = client.get(
                "/api/v1/auth/oauth/microsoft/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "#code=" in resp["Location"]
        assert "email_not_verified" not in resp["Location"]

        user = User.objects.get(email="alice@verified-tenant.com")
        assert user.registration_method == "microsoft"
        assert user.is_verified is True
        assert SocialAccount.objects.filter(
            user=user, provider="microsoft", provider_user_id="ms-oid-1"
        ).exists()

    def test_microsoft_unverified_email_redirects_to_error(self, client, _oauth_state):
        # Microsoft Graph /me does not prove email ownership, so a brand-new
        # MS sign-in must NOT auto-create a verified user — that would let a
        # tenant admin take over an existing saasmint account by setting
        # `mail=victim@example.com`. The callback redirects to the frontend
        # error page; the frontend then asks the user to verify via email.
        info = _mock_exchange(
            email="dan@example.com",
            provider_user_id="ms-1",
            email_verified=False,
        )
        with _patch_exchange(return_value=info):
            resp = client.get(
                "/api/v1/auth/oauth/microsoft/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "email_not_verified" in resp["Location"]
        assert not User.objects.filter(email="dan@example.com").exists()

    def test_no_subscription_created(self, client, _oauth_state):
        """OAuth-created users no longer get a free Subscription assigned —
        Subscription is a pure Stripe mirror after this refactor."""
        from apps.billing.models import Subscription

        with _patch_exchange(return_value=_mock_exchange()):
            client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        user = User.objects.get(email="oauth@example.com")
        assert not Subscription.objects.filter(user=user).exists()


@pytest.mark.django_db
class TestOAuthCallbackExistingEmailUser:
    def test_auto_links_social_account(self, client, _oauth_state):
        user = User.objects.create_user(
            email="existing@example.com",
            password="testpass123",  # noqa: S106
            full_name="Existing User",
        )
        info = _mock_exchange(email="existing@example.com", provider_user_id="g-99")
        with _patch_exchange(return_value=info):
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "#code=" in resp["Location"]

        # User keeps original registration_method
        user.refresh_from_db()
        assert user.registration_method == "email"

        # SocialAccount was auto-linked
        assert SocialAccount.objects.filter(user=user, provider="google").exists()

    def test_microsoft_auto_links_social_account_for_email_user(self, client, _oauth_state):
        # Regression guard for the Microsoft email_verified fix: pre-fix, a
        # Microsoft sign-in matching an existing email-registered user was
        # bounced to email_not_verified and never linked.
        user = User.objects.create_user(
            email="existing@example.com",
            password="testpass123",  # noqa: S106
            full_name="Existing User",
        )
        info = _mock_exchange(
            email="existing@example.com",
            provider_user_id="ms-99",
            email_verified=True,
        )
        with _patch_exchange(return_value=info):
            resp = client.get(
                "/api/v1/auth/oauth/microsoft/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "#code=" in resp["Location"]
        assert "email_not_verified" not in resp["Location"]

        user.refresh_from_db()
        assert user.registration_method == "email"
        assert SocialAccount.objects.filter(
            user=user, provider="microsoft", provider_user_id="ms-99"
        ).exists()


@pytest.mark.django_db
class TestOAuthCallbackReturningSocialUser:
    def test_finds_user_by_social_account(self, client, _oauth_state):
        user = User.objects.create_user(
            email="returning@example.com",
            full_name="Returning User",
            registration_method="github",
        )
        SocialAccount.objects.create(user=user, provider="github", provider_user_id="gh-42")

        info = _mock_exchange(email="returning@example.com", provider_user_id="gh-42")
        with _patch_exchange(return_value=info):
            resp = client.get(
                "/api/v1/auth/oauth/github/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "#code=" in resp["Location"]

        # No duplicate SocialAccount created
        assert SocialAccount.objects.filter(user=user, provider="github").count() == 1

    def test_returning_microsoft_user_logs_in_even_when_unverified(self, client, _oauth_state):
        # SocialAccount-by-provider-id lookup short-circuits BEFORE the
        # email_verified gate (resolve_oauth_user step 1). This matters for
        # Microsoft specifically: a user who originally signed in pre-fix
        # already has a SocialAccount row, and on subsequent sign-ins the
        # id_token may still lack `xms_edov` (consumer MSA, etc.), giving
        # email_verified=False. They must still be able to log in — only
        # brand-new users need the verified-email gate.
        user = User.objects.create_user(
            email="returning-ms@example.com",
            full_name="Returning MS",
            registration_method="microsoft",
        )
        SocialAccount.objects.create(user=user, provider="microsoft", provider_user_id="ms-ret")

        info = _mock_exchange(
            email="returning-ms@example.com",
            provider_user_id="ms-ret",
            email_verified=False,
        )
        with _patch_exchange(return_value=info):
            resp = client.get(
                "/api/v1/auth/oauth/microsoft/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "#code=" in resp["Location"]
        assert "email_not_verified" not in resp["Location"]
        assert SocialAccount.objects.filter(user=user, provider="microsoft").count() == 1


@pytest.mark.django_db
class TestOAuthCallbackCodeInFragment:
    def test_code_is_placed_in_url_fragment(self, client, _oauth_state):
        with _patch_exchange(return_value=_mock_exchange()):
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        location = resp["Location"]
        # One-time code must be in the fragment (after #), not the query string,
        # so it never leaks into referrer headers or server logs.
        assert "#code=" in location
        assert "?code=" not in location
        assert "access_token" not in location
        assert "refresh_token" not in location


@pytest.mark.django_db
class TestOAuthExchange:
    """The exchange endpoint trades the one-time code for tokens."""

    def _get_code(self, client) -> str:
        session = client.session
        session["oauth_state"] = "test-state"
        session.save()
        with _patch_exchange(return_value=_mock_exchange()):
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        location = resp["Location"]
        return location.split("#code=", 1)[1]

    def test_valid_code_returns_tokens(self, client):
        code = self._get_code(client)
        resp = client.post(
            "/api/v1/auth/oauth/exchange/",
            {"code": code},
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] == 15 * 60

    def test_code_is_single_use(self, client):
        code = self._get_code(client)
        first = client.post(
            "/api/v1/auth/oauth/exchange/",
            {"code": code},
            content_type="application/json",
        )
        assert first.status_code == 200
        second = client.post(
            "/api/v1/auth/oauth/exchange/",
            {"code": code},
            content_type="application/json",
        )
        assert second.status_code in (400, 401)

    def test_invalid_code_rejected(self, client):
        resp = client.post(
            "/api/v1/auth/oauth/exchange/",
            {"code": "not-a-real-code"},
            content_type="application/json",
        )
        assert resp.status_code in (400, 401)


@pytest.mark.django_db
class TestOAuthCallbackUnverifiedEmail:
    def test_unverified_email_blocks_new_user(self, client, _oauth_state):
        info = _mock_exchange(email="unverified@example.com", email_verified=False)
        with _patch_exchange(return_value=info):
            resp = client.get(
                "/api/v1/auth/oauth/microsoft/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "email_not_verified" in resp["Location"]
        assert not User.objects.filter(email="unverified@example.com").exists()

    def test_unverified_email_collision_sends_link_email(self, client, _oauth_state):
        """Unverified provider + existing local account → mint a SocialLinkRequest,
        queue the confirmation email, redirect to /auth/link-email-sent. The
        SocialAccount is NOT created until the user clicks the email link."""
        from apps.users.models import SocialLinkRequest

        existing = User.objects.create_user(
            email="victim@example.com",
            password="testpass123",  # noqa: S106
            full_name="Victim",
        )
        info = _mock_exchange(
            email="victim@example.com",
            provider_user_id="ms-attacker",
            email_verified=False,
        )
        with (
            _patch_exchange(return_value=info),
            patch("apps.users.auth_views.send_social_link_email_task.delay") as mock_send,
        ):
            resp = client.get(
                "/api/v1/auth/oauth/microsoft/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert resp["Location"].endswith("/auth/link-email-sent")
        assert not SocialAccount.objects.filter(
            provider="microsoft", provider_user_id="ms-attacker"
        ).exists()

        link_request = SocialLinkRequest.objects.get(
            user=existing, provider="microsoft", provider_user_id="ms-attacker"
        )
        assert link_request.used_at is None
        mock_send.assert_called_once()
        called_email, _called_token, called_provider = mock_send.call_args.args
        assert called_email == "victim@example.com"
        assert called_provider == "microsoft"

    def test_unverified_email_collision_inactive_user_silently_drops(
        self, client, _oauth_state
    ):
        """Inactive existing user → same redirect as active, but NO email
        queued and NO SocialLinkRequest minted. Anti-enumeration: an
        attacker cannot probe whether a deactivated account exists."""
        from apps.users.models import SocialLinkRequest

        User.objects.create_user(
            email="dormant@example.com",
            password="testpass123",  # noqa: S106
            full_name="Dormant",
            is_active=False,
        )
        info = _mock_exchange(
            email="dormant@example.com",
            provider_user_id="ms-x",
            email_verified=False,
        )
        with (
            _patch_exchange(return_value=info),
            patch("apps.users.auth_views.send_social_link_email_task.delay") as mock_send,
        ):
            resp = client.get(
                "/api/v1/auth/oauth/microsoft/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert resp["Location"].endswith("/auth/link-email-sent")
        mock_send.assert_not_called()
        assert not SocialLinkRequest.objects.filter(provider="microsoft").exists()

    def test_collision_invalidates_prior_pending_requests(self, client, _oauth_state):
        """Re-initiating the OAuth flow invalidates any older pending link
        requests so only the freshest emailed link works."""
        from datetime import UTC, datetime, timedelta

        from apps.users.models import SocialLinkRequest

        user = User.objects.create_user(
            email="mailbox@example.com",
            password="testpass123",  # noqa: S106
            full_name="Mailbox",
        )
        # Stale prior request
        stale = SocialLinkRequest.objects.create(
            user=user,
            token_hash="x" * 64,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            provider="microsoft",
            provider_user_id="old-id",
            full_name="",
        )
        info = _mock_exchange(
            email="mailbox@example.com",
            provider_user_id="new-id",
            email_verified=False,
        )
        with (
            _patch_exchange(return_value=info),
            patch("apps.users.auth_views.send_social_link_email_task.delay"),
        ):
            client.get(
                "/api/v1/auth/oauth/microsoft/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        stale.refresh_from_db()
        assert stale.used_at is not None


@pytest.mark.django_db
class TestOAuthCallbackDeactivatedUser:
    def test_deactivated_user_blocked(self, client, _oauth_state):
        user = User.objects.create_user(
            email="deact@example.com",
            full_name="Deact User",
            is_active=False,
            registration_method="google",
        )
        SocialAccount.objects.create(user=user, provider="google", provider_user_id="g-deact")

        info = _mock_exchange(email="deact@example.com", provider_user_id="g-deact")
        with _patch_exchange(return_value=info):
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "account_deactivated" in resp["Location"]


@pytest.mark.django_db
class TestOAuthCallbackStateValidation:
    def test_missing_state_param_redirects_invalid_state(self, client, _oauth_state):
        with _patch_exchange() as mock_exchange:
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code"},
            )
        assert resp.status_code == 302
        assert "invalid_state" in resp["Location"]
        mock_exchange.assert_not_called()

    def test_mismatched_state_redirects_invalid_state(self, client, _oauth_state):
        with _patch_exchange() as mock_exchange:
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "attacker-forged"},
            )
        assert resp.status_code == 302
        assert "invalid_state" in resp["Location"]
        mock_exchange.assert_not_called()

    def test_missing_session_state_redirects_invalid_state(self, client):
        # No `_oauth_state` fixture — session has no expected state.
        with _patch_exchange() as mock_exchange:
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "invalid_state" in resp["Location"]
        mock_exchange.assert_not_called()

    def test_state_is_popped_after_callback(self, client, _oauth_state):
        with _patch_exchange(return_value=_mock_exchange()):
            client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert "oauth_state" not in client.session


@pytest.mark.django_db
class TestOAuthCallbackParamValidation:
    def test_unsupported_provider_returns_400(self, client, _oauth_state):
        resp = client.get(
            "/api/v1/auth/oauth/facebook/callback/",
            {"code": "auth-code", "state": "test-state"},
        )
        assert resp.status_code == 400

    def test_missing_code_redirects_missing_code(self, client, _oauth_state):
        with _patch_exchange() as mock_exchange:
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"state": "test-state"},
            )
        assert resp.status_code == 302
        assert "missing_code" in resp["Location"]
        mock_exchange.assert_not_called()

    def test_provider_error_param_short_circuits(self, client, _oauth_state):
        # Provider may redirect back with ?error=access_denied without `code`.
        with _patch_exchange() as mock_exchange:
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"error": "access_denied", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "access_denied" in resp["Location"]
        mock_exchange.assert_not_called()


@pytest.mark.django_db
class TestOAuthCallbackExchangeFailures:
    def test_http_error_redirects_exchange_failed(self, client, _oauth_state):
        req = httpx.Request("POST", "https://oauth2.googleapis.com/token")
        resp_obj = httpx.Response(400, request=req)
        err = httpx.HTTPStatusError("bad", request=req, response=resp_obj)
        with _patch_exchange(side_effect=err):
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "exchange_failed" in resp["Location"]
        assert not User.objects.filter(email="oauth@example.com").exists()

    def test_oauth_error_redirects_exchange_failed(self, client, _oauth_state):
        with _patch_exchange(side_effect=OAuthError("missing access_token")):
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "exchange_failed" in resp["Location"]

    def test_value_error_redirects_exchange_failed(self, client, _oauth_state):
        # e.g. Provider(provider) raising on an enum-coerce edge case.
        with _patch_exchange(side_effect=ValueError("bad")):
            resp = client.get(
                "/api/v1/auth/oauth/google/callback/",
                {"code": "auth-code", "state": "test-state"},
            )
        assert resp.status_code == 302
        assert "exchange_failed" in resp["Location"]
