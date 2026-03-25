"""Organisation and membership API views."""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import ValidationError
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from stripe_saas_core.domain.org import OrgRole as CoreOrgRole
from stripe_saas_core.exceptions import InsufficientPermissionError, OrgNotFoundError
from stripe_saas_core.services.orgs import check_can_assign_role, check_can_manage_member

from apps.orgs.models import Org, OrgMember, OrgRole
from apps.orgs.serializers import (
    AddMemberSerializer,
    CreateOrgSerializer,
    OrgMemberSerializer,
    OrgSerializer,
    UpdateMemberSerializer,
    UpdateOrgSerializer,
)
from apps.users.models import User
from helpers import get_user

_ADMIN_OR_ABOVE = (OrgRole.OWNER, OrgRole.ADMIN)
_OWNER_ONLY = (OrgRole.OWNER,)


def _get_org_and_member(
    user_id: UUID,
    org_id: UUID,
    allowed_roles: tuple[OrgRole, ...] | None = None,
) -> tuple[Org, OrgMember]:
    """Fetch an org and verify the user's membership in a single query.

    Raises OrgNotFoundError or InsufficientPermissionError as appropriate.
    """
    try:
        member = OrgMember.objects.select_related("org").get(
            org_id=org_id, org__deleted_at__isnull=True, user_id=user_id
        )
    except OrgMember.DoesNotExist:
        if not Org.objects.filter(id=org_id, deleted_at__isnull=True).exists():
            raise OrgNotFoundError(org_id) from None
        raise InsufficientPermissionError("Access denied.") from None
    if allowed_roles is not None and OrgRole(member.role) not in allowed_roles:
        raise InsufficientPermissionError("Insufficient permissions for this action.")
    return member.org, member


class OrgListCreateView(APIView):
    """GET /api/v1/orgs/ — list user's orgs; POST — create a new org."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "orgs"

    def get(self, request: Request) -> Response:
        user = get_user(request)
        orgs = Org.objects.filter(
            id__in=OrgMember.objects.filter(user=user).values("org_id"),
            deleted_at__isnull=True,
        ).order_by("name")
        paginator = LimitOffsetPagination()
        paginator.default_limit = 50
        paginator.max_limit = 100
        page = paginator.paginate_queryset(orgs, request)
        return paginator.get_paginated_response(OrgSerializer(page, many=True).data)

    def post(self, request: Request) -> Response:
        user = get_user(request)
        ser = CreateOrgSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                org = Org.objects.create(
                    name=ser.validated_data["name"],
                    slug=ser.validated_data["slug"],
                    logo_url=ser.validated_data.get("logo_url"),
                    created_by=user,
                )
                OrgMember.objects.create(
                    org=org,
                    user=user,
                    role=OrgRole.OWNER,
                )
        except IntegrityError:
            raise ValidationError({"slug": ["An org with this slug already exists."]}) from None
        return Response(OrgSerializer(org).data, status=status.HTTP_201_CREATED)


class OrgDetailView(APIView):
    """GET/PATCH/DELETE /api/v1/orgs/{org_id}/."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "orgs"

    def get(self, request: Request, org_id: UUID) -> Response:
        user = get_user(request)
        org, _ = _get_org_and_member(user.id, org_id)
        return Response(OrgSerializer(org).data)

    def patch(self, request: Request, org_id: UUID) -> Response:
        user = get_user(request)
        org, _ = _get_org_and_member(user.id, org_id, allowed_roles=_ADMIN_OR_ABOVE)
        ser = UpdateOrgSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if not ser.validated_data:
            return Response(OrgSerializer(org).data)
        for field, value in ser.validated_data.items():
            setattr(org, field, value)
        org.save(update_fields=list(ser.validated_data.keys()))
        return Response(OrgSerializer(org).data)

    def delete(self, request: Request, org_id: UUID) -> Response:
        user = get_user(request)
        org, _ = _get_org_and_member(user.id, org_id, allowed_roles=_OWNER_ONLY)
        org.deleted_at = timezone.now()
        org.save(update_fields=["deleted_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class OrgMemberListView(APIView):
    """GET /api/v1/orgs/{org_id}/members/ — list members; POST — add member."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "orgs"

    def get(self, request: Request, org_id: UUID) -> Response:
        user = get_user(request)
        org, _ = _get_org_and_member(user.id, org_id)
        queryset = OrgMember.objects.filter(org=org).select_related("user").order_by("joined_at")
        paginator = LimitOffsetPagination()
        paginator.default_limit = 50
        paginator.max_limit = 200
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response(OrgMemberSerializer(page, many=True).data)

    def post(self, request: Request, org_id: UUID) -> Response:
        user = get_user(request)
        org, caller = _get_org_and_member(user.id, org_id, allowed_roles=_ADMIN_OR_ABOVE)
        ser = AddMemberSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        # Prevent escalation: cannot assign a role at or above caller's own level
        check_can_assign_role(
            caller_role=CoreOrgRole(caller.role),
            new_role=CoreOrgRole(ser.validated_data["role"]),
        )

        target_user = get_object_or_404(User, id=ser.validated_data["user_id"])
        member, created = OrgMember.objects.get_or_create(
            org=org,
            user=target_user,
            defaults={
                "role": ser.validated_data["role"],
                "is_billing": ser.validated_data["is_billing"],
            },
        )
        code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(OrgMemberSerializer(member).data, status=code)


class OrgMemberDetailView(APIView):
    """PATCH/DELETE /api/v1/orgs/{org_id}/members/{user_id}/."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]
    throttle_scope = "orgs"

    def patch(self, request: Request, org_id: UUID, member_user_id: UUID) -> Response:
        user = get_user(request)
        _, caller = _get_org_and_member(user.id, org_id, allowed_roles=_ADMIN_OR_ABOVE)
        target = get_object_or_404(
            OrgMember, org_id=org_id, user_id=member_user_id, org__deleted_at__isnull=True
        )

        # Only OWNER can modify roles at or above ADMIN level
        check_can_manage_member(
            caller_role=CoreOrgRole(caller.role),
            target_role=CoreOrgRole(target.role),
        )

        ser = UpdateMemberSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if not ser.validated_data:
            return Response(OrgMemberSerializer(target).data)

        # Prevent escalation: new role cannot exceed caller's own role
        new_role = ser.validated_data.get("role")
        if new_role is not None:
            check_can_assign_role(
                caller_role=CoreOrgRole(caller.role),
                new_role=CoreOrgRole(new_role),
            )

        for field, value in ser.validated_data.items():
            setattr(target, field, value)
        target.save(update_fields=list(ser.validated_data.keys()))
        return Response(OrgMemberSerializer(target).data)

    def delete(self, request: Request, org_id: UUID, member_user_id: UUID) -> Response:
        user = get_user(request)
        _, caller = _get_org_and_member(user.id, org_id, allowed_roles=_ADMIN_OR_ABOVE)
        target = get_object_or_404(
            OrgMember, org_id=org_id, user_id=member_user_id, org__deleted_at__isnull=True
        )

        # Prevent owner from removing themselves (would leave org ownerless)
        if target.user_id == user.id and target.role == OrgRole.OWNER:
            raise InsufficientPermissionError(
                "Owner cannot remove themselves. Transfer ownership first."
            )

        # Cannot remove members at or above your own role level
        check_can_manage_member(
            caller_role=CoreOrgRole(caller.role),
            target_role=CoreOrgRole(target.role),
        )

        target.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
