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

# Roles that can be assigned by admins/owners — owner is set only via the
# dedicated ownership-transfer endpoint, never via invitation or member PATCH.
_ASSIGNABLE_ROLES = [
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
    org = OrgSerializer(read_only=True)

    class Meta:
        model = OrgMember
        fields = ("id", "org", "user", "role", "is_billing", "joined_at")
        read_only_fields = fields


class UpdateMemberSerializer(serializers.Serializer[OrgMember]):
    role = serializers.ChoiceField(choices=_ASSIGNABLE_ROLES, required=False)
    is_billing = serializers.BooleanField(required=False)


class _InvitedBySerializer(serializers.ModelSerializer[User]):
    """Inviter shape for authenticated invitation listings.

    Excludes ``email`` — leaking the inviter's address to invitees (or any
    party holding the token) is not required by the accept-page UX, and
    the unauthenticated public detail view reuses this shape.
    """

    class Meta:
        model = User
        fields = ("id", "full_name")
        read_only_fields = fields


class InvitationSerializer(serializers.ModelSerializer[Invitation]):
    """Authenticated view of an invitation (admin listings, create response).

    The unauthenticated detail view uses :class:`PublicInvitationSerializer`
    instead, which strips fields that would leak the invitee's email.
    """

    invited_by = _InvitedBySerializer(read_only=True)
    org = OrgSerializer(read_only=True)

    class Meta:
        model = Invitation
        fields = (
            "id",
            "org",
            "email",
            "role",
            "status",
            "invited_by",
            "created_at",
            "expires_at",
        )
        read_only_fields = fields


class PublicInvitationSerializer(serializers.ModelSerializer[Invitation]):
    """Unauthenticated GET /invitations/{token}/ shape — no PII.

    Anyone holding the token can read this. ``email`` (the invitee address)
    is dropped so a leaked token cannot enumerate addresses; the inviter is
    reduced to ``full_name`` via :class:`_InvitedBySerializer`.
    """

    invited_by = _InvitedBySerializer(read_only=True)
    org = OrgSerializer(read_only=True)

    class Meta:
        model = Invitation
        fields = (
            "id",
            "org",
            "role",
            "status",
            "invited_by",
            "created_at",
            "expires_at",
        )
        read_only_fields = fields


class CreateInvitationSerializer(serializers.Serializer[Invitation]):
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=_ASSIGNABLE_ROLES, default=OrgRole.MEMBER)


class InvitationAcceptSerializer(serializers.Serializer[Invitation]):
    full_name = serializers.CharField(min_length=3, max_length=255)


class TransferOwnershipSerializer(serializers.Serializer[OrgMember]):
    user_id = serializers.UUIDField()
