"""Tests for AccountView and AccountExportView."""

from __future__ import annotations

import io
import tempfile
from unittest.mock import patch

import pytest
from django.core.files.storage import default_storage
from django.test import override_settings
from PIL import Image
from rest_framework.test import APIClient

from apps.users.models import User

# Relax throttling in tests — keep scoped rates so ScopedRateThrottle can resolve them
_TEST_DRF = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {
        "account": "1000/hour",
        "account_export": "1000/hour",
    },
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}


@pytest.fixture
def user(db):
    return User.objects.create_user(
        email="view@example.com",
        full_name="View User",
    )


@pytest.fixture
def authed_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture(autouse=True)
def _disable_throttle(settings):
    settings.REST_FRAMEWORK = _TEST_DRF


@pytest.mark.django_db
class TestAccountViewGET:
    def test_returns_current_user(self, authed_client, user):
        resp = authed_client.get("/api/v1/account/")
        assert resp.status_code == 200
        assert resp.data["email"] == user.email
        assert resp.data["full_name"] == "View User"

    def test_unauthenticated_returns_403(self):
        client = APIClient()
        resp = client.get("/api/v1/account/")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestAccountViewPATCH:
    def test_update_full_name(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"full_name": "Updated Name"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["full_name"] == "Updated Name"
        user.refresh_from_db()
        assert user.full_name == "Updated Name"

    def test_update_locale(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"preferred_locale": "es"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["preferred_locale"] == "es"

    def test_invalid_locale_returns_400(self, authed_client):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"preferred_locale": "invalid"},
            format="json",
        )
        assert resp.status_code == 400

    def test_invalid_currency_returns_400(self, authed_client):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"preferred_currency": "zzz"},
            format="json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestAccountViewPATCHEdgeCases:
    def test_update_multiple_fields_at_once(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"full_name": "Multi Update", "preferred_locale": "en", "preferred_currency": "eur"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["full_name"] == "Multi Update"
        assert resp.data["preferred_locale"] == "en"
        assert resp.data["preferred_currency"] == "eur"
        user.refresh_from_db()
        assert user.preferred_currency == "eur"

    def test_patch_ignores_avatar_url(self, authed_client, user):
        """avatar_url is read-only on PATCH; use AvatarView (POST/DELETE) instead.

        Prevents stored-XSS via `javascript:`/`data:` URLs or phishing links.
        """
        original_url = user.avatar_url
        resp = authed_client.patch(
            "/api/v1/account/",
            {"avatar_url": "javascript:alert(1)"},
            format="json",
        )
        assert resp.status_code == 200
        user.refresh_from_db()
        assert user.avatar_url == original_url
        assert resp.data["avatar_url"] == original_url

    def test_update_empty_body_is_noop(self, authed_client, user):
        original_name = user.full_name
        resp = authed_client.patch(
            "/api/v1/account/",
            {},
            format="json",
        )
        assert resp.status_code == 200
        user.refresh_from_db()
        assert user.full_name == original_name

    def test_update_phone(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"phone": {"prefix": "+34", "number": "612345678"}},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["phone"] == {"prefix": "+34", "number": "612345678"}
        user.refresh_from_db()
        assert user.phone_prefix == "+34"
        assert user.phone == "612345678"

    def test_clear_phone(self, authed_client, user):
        user.phone_prefix = "+1"
        user.phone = "5551234"
        user.save(update_fields=["phone_prefix", "phone"])
        resp = authed_client.patch(
            "/api/v1/account/",
            {"phone": None},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["phone"] is None
        user.refresh_from_db()
        assert user.phone_prefix is None
        assert user.phone is None

    def test_update_timezone(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"timezone": "Europe/Madrid"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["timezone"] == "Europe/Madrid"

    def test_update_job_title(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"job_title": "Engineer"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["job_title"] == "Engineer"

    def test_update_pronouns(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"pronouns": "they/them"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["pronouns"] == "they/them"

    def test_update_bio(self, authed_client, user):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"bio": "Hello world"},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["bio"] == "Hello world"

    def test_update_invalid_phone_prefix_returns_400(self, authed_client):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"phone": {"prefix": "+9999", "number": "123456"}},
            format="json",
        )
        assert resp.status_code == 400

    def test_update_bio_max_length_returns_400(self, authed_client):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"bio": "x" * 501},
            format="json",
        )
        assert resp.status_code == 400

    def test_update_job_title_max_length_returns_400(self, authed_client):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"job_title": "x" * 101},
            format="json",
        )
        assert resp.status_code == 400

    def test_update_pronouns_max_length_returns_400(self, authed_client):
        resp = authed_client.patch(
            "/api/v1/account/",
            {"pronouns": "x" * 51},
            format="json",
        )
        assert resp.status_code == 400

    def test_clear_timezone(self, authed_client, user):
        user.timezone = "Europe/Madrid"
        user.save(update_fields=["timezone"])
        resp = authed_client.patch(
            "/api/v1/account/",
            {"timezone": None},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["timezone"] is None
        user.refresh_from_db()
        assert user.timezone is None

    def test_clear_bio(self, authed_client, user):
        user.bio = "Some bio"
        user.save(update_fields=["bio"])
        resp = authed_client.patch(
            "/api/v1/account/",
            {"bio": None},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["bio"] is None
        user.refresh_from_db()
        assert user.bio is None

    def test_clear_job_title(self, authed_client, user):
        user.job_title = "Engineer"
        user.save(update_fields=["job_title"])
        resp = authed_client.patch(
            "/api/v1/account/",
            {"job_title": None},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["job_title"] is None

    def test_clear_pronouns(self, authed_client, user):
        user.pronouns = "she/her"
        user.save(update_fields=["pronouns"])
        resp = authed_client.patch(
            "/api/v1/account/",
            {"pronouns": None},
            format="json",
        )
        assert resp.status_code == 200
        assert resp.data["pronouns"] is None

    def test_unauthenticated_patch_rejected(self):
        client = APIClient()
        resp = client.patch("/api/v1/account/", {"full_name": "Hacker"}, format="json")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestAccountViewDELETE:
    @patch("saasmint_core.services.gdpr.stripe.Subscription.cancel")
    @patch("saasmint_core.services.gdpr.stripe.Customer.delete")
    def test_delete_hard_deletes_user_and_stripe_customer(
        self, mock_cust_del, mock_sub_cancel, authed_client, user
    ):
        from datetime import UTC, datetime

        from apps.billing.models import (
            Plan,
            PlanContext,
            PlanInterval,
            PlanPrice,
            PlanTier,
            StripeCustomer,
            Subscription,
        )

        cust = StripeCustomer.objects.create(stripe_id="cus_gdpr_del", user=user, livemode=False)
        plan = Plan.objects.create(
            name="Personal Basic",
            context=PlanContext.PERSONAL,
            tier=PlanTier.BASIC,
            interval=PlanInterval.MONTH,
            is_active=True,
        )
        PlanPrice.objects.create(plan=plan, stripe_price_id="price_gdpr_del", amount=999)
        Subscription.objects.create(
            stripe_id="sub_gdpr_del",
            stripe_customer=cust,
            status="active",
            plan=plan,
            seat_limit=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )

        resp = authed_client.delete("/api/v1/account/")

        assert resp.status_code == 204
        assert not User.objects.filter(id=user.id).exists()
        assert not StripeCustomer.objects.filter(id=cust.id).exists()
        mock_cust_del.assert_called_once_with("cus_gdpr_del")
        mock_sub_cancel.assert_called_once_with("sub_gdpr_del", prorate=False)

    @patch("saasmint_core.services.gdpr.stripe.Customer.delete")
    def test_delete_without_stripe_customer_still_hard_deletes_user(
        self, mock_cust_del, authed_client, user
    ):
        resp = authed_client.delete("/api/v1/account/")
        assert resp.status_code == 204
        assert not User.objects.filter(id=user.id).exists()
        mock_cust_del.assert_not_called()

    @patch("saasmint_core.services.gdpr.stripe.Customer.delete")
    def test_delete_non_owner_membership_decrements_seats(self, mock_cust_del, authed_client, user):
        """Deleting a non-owner member cascades through pre_delete_hook:
        the membership is removed and the team sub quantity is decremented."""
        from datetime import UTC, datetime

        from apps.billing.models import (
            Plan,
            PlanContext,
            PlanInterval,
            PlanPrice,
            PlanTier,
            StripeCustomer,
            Subscription,
        )
        from apps.orgs.models import Org, OrgMember, OrgRole

        owner = User.objects.create_user(
            email="teamowner@example.com",
            full_name="Team Owner",
        )
        org = Org.objects.create(name="Delete Test Org", slug="delete-test-org", created_by=owner)
        OrgMember.objects.create(org=org, user=owner, role=OrgRole.OWNER, is_billing=True)
        OrgMember.objects.create(org=org, user=user, role=OrgRole.MEMBER)

        team_cust = StripeCustomer.objects.create(stripe_id="cus_seatdec", org=org, livemode=False)
        team_plan = Plan.objects.create(
            name="Team",
            context=PlanContext.TEAM,
            tier=PlanTier.BASIC,
            interval=PlanInterval.MONTH,
            is_active=True,
        )
        PlanPrice.objects.create(plan=team_plan, stripe_price_id="price_seatdec", amount=1500)
        Subscription.objects.create(
            stripe_id="sub_seatdec",
            stripe_customer=team_cust,
            status="active",
            plan=team_plan,
            seat_limit=2,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )

        resp = authed_client.delete("/api/v1/account/")

        assert resp.status_code == 204
        assert not User.objects.filter(id=user.id).exists()
        # Org and owner survive
        assert User.objects.filter(id=owner.id).exists()
        assert Org.objects.filter(id=org.id).exists()
        # Deleted user's membership is gone — but the seat *limit* on the
        # team sub is intentionally untouched. Reducing it is an explicit
        # action via PATCH /subscriptions/me/.
        assert not OrgMember.objects.filter(user_id=user.id).exists()

    def test_unauthenticated_delete_rejected(self):
        client = APIClient()
        resp = client.delete("/api/v1/account/")
        assert resp.status_code in (401, 403)


@pytest.mark.django_db
class TestAccountExportView:
    def test_export_returns_user_data_without_stripe(self, authed_client, user):
        resp = authed_client.get("/api/v1/account/export/")
        assert resp.status_code == 200
        assert resp.data["user"]["email"] == user.email
        assert resp.data["user"]["id"] == str(user.id)
        assert "stripe_customer" not in resp.data
        assert "subscription" not in resp.data

    def test_export_includes_stripe_customer_and_subscription(self, authed_client, user):
        from datetime import UTC, datetime

        from apps.billing.models import (
            Plan,
            PlanContext,
            PlanInterval,
            PlanPrice,
            PlanTier,
            StripeCustomer,
            Subscription,
        )

        cust = StripeCustomer.objects.create(stripe_id="cus_export", user=user, livemode=False)
        plan = Plan.objects.create(
            name="Personal Basic",
            context=PlanContext.PERSONAL,
            tier=PlanTier.BASIC,
            interval=PlanInterval.MONTH,
            is_active=True,
        )
        PlanPrice.objects.create(plan=plan, stripe_price_id="price_export", amount=999)
        Subscription.objects.create(
            stripe_id="sub_export",
            stripe_customer=cust,
            status="active",
            plan=plan,
            seat_limit=1,
            current_period_start=datetime(2026, 1, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 2, 1, tzinfo=UTC),
        )

        resp = authed_client.get("/api/v1/account/export/")
        assert resp.status_code == 200
        assert resp.data["user"]["email"] == user.email
        assert resp.data["stripe_customer"]["stripe_id"] == "cus_export"
        assert resp.data["subscription"]["stripe_id"] == "sub_export"

    def test_unauthenticated_export_rejected(self):
        client = APIClient()
        resp = client.get("/api/v1/account/export/")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# AvatarView — POST/DELETE /api/v1/account/avatar/
# ---------------------------------------------------------------------------


def _png_upload(size: tuple[int, int] = (200, 200), mode: str = "RGB", color: object = "red"):
    """Build an in-memory PNG :class:`SimpleUploadedFile` for multipart upload."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return SimpleUploadedFile("avatar.png", buf.getvalue(), content_type="image/png")


@pytest.mark.django_db
class TestAvatarView:
    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_avatar_post_returns_201_with_location_header(self, authed_client, user):
        upload = _png_upload()
        resp = authed_client.post(
            "/api/v1/account/avatar/",
            {"avatar": upload},
            format="multipart",
        )
        assert resp.status_code == 201
        avatar_url = resp.data["avatar_url"]
        assert resp["Location"] == avatar_url
        user.refresh_from_db()
        assert user.avatar_url == avatar_url

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_avatar_post_re_encodes_to_webp(self, authed_client, user):
        upload = _png_upload()
        resp = authed_client.post(
            "/api/v1/account/avatar/",
            {"avatar": upload},
            format="multipart",
        )
        assert resp.status_code == 201
        # Stored under .webp regardless of input format.
        assert resp.data["avatar_url"].endswith(".webp")
        user.refresh_from_db()
        assert user.avatar_url.endswith(".webp")

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_avatar_post_strips_exif_orientation(self, authed_client, user):
        # Patch ImageOps.exif_transpose to verify the avatar pipeline calls it
        # before resizing — covers the EXIF orientation strip without needing
        # a binary EXIF fixture.
        from PIL import ImageOps

        original = ImageOps.exif_transpose
        with patch(
            "apps.users.services.ImageOps.exif_transpose",
            wraps=original,
        ) as mock_transpose:
            upload = _png_upload(size=(100, 200))
            resp = authed_client.post(
                "/api/v1/account/avatar/",
                {"avatar": upload},
                format="multipart",
            )
        assert resp.status_code == 201
        mock_transpose.assert_called_once()

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_avatar_post_converts_palette_image_to_rgb(self, authed_client, user):
        # Build a palette ("P" mode) PNG and upload it. The pipeline should
        # accept it without crashing and re-encode it as WebP.
        from django.core.files.uploadedfile import SimpleUploadedFile

        img = Image.new("P", (100, 100))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        upload = SimpleUploadedFile("p.png", buf.getvalue(), content_type="image/png")

        resp = authed_client.post(
            "/api/v1/account/avatar/",
            {"avatar": upload},
            format="multipart",
        )
        assert resp.status_code == 201
        assert resp.data["avatar_url"].endswith(".webp")

    def test_avatar_post_rejects_invalid_image(self, authed_client):
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("evil.jpg", b"not-an-image", content_type="image/jpeg")
        resp = authed_client.post(
            "/api/v1/account/avatar/",
            {"avatar": upload},
            format="multipart",
        )
        assert resp.status_code == 400

    def test_avatar_post_rejects_oversize(self, authed_client):
        from django.core.files.uploadedfile import SimpleUploadedFile

        # 5 MB + 1 byte is over the AvatarUploadSerializer cap (5 MB).
        oversized = SimpleUploadedFile(
            "big.png", b"X" * (5 * 1024 * 1024 + 1), content_type="image/png"
        )
        resp = authed_client.post(
            "/api/v1/account/avatar/",
            {"avatar": oversized},
            format="multipart",
        )
        assert resp.status_code == 400

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_avatar_post_rejects_disallowed_format(self, authed_client):
        # Real PNG bytes pass the ImageField magic-byte sniff, but we patch
        # the post-validate Image.open used by the avatar pipeline to claim
        # an unsupported format (BMP) and verify the 400.
        upload = _png_upload()

        fake_img = Image.new("RGB", (50, 50))
        fake_img.format = "BMP"  # unsupported by the avatar allow-list

        class _Ctx:
            def __enter__(self):
                return fake_img

            def __exit__(self, *exc):
                return False

        with patch("apps.users.services.Image.open", return_value=_Ctx()):
            resp = authed_client.post(
                "/api/v1/account/avatar/",
                {"avatar": upload},
                format="multipart",
            )
        assert resp.status_code == 400

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_avatar_post_replaces_existing_local_file(self, authed_client, user):
        # Upload once to populate avatar_url with a local file under MEDIA_URL.
        first = _png_upload()
        first_resp = authed_client.post(
            "/api/v1/account/avatar/",
            {"avatar": first},
            format="multipart",
        )
        assert first_resp.status_code == 201
        from django.conf import settings

        first_path = first_resp.data["avatar_url"].split(settings.MEDIA_URL, 1)[1]
        assert default_storage.exists(first_path)

        # Upload a second time — the first stored file should be deleted.
        second = _png_upload(color="blue")
        second_resp = authed_client.post(
            "/api/v1/account/avatar/",
            {"avatar": second},
            format="multipart",
        )
        assert second_resp.status_code == 201
        assert not default_storage.exists(first_path)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_avatar_post_does_not_unlink_external_oauth_url(self, authed_client, user):
        # Set an externally-hosted (OAuth provider CDN) avatar.
        user.avatar_url = "https://lh3.googleusercontent.com/a/abc123"
        user.save(update_fields=["avatar_url"])

        with patch.object(default_storage, "delete", wraps=default_storage.delete) as mock_delete:
            upload = _png_upload()
            resp = authed_client.post(
                "/api/v1/account/avatar/",
                {"avatar": upload},
                format="multipart",
            )
        assert resp.status_code == 201
        mock_delete.assert_not_called()

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_avatar_delete_returns_204_and_clears_url(self, authed_client, user):
        # Seed an avatar via POST so the saved file exists on local storage.
        upload = _png_upload()
        post_resp = authed_client.post(
            "/api/v1/account/avatar/",
            {"avatar": upload},
            format="multipart",
        )
        assert post_resp.status_code == 201

        del_resp = authed_client.delete("/api/v1/account/avatar/")
        assert del_resp.status_code == 204
        user.refresh_from_db()
        # The view sets avatar_url = None.
        assert user.avatar_url in ("", None)

    def test_avatar_delete_when_unset_is_noop(self, authed_client, user):
        # No prior avatar — DELETE should still succeed.
        assert not user.avatar_url
        resp = authed_client.delete("/api/v1/account/avatar/")
        assert resp.status_code == 204

    def test_avatar_post_unauthenticated_rejected(self):
        client = APIClient()
        upload = _png_upload()
        resp = client.post(
            "/api/v1/account/avatar/",
            {"avatar": upload},
            format="multipart",
        )
        assert resp.status_code in (401, 403)

    def test_avatar_delete_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.delete("/api/v1/account/avatar/")
        assert resp.status_code in (401, 403)
