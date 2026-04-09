"""URL patterns for the users app."""

from django.urls import path

from apps.users.views import AccountExportView, AccountView, AvatarView, CancelDeletionView

urlpatterns = [
    path("", AccountView.as_view(), name="account"),
    path("avatar/", AvatarView.as_view(), name="account-avatar"),
    path("export/", AccountExportView.as_view(), name="account-export"),
    path("cancel-deletion/", CancelDeletionView.as_view(), name="cancel-deletion"),
]
