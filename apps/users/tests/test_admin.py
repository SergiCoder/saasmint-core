"""Tests for the UserAdmin customisation — get_fieldsets password hiding."""

from __future__ import annotations

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from apps.users.admin import UserAdmin
from apps.users.models import User


@pytest.fixture
def admin_site():
    return UserAdmin(User, AdminSite())


@pytest.fixture
def rf():
    return RequestFactory()


@pytest.mark.django_db
class TestUserAdminGetFieldsets:
    def test_non_staff_user_hides_password_field(self, admin_site, rf):
        """For non-staff (Supabase-only) users, the password field should be hidden."""
        user = User.objects.create_user(
            email="regular@example.com",
            supabase_uid="sup_regular",
            is_staff=False,
        )
        request = rf.get("/admin/users/user/")
        fieldsets = admin_site.get_fieldsets(request, obj=user)
        all_fields = [f for _name, opts in fieldsets for f in opts["fields"]]
        assert "password" not in all_fields

    def test_staff_user_shows_password_field(self, admin_site, rf):
        """For staff users, the password field should remain visible."""
        user = User.objects.create_user(
            email="staff@example.com",
            supabase_uid="sup_staff",
            is_staff=True,
        )
        request = rf.get("/admin/users/user/")
        fieldsets = admin_site.get_fieldsets(request, obj=user)
        all_fields = [f for _name, opts in fieldsets for f in opts["fields"]]
        assert "password" in all_fields

    def test_add_form_returns_add_fieldsets(self, admin_site, rf):
        """When obj is None (add form), get_fieldsets returns add_fieldsets unchanged."""
        request = rf.get("/admin/users/user/add/")
        fieldsets = admin_site.get_fieldsets(request, obj=None)
        # add_fieldsets has email and supabase_uid, no password
        all_fields = [f for _name, opts in fieldsets for f in opts["fields"]]
        assert "email" in all_fields
        assert "supabase_uid" in all_fields

    def test_non_staff_preserves_other_fields(self, admin_site, rf):
        """Hiding password should not remove other fields from the first fieldset."""
        user = User.objects.create_user(
            email="check@example.com",
            supabase_uid="sup_check",
            is_staff=False,
        )
        request = rf.get("/admin/users/user/")
        fieldsets = admin_site.get_fieldsets(request, obj=user)
        all_fields = [f for _name, opts in fieldsets for f in opts["fields"]]
        # These fields from the first fieldset should survive
        assert "email" in all_fields
        assert "supabase_uid" in all_fields
        assert "id" in all_fields
