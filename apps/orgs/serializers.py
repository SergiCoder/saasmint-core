"""Request/response serializers for the orgs app."""

from __future__ import annotations

from rest_framework import serializers

from apps.orgs.models import Org, OrgMember, OrgRole


class OrgSerializer(serializers.ModelSerializer[Org]):
    class Meta:
        model = Org
        fields = ("id", "name", "slug", "logo_url", "created_at")
        read_only_fields = fields


class CreateOrgSerializer(serializers.Serializer[Org]):
    name = serializers.CharField(max_length=255)
    slug = serializers.SlugField(max_length=255)
    logo_url = serializers.URLField(required=False, allow_null=True, default=None)

    def validate_slug(self, value: str) -> str:
        if Org.objects.filter(slug=value, deleted_at__isnull=True).exists():
            raise serializers.ValidationError("An org with this slug already exists.")
        return value


class UpdateOrgSerializer(serializers.Serializer[Org]):
    name = serializers.CharField(max_length=255, required=False)
    logo_url = serializers.URLField(required=False, allow_null=True)


class OrgMemberSerializer(serializers.ModelSerializer[OrgMember]):
    class Meta:
        model = OrgMember
        fields = ("id", "org", "user", "role", "is_billing", "joined_at")
        read_only_fields = fields


class AddMemberSerializer(serializers.Serializer[OrgMember]):
    user_id = serializers.UUIDField()
    role = serializers.ChoiceField(choices=OrgRole.choices, default=OrgRole.MEMBER)
    is_billing = serializers.BooleanField(default=False)


class UpdateMemberSerializer(serializers.Serializer[OrgMember]):
    role = serializers.ChoiceField(choices=OrgRole.choices, required=False)
    is_billing = serializers.BooleanField(required=False)
