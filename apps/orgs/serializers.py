"""Request/response serializers for the orgs app."""

from __future__ import annotations

from django.core.validators import URLValidator
from rest_framework import serializers

from apps.orgs.models import Invitation, Org, OrgMember, OrgRole
from apps.users.models import User

# Reject non-HTTPS schemes (``javascript:``, ``data:``, ``http:``) so a stored
# logo_url cannot be used to redirect the browser into a phishing flow or leak
# data over plain HTTP when the page renders ``<img src=...>``. Submissions
# must use HTTPS — covers every reasonable hosted-logo CDN.
_HTTPS_ONLY = URLValidator(schemes=["https"])

# Roles that can be assigned via invitation (owner is never invited)
_INVITABLE_ROLES = [
    (OrgRole.ADMIN, "Admin"),
    (OrgRole.MEMBER, "Member"),
]


class OrgSerializer(serializers.ModelSerializer[Org]):
    class Meta:
        model = Org
        fields = ("id", "name", "slug", "logo_url", "created_at")
        read_only_fields = fields


class UpdateOrgSerializer(serializers.Serializer[Org]):
    name = serializers.CharField(max_length=255, required=False)
    logo_url = serializers.URLField(required=False, allow_null=True, validators=[_HTTPS_ONLY])


class _MemberUserSerializer(serializers.ModelSerializer[User]):
    class Meta:
        model = User
        fields = ("id", "email", "full_name", "avatar_url")
        read_only_fields = fields


class OrgMemberSerializer(serializers.ModelSerializer[OrgMember]):
    user = _MemberUserSerializer(read_only=True)

    class Meta:
        model = OrgMember
        fields = ("id", "org", "user", "role", "is_billing", "joined_at")
        read_only_fields = fields


class UpdateMemberSerializer(serializers.Serializer[OrgMember]):
    role = serializers.ChoiceField(choices=OrgRole.choices, required=False)
    is_billing = serializers.BooleanField(required=False)


class _InvitedBySerializer(serializers.ModelSerializer[User]):
    class Meta:
        model = User
        fields = ("id", "email", "full_name")
        read_only_fields = fields


class InvitationSerializer(serializers.ModelSerializer[Invitation]):
    invited_by = _InvitedBySerializer(read_only=True)
    org_name = serializers.CharField(source="org.name", read_only=True)

    class Meta:
        model = Invitation
        fields = (
            "id",
            "org",
            "org_name",
            "email",
            "role",
            "status",
            "invited_by",
            "created_at",
            "expires_at",
        )
        read_only_fields = fields


class CreateInvitationSerializer(serializers.Serializer[Invitation]):
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=_INVITABLE_ROLES, default=OrgRole.MEMBER)


class InvitationAcceptSerializer(serializers.Serializer[Invitation]):
    full_name = serializers.CharField(min_length=3, max_length=255)


class TransferOwnershipSerializer(serializers.Serializer[OrgMember]):
    user_id = serializers.UUIDField()
