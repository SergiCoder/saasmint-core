"""drf-spectacular extensions for custom authentication classes."""

from __future__ import annotations

from drf_spectacular.extensions import OpenApiAuthenticationExtension


class JWTAuthenticationScheme(OpenApiAuthenticationExtension):  # type: ignore[no-untyped-call]
    target_class = "apps.users.authentication.JWTAuthentication"
    name = "JWT"

    def get_security_definition(self, auto_schema: object) -> dict[str, object]:
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Django-issued JWT. Pass as `Authorization: Bearer <token>`.",
        }
