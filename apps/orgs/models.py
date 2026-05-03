"""Django ORM models for organizations, memberships, and invitations."""

from __future__ import annotations

import uuid

from django.db import models


class OrgRole(models.TextChoices):
    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    MEMBER = "member", "Member"


class InvitationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    EXPIRED = "expired", "Expired"
    CANCELLED = "cancelled", "Cancelled"
    DECLINED = "declined", "Declined"


class Org(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    logo_url = models.TextField(null=True, blank=True)  # noqa: DJ001  # nullable TextField intentional: NULL means no logo set (distinguishable from empty string)
    created_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_orgs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "orgs"
        constraints = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            models.UniqueConstraint(
                fields=["slug"],
                name="idx_orgs_slug_active",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class OrgMember(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    org = models.ForeignKey(Org, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="org_memberships")
    role = models.CharField(max_length=20, choices=OrgRole.choices, default=OrgRole.MEMBER)
    is_billing = models.BooleanField(default=False)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "org_members"
        constraints = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            models.UniqueConstraint(fields=["org", "user"], name="org_members_org_user_uniq"),
            # Rule 8: one owned org per user. Partial unique index on the
            # owner-row guarantees DB-level enforcement so two parallel team
            # checkouts can't both win the rule-8 view-layer guard
            # (CheckoutSessionView.post → already_owns_org). The view's
            # ``.exists()`` check stays as a fast-path UX guard; the DB
            # constraint is the authoritative enforcer for the race.
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(role="owner"),
                name="uniq_org_owner_per_user",
            ),
        ]
        indexes = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            # Hot path: `SubscriptionView` checks whether an org-member user has
            # the billing flag on every `GET /billing/subscriptions/me/`. The
            # unique (org, user) index doesn't help since `user` isn't the prefix.
            models.Index(
                fields=["user"],
                name="idx_orgmember_billing_user",
                condition=models.Q(is_billing=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user} @ {self.org} ({self.role})"


class Invitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    org = models.ForeignKey(Org, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    role = models.CharField(
        max_length=20,
        choices=[
            (OrgRole.ADMIN, "Admin"),
            (OrgRole.MEMBER, "Member"),
        ],
        default=OrgRole.MEMBER,
    )
    token = models.CharField(max_length=255, unique=True)
    status = models.CharField(
        max_length=20, choices=InvitationStatus.choices, default=InvitationStatus.PENDING
    )
    invited_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_invitations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "invitations"
        constraints = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            models.UniqueConstraint(
                fields=["org", "email"],
                condition=models.Q(status="pending"),
                name="idx_invitations_org_email_pending",
            ),
        ]
        indexes = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            # Hot paths: listing pending invites for an org, seat-limit counts,
            # and bulk-cancel on org delete. Partial on status='pending' so the
            # tree stays small once invites age into accepted/expired/declined.
            models.Index(
                fields=["org", "-created_at"],
                name="idx_invitation_pending_org",
                condition=models.Q(status="pending"),
            ),
        ]

    def __str__(self) -> str:
        return f"Invitation to {self.email} for {self.org} ({self.status})"
