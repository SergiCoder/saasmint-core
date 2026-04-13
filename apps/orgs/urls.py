"""URL patterns for the orgs app."""

from django.urls import path

from apps.orgs.views import (
    InvitationCancelView,
    InvitationListCreateView,
    OrgDetailView,
    OrgListView,
    OrgMemberDetailView,
    OrgMemberListView,
    OrgTransferOwnershipView,
)

urlpatterns = [
    path("", OrgListView.as_view(), name="org-list"),
    path("<uuid:org_id>/", OrgDetailView.as_view(), name="org-detail"),
    path("<uuid:org_id>/members/", OrgMemberListView.as_view(), name="org-member-list"),
    path(
        "<uuid:org_id>/members/<uuid:member_user_id>/",
        OrgMemberDetailView.as_view(),
        name="org-member-detail",
    ),
    path(
        "<uuid:org_id>/transfer-ownership/",
        OrgTransferOwnershipView.as_view(),
        name="org-transfer-ownership",
    ),
    path(
        "<uuid:org_id>/invitations/",
        InvitationListCreateView.as_view(),
        name="org-invitation-list-create",
    ),
    path(
        "<uuid:org_id>/invitations/<uuid:invitation_id>/",
        InvitationCancelView.as_view(),
        name="org-invitation-cancel",
    ),
]
