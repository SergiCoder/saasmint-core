"""URL patterns for authentication endpoints."""

from django.urls import path

from apps.users.auth_views import (
    ChangePasswordView,
    ForgotPasswordView,
    LoginView,
    LogoutView,
    OAuthAuthorizeView,
    OAuthCallbackView,
    RefreshView,
    RegisterOrgOwnerView,
    RegisterView,
    ResetPasswordView,
    VerifyEmailView,
)

urlpatterns = [
    path("register/", RegisterView.as_view(), name="auth-register"),
    path("register/org-owner/", RegisterOrgOwnerView.as_view(), name="auth-register-org-owner"),
    path("login/", LoginView.as_view(), name="auth-login"),
    path("refresh/", RefreshView.as_view(), name="auth-refresh"),
    path("logout/", LogoutView.as_view(), name="auth-logout"),
    path("verify-email/", VerifyEmailView.as_view(), name="auth-verify-email"),
    path("forgot-password/", ForgotPasswordView.as_view(), name="auth-forgot-password"),
    path("reset-password/", ResetPasswordView.as_view(), name="auth-reset-password"),
    path("change-password/", ChangePasswordView.as_view(), name="auth-change-password"),
    path("oauth/<str:provider>/", OAuthAuthorizeView.as_view(), name="auth-oauth-authorize"),
    path(
        "oauth/<str:provider>/callback/",
        OAuthCallbackView.as_view(),
        name="auth-oauth-callback",
    ),
]
