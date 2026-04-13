"""URL patterns for the users app."""

from django.urls import path

from apps.users.views import AccountExportView, AccountView, AvatarView

urlpatterns = [
    path("", AccountView.as_view(), name="account"),
    path("avatar/", AvatarView.as_view(), name="account-avatar"),
    path("export/", AccountExportView.as_view(), name="account-export"),
]
