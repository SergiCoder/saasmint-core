"""Server-side OAuth code exchange for Google, GitHub, and Microsoft."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict, assert_never
from urllib.parse import urlencode

import httpx
from django.conf import settings

_OAUTH_TIMEOUT = httpx.Timeout(10.0)


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


def exchange_code(provider: str, code: str, redirect_uri: str) -> OAuthUserInfo:
    """Exchange an authorization code for user info."""
    prov = Provider(provider)
    cfg = _get_config(prov)

    token_resp = httpx.post(
        cfg["token_url"],
        data={
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        headers={"Accept": "application/json"},
        timeout=_OAUTH_TIMEOUT,
    )
    token_resp.raise_for_status()
    token_data: _TokenResponse = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise OAuthError("OAuth token response missing access_token")

    userinfo_resp = httpx.get(
        cfg["userinfo_url"],
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=_OAUTH_TIMEOUT,
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
            email = _fetch_github_primary_email(access_token)
            return OAuthUserInfo(
                email=email,
                full_name=github.get("name") or github.get("login") or email.split("@")[0],
                provider_user_id=str(github["id"]),
                avatar_url=github.get("avatar_url"),
                email_verified=True,
            )
        case Provider.MICROSOFT:
            ms: _MicrosoftUserInfo = info
            # Microsoft Graph /me returns the email of the authenticated identity
            # (work/school tenant or consumer MSA). The OAuth handshake itself
            # establishes ownership, on par with Google's `email_verified: true`.
            email = ms.get("mail") or ms.get("userPrincipalName")
            if not email:
                raise OAuthError("Microsoft OAuth response missing email")
            return OAuthUserInfo(
                email=email,
                full_name=ms.get("displayName", ""),
                provider_user_id=str(ms["id"]),
                email_verified=True,
            )
        case _ as unreachable:
            assert_never(unreachable)


def _fetch_github_primary_email(access_token: str) -> str:
    """GitHub may not include email in user profile — fetch from /user/emails."""
    resp = httpx.get(
        "https://api.github.com/user/emails",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=_OAUTH_TIMEOUT,
    )
    resp.raise_for_status()
    entries: list[_GitHubEmailEntry] = resp.json()
    for entry in entries:
        if entry.get("primary") and entry.get("verified"):
            return str(entry["email"])
    raise OAuthError("No verified primary email found on GitHub account")
