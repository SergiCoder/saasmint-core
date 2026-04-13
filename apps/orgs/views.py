"""Organization, membership, and invitation API views."""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import ClassVar
from uuid import UUID

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from saasmint_core.domain.org import OrgRole as CoreOrgRole
from saasmint_core.exceptions import InsufficientPermissionError, OrgNotFoundError
from saasmint_core.services.orgs import check_can_manage_member

from apps.orgs.models import Invitation, InvitationStatus, Org, OrgMember, OrgRole
from apps.orgs.serializers import (
    CreateInvitationSerializer,
    InvitationAcceptSerializer,
    InvitationSerializer,
    OrgMemberSerializer,
    OrgSerializer,
    TransferOwnershipSerializer,
    UpdateMemberSerializer,
    UpdateOrgSerializer,
)
from apps.users.models import AccountType, User
from helpers import get_user

logger = logging.getLogger(__name__)

_ADMIN_OR_ABOVE = (OrgRole.OWNER, OrgRole.ADMIN)
_OWNER_ONLY = (OrgRole.OWNER,)

INVITATION_EXPIRY_DAYS = 7


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
            org_id=org_id, org__deleted_at__isnull=True, org__is_active=True, user_id=user_id
        )
    except OrgMember.DoesNotExist:
        if not Org.objects.filter(id=org_id, deleted_at__isnull=True, is_active=True).exists():
            raise OrgNotFoundError(org_id) from None
        raise InsufficientPermissionError("Access denied.") from None
    if allowed_roles is not None and OrgRole(member.role) not in allowed_roles:
        raise InsufficientPermissionError("Insufficient permissions for this action.")
    return member.org, member


# ---------------------------------------------------------------------------
# Org List / Detail
# ---------------------------------------------------------------------------


class OrgListView(APIView):
    """GET /api/v1/orgs/ — list user's orgs."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "orgs"

    @extend_schema(
        responses=OrgSerializer(many=True),
        parameters=[
            OpenApiParameter("limit", int, description="Page size (max 100)"),
            OpenApiParameter("offset", int, description="Number of items to skip"),
        ],
        tags=["orgs"],
    )
    def get(self, request: Request) -> Response:
        user = get_user(request)
        orgs = Org.objects.filter(
            id__in=OrgMember.objects.filter(user=user).values("org_id"),
            deleted_at__isnull=True,
            is_active=True,
        ).order_by("name")
        paginator = LimitOffsetPagination()
        paginator.default_limit = 50
        paginator.max_limit = 100
        page = paginator.paginate_queryset(orgs, request)
        return paginator.get_paginated_response(OrgSerializer(page, many=True).data)


class OrgDetailView(APIView):
    """GET/PATCH/DELETE /api/v1/orgs/{org_id}/."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "orgs"

    @extend_schema(responses=OrgSerializer, tags=["orgs"])
    def get(self, request: Request, org_id: UUID) -> Response:
        user = get_user(request)
        org, _ = _get_org_and_member(user.id, org_id)
        return Response(OrgSerializer(org).data)

    @extend_schema(request=UpdateOrgSerializer, responses=OrgSerializer, tags=["orgs"])
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

    # Org deletion is admin-only (Django admin action). No API endpoint.


# ---------------------------------------------------------------------------
# Org Members
# ---------------------------------------------------------------------------


class OrgMemberListView(APIView):
    """GET /api/v1/orgs/{org_id}/members/ — list members."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "orgs"

    @extend_schema(
        responses=OrgMemberSerializer(many=True),
        parameters=[
            OpenApiParameter("limit", int, description="Page size (max 200)"),
            OpenApiParameter("offset", int, description="Number of items to skip"),
        ],
        tags=["orgs"],
    )
    def get(self, request: Request, org_id: UUID) -> Response:
        user = get_user(request)
        org, _ = _get_org_and_member(user.id, org_id)
        queryset = OrgMember.objects.filter(org=org).select_related("user").order_by("joined_at")
        paginator = LimitOffsetPagination()
        paginator.default_limit = 50
        paginator.max_limit = 200
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response(OrgMemberSerializer(page, many=True).data)


class OrgMemberDetailView(APIView):
    """PATCH/DELETE /api/v1/orgs/{org_id}/members/{user_id}/."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "orgs"

    @extend_schema(request=UpdateMemberSerializer, responses=OrgMemberSerializer, tags=["orgs"])
    def patch(self, request: Request, org_id: UUID, member_user_id: UUID) -> Response:
        user = get_user(request)
        _, caller = _get_org_and_member(user.id, org_id, allowed_roles=_ADMIN_OR_ABOVE)
        target = get_object_or_404(
            OrgMember, org_id=org_id, user_id=member_user_id, org__deleted_at__isnull=True
        )

        check_can_manage_member(
            caller_role=CoreOrgRole(caller.role),
            target_role=CoreOrgRole(target.role),
        )

        ser = UpdateMemberSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if not ser.validated_data:
            return Response(OrgMemberSerializer(target).data)

        new_role = ser.validated_data.get("role")
        if new_role is not None:
            from saasmint_core.services.orgs import check_can_assign_role

            check_can_assign_role(
                caller_role=CoreOrgRole(caller.role),
                new_role=CoreOrgRole(new_role),
            )

        for field, value in ser.validated_data.items():
            setattr(target, field, value)
        target.save(update_fields=list(ser.validated_data.keys()))
        return Response(OrgMemberSerializer(target).data)

    @extend_schema(request=None, responses={204: None}, tags=["orgs"])
    def delete(self, request: Request, org_id: UUID, member_user_id: UUID) -> Response:
        """Remove a member — decrements Stripe seats and hard-deletes their account."""
        user = get_user(request)
        _, caller = _get_org_and_member(user.id, org_id, allowed_roles=_ADMIN_OR_ABOVE)
        target = get_object_or_404(
            OrgMember.objects.select_related("user"),
            org_id=org_id,
            user_id=member_user_id,
            org__deleted_at__isnull=True,
        )

        # Cannot remove the owner
        if target.role == OrgRole.OWNER:
            raise InsufficientPermissionError("Owner cannot be removed. Transfer ownership first.")

        # Cannot remove members at or above your own role level
        check_can_manage_member(
            caller_role=CoreOrgRole(caller.role),
            target_role=CoreOrgRole(target.role),
        )

        target_user = target.user
        target.delete()

        # Decrement seats on the Stripe subscription
        _decrement_subscription_seats(org_id)

        # Hard-delete the removed user's account (CASCADE handles related models)
        target_user.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


def _decrement_subscription_seats(org_id: UUID) -> None:
    """Decrement the team subscription's seat count to match member count."""
    from apps.orgs.services import decrement_subscription_seats

    decrement_subscription_seats(org_id)


# ---------------------------------------------------------------------------
# Transfer Ownership
# ---------------------------------------------------------------------------


class OrgTransferOwnershipView(APIView):
    """POST /api/v1/orgs/{org_id}/transfer-ownership/ — transfer owner role."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "orgs"

    @extend_schema(
        request=TransferOwnershipSerializer,
        responses={200: OrgMemberSerializer},
        tags=["orgs"],
    )
    def post(self, request: Request, org_id: UUID) -> Response:
        user = get_user(request)
        _, caller = _get_org_and_member(user.id, org_id, allowed_roles=_OWNER_ONLY)

        ser = TransferOwnershipSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        target_user_id = ser.validated_data["user_id"]

        target = get_object_or_404(
            OrgMember, org_id=org_id, user_id=target_user_id, org__deleted_at__isnull=True
        )

        if target.role != OrgRole.ADMIN:
            raise InsufficientPermissionError("Ownership can only be transferred to an admin.")

        with transaction.atomic():
            # New owner gets owner role + billing
            target.role = OrgRole.OWNER
            target.is_billing = True
            target.save(update_fields=["role", "is_billing"])

            # Former owner becomes admin, loses billing
            caller.role = OrgRole.ADMIN
            caller.is_billing = False
            caller.save(update_fields=["role", "is_billing"])

        return Response(OrgMemberSerializer(target).data)


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


class InvitationListCreateView(APIView):
    """GET/POST /api/v1/orgs/{org_id}/invitations/ — list or create invitations."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "orgs"

    @extend_schema(
        responses=InvitationSerializer(many=True),
        tags=["orgs"],
    )
    def get(self, request: Request, org_id: UUID) -> Response:
        user = get_user(request)
        _get_org_and_member(user.id, org_id, allowed_roles=_ADMIN_OR_ABOVE)
        invitations = (
            Invitation.objects.filter(org_id=org_id, status=InvitationStatus.PENDING)
            .select_related("invited_by")
            .order_by("-created_at")
        )
        return Response(InvitationSerializer(invitations, many=True).data)

    @extend_schema(
        request=CreateInvitationSerializer,
        responses={201: InvitationSerializer},
        tags=["orgs"],
    )
    def post(self, request: Request, org_id: UUID) -> Response:
        from apps.orgs.tasks import send_invitation_email_task

        user = get_user(request)
        org, _ = _get_org_and_member(user.id, org_id, allowed_roles=_ADMIN_OR_ABOVE)

        ser = CreateInvitationSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        email = ser.validated_data["email"]
        role = ser.validated_data["role"]

        # Cannot invite users who already have an account
        if User.objects.filter(
            email=email,
        ).exists():
            from rest_framework.exceptions import ValidationError

            raise ValidationError(
                {"email": ["This email is already registered. Only new users can be invited."]}
            )

        # Cannot invite someone with a pending invitation
        if Invitation.objects.filter(
            org=org, email=email, status=InvitationStatus.PENDING
        ).exists():
            from rest_framework.exceptions import ValidationError

            raise ValidationError(
                {"email": ["A pending invitation already exists for this email."]}
            )

        # Check seat limit against subscription quantity
        _validate_seat_limit(org)

        token = secrets.token_urlsafe(32)
        invitation = Invitation.objects.create(
            org=org,
            email=email,
            role=role,
            token=token,
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=INVITATION_EXPIRY_DAYS),
        )

        # Send invitation email asynchronously
        send_invitation_email_task.delay(
            email=email,
            token=token,
            org_name=org.name,
            inviter_name=user.full_name,
        )

        return Response(
            InvitationSerializer(invitation).data,
            status=status.HTTP_201_CREATED,
        )


def _validate_seat_limit(org: Org) -> None:
    """Raise ValidationError if the org has reached its subscription seat limit."""
    from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES, StripeCustomer
    from apps.billing.models import Subscription as SubscriptionModel

    try:
        customer = StripeCustomer.objects.get(org=org)
    except StripeCustomer.DoesNotExist:
        return  # No subscription — can't validate seats

    try:
        sub = SubscriptionModel.objects.get(
            stripe_customer=customer,
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
        )
    except SubscriptionModel.DoesNotExist:
        return

    current_members = OrgMember.objects.filter(org=org).count()
    pending_invitations = Invitation.objects.filter(
        org=org, status=InvitationStatus.PENDING
    ).count()

    if current_members + pending_invitations >= sub.quantity:
        from rest_framework.exceptions import ValidationError

        raise ValidationError(
            {"detail": "Org has reached its seat limit. Upgrade your plan to invite more members."}
        )


class InvitationCancelView(APIView):
    """DELETE /api/v1/orgs/{org_id}/invitations/{invitation_id}/ — cancel invitation."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "orgs"

    @extend_schema(request=None, responses={204: None}, tags=["orgs"])
    def delete(self, request: Request, org_id: UUID, invitation_id: UUID) -> Response:
        user = get_user(request)
        _get_org_and_member(user.id, org_id, allowed_roles=_ADMIN_OR_ABOVE)

        invitation = get_object_or_404(
            Invitation, id=invitation_id, org_id=org_id, status=InvitationStatus.PENDING
        )
        invitation.status = InvitationStatus.CANCELLED
        invitation.save(update_fields=["status"])
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Invitation Accept / Decline (token-based, outside org context)
# ---------------------------------------------------------------------------


class InvitationDetailView(APIView):
    """GET /api/v1/invitations/{token}/ — fetch invitation details by token.

    Unauthenticated endpoint. Returns invitation info including the
    organization name so the accept/decline page can display it.
    """

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "orgs"

    @extend_schema(responses={200: InvitationSerializer}, tags=["orgs"])
    def get(self, request: Request, token: str) -> Response:
        invitation = get_object_or_404(
            Invitation.objects.select_related("org", "invited_by"),
            token=token,
            status=InvitationStatus.PENDING,
        )
        return Response(InvitationSerializer(invitation).data)


class InvitationAcceptView(APIView):
    """POST /api/v1/invitations/{token}/accept/ — register and join an org.

    Unauthenticated endpoint. The invitee provides registration data
    (full_name, password) and is created as an org_member user.
    """

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "auth"

    @extend_schema(
        request=InvitationAcceptSerializer,
        responses={201: OrgSerializer},
        tags=["orgs"],
    )
    def post(self, request: Request, token: str) -> Response:
        from apps.users.authentication import create_access_token, create_refresh_token

        invitation = get_object_or_404(Invitation, token=token, status=InvitationStatus.PENDING)

        # Check expiry
        if invitation.expires_at < timezone.now():
            invitation.status = InvitationStatus.EXPIRED
            invitation.save(update_fields=["status"])
            from rest_framework.exceptions import ValidationError

            raise ValidationError({"detail": "This invitation has expired."})

        org = invitation.org
        if org.deleted_at is not None:
            from rest_framework.exceptions import ValidationError

            raise ValidationError({"detail": "This organization no longer exists."})

        # Email must not already be registered
        if User.objects.filter(
            email=invitation.email,
        ).exists():
            from rest_framework.exceptions import ValidationError

            raise ValidationError({"detail": "This email is already registered."})

        ser = InvitationAcceptSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        # Create user + membership in a single transaction
        with transaction.atomic():
            user = User.objects.create_user(
                email=invitation.email,
                password=ser.validated_data["password"],
                full_name=ser.validated_data["full_name"],
                account_type=AccountType.ORG_MEMBER,
                is_verified=True,  # trusted: invited by existing member
            )
            OrgMember.objects.create(
                org=org,
                user=user,
                role=invitation.role,
            )
            invitation.status = InvitationStatus.ACCEPTED
            invitation.save(update_fields=["status"])

        refresh = create_refresh_token(user)
        access = create_access_token(user)
        return Response(
            {
                "org": OrgSerializer(org).data,
                "access_token": access,
                "refresh_token": refresh,
                "token_type": "Bearer",
            },
            status=status.HTTP_201_CREATED,
        )


class InvitationDeclineView(APIView):
    """POST /api/v1/invitations/{token}/decline/ — decline an invitation."""

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]
    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "orgs"

    @extend_schema(request=None, responses={204: None}, tags=["orgs"])
    def post(self, request: Request, token: str) -> Response:
        invitation = get_object_or_404(Invitation, token=token, status=InvitationStatus.PENDING)
        invitation.status = InvitationStatus.CANCELLED
        invitation.save(update_fields=["status"])
        return Response(status=status.HTTP_204_NO_CONTENT)
