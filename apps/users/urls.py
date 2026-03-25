"""URL patterns for the users app."""

from django.urls import path

from apps.users.views import AccountExportView, AccountView

urlpatterns = [
    path("", AccountView.as_view(), name="account"),
    path("export/", AccountExportView.as_view(), name="account-export"),
]
