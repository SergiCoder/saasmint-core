"""URL patterns for the orgs app."""

from django.urls import path

from apps.orgs.views import OrgDetailView, OrgListCreateView, OrgMemberDetailView, OrgMemberListView

urlpatterns = [
    path("", OrgListCreateView.as_view(), name="org-list-create"),
    path("<uuid:org_id>/", OrgDetailView.as_view(), name="org-detail"),
    path("<uuid:org_id>/members/", OrgMemberListView.as_view(), name="org-member-list"),
    path(
        "<uuid:org_id>/members/<uuid:member_user_id>/",
        OrgMemberDetailView.as_view(),
        name="org-member-detail",
    ),
]
