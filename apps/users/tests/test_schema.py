"""Tests for the drf-spectacular JWT authentication extension."""

from __future__ import annotations

from apps.users.authentication import JWTAuthentication
from apps.users.schema import JWTAuthenticationScheme


class TestJWTAuthenticationScheme:
    def test_target_class_is_correct(self):
        target = JWTAuthenticationScheme.target_class
        if isinstance(target, str):
            assert target == "apps.users.authentication.JWTAuthentication"
        else:
            assert target is JWTAuthentication

    def test_name_is_jwt(self):
        assert JWTAuthenticationScheme.name == "JWT"

    def test_get_security_definition_returns_bearer_scheme(self):
        extension = JWTAuthenticationScheme(target=None)
        defn = extension.get_security_definition(auto_schema=None)
        assert defn["type"] == "http"
        assert defn["scheme"] == "bearer"
        assert defn["bearerFormat"] == "JWT"

    def test_get_security_definition_includes_description(self):
        extension = JWTAuthenticationScheme(target=None)
        defn = extension.get_security_definition(auto_schema=None)
        assert "description" in defn
        assert "Django" in defn["description"]
