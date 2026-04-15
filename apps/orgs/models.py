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
    # `deleted_at` is the canonical soft-delete marker (IS NULL = live).
    # `is_active` is an orthogonal flag used to pause/disable an org (e.g. after
    # the team subscription is cancelled) without deleting it, so both columns
    # are intentional and should be filtered together: deleted_at__isnull=True, is_active=True.
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "orgs"
        constraints = [  # noqa: RUF012  # mutable default in Meta inner class; ClassVar not applicable here
            models.UniqueConstraint(
                fields=["slug"],
                condition=models.Q(deleted_at__isnull=True),
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

    def __str__(self) -> str:
        return f"Invitation to {self.email} for {self.org} ({self.status})"
