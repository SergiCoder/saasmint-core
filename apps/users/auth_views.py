"""Authentication API views — register, login, refresh, logout, verify, password reset, OAuth."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import urlencode

import httpx
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import authenticate
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect
from drf_spectacular.utils import extend_schema
from rest_framework import serializers as drf_serializers
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.base_views import (
    AuthLoginView,
    AuthPublicView,
    AuthRefreshView,
    AuthRegisterView,
    AuthScopedView,
)
from apps.users.auth_serializers import (
    ChangePasswordSerializer,
    ForgotPasswordSerializer,
    LoginSerializer,
    LogoutSerializer,
    MessageResponseSerializer,
    OAuthConfirmLinkSerializer,
    RefreshSerializer,
    RegisterSerializer,
    ResendVerificationSerializer,
    ResetPasswordSerializer,
    TokenResponseSerializer,
    VerifyEmailSerializer,
)
from apps.users.authentication import (
    ACCESS_TOKEN_LIFETIME,
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
from apps.users.models import EmailVerificationToken, SocialAccount, SocialLinkRequest, User
from apps.users.oauth import (
    PROVIDERS,
    OAuthEmailNotVerifiedError,
    OAuthError,
    exchange_code,
    get_authorization_url,
)
from apps.users.services import email_is_registered, resolve_oauth_user
from apps.users.tasks import (
    send_password_reset_email_task,
    send_social_link_email_task,
    send_verification_email_task,
)
from helpers import get_user

logger = logging.getLogger(__name__)


class EmailAlreadyExists(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Email already registered."
    default_code = "email_exists"


class InvalidCredentials(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Invalid credentials."
    default_code = "invalid_credentials"


class AccountDeactivated(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Account is deactivated."
    default_code = "account_deactivated"


class InvalidPassword(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Current password is incorrect."
    default_code = "invalid_password"


class InvalidOAuthCode(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Invalid or expired code."
    default_code = "invalid_code"


def _token_response(
    user: User,
    refresh_token: str,
    http_status: int = 200,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    return Response(
        {
            "access_token": create_access_token(user),
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": int(ACCESS_TOKEN_LIFETIME.total_seconds()),
        },
        status=http_status,
        headers=headers,
    )


def _register_user(
    *,
    email: str,
    password: str,
    full_name: str,
) -> Response:
    """Create a new user and return a 201 token response."""
    if email_is_registered(email):
        raise EmailAlreadyExists

    try:
        with transaction.atomic():
            user = User.objects.create_user(
                email=email,
                password=password,
                full_name=full_name,
                is_verified=False,
            )
    except IntegrityError as exc:
        raise EmailAlreadyExists from exc

    token = create_email_verification_token(user)
    send_verification_email_task.delay(user.email, token)

    refresh = create_refresh_token(user)
    return _token_response(
        user,
        refresh,
        http_status=status.HTTP_201_CREATED,
        headers={"Location": "/api/v1/account/"},
    )


class RegisterView(AuthRegisterView):
    """POST /api/v1/auth/register — create a new account."""

    @extend_schema(
        request=RegisterSerializer,
        responses={201: TokenResponseSerializer},
        tags=["auth"],
    )
    def post(self, request: Request) -> Response:
        ser = RegisterSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        return _register_user(
            email=ser.validated_data["email"],
            password=ser.validated_data["password"],
            full_name=ser.validated_data["full_name"],
        )


class VerifyEmailView(AuthPublicView):
    """POST /api/v1/auth/verify-email — activate a user account.

    Accepts an optional ``password`` field. Required for invitee accounts
    (created without a usable password by ``accept_invitation``); ignored
    for users who already have a usable password (normal registration flow).
    Setting the password here closes the invitation-token interception path:
    only someone who can read the verification email — sent server-side to
    the invitee's mailbox — can bind credentials to the account.
    """

    @extend_schema(request=VerifyEmailSerializer, responses=TokenResponseSerializer, tags=["auth"])
    def post(self, request: Request) -> Response:
        ser = VerifyEmailSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user = verify_email_token(ser.validated_data["token"])
        password = ser.validated_data.get("password")
        update_fields: list[str] = []

        if not user.has_usable_password():
            if not password:
                return Response(
                    {
                        "detail": (
                            "This account requires a password to be set."
                            " Provide ``password`` to complete verification."
                        ),
                        "code": "password_required",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user.set_password(password)
            update_fields.append("password")

        if not user.is_verified:
            user.is_verified = True
            update_fields.append("is_verified")

        if update_fields:
            update_fields.append("updated_at")
            user.save(update_fields=update_fields)

        refresh = create_refresh_token(user)
        return _token_response(user, refresh)


class ResendVerificationView(AuthPublicView):
    """POST /api/v1/auth/resend-verification — re-send the verification email."""

    @extend_schema(
        request=ResendVerificationSerializer,
        responses={200: MessageResponseSerializer},
        tags=["auth"],
    )
    def post(self, request: Request) -> Response:
        ser = ResendVerificationSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        # Always return 200 to prevent email enumeration. Silently no-op when
        # the address has no account, the account is inactive, or the user is
        # already verified. ``email__iexact`` lands on the functional
        # ``uniq_users_lower_email`` index — case-sensitive ``email=`` would
        # miss differently-cased rows the resolver/registration creates.
        try:
            user = User.objects.get(
                email__iexact=ser.validated_data["email"],
                is_active=True,
                is_verified=False,
            )
        except User.DoesNotExist:
            user = None

        if user is not None:
            # Invalidate any prior unused verification tokens for this user so
            # only the freshest link works.
            EmailVerificationToken.objects.filter(
                user=user, used_at__isnull=True
            ).update(used_at=datetime.now(UTC))
            token = create_email_verification_token(user)
            send_verification_email_task.delay(user.email, token)

        return Response(
            {
                "detail": "If the email exists and is unverified, a new link has been sent.",
                "code": "verification_email_queued",
            }
        )


class LoginView(AuthLoginView):
    """POST /api/v1/auth/login — authenticate with email + password."""

    @extend_schema(request=LoginSerializer, responses=TokenResponseSerializer, tags=["auth"])
    def post(self, request: Request) -> Response:
        ser = LoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user = authenticate(
            request,
            username=ser.validated_data["email"],
            password=ser.validated_data["password"],
        )
        if user is None or not isinstance(user, User):
            raise InvalidCredentials

        if not user.is_active:
            raise AccountDeactivated

        if not user.is_verified:
            # Collapse the unverified path into the invalid-credentials
            # envelope: a distinct ``email_not_verified`` 403 lets an
            # attacker confirm that an email + password combination is
            # valid without yet being verified, leaking signal that helps
            # credential-stuffing campaigns. The frontend can still recover
            # by calling ``POST /auth/resend-verification`` (which is
            # itself enumeration-safe).
            raise InvalidCredentials

        refresh = create_refresh_token(user)
        return _token_response(user, refresh)


class RefreshView(AuthRefreshView):
    """POST /api/v1/auth/refresh — rotate refresh token and get new tokens."""

    @extend_schema(request=RefreshSerializer, responses=TokenResponseSerializer, tags=["auth"])
    def post(self, request: Request) -> Response:
        ser = RefreshSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user, new_refresh = rotate_refresh_token(ser.validated_data["refresh_token"])
        return _token_response(user, new_refresh)


class LogoutView(AuthScopedView):
    """POST /api/v1/auth/logout — revoke refresh token."""

    @extend_schema(request=LogoutSerializer, responses={204: None}, tags=["auth"])
    def post(self, request: Request) -> Response:
        ser = LogoutSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        revoke_refresh_token(ser.validated_data["refresh_token"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class ForgotPasswordView(AuthPublicView):
    """POST /api/v1/auth/forgot-password — send reset email (always 200)."""

    @extend_schema(
        request=ForgotPasswordSerializer,
        responses={200: MessageResponseSerializer},
        tags=["auth"],
    )
    def post(self, request: Request) -> Response:
        ser = ForgotPasswordSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        # Always return 200 to prevent email enumeration. ``email__iexact``
        # uses uniq_users_lower_email so a case-mismatched lookup still
        # finds the row. Match the ``.first()`` pattern used by
        # ``ResendVerificationView`` instead of try/except.
        user = User.objects.filter(
            email__iexact=ser.validated_data["email"],
            is_active=True,
        ).first()
        if user is not None:
            token = create_password_reset_token(user)
            send_password_reset_email_task.delay(user.email, token)

        return Response(
            {
                "detail": "If the email exists, a reset link has been sent.",
                "code": "reset_email_queued",
            }
        )


class ResetPasswordView(AuthPublicView):
    """POST /api/v1/auth/reset-password — validate token and set new password."""

    @extend_schema(
        request=ResetPasswordSerializer, responses=TokenResponseSerializer, tags=["auth"]
    )
    def post(self, request: Request) -> Response:
        ser = ResetPasswordSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user = verify_password_reset_token(ser.validated_data["token"])
        user.set_password(ser.validated_data["password"])
        # Stamp ``password_changed_at`` so any access tokens minted before
        # this moment fail the ``pwd_iat`` check in ``JWTAuthentication``.
        user.password_changed_at = datetime.now(UTC)
        update_fields = ["password", "password_changed_at", "updated_at"]
        # Consuming a reset link delivered to the user's email proves mailbox
        # control — equivalent to clicking the verification link.
        if not user.is_verified:
            user.is_verified = True
            update_fields.append("is_verified")
        user.save(update_fields=update_fields)

        # Revoke all existing refresh tokens after password reset
        revoke_all_refresh_tokens(user)

        refresh = create_refresh_token(user)
        return _token_response(user, refresh)


class ChangePasswordView(AuthScopedView):
    """POST /api/v1/auth/change-password — change password while authenticated."""

    permission_classes: ClassVar[list[type[BasePermission]]] = [IsAuthenticated]  # type: ignore[misc]

    @extend_schema(
        request=ChangePasswordSerializer, responses=TokenResponseSerializer, tags=["auth"]
    )
    def post(self, request: Request) -> Response:
        ser = ChangePasswordSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user = get_user(request)
        if not user.check_password(ser.validated_data["current_password"]):
            raise InvalidPassword

        user.set_password(ser.validated_data["new_password"])
        # Stamp ``password_changed_at`` so any access tokens minted before
        # this moment fail the ``pwd_iat`` check in ``JWTAuthentication``.
        user.password_changed_at = datetime.now(UTC)
        user.save(update_fields=["password", "password_changed_at", "updated_at"])

        # Revoke all existing refresh tokens — force re-login on other devices
        revoke_all_refresh_tokens(user)

        refresh = create_refresh_token(user)
        return _token_response(user, refresh)


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def _oauth_error_redirect(frontend_url: str, code: str) -> HttpResponseRedirect:
    """Send the browser back to the frontend's OAuth error page."""
    return HttpResponseRedirect(f"{frontend_url}/auth/error?{urlencode({'error': code})}")


class OAuthAuthorizeView(AuthPublicView):
    """GET /api/v1/auth/oauth/{provider}/ — redirect to OAuth provider."""

    @extend_schema(exclude=True)
    def get(self, request: Request, provider: str) -> HttpResponseRedirect:
        # The whole endpoint is a top-of-funnel redirect; surfacing a JSON
        # ``{detail,code}`` body for an unknown provider would dump raw API
        # output into the user's browser. Funnel the error through the same
        # frontend redirect path used by the callback so the FE can render a
        # consistent ``/auth/error`` page.
        frontend_url: str = settings.FRONTEND_URL
        if provider not in PROVIDERS:
            return _oauth_error_redirect(frontend_url, "invalid_provider")

        state = secrets.token_urlsafe(32)
        request.session["oauth_state"] = state

        redirect_uri = request.build_absolute_uri(f"/api/v1/auth/oauth/{provider}/callback/")
        url = get_authorization_url(provider, redirect_uri, state)

        return HttpResponseRedirect(url)


class OAuthCallbackView(AuthPublicView):
    """GET /api/v1/auth/oauth/{provider}/callback/ — exchange code for tokens.

    The view itself is sync (DRF + the project's sync session/auth/hijack
    middleware chain require this), but it drives the OAuth provider
    round-trip via :func:`exchange_code`, an async function backed by a
    shared :class:`httpx.AsyncClient`. The ``async_to_sync`` bridge keeps
    the connection pool warm across the back-to-back token/userinfo/emails
    calls in a single callback while leaving the request thread blocked
    only on the actual provider latency.
    """

    @extend_schema(exclude=True)
    def get(self, request: Request, provider: str) -> HttpResponseRedirect:
        frontend_url: str = settings.FRONTEND_URL

        # Same reasoning as OAuthAuthorizeView: the callback is a redirect
        # endpoint, so the unknown-provider path goes through the same
        # frontend error page rather than rendering JSON.
        if provider not in PROVIDERS:
            return _oauth_error_redirect(frontend_url, "invalid_provider")

        code = request.query_params.get("code")
        state = request.query_params.get("state")
        error = request.query_params.get("error")

        if error:
            return _oauth_error_redirect(frontend_url, error)

        expected_state = request.session.pop("oauth_state", None)
        if not state or state != expected_state:
            return _oauth_error_redirect(frontend_url, "invalid_state")

        if not code:
            return _oauth_error_redirect(frontend_url, "missing_code")

        try:
            redirect_uri = request.build_absolute_uri(f"/api/v1/auth/oauth/{provider}/callback/")
            user_info = async_to_sync(exchange_code)(provider, code, redirect_uri)
        except (httpx.HTTPError, OAuthError):
            # Transient/expected provider errors (network, 5xx, OAuth domain
            # rejections). Dashboards alert on volume here rather than shape.
            logger.exception("OAuth code exchange failed for %s", provider)
            return _oauth_error_redirect(frontend_url, "exchange_failed")
        except (ValueError, KeyError) as exc:
            # The provider returned a payload we couldn't parse (missing field,
            # wrong type). Split the dashboard signal from transient errors so
            # a sudden burst is visible against a quiet baseline — provider
            # API drift is something we want to catch fast.
            logger.exception("OAuth provider response shape unexpected: %s", exc)
            return _oauth_error_redirect(frontend_url, "provider_error")

        try:
            resolution = resolve_oauth_user(provider, user_info)
        except OAuthEmailNotVerifiedError:
            return _oauth_error_redirect(frontend_url, "email_not_verified")
        except ValueError:
            return _oauth_error_redirect(frontend_url, "account_deactivated")

        if resolution.kind == "collision":
            # Provider didn't verify the email AND isn't on the trust list,
            # but the email matches an existing local account. Mint an
            # email-confirm token bound to the OAuth identity and queue an
            # email asking the inbox owner to confirm the link. Inactive
            # accounts (existing_user is None) collapse to the same redirect
            # without queuing an email — anti-enumeration.
            if resolution.existing_user is not None:
                # Invalidate any prior pending requests so only the freshest
                # link works (mirrors ResendVerificationView's pattern).
                SocialLinkRequest.objects.filter(
                    user=resolution.existing_user,
                    used_at__isnull=True,
                ).update(used_at=datetime.now(UTC))
                token = create_social_link_token(
                    resolution.existing_user,
                    provider=provider,
                    provider_user_id=user_info.provider_user_id,
                    full_name=user_info.full_name,
                    avatar_url=user_info.avatar_url,
                )
                send_social_link_email_task.delay(
                    resolution.existing_user.email, token, provider
                )
            return HttpResponseRedirect(f"{frontend_url}/auth/link-email-sent")

        user = resolution.user
        assert user is not None  # noqa: S101  # resolution.kind=="user" guarantees this
        if not user.is_active:
            return _oauth_error_redirect(frontend_url, "account_deactivated")

        refresh = create_refresh_token(user)
        access = create_access_token(user)
        # Issue a single-use opaque code instead of embedding tokens in the
        # redirect URL. Any third-party script that runs on /auth/callback
        # (analytics, chat widgets) would otherwise be able to read tokens
        # directly from window.location.hash. The frontend POSTs the code
        # to /oauth/exchange/ which swaps it for the actual token pair.
        code = _store_oauth_exchange(access, refresh)
        return HttpResponseRedirect(f"{frontend_url}/auth/callback#{urlencode({'code': code})}")


# ---------------------------------------------------------------------------
# OAuth one-time-code exchange (PKCE-style)
# ---------------------------------------------------------------------------

_OAUTH_EXCHANGE_PREFIX = "oauth_exchange:"
_OAUTH_EXCHANGE_TTL = 60  # seconds


def _store_oauth_exchange(access_token: str, refresh_token: str) -> str:
    """Cache the issued token pair under a fresh opaque code and return the code."""
    code = secrets.token_urlsafe(32)
    cache.set(
        f"{_OAUTH_EXCHANGE_PREFIX}{code}",
        {"access_token": access_token, "refresh_token": refresh_token},
        timeout=_OAUTH_EXCHANGE_TTL,
    )
    return code


def _consume_oauth_exchange(code: str) -> dict[str, str] | None:
    """Atomically retrieve-and-delete a cached token pair by its one-time code.

    Two concurrent callers must never both succeed: the issued code grants
    a session, so a double-redeem race could hand a session to an attacker
    racing the legitimate frontend. ``cache.add`` is atomic on both Redis
    (``SET NX``) and LocMemCache, so the first caller to land the
    ``:consumed`` sentinel is the unique winner; subsequent callers see
    ``add`` return ``False`` and get ``None`` even if the original code
    has not yet been deleted from the cache.
    """
    key = f"{_OAUTH_EXCHANGE_PREFIX}{code}"
    sentinel = f"{key}:consumed"
    if not cache.add(sentinel, 1, timeout=_OAUTH_EXCHANGE_TTL):
        return None
    data: dict[str, str] | None = cache.get(key)
    cache.delete(key)
    return data


class OAuthExchangeRequestSerializer(drf_serializers.Serializer[object]):
    code = drf_serializers.CharField(max_length=128)


class OAuthExchangeView(AuthPublicView):
    """POST /api/v1/auth/oauth/exchange/ — swap a one-time code for a token pair."""

    @extend_schema(
        request=OAuthExchangeRequestSerializer,
        responses={200: TokenResponseSerializer},
        tags=["auth"],
    )
    def post(self, request: Request) -> Response:
        ser = OAuthExchangeRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = _consume_oauth_exchange(ser.validated_data["code"])
        if data is None:
            raise InvalidOAuthCode
        return Response(
            {
                "access_token": data["access_token"],
                "refresh_token": data["refresh_token"],
                "token_type": "Bearer",
                "expires_in": int(ACCESS_TOKEN_LIFETIME.total_seconds()),
            }
        )


class OAuthConfirmLinkView(AuthPublicView):
    """POST /api/v1/auth/oauth/confirm-link/ — link an OAuth provider via email proof.

    The OAuth callback mints a SocialLinkRequest when the provider's email
    matches an existing account but cannot be auto-linked (either
    ``email_verified`` is false, or the provider is not on
    ``TRUSTED_FOR_AUTO_LINK``). Clicking the link in that email re-proves
    mailbox control; this endpoint then attaches the SocialAccount and
    signs the user in.
    """

    @extend_schema(
        request=OAuthConfirmLinkSerializer,
        responses={200: TokenResponseSerializer},
        tags=["auth"],
    )
    def post(self, request: Request) -> Response:
        ser = OAuthConfirmLinkSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        link_request = verify_social_link_token(ser.validated_data["token"])
        user = link_request.user

        # Defensive collision check: if some other user already owns
        # (provider, provider_user_id), refuse — should never happen since
        # the SocialLinkRequest was minted for *this* user, but a stale
        # token plus an intervening manual link in admin could in principle
        # produce it.
        existing = SocialAccount.objects.filter(
            provider=link_request.provider,
            provider_user_id=link_request.provider_user_id,
        ).first()
        if existing is not None and existing.user_id != user.id:
            return Response(
                {
                    "detail": "Provider account already linked to another user.",
                    "code": "social_account_collision",
                },
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            if existing is None:
                # ``get_or_create`` swallows the rare TOCTOU race where a
                # parallel request inserted the same row between our SELECT
                # and INSERT.
                SocialAccount.objects.get_or_create(
                    provider=link_request.provider,
                    provider_user_id=link_request.provider_user_id,
                    defaults={"user": user},
                )
            # Mailbox proof: equivalent to clicking the verify-email link or
            # consuming a password-reset token. Mark the existing user
            # verified if they weren't already.
            if not user.is_verified:
                user.is_verified = True
                user.save(update_fields=["is_verified", "updated_at"])

        # Reuse the shared token envelope so the confirm-link response stays
        # bit-identical with login/refresh/register.
        return _token_response(user, create_refresh_token(user))
