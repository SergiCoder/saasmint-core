"""Direct unit tests for apps.users.oauth — exchange_code and _fetch_github_primary_email."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import httpx
import jwt
import pytest

from apps.users.oauth import (
    OAuthError,
    _fetch_github_primary_email,
    _verify_microsoft_id_token,
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
    def test_unverified_when_id_token_missing(self):
        # No id_token in the token response → caller cannot trust email.
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

    def test_unverified_when_id_token_verification_fails(self):
        # id_token present but signature/audience verification fails →
        # we have no proof of email ownership, so fall back to unverified.
        token_resp = _mock_response(json_data={"access_token": "tok", "id_token": "bogus"})
        user_resp = _mock_response(
            json_data={"id": "ms-2", "mail": "dan@example.com", "displayName": "Dan"}
        )
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=user_resp),
            patch("apps.users.oauth._verify_microsoft_id_token", return_value=None),
        ):
            info = exchange_code("microsoft", "c", "https://host/cb")

        assert info.email == "dan@example.com"
        assert info.email_verified is False

    def test_unverified_when_xms_edov_missing(self):
        # id_token verifies but Microsoft did not assert domain ownership
        # (consumer MSA, or work account in a tenant where domain isn't
        # verified) → still unverified.
        token_resp = _mock_response(json_data={"access_token": "tok", "id_token": "real"})
        user_resp = _mock_response(
            json_data={"id": "ms-3", "mail": "dan@example.com", "displayName": "Dan"}
        )
        claims = {"email": "dan@example.com", "oid": "ms-3", "name": "Dan"}
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=user_resp),
            patch("apps.users.oauth._verify_microsoft_id_token", return_value=claims),
        ):
            info = exchange_code("microsoft", "c", "https://host/cb")

        assert info.email_verified is False

    def test_verified_when_xms_edov_true(self):
        # Happy path: tenant-verified domain. Trust id_token claims as
        # the source of truth for email/name/oid.
        token_resp = _mock_response(json_data={"access_token": "tok", "id_token": "real"})
        user_resp = _mock_response(
            json_data={"id": "ms-graph-id", "mail": "ignored@example.com", "displayName": "Ignored"}
        )
        claims = {
            "email": "alice@verified-tenant.com",
            "oid": "ms-oid-4",
            "name": "Alice",
            "xms_edov": True,
        }
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=user_resp),
            patch("apps.users.oauth._verify_microsoft_id_token", return_value=claims),
        ):
            info = exchange_code("microsoft", "c", "https://host/cb")

        # id_token claims win over Graph /me when verified.
        assert info.email == "alice@verified-tenant.com"
        assert info.full_name == "Alice"
        assert info.provider_user_id == "ms-oid-4"
        assert info.email_verified is True

    def test_unverified_when_email_claim_missing_even_if_xms_edov_true(self):
        # `preferred_username` is mutable / not authorization-safe per
        # Microsoft docs, and `xms_edov` only attests to the `email` claim's
        # domain. Without an `email` claim, drop to the unverified path —
        # we must NOT promote `preferred_username` to a verified email.
        token_resp = _mock_response(json_data={"access_token": "tok", "id_token": "real"})
        user_resp = _mock_response(
            json_data={"id": "ms-5", "mail": "graph@example.com", "displayName": "Bob"}
        )
        claims = {
            "preferred_username": "bob@verified-tenant.com",
            "oid": "ms-oid-5",
            "name": "Bob",
            "xms_edov": True,
        }
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=user_resp),
            patch("apps.users.oauth._verify_microsoft_id_token", return_value=claims),
        ):
            info = exchange_code("microsoft", "c", "https://host/cb")

        # Falls through to Graph /me with email_verified=False.
        assert info.email == "graph@example.com"
        assert info.email_verified is False

    def test_unverified_when_id_token_has_no_email_claim(self):
        # No verified `email` claim -> fall through to unverified Graph path,
        # NOT raise. Graph /me still provides a usable email for the
        # email-link verification flow.
        token_resp = _mock_response(json_data={"access_token": "tok", "id_token": "real"})
        user_resp = _mock_response(
            json_data={"id": "ms-6", "mail": "dan@example.com", "displayName": "Dan"}
        )
        claims = {"oid": "ms-oid-6", "xms_edov": True}
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=user_resp),
            patch("apps.users.oauth._verify_microsoft_id_token", return_value=claims),
        ):
            info = exchange_code("microsoft", "c", "https://host/cb")

        assert info.email == "dan@example.com"
        assert info.email_verified is False

    @pytest.mark.parametrize(
        "edov_value",
        [
            "true",  # string from a non-conformant IdP / future MS change
            "True",
            1,  # truthy int — ` is True` must reject
            "1",
            False,  # explicit negative
            None,
        ],
        ids=["str-true", "str-True", "int-1", "str-1", "explicit-false", "none"],
    )
    def test_unverified_when_xms_edov_is_truthy_but_not_strictly_true(self, edov_value):
        # The verified path uses `claims.get("xms_edov") is True` — a strict
        # identity check. Anything other than the bool True (truthy strings,
        # ints, or explicit False/None) must NOT be promoted to verified,
        # because Microsoft documents this claim as a JSON boolean and
        # accepting other shapes risks trusting a forged or upstream-mangled
        # value.
        token_resp = _mock_response(json_data={"access_token": "tok", "id_token": "real"})
        user_resp = _mock_response(
            json_data={"id": "ms-edov", "mail": "dan@example.com", "displayName": "Dan"}
        )
        claims = {
            "email": "dan@verified-tenant.com",
            "oid": "ms-oid-edov",
            "name": "Dan",
            "xms_edov": edov_value,
        }
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=user_resp),
            patch("apps.users.oauth._verify_microsoft_id_token", return_value=claims),
        ):
            info = exchange_code("microsoft", "c", "https://host/cb")

        # Falls through to unverified Graph /me path.
        assert info.email == "dan@example.com"
        assert info.email_verified is False

    def test_unverified_when_id_token_is_empty_string(self):
        # `if id_token` short-circuits before invoking the verifier — so an
        # empty-string id_token (some IdPs return "" instead of omitting the
        # field) must not be treated as a verifiable token.
        token_resp = _mock_response(json_data={"access_token": "tok", "id_token": ""})
        user_resp = _mock_response(
            json_data={"id": "ms-empty", "mail": "dan@example.com", "displayName": "Dan"}
        )
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=user_resp),
            patch("apps.users.oauth._verify_microsoft_id_token") as mock_verify,
        ):
            info = exchange_code("microsoft", "c", "https://host/cb")

        mock_verify.assert_not_called()
        assert info.email == "dan@example.com"
        assert info.email_verified is False

    def test_verified_provider_user_id_falls_back_to_graph_id_when_oid_missing(self):
        # `claims.get("oid") or ms["id"]` — if Microsoft omits the `oid` claim
        # (rare but spec-permitted), provider_user_id must come from Graph /me.
        token_resp = _mock_response(json_data={"access_token": "tok", "id_token": "real"})
        user_resp = _mock_response(
            json_data={"id": "graph-fallback-id", "mail": "x@y.com", "displayName": "X"}
        )
        claims = {
            "email": "alice@verified-tenant.com",
            "name": "Alice",
            "xms_edov": True,
        }
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=user_resp),
            patch("apps.users.oauth._verify_microsoft_id_token", return_value=claims),
        ):
            info = exchange_code("microsoft", "c", "https://host/cb")

        assert info.provider_user_id == "graph-fallback-id"
        assert info.email_verified is True

    def test_falls_back_to_user_principal_name_unverified(self):
        # Unverified path: no id_token, Graph /me gives only userPrincipalName.
        token_resp = _mock_response(json_data={"access_token": "tok"})
        user_resp = _mock_response(
            json_data={"id": "ms-7", "userPrincipalName": "eve@example.com", "displayName": "Eve"}
        )
        with (
            patch("apps.users.oauth.httpx.post", return_value=token_resp),
            patch("apps.users.oauth.httpx.get", return_value=user_resp),
        ):
            info = exchange_code("microsoft", "c", "https://host/cb")

        assert info.email == "eve@example.com"
        assert info.email_verified is False

    def test_raises_when_email_missing(self):
        # No id_token AND Graph /me has no mail/UPN → cannot proceed.
        token_resp = _mock_response(json_data={"access_token": "tok"})
        user_resp = _mock_response(json_data={"id": "ms-8", "displayName": "No Email"})
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


# ---------------------------------------------------------------------------
# _verify_microsoft_id_token
# ---------------------------------------------------------------------------


class TestVerifyMicrosoftIdToken:
    """Direct unit tests for the JWKS-backed id_token verifier."""

    _GOOD_CLAIMS: ClassVar[dict[str, object]] = {
        "iss": "https://login.microsoftonline.com/abc-tenant-id/v2.0",
        "aud": "test-client-id",
        "email": "alice@verified-tenant.com",
        "oid": "ms-oid-1",
        "xms_edov": True,
    }

    def _patch_decode(
        self,
        return_value: dict[str, Any] | None = None,
        side_effect: BaseException | type[BaseException] | None = None,
    ) -> tuple[AbstractContextManager[MagicMock], AbstractContextManager[MagicMock]]:
        signing_key = MagicMock()
        signing_key.key = "fake-public-key"
        client = MagicMock()
        client.get_signing_key_from_jwt.return_value = signing_key
        return (
            patch("apps.users.oauth._ms_jwks_client", return_value=client),
            patch(
                "apps.users.oauth.jwt.decode",
                return_value=return_value,
                side_effect=side_effect,
            ),
        )

    def test_returns_claims_on_success(self):
        jwks_patch, decode_patch = self._patch_decode(return_value=self._GOOD_CLAIMS)
        with jwks_patch, decode_patch:
            claims = _verify_microsoft_id_token("good-jwt")
        assert claims == self._GOOD_CLAIMS

    def test_returns_none_on_invalid_signature(self):
        jwks_patch, decode_patch = self._patch_decode(
            side_effect=jwt.InvalidSignatureError("bad sig")
        )
        with jwks_patch, decode_patch:
            assert _verify_microsoft_id_token("bad-jwt") is None

    def test_returns_none_on_expired_token(self):
        jwks_patch, decode_patch = self._patch_decode(
            side_effect=jwt.ExpiredSignatureError("expired")
        )
        with jwks_patch, decode_patch:
            assert _verify_microsoft_id_token("expired-jwt") is None

    def test_returns_none_on_audience_mismatch(self):
        jwks_patch, decode_patch = self._patch_decode(
            side_effect=jwt.InvalidAudienceError("wrong aud")
        )
        with jwks_patch, decode_patch:
            assert _verify_microsoft_id_token("foreign-jwt") is None

    def test_returns_none_on_jwks_fetch_failure(self):
        signing_key = MagicMock()
        signing_key.key = "fake"
        client = MagicMock()
        client.get_signing_key_from_jwt.side_effect = jwt.PyJWKClientError("network down")
        with patch("apps.users.oauth._ms_jwks_client", return_value=client):
            assert _verify_microsoft_id_token("any-jwt") is None

    def test_returns_none_when_issuer_is_not_microsoft(self):
        # Signature OK but issuer is some other identity provider — reject.
        forged = {**self._GOOD_CLAIMS, "iss": "https://attacker.example.com/abc/v2.0"}
        jwks_patch, decode_patch = self._patch_decode(return_value=forged)
        with jwks_patch, decode_patch:
            assert _verify_microsoft_id_token("forged-jwt") is None

    def test_returns_none_when_issuer_missing(self):
        no_iss = {**self._GOOD_CLAIMS}
        del no_iss["iss"]
        jwks_patch, decode_patch = self._patch_decode(return_value=no_iss)
        with jwks_patch, decode_patch:
            assert _verify_microsoft_id_token("no-iss-jwt") is None

    def test_returns_none_when_issuer_has_microsoft_prefix_but_wrong_suffix(self):
        # A Microsoft-prefixed issuer with a non-`/v2.0` suffix (e.g. legacy
        # v1.0 STS endpoint) must be rejected — the verifier explicitly
        # validates BOTH ends of the issuer string. This pins the suffix
        # check so a future refactor can't quietly drop it.
        legacy_v1 = {
            **self._GOOD_CLAIMS,
            "iss": "https://login.microsoftonline.com/abc-tenant-id/",
        }
        jwks_patch, decode_patch = self._patch_decode(return_value=legacy_v1)
        with jwks_patch, decode_patch:
            assert _verify_microsoft_id_token("v1-jwt") is None
