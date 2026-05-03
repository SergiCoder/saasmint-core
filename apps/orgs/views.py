"""Organization, membership, and invitation API views."""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Any, ClassVar
from uuid import UUID

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers as drf_serializers
from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from saasmint_core.domain.org import OrgRole as CoreOrgRole
from saasmint_core.exceptions import InsufficientPermissionError, OrgNotFoundError
from saasmint_core.services.orgs import check_can_manage_member

from apps.base_views import OrgsScopedView
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
from apps.users.services import email_is_registered
from helpers import get_user

logger = logging.getLogger(__name__)

_ADMIN_OR_ABOVE = (OrgRole.OWNER, OrgRole.ADMIN)
_OWNER_ONLY = (OrgRole.OWNER,)


class _Gone(APIException):
    status_code = status.HTTP_410_GONE
    default_detail = "Resource is no longer available."
    default_code = "gone"


class _Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Conflict."
    default_code = "conflict"


class _InvitationExpired(_Gone):
    default_detail = "This invitation has expired."
    default_code = "invitation_expired"


class _InvitationEmailExists(_Conflict):
    default_detail = "This email is already registered."
    default_code = "email_exists"


class _InvitationPendingExists(_Conflict):
    default_detail = "A pending invitation already exists for this email."
    default_code = "invitation_pending"


class _SeatLimitReached(_Conflict):
    default_detail = "Org has reached its seat limit. Upgrade your plan to invite more members."
    default_code = "seat_limit_reached"


class _InvitationAddressedToOther(PermissionDenied):
    default_detail = "This invitation is addressed to another account."
    default_code = "forbidden"


INVITATION_EXPIRY_DAYS = 7

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 100


def _default_paginator() -> LimitOffsetPagination:
    """Return a paginator with the app's standard defaults."""
    paginator = LimitOffsetPagination()
    paginator.default_limit = _DEFAULT_PAGE_SIZE
    paginator.max_limit = _MAX_PAGE_SIZE
    return paginator


def _paginated_response_schema(
    name: str, child: drf_serializers.BaseSerializer[Any]
) -> drf_serializers.Serializer[object]:
    """Build an inline serializer for the DRF paginated envelope.

    Document the real wire shape of ``LimitOffsetPagination`` responses —
    a bare ``child(many=True)`` hides ``count``/``next``/``previous``.
    """
    return inline_serializer(
        name,
        {
            "count": drf_serializers.IntegerField(),
            "next": drf_serializers.URLField(allow_null=True),
            "previous": drf_serializers.URLField(allow_null=True),
            "results": child,
        },
    )


def _get_org_and_member(
    user_id: UUID,
    org_id: UUID,
    allowed_roles: tuple[OrgRole, ...] | None = None,
) -> tuple[Org, OrgMember]:
    """Fetch an org and verify the user's membership in a single query.

    Raises OrgNotFoundError or InsufficientPermissionError as appropriate.
    """
    try:
        member = OrgMember.objects.select_related("org").get(org_id=org_id, user_id=user_id)
    except OrgMember.DoesNotExist:
        if not Org.objects.filter(id=org_id).exists():
            raise OrgNotFoundError(org_id) from None
        raise InsufficientPermissionError("Access denied.") from None
    if allowed_roles is not None and OrgRole(member.role) not in allowed_roles:
        raise InsufficientPermissionError("Insufficient permissions for this action.")
    return member.org, member


# ---------------------------------------------------------------------------
# Org List / Detail
# ---------------------------------------------------------------------------


class OrgListView(OrgsScopedView):
    """GET /api/v1/orgs/ — list user's orgs."""

    @extend_schema(
        responses=_paginated_response_schema("OrgListResponse", OrgSerializer(many=True)),
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
        ).order_by("name")
        paginator = _default_paginator()
        page = paginator.paginate_queryset(orgs, request)
        return paginator.get_paginated_response(OrgSerializer(page, many=True).data)


class OrgDetailView(OrgsScopedView):
    """GET/PATCH /api/v1/orgs/{org_id}/."""

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

    @extend_schema(
        request=None,
        responses={
            204: OpenApiResponse(description="Org deleted."),
            403: OpenApiResponse(description="Caller is not the org owner."),
            404: OpenApiResponse(description="Org does not exist or is inaccessible."),
        },
        description=(
            "Delete the organization (owner only). Cascades to memberships,"
            " invitations, and single-org member user accounts, and immediately"
            " cancels the Stripe subscription (no refund). If the caller's only"
            " org is this one, their own user account is deleted too — any"
            " existing auth tokens will fail on their next request."
        ),
        tags=["orgs"],
    )
    def delete(self, request: Request, org_id: UUID) -> Response:
        from apps.orgs.services import delete_org

        user = get_user(request)
        org, _ = _get_org_and_member(user.id, org_id, allowed_roles=_OWNER_ONLY)
        delete_org(org)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Org Members
# ---------------------------------------------------------------------------


class OrgMemberListView(OrgsScopedView):
    """GET /api/v1/orgs/{org_id}/members/ — list members."""

    @extend_schema(
        responses=_paginated_response_schema(
            "OrgMemberListResponse", OrgMemberSerializer(many=True)
        ),
        parameters=[
            OpenApiParameter("limit", int, description="Page size (max 100)"),
            OpenApiParameter("offset", int, description="Number of items to skip"),
        ],
        tags=["orgs"],
    )
    def get(self, request: Request, org_id: UUID) -> Response:
        user = get_user(request)
        org, _ = _get_org_and_member(user.id, org_id)
        queryset = OrgMember.objects.filter(org=org).select_related("user").order_by("joined_at")
        paginator = _default_paginator()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response(OrgMemberSerializer(page, many=True).data)


class OrgMemberDetailView(OrgsScopedView):
    """PATCH/DELETE /api/v1/orgs/{org_id}/members/{user_id}/."""

    @extend_schema(request=UpdateMemberSerializer, responses=OrgMemberSerializer, tags=["orgs"])
    def patch(self, request: Request, org_id: UUID, member_user_id: UUID) -> Response:
        user = get_user(request)
        _, caller = _get_org_and_member(user.id, org_id, allowed_roles=_ADMIN_OR_ABOVE)
        target = get_object_or_404(OrgMember, org_id=org_id, user_id=member_user_id)

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

    @extend_schema(
        request=None,
        responses={204: None},
        description=(
            "Remove a member from the org. **Destructive:** this hard-deletes the"
            " member's user account in addition to their membership row, and"
            " decrements the org's Stripe seat count. The target cannot be the"
            " org owner — transfer ownership first."
        ),
        tags=["orgs"],
    )
    def delete(self, request: Request, org_id: UUID, member_user_id: UUID) -> Response:
        """Remove a member — decrements Stripe seats and hard-deletes their account."""
        user = get_user(request)
        _, caller = _get_org_and_member(user.id, org_id, allowed_roles=_ADMIN_OR_ABOVE)
        target = get_object_or_404(
            OrgMember.objects.select_related("user"),
            org_id=org_id,
            user_id=member_user_id,
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
        with transaction.atomic():
            target.delete()
            target_user.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Transfer Ownership
# ---------------------------------------------------------------------------


class OrgOwnerView(OrgsScopedView):
    """PUT /api/v1/orgs/{org_id}/owner/ — transfer ownership to another admin."""

    @extend_schema(
        request=TransferOwnershipSerializer,
        responses={200: OrgMemberSerializer},
        tags=["orgs"],
    )
    def put(self, request: Request, org_id: UUID) -> Response:
        user = get_user(request)
        _, caller = _get_org_and_member(user.id, org_id, allowed_roles=_OWNER_ONLY)

        ser = TransferOwnershipSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        target_user_id = ser.validated_data["user_id"]

        target = get_object_or_404(OrgMember, org_id=org_id, user_id=target_user_id)

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

        location = request.build_absolute_uri(
            reverse(
                "org-member-detail",
                kwargs={"org_id": org_id, "member_user_id": target.user_id},
            )
        )
        return Response(OrgMemberSerializer(target).data, headers={"Location": location})


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


class InvitationListCreateView(OrgsScopedView):
    """GET/POST /api/v1/orgs/{org_id}/invitations/ — list or create invitations."""

    @extend_schema(
        responses=_paginated_response_schema(
            "InvitationListResponse", InvitationSerializer(many=True)
        ),
        parameters=[
            OpenApiParameter("limit", int, description="Page size (max 100)"),
            OpenApiParameter("offset", int, description="Number of items to skip"),
        ],
        tags=["orgs"],
    )
    def get(self, request: Request, org_id: UUID) -> Response:
        user = get_user(request)
        _get_org_and_member(user.id, org_id, allowed_roles=_ADMIN_OR_ABOVE)
        invitations = (
            Invitation.objects.filter(org_id=org_id, status=InvitationStatus.PENDING)
            .select_related("org", "invited_by")
            .order_by("-created_at")
        )
        paginator = _default_paginator()
        page = paginator.paginate_queryset(invitations, request)
        return paginator.get_paginated_response(InvitationSerializer(page, many=True).data)

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
        if email_is_registered(email):
            raise _InvitationEmailExists

        # Cannot invite someone with a pending invitation
        if Invitation.objects.filter(
            org=org, email=email, status=InvitationStatus.PENDING
        ).exists():
            raise _InvitationPendingExists

        token = secrets.token_urlsafe(32)
        # The caller owns the atomic block so the seat-limit check and the
        # Invitation INSERT run under the same transaction — otherwise the
        # row-lock released between check and insert lets two concurrent
        # invites both pass the check and overrun the seat quota.
        with transaction.atomic():
            _validate_seat_limit(org)
            invitation = Invitation.objects.create(
                org=org,
                email=email,
                role=role,
                token=token,
                invited_by=user,
                expires_at=timezone.now() + timedelta(days=INVITATION_EXPIRY_DAYS),
            )
            # Defer email dispatch until commit so the worker can't race ahead
            # of the DB write and handle a missing invitation row.
            transaction.on_commit(
                lambda: send_invitation_email_task.delay(
                    email=email,
                    token=token,
                    org_name=org.name,
                    inviter_name=user.full_name,
                )
            )

        location = request.build_absolute_uri(
            reverse("orgs-invitations:invitation-detail", kwargs={"token": token})
        )
        return Response(
            InvitationSerializer(invitation).data,
            status=status.HTTP_201_CREATED,
            headers={"Location": location},
        )


def _validate_seat_limit(org: Org) -> None:
    """Raise ``_SeatLimitReached`` if the org has reached its subscription seat limit.

    Must be called inside an ``atomic()`` block owned by the caller so the
    row lock taken here stays held until the caller's Invitation INSERT
    commits — otherwise two concurrent invites can both pass the check and
    overrun the quota.
    """
    from apps.billing.models import ACTIVE_SUBSCRIPTION_STATUSES
    from apps.billing.models import Subscription as SubscriptionModel

    # Lock the active team sub row. We can't use the shared read-only helper
    # here because this one must take a row lock.
    sub = (
        SubscriptionModel.objects.select_for_update()
        .select_related("stripe_customer")
        .filter(
            stripe_customer__org=org,
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
        )
        .first()
    )
    if sub is None:
        return  # No active subscription — can't validate seats

    current_members = OrgMember.objects.filter(org=org).count()
    pending_invitations = Invitation.objects.filter(
        org=org, status=InvitationStatus.PENDING
    ).count()

    if current_members + pending_invitations >= sub.seat_limit:
        raise _SeatLimitReached


class InvitationCancelView(OrgsScopedView):
    """DELETE /api/v1/orgs/{org_id}/invitations/{invitation_id}/ — cancel invitation."""

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


class InvitationDetailView(OrgsScopedView):
    """GET /api/v1/invitations/{token}/ — fetch invitation details by token.

    Unauthenticated endpoint. Returns invitation info including the
    organization name so the accept/decline page can display it.
    """

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]
    throttle_scope = "auth"

    @extend_schema(responses={200: InvitationSerializer}, tags=["orgs"])
    def get(self, request: Request, token: str) -> Response:
        invitation = get_object_or_404(
            Invitation.objects.select_related("org", "invited_by"),
            token=token,
            status=InvitationStatus.PENDING,
        )
        return Response(InvitationSerializer(invitation).data)


class InvitationAcceptView(OrgsScopedView):
    """POST /api/v1/invitations/{token}/accept/ — register and join an org.

    Unauthenticated endpoint. The invitee provides registration data
    (full_name, password) and is created as an org_member user.

    No session tokens are issued here — the invite token alone does not prove
    mailbox control, so a leaked/forwarded link would otherwise onboard an
    attacker with live credentials. Instead the account is created unverified
    and a verification email is sent; the invitee must click the link to
    activate and sign in (``POST /api/v1/auth/verify-email/`` returns tokens).
    """

    permission_classes: ClassVar[list[type[AllowAny]]] = [AllowAny]  # type: ignore[misc]
    throttle_scope = "auth"

    @extend_schema(
        request=InvitationAcceptSerializer,
        responses={
            201: inline_serializer(
                "InvitationAcceptResponse",
                {
                    "org": OrgSerializer(),
                    "detail": drf_serializers.CharField(),
                    "code": drf_serializers.CharField(),
                },
            )
        },
        tags=["orgs"],
    )
    def post(self, request: Request, token: str) -> Response:
        invitation = get_object_or_404(Invitation, token=token, status=InvitationStatus.PENDING)

        # Check expiry
        if invitation.expires_at < timezone.now():
            invitation.status = InvitationStatus.EXPIRED
            invitation.save(update_fields=["status"])
            raise _InvitationExpired

        org = invitation.org

        # Email must not already be registered
        if email_is_registered(invitation.email):
            raise _InvitationEmailExists

        ser = InvitationAcceptSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        from apps.orgs.services import accept_invitation

        _user, org = accept_invitation(
            invitation,
            password=ser.validated_data["password"],
            full_name=ser.validated_data["full_name"],
        )

        return Response(
            {
                "org": OrgSerializer(org).data,
                "detail": "Account created. Check your email to verify and sign in.",
                "code": "verification_email_sent",
            },
            status=status.HTTP_201_CREATED,
        )


class InvitationDeclineView(OrgsScopedView):
    """POST /api/v1/invitations/{token}/decline/ — decline an invitation."""

    permission_classes: ClassVar[list[type[IsAuthenticated]]] = [IsAuthenticated]  # type: ignore[misc]
    throttle_scope = "auth"

    @extend_schema(request=None, responses={204: None}, tags=["orgs"])
    def post(self, request: Request, token: str) -> Response:
        invitation = get_object_or_404(Invitation, token=token, status=InvitationStatus.PENDING)
        # Require the authenticated user's email to match the invitee's. Prevents
        # a leaked/guessed token from silently cancelling someone else's invite.
        user = get_user(request)
        if user.email.lower() != invitation.email.lower():
            raise _InvitationAddressedToOther
        invitation.status = InvitationStatus.DECLINED
        invitation.save(update_fields=["status"])
        return Response(status=status.HTTP_204_NO_CONTENT)
