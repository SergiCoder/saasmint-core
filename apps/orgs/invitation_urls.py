"""URL patterns for token-based invitation endpoints (outside org context)."""

from django.urls import path

from apps.orgs.views import InvitationAcceptView, InvitationDeclineView

urlpatterns = [
    path(
        "invitations/<str:token>/accept/",
        InvitationAcceptView.as_view(),
        name="invitation-accept",
    ),
    path(
        "invitations/<str:token>/decline/",
        InvitationDeclineView.as_view(),
        name="invitation-decline",
    ),
]
