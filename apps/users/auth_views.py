"""Authentication API views — register, login, refresh, logout, verify, password reset, OAuth."""

from __future__ import annotations

import logging
import secrets
from typing import ClassVar
from urllib.parse import urlencode

import httpx
from django.conf import settings
from django.contrib.auth import authenticate
from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.base_views import AuthPublicView, AuthScopedView
from apps.billing.services import assign_free_plan
from apps.users.auth_serializers import (
    ChangePasswordSerializer,
    ForgotPasswordSerializer,
    LoginSerializer,
    LogoutSerializer,
    MessageResponseSerializer,
    RefreshSerializer,
    RegisterSerializer,
    ResetPasswordSerializer,
    TokenResponseSerializer,
    VerifyEmailSerializer,
)
from apps.users.authentication import (
    create_access_token,
    create_email_verification_token,
    create_password_reset_token,
    create_refresh_token,
    revoke_all_refresh_tokens,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_email_token,
    verify_password_reset_token,
)
from apps.users.models import AccountType, User
from apps.users.oauth import (
    PROVIDERS,
    OAuthEmailNotVerifiedError,
    OAuthError,
    exchange_code,
    get_authorization_url,
)
from apps.users.services import email_is_registered, resolve_oauth_user
from apps.users.tasks import send_password_reset_email_task, send_verification_email_task
from helpers import get_user

logger = logging.getLogger(__name__)


def _token_response(user: User, refresh_token: str, http_status: int = 200) -> Response:
    return Response(
        {
            "access_token": create_access_token(user),
            "refresh_token": refresh_token,
            "token_type": "Bearer",
        },
        status=http_status,
    )


def _register_user(
    *,
    email: str,
    password: str,
    full_name: str,
    assign_free: bool,
) -> Response:
    """Create a new user and return a 201 token response.

    ``assign_free=True`` creates a PERSONAL user and assigns the free plan;
    ``assign_free=False`` creates an ORG_MEMBER user (team checkout will later
    create the org + subscription).
    """
    if email_is_registered(email):
        return Response(
            {"detail": "Email already registered.", "code": "email_exists"},
            status=status.HTTP_409_CONFLICT,
        )

    account_type = AccountType.PERSONAL if assign_free else AccountType.ORG_MEMBER

    try:
        with transaction.atomic():
            user = User.objects.create_user(
                email=email,
                password=password,
                full_name=full_name,
                is_verified=False,
                account_type=account_type,
            )
    except IntegrityError:
        return Response(
            {"detail": "Email already registered.", "code": "email_exists"},
            status=status.HTTP_409_CONFLICT,
        )

    if assign_free:
        assign_free_plan(user)

    token = create_email_verification_token(user)
    send_verification_email_task.delay(user.email, token)

    refresh = create_refresh_token(user)
    return _token_response(user, refresh, http_status=status.HTTP_201_CREATED)


class RegisterView(AuthPublicView):
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
            assign_free=True,
        )


class RegisterOrgOwnerView(AuthPublicView):
    """POST /api/v1/auth/register/org-owner — register as an org owner.

    Creates a user with account_type=ORG_MEMBER. No free plan is assigned;
    the user must complete team checkout to create an org and subscription.
    """

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
            assign_free=False,
        )


class VerifyEmailView(AuthPublicView):
    """POST /api/v1/auth/verify-email — activate a user account."""

    @extend_schema(request=VerifyEmailSerializer, responses=TokenResponseSerializer, tags=["auth"])
    def post(self, request: Request) -> Response:
        ser = VerifyEmailSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        user = verify_email_token(ser.validated_data["token"])
        if not user.is_verified:
            user.is_verified = True
            user.save(update_fields=["is_verified", "updated_at"])

        refresh = create_refresh_token(user)
        return _token_response(user, refresh)


class LoginView(AuthPublicView):
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
            return Response(
                {"detail": "Invalid credentials.", "code": "invalid_credentials"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_active:
            return Response(
                {"detail": "Account is deactivated.", "code": "account_deactivated"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not user.is_verified:
            return Response(
                {"detail": "Email not verified.", "code": "email_not_verified"},
                status=status.HTTP_403_FORBIDDEN,
            )

        refresh = create_refresh_token(user)
        return _token_response(user, refresh)


class RefreshView(AuthPublicView):
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

        # Always return 200 to prevent email enumeration
        try:
            user = User.objects.get(
                email=ser.validated_data["email"],
                is_active=True,
            )
            token = create_password_reset_token(user)
            send_password_reset_email_task.delay(user.email, token)
        except User.DoesNotExist:
            pass

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
        user.save(update_fields=["password", "updated_at"])

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
            return Response(
                {"detail": "Current password is incorrect.", "code": "invalid_password"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(ser.validated_data["new_password"])
        user.save(update_fields=["password", "updated_at"])

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
    def get(self, request: Request, provider: str) -> Response | HttpResponseRedirect:
        if provider not in PROVIDERS:
            return Response(
                {"detail": f"Unsupported provider: {provider}", "code": "invalid_provider"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        state = secrets.token_urlsafe(32)
        request.session["oauth_state"] = state

        redirect_uri = request.build_absolute_uri(f"/api/v1/auth/oauth/{provider}/callback/")
        url = get_authorization_url(provider, redirect_uri, state)

        return HttpResponseRedirect(url)


class OAuthCallbackView(AuthPublicView):
    """GET /api/v1/auth/oauth/{provider}/callback/ — exchange code for tokens."""

    @extend_schema(exclude=True)
    def get(self, request: Request, provider: str) -> Response | HttpResponseRedirect:
        if provider not in PROVIDERS:
            return Response(
                {"detail": f"Unsupported provider: {provider}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        code = request.query_params.get("code")
        state = request.query_params.get("state")
        error = request.query_params.get("error")

        frontend_url: str = settings.FRONTEND_URL

        if error:
            return _oauth_error_redirect(frontend_url, error)

        expected_state = request.session.pop("oauth_state", None)
        if not state or state != expected_state:
            return _oauth_error_redirect(frontend_url, "invalid_state")

        if not code:
            return _oauth_error_redirect(frontend_url, "missing_code")

        try:
            redirect_uri = request.build_absolute_uri(f"/api/v1/auth/oauth/{provider}/callback/")
            user_info = exchange_code(provider, code, redirect_uri)
        except (httpx.HTTPError, OAuthError, ValueError, KeyError):
            logger.exception("OAuth code exchange failed for %s", provider)
            return _oauth_error_redirect(frontend_url, "exchange_failed")

        try:
            user = resolve_oauth_user(provider, user_info)
        except OAuthEmailNotVerifiedError:
            return _oauth_error_redirect(frontend_url, "email_not_verified")
        except ValueError:
            return _oauth_error_redirect(frontend_url, "account_deactivated")

        if not user.is_active:
            return _oauth_error_redirect(frontend_url, "account_deactivated")

        refresh = create_refresh_token(user)
        access = create_access_token(user)
        # Tokens go into the URL fragment, not the query string, so they are
        # not logged in browser history, proxies, or leaked via Referer.
        params = urlencode({"access_token": access, "refresh_token": refresh})
        return HttpResponseRedirect(f"{frontend_url}/auth/callback#{params}")
