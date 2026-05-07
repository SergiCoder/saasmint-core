"""Server-side OAuth code exchange for Google, GitHub, and Microsoft."""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypedDict, assert_never
from urllib.parse import urlencode

import httpx
import jwt
from django.conf import settings
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

# OAuth providers (Google/GitHub/Microsoft) reliably respond within 1-2s; 5s
# leaves headroom without blocking the worker thread for an unbounded retry
# from a degraded provider. The shared httpx.AsyncClient reuses TCP
# connections across the token-exchange, userinfo, and (GitHub)
# /user/emails requests fired back-to-back inside one callback, and lets
# the awaiting view yield the worker thread for the duration of each
# provider round-trip.
_OAUTH_TIMEOUT = httpx.Timeout(5.0)
_oauth_client = httpx.AsyncClient(timeout=_OAUTH_TIMEOUT)

# Microsoft OIDC: keys served at the v2.0 multi-tenant endpoint cover both
# work/school and consumer (MSA) tokens. Issuer format is per-tenant
# (`https://login.microsoftonline.com/{tid}/v2.0`); we don't pin a tid so we
# validate prefix/suffix instead of using jwt.decode's strict issuer check.
_MS_JWKS_URI = "https://login.microsoftonline.com/common/discovery/v2.0/keys"
_MS_OIDC_ISSUER_PREFIX = "https://login.microsoftonline.com/"
_MS_OIDC_ISSUER_SUFFIX = "/v2.0"


class Provider(StrEnum):
    GOOGLE = "google"
    GITHUB = "github"
    MICROSOFT = "microsoft"


PROVIDERS: tuple[str, ...] = tuple(p.value for p in Provider)


class OAuthError(Exception):
    """Raised when OAuth flow fails for domain reasons (not HTTP)."""


class _ProviderConfig(TypedDict):
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    scopes: str


class _TokenResponse(TypedDict, total=False):
    access_token: str
    id_token: str


class _GoogleUserInfo(TypedDict, total=False):
    id: str
    email: str
    name: str
    picture: str
    verified_email: bool
    email_verified: bool


class _GitHubUserInfo(TypedDict, total=False):
    id: int
    name: str
    login: str
    avatar_url: str


class _GitHubEmailEntry(TypedDict, total=False):
    email: str
    primary: bool
    verified: bool


class _MicrosoftUserInfo(TypedDict, total=False):
    id: str
    mail: str
    userPrincipalName: str
    displayName: str


class OAuthEmailNotVerifiedError(Exception):
    """Raised when the OAuth provider did not confirm email ownership."""


@dataclass(frozen=True)
class OAuthUserInfo:
    email: str
    full_name: str
    provider_user_id: str
    avatar_url: str | None = None
    email_verified: bool = False


@functools.cache
def _get_config(provider: Provider) -> _ProviderConfig:
    match provider:
        case Provider.GOOGLE:
            return _ProviderConfig(
                client_id=settings.OAUTH_GOOGLE_CLIENT_ID,
                client_secret=settings.OAUTH_GOOGLE_CLIENT_SECRET,
                authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
                token_url="https://oauth2.googleapis.com/token",  # noqa: S106  # URL, not a credential
                userinfo_url="https://www.googleapis.com/oauth2/v2/userinfo",
                scopes="openid email profile",
            )
        case Provider.GITHUB:
            return _ProviderConfig(
                client_id=settings.OAUTH_GITHUB_CLIENT_ID,
                client_secret=settings.OAUTH_GITHUB_CLIENT_SECRET,
                authorize_url="https://github.com/login/oauth/authorize",
                token_url="https://github.com/login/oauth/access_token",  # noqa: S106  # URL, not a credential
                userinfo_url="https://api.github.com/user",
                scopes="read:user user:email",
            )
        case Provider.MICROSOFT:
            return _ProviderConfig(
                client_id=settings.OAUTH_MICROSOFT_CLIENT_ID,
                client_secret=settings.OAUTH_MICROSOFT_CLIENT_SECRET,
                authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
                token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",  # noqa: S106  # URL, not a credential
                userinfo_url="https://graph.microsoft.com/v1.0/me",
                scopes="openid email profile User.Read",
            )


@functools.cache
def _ms_jwks_client() -> PyJWKClient:
    # PyJWKClient caches keys in-process for the lifetime of the worker.
    # Lazy-initialised so import-time has no network dependency.
    return PyJWKClient(_MS_JWKS_URI)


def _verify_microsoft_id_token(id_token: str) -> dict[str, Any] | None:
    """Verify a Microsoft OIDC id_token and return its claims, or None.

    Returns None on any failure (malformed token, JWKS fetch error, bad
    signature, expired, wrong audience, non-Microsoft issuer). Callers
    treat None as "we cannot trust this id_token" and fall back to the
    unverified path — which is safe, just worse UX (the user gets sent
    through email-link verification).
    """
    try:
        signing_key = _ms_jwks_client().get_signing_key_from_jwt(id_token)
        claims: dict[str, Any] = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.OAUTH_MICROSOFT_CLIENT_ID,
            # Microsoft's `iss` is per-tenant (`.../{tid}/v2.0`); we accept
            # any tenant and validate the prefix below instead of pinning a
            # single issuer string here.
            options={"verify_iss": False},
        )
    except (jwt.InvalidTokenError, jwt.PyJWKClientError, httpx.HTTPError) as exc:
        logger.warning("Microsoft id_token verification failed: %s", exc)
        return None

    iss = claims.get("iss", "")
    if not (iss.startswith(_MS_OIDC_ISSUER_PREFIX) and iss.endswith(_MS_OIDC_ISSUER_SUFFIX)):
        logger.warning("Microsoft id_token has unexpected issuer: %s", iss)
        return None

    return claims


def get_authorization_url(provider: str, redirect_uri: str, state: str) -> str:
    """Build the OAuth authorization URL for a 302 redirect."""
    cfg = _get_config(Provider(provider))
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": cfg["scopes"],
        "state": state,
    }
    return f"{cfg['authorize_url']}?{urlencode(params)}"


async def exchange_code(provider: str, code: str, redirect_uri: str) -> OAuthUserInfo:
    """Exchange an authorization code for user info."""
    prov = Provider(provider)
    cfg = _get_config(prov)

    token_resp = await _oauth_client.post(
        cfg["token_url"],
        data={
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        headers={"Accept": "application/json"},
    )
    token_resp.raise_for_status()
    token_data: _TokenResponse = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise OAuthError("OAuth token response missing access_token")

    userinfo_resp = await _oauth_client.get(
        cfg["userinfo_url"],
        headers={"Authorization": f"Bearer {access_token}"},
    )
    userinfo_resp.raise_for_status()
    info = userinfo_resp.json()

    match prov:
        case Provider.GOOGLE:
            google: _GoogleUserInfo = info
            email = google.get("email")
            if not email:
                raise OAuthError("Google OAuth response missing email")
            return OAuthUserInfo(
                email=email,
                full_name=google.get("name") or email.split("@")[0],
                provider_user_id=str(google["id"]),
                avatar_url=google.get("picture"),
                email_verified=bool(google.get("verified_email") or google.get("email_verified")),
            )
        case Provider.GITHUB:
            github: _GitHubUserInfo = info
            # Always use /user/emails as the authoritative source — the public
            # email on /user is not guaranteed verified.
            email = await _fetch_github_primary_email(access_token)
            return OAuthUserInfo(
                email=email,
                full_name=github.get("name") or github.get("login") or email.split("@")[0],
                provider_user_id=str(github["id"]),
                avatar_url=github.get("avatar_url"),
                email_verified=True,
            )
        case Provider.MICROSOFT:
            ms: _MicrosoftUserInfo = info
            # Trust the email iff Microsoft's OIDC id_token is signature-valid
            # AND carries `xms_edov: true` — the claim Microsoft sets only when
            # it has verified the email's domain belongs to the user's tenant.
            # Graph /me on its own does NOT prove ownership: a tenant admin can
            # set `mail` to any string (including a third-party domain) without
            # verifying the destination mailbox, which combined with the email
            # auto-link in resolve_oauth_user would enable account takeover.
            id_token = token_data.get("id_token")
            claims = _verify_microsoft_id_token(id_token) if id_token else None
            # Only the `email` claim is covered by xms_edov's domain-ownership
            # attestation. `preferred_username` is human-readable, mutable, and
            # explicitly documented as unsafe for authorization decisions
            # (Microsoft can return e.g. a tenant admin-controlled UPN that
            # doesn't match the verified mailbox), so we do NOT fall back to
            # it on the verified path — without an `email` claim we drop to
            # the unverified branch.
            verified_email = claims.get("email") if claims else None
            if claims and claims.get("xms_edov") is True and verified_email:
                return OAuthUserInfo(
                    email=verified_email,
                    full_name=claims.get("name") or ms.get("displayName", ""),
                    provider_user_id=str(claims.get("oid") or ms["id"]),
                    email_verified=True,
                )

            email = ms.get("mail") or ms.get("userPrincipalName")
            if not email:
                raise OAuthError("Microsoft OAuth response missing email")
            return OAuthUserInfo(
                email=email,
                full_name=ms.get("displayName", ""),
                provider_user_id=str(ms["id"]),
                email_verified=False,
            )
        case _ as unreachable:
            assert_never(unreachable)


async def _fetch_github_primary_email(access_token: str) -> str:
    """GitHub may not include email in user profile — fetch from /user/emails."""
    resp = await _oauth_client.get(
        "https://api.github.com/user/emails",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    entries: list[_GitHubEmailEntry] = resp.json()
    for entry in entries:
        if entry.get("primary") and entry.get("verified"):
            return str(entry["email"])
    raise OAuthError("No verified primary email found on GitHub account")
