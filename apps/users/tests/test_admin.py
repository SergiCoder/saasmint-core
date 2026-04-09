"""Tests for the UserAdmin customisation."""

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
    def test_all_users_show_password_field(self, admin_site, rf):
        """All users should have the password field visible (Django manages passwords now)."""
        user = User.objects.create_user(
            email="regular@example.com",
            full_name="Regular User",
            is_staff=False,
        )
        request = rf.get("/admin/users/user/")
        fieldsets = admin_site.get_fieldsets(request, obj=user)
        all_fields = [f for _name, opts in fieldsets for f in opts["fields"]]
        assert "password" in all_fields

    def test_staff_user_shows_password_field(self, admin_site, rf):
        user = User.objects.create_user(
            email="staff@example.com",
            full_name="Staff User",
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
        all_fields = [f for _name, opts in fieldsets for f in opts["fields"]]
        assert "email" in all_fields
        assert "full_name" in all_fields

    def test_fieldsets_contain_expected_fields(self, admin_site, rf):
        user = User.objects.create_user(
            email="check@example.com",
            full_name="Check User",
        )
        request = rf.get("/admin/users/user/")
        fieldsets = admin_site.get_fieldsets(request, obj=user)
        all_fields = [f for _name, opts in fieldsets for f in opts["fields"]]
        assert "email" in all_fields
        assert "id" in all_fields
        assert "password" in all_fields
