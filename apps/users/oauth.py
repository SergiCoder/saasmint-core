"""Server-side OAuth code exchange for Google, GitHub, and Microsoft."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from django.conf import settings

PROVIDERS = ("google", "github", "microsoft")


@dataclass(frozen=True)
class OAuthUserInfo:
    email: str
    full_name: str
    provider_user_id: str
    avatar_url: str | None = None


def _get_config(provider: str) -> dict[str, Any]:
    configs: dict[str, dict[str, Any]] = {
        "google": {
            "client_id": settings.OAUTH_GOOGLE_CLIENT_ID,
            "client_secret": settings.OAUTH_GOOGLE_CLIENT_SECRET,
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "userinfo_url": "https://www.googleapis.com/oauth2/v2/userinfo",
            "scopes": "openid email profile",
        },
        "github": {
            "client_id": settings.OAUTH_GITHUB_CLIENT_ID,
            "client_secret": settings.OAUTH_GITHUB_CLIENT_SECRET,
            "authorize_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "userinfo_url": "https://api.github.com/user",
            "scopes": "read:user user:email",
        },
        "microsoft": {
            "client_id": settings.OAUTH_MICROSOFT_CLIENT_ID,
            "client_secret": settings.OAUTH_MICROSOFT_CLIENT_SECRET,
            "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            "userinfo_url": "https://graph.microsoft.com/v1.0/me",
            "scopes": "openid email profile User.Read",
        },
    }
    return configs[provider]


def get_authorization_url(provider: str, redirect_uri: str, state: str) -> str:
    """Build the OAuth authorization URL for a 302 redirect."""
    cfg = _get_config(provider)
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
    cfg = _get_config(provider)

    # Exchange code for access token
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
    )
    token_resp.raise_for_status()
    token_data = token_resp.json()
    access_token = token_data["access_token"]

    # Fetch user info
    userinfo_resp = httpx.get(
        cfg["userinfo_url"],
        headers={"Authorization": f"Bearer {access_token}"},
    )
    userinfo_resp.raise_for_status()
    info = userinfo_resp.json()

    if provider == "google":
        return OAuthUserInfo(
            email=info["email"],
            full_name=info.get("name", info["email"].split("@")[0]),
            provider_user_id=str(info["id"]),
            avatar_url=info.get("picture"),
        )
    elif provider == "github":
        email = info.get("email") or _fetch_github_primary_email(access_token)
        return OAuthUserInfo(
            email=email,
            full_name=info.get("name") or info.get("login", email.split("@")[0]),
            provider_user_id=str(info["id"]),
            avatar_url=info.get("avatar_url"),
        )
    else:  # microsoft
        return OAuthUserInfo(
            email=info.get("mail") or info.get("userPrincipalName", ""),
            full_name=info.get("displayName", ""),
            provider_user_id=str(info["id"]),
        )


def _fetch_github_primary_email(access_token: str) -> str:
    """GitHub may not include email in user profile — fetch from /user/emails."""
    resp = httpx.get(
        "https://api.github.com/user/emails",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    resp.raise_for_status()
    for entry in resp.json():
        if entry.get("primary") and entry.get("verified"):
            return str(entry["email"])
    raise ValueError("No verified primary email found on GitHub account")
