"""Organisation, membership, and invitation API views."""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import ClassVar
from uuid import UUID

from asgiref.sync import async_to_sync
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.pagination import LimitOffsetPagination
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
    InvitationSerializer,
    OrgMemberSerializer,
    OrgSerializer,
    TransferOwnershipSerializer,
    UpdateMemberSerializer,
    UpdateOrgSerializer,
)
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
            org_id=org_id, org__deleted_at__isnull=True, user_id=user_id
        )
    except OrgMember.DoesNotExist:
        if not Org.objects.filter(id=org_id, deleted_at__isnull=True).exists():
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

    @extend_schema(request=None, responses={204: None}, tags=["orgs"])
    def delete(self, request: Request, org_id: UUID) -> Response:
        """Delete org — cascades: cancels subscription, reverts members, clears invitations."""
        from apps.orgs.services import cancel_pending_invitations_for_org, revert_to_personal

        user = get_user(request)
        org, _ = _get_org_and_member(user.id, org_id, allowed_roles=_OWNER_ONLY)

        members = list(OrgMember.objects.filter(org=org).select_related("user"))

        # Cancel team subscription via Stripe (immediate)
        _cancel_team_subscription(org)

        # Revert all members to personal + free plan
        for member in members:
            async_to_sync(revert_to_personal)(member.user)

        # Delete all memberships
        OrgMember.objects.filter(org=org).delete()

        # Cancel pending invitations
        async_to_sync(cancel_pending_invitations_for_org)(org.id)

        # Soft-delete the org
        org.deleted_at = timezone.now()
        org.save(update_fields=["deleted_at"])

        return Response(status=status.HTTP_204_NO_CONTENT)


def _cancel_team_subscription(org: Org) -> None:
    """Cancel the team subscription for an org via Stripe (immediate cancellation)."""
    import stripe as stripe_lib

    from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES, StripeCustomer
    from apps.billing.models import Subscription as SubscriptionModel

    try:
        customer = StripeCustomer.objects.get(org=org)
    except StripeCustomer.DoesNotExist:
        return

    subs = SubscriptionModel.objects.filter(
        stripe_customer=customer,
        status__in=ACTIVE_SUBSCRIPTION_STATUSES,
        stripe_id__isnull=False,
    )
    for sub in subs:
        if sub.stripe_id is None:
            continue
        try:
            stripe_lib.Subscription.cancel(sub.stripe_id)
        except stripe_lib.StripeError:
            logger.exception(
                "Failed to cancel Stripe sub %s for org %s",
                sub.stripe_id,
                org.id,
            )


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
        """Remove a member — reverts their accountType and decrements Stripe seats."""
        from apps.orgs.services import revert_to_personal

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

        target.delete()

        # Revert removed user to personal + free plan
        async_to_sync(revert_to_personal)(target.user)

        # Decrement seats on the Stripe subscription
        _decrement_subscription_seats(org_id)

        return Response(status=status.HTTP_204_NO_CONTENT)


def _decrement_subscription_seats(org_id: UUID) -> None:
    """Decrement the team subscription's seat count to match member count."""
    from saasmint_core.services.subscriptions import update_seat_count

    from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES, StripeCustomer
    from apps.billing.models import Subscription as SubscriptionModel

    try:
        customer = StripeCustomer.objects.get(org_id=org_id)
    except StripeCustomer.DoesNotExist:
        return

    try:
        sub = SubscriptionModel.objects.get(
            stripe_customer=customer,
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
            stripe_id__isnull=False,
        )
    except SubscriptionModel.DoesNotExist:
        return

    if sub.stripe_id is None:
        return

    new_quantity = OrgMember.objects.filter(org_id=org_id, org__deleted_at__isnull=True).count()

    if new_quantity < 1:
        return

    try:
        async_to_sync(update_seat_count)(
            stripe_subscription_id=sub.stripe_id,
            quantity=new_quantity,
        )
    except Exception:
        logger.exception(
            "Failed to update seat count to %d for sub %s",
            new_quantity,
            sub.stripe_id,
        )


# ---------------------------------------------------------------------------
# Leave Org
# ---------------------------------------------------------------------------


class OrgLeaveView(APIView):
    """POST /api/v1/orgs/{org_id}/leave/ — member leaves voluntarily."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "orgs"

    @extend_schema(request=None, responses={204: None}, tags=["orgs"])
    def post(self, request: Request, org_id: UUID) -> Response:
        from apps.orgs.services import revert_to_personal

        user = get_user(request)
        _, member = _get_org_and_member(user.id, org_id)

        if member.role == OrgRole.OWNER:
            raise InsufficientPermissionError(
                "Owner cannot leave. Transfer ownership or delete the org first."
            )

        member.delete()
        async_to_sync(revert_to_personal)(user)
        _decrement_subscription_seats(org_id)

        return Response(status=status.HTTP_204_NO_CONTENT)


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

        # Cannot invite existing members
        from apps.users.models import User

        existing_user = User.objects.filter(email=email, deleted_at__isnull=True).first()
        if existing_user and OrgMember.objects.filter(org=org, user=existing_user).exists():
            from rest_framework.exceptions import ValidationError

            raise ValidationError({"email": ["This user is already a member of this org."]})

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


class InvitationAcceptView(APIView):
    """POST /api/v1/invitations/{token}/accept/ — accept an invitation."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "orgs"

    @extend_schema(request=None, responses={200: OrgSerializer}, tags=["orgs"])
    def post(self, request: Request, token: str) -> Response:
        from apps.orgs.services import _cancel_personal_subscription, revert_to_personal

        user = get_user(request)

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

            raise ValidationError({"detail": "This organisation no longer exists."})

        # Remove user from any existing org (safety for one-org rule)
        existing_membership = OrgMember.objects.filter(user=user).select_related("user").first()
        if existing_membership:
            existing_membership.delete()
            async_to_sync(revert_to_personal)(user)
            user.refresh_from_db()

        # Cancel personal paid subscription with prorated refund
        async_to_sync(_cancel_personal_subscription)(user.id)

        # Create membership
        with transaction.atomic():
            OrgMember.objects.create(
                org=org,
                user=user,
                role=invitation.role,
            )
            user.account_type = "org_member"
            user.save(update_fields=["account_type"])
            invitation.status = InvitationStatus.ACCEPTED
            invitation.save(update_fields=["status"])

        return Response(OrgSerializer(org).data)


class InvitationDeclineView(APIView):
    """POST /api/v1/invitations/{token}/decline/ — decline an invitation."""

    throttle_classes: ClassVar[list[type[ScopedRateThrottle]]] = [ScopedRateThrottle]  # type: ignore[misc]  # drf-stubs types throttle_classes as list[type[BaseThrottle]]; narrowing to ScopedRateThrottle triggers misc
    throttle_scope = "orgs"

    @extend_schema(request=None, responses={204: None}, tags=["orgs"])
    def post(self, request: Request, token: str) -> Response:
        get_user(request)  # auth check

        invitation = get_object_or_404(Invitation, token=token, status=InvitationStatus.PENDING)
        invitation.status = InvitationStatus.CANCELLED
        invitation.save(update_fields=["status"])
        return Response(status=status.HTTP_204_NO_CONTENT)
