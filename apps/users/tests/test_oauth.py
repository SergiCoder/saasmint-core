"""Direct unit tests for apps.users.oauth — exchange_code and _fetch_github_primary_email."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from apps.users.oauth import (
    OAuthError,
    _fetch_github_primary_email,
    exchange_code,
)


def _mock_response(status_code: int = 200, json_data: dict | list | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}

    if status_code >= 400:

        def _raise():
            raise httpx.HTTPStatusError(
                f"{status_code}",
                request=httpx.Request("POST", "https://example.com"),
                response=httpx.Response(status_code),
            )

        resp.raise_for_status.side_effect = _raise
    else:
        resp.raise_for_status.return_value = None
    return resp


# ---------------------------------------------------------------------------
# exchange_code — Google
# ---------------------------------------------------------------------------


class TestExchangeCodeGoogle:
    def test_returns_user_info_when_email_verified(self):
        token_resp = _mock_response(json_data={"access_token": "tok"})
        userinfo_resp = _mock_response(
            json_data={
                "id": "g-1",
                "email": "alice@example.com",
                "name": "Alice",
                "picture": "https://example.com/a.png",
                "verified_email": True,
            }
        )
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=userinfo_resp),
        ):
            info = exchange_code("google", "auth-code", "https://host/cb")

        assert info.email == "alice@example.com"
        assert info.full_name == "Alice"
        assert info.provider_user_id == "g-1"
        assert info.avatar_url == "https://example.com/a.png"
        assert info.email_verified is True

    def test_falls_back_to_email_local_part_when_name_missing(self):
        token_resp = _mock_response(json_data={"access_token": "tok"})
        userinfo_resp = _mock_response(
            json_data={
                "id": "g-2",
                "email": "bob@example.com",
                "verified_email": False,
            }
        )
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=userinfo_resp),
        ):
            info = exchange_code("google", "c", "https://host/cb")

        assert info.full_name == "bob"
        assert info.email_verified is False

    def test_raises_when_email_missing(self):
        token_resp = _mock_response(json_data={"access_token": "tok"})
        userinfo_resp = _mock_response(json_data={"id": "g-3"})
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=userinfo_resp),
            pytest.raises(OAuthError),
        ):
            exchange_code("google", "c", "https://host/cb")

    def test_raises_when_token_response_missing_access_token(self):
        token_resp = _mock_response(json_data={})
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            pytest.raises(OAuthError),
        ):
            exchange_code("google", "c", "https://host/cb")

    def test_token_endpoint_http_error_propagates(self):
        token_resp = _mock_response(status_code=400)
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            pytest.raises(httpx.HTTPStatusError),
        ):
            exchange_code("google", "c", "https://host/cb")


# ---------------------------------------------------------------------------
# exchange_code — GitHub
# ---------------------------------------------------------------------------


class TestExchangeCodeGitHub:
    def test_uses_emails_endpoint_for_primary_verified_email(self):
        token_resp = _mock_response(json_data={"access_token": "tok"})
        user_resp = _mock_response(
            json_data={"id": 77, "name": "Carol", "login": "carol", "avatar_url": "a"}
        )
        emails_resp = _mock_response(
            json_data=[
                {"email": "carol+2@example.com", "primary": False, "verified": True},
                {"email": "carol@example.com", "primary": True, "verified": True},
            ]
        )
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", side_effect=[user_resp, emails_resp]),
        ):
            info = exchange_code("github", "c", "https://host/cb")

        assert info.email == "carol@example.com"
        assert info.full_name == "Carol"
        assert info.provider_user_id == "77"
        assert info.avatar_url == "a"
        assert info.email_verified is True

    def test_falls_back_to_login_when_name_missing(self):
        token_resp = _mock_response(json_data={"access_token": "tok"})
        user_resp = _mock_response(json_data={"id": 1, "login": "carol"})
        emails_resp = _mock_response(
            json_data=[{"email": "c@example.com", "primary": True, "verified": True}]
        )
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", side_effect=[user_resp, emails_resp]),
        ):
            info = exchange_code("github", "c", "https://host/cb")

        assert info.full_name == "carol"


# ---------------------------------------------------------------------------
# exchange_code — Microsoft
# ---------------------------------------------------------------------------


class TestExchangeCodeMicrosoft:
    def test_returns_unverified_even_when_mail_present(self):
        token_resp = _mock_response(json_data={"access_token": "tok"})
        user_resp = _mock_response(
            json_data={"id": "ms-1", "mail": "dan@example.com", "displayName": "Dan"}
        )
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=user_resp),
        ):
            info = exchange_code("microsoft", "c", "https://host/cb")

        assert info.email == "dan@example.com"
        assert info.full_name == "Dan"
        assert info.email_verified is False

    def test_falls_back_to_user_principal_name(self):
        token_resp = _mock_response(json_data={"access_token": "tok"})
        user_resp = _mock_response(
            json_data={"id": "ms-2", "userPrincipalName": "eve@example.com", "displayName": "Eve"}
        )
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=user_resp),
        ):
            info = exchange_code("microsoft", "c", "https://host/cb")

        assert info.email == "eve@example.com"

    def test_raises_when_email_missing(self):
        token_resp = _mock_response(json_data={"access_token": "tok"})
        user_resp = _mock_response(json_data={"id": "ms-3", "displayName": "No Email"})
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=user_resp),
            pytest.raises(OAuthError),
        ):
            exchange_code("microsoft", "c", "https://host/cb")


class TestExchangeCodeProviderValidation:
    def test_unknown_provider_raises_value_error(self):
        with pytest.raises(ValueError):
            exchange_code("facebook", "c", "https://host/cb")


# ---------------------------------------------------------------------------
# _fetch_github_primary_email
# ---------------------------------------------------------------------------


class TestFetchGitHubPrimaryEmail:
    def test_returns_primary_verified_email(self):
        resp = _mock_response(
            json_data=[
                {"email": "alt@example.com", "primary": False, "verified": True},
                {"email": "main@example.com", "primary": True, "verified": True},
            ]
        )
        with patch("apps.users.oauth.httpx.get", return_value=resp):
            assert _fetch_github_primary_email("tok") == "main@example.com"

    def test_raises_when_primary_is_unverified(self):
        resp = _mock_response(
            json_data=[{"email": "main@example.com", "primary": True, "verified": False}]
        )
        with patch("apps.users.oauth.httpx.get", return_value=resp), pytest.raises(OAuthError):
            _fetch_github_primary_email("tok")

    def test_raises_when_no_primary_entry(self):
        resp = _mock_response(
            json_data=[{"email": "x@example.com", "primary": False, "verified": True}]
        )
        with patch("apps.users.oauth.httpx.get", return_value=resp), pytest.raises(OAuthError):
            _fetch_github_primary_email("tok")

    def test_raises_on_empty_list(self):
        resp = _mock_response(json_data=[])
        with patch("apps.users.oauth.httpx.get", return_value=resp), pytest.raises(OAuthError):
            _fetch_github_primary_email("tok")

    def test_http_error_propagates(self):
        resp = _mock_response(status_code=401)
        with (
            patch("apps.users.oauth.httpx.get", return_value=resp),
            pytest.raises(httpx.HTTPStatusError),
        ):
            _fetch_github_primary_email("tok")
